import os
import subprocess

import pytest
from conftest import ROOT_ADDON, blender_environment


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for native render jobs",
)
@pytest.mark.parametrize("engine", ["CYCLES", "BLENDER_EEVEE", "BLENDER_WORKBENCH"])
def test_render_snapshot_image_cancellation_and_scene_preservation(tmp_path, engine):
    script = tmp_path / "check_render.py"
    script.write_text(
        """import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import bpy

spec = importlib.util.spec_from_file_location("render_jobs_native", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
scene = bpy.context.scene
scene.render.engine = RENDER_ENGINE
scene.cycles.samples = 1
scene.render.resolution_x = 128
scene.render.resolution_y = 96
scene.render.resolution_percentage = 100
scene.frame_set(7)
scene.render.filepath = '//must-not-overwrite.png'
scene.render.image_settings.file_format = 'JPEG'
output_directory = Path(__file__).parent / 'must-not-create'
if hasattr(scene, 'compositing_node_group'):
    tree = bpy.data.node_groups.new('Test compositor', 'CompositorNodeTree')
    scene.compositing_node_group = tree
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    output = tree.nodes.new('NodeGroupOutput')
else:
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('CompositorNodeComposite')
layers = tree.nodes.new('CompositorNodeRLayers')
tree.links.new(layers.outputs['Image'], output.inputs[0])
file_output = tree.nodes.new('CompositorNodeOutputFile')
if hasattr(file_output, 'directory'):
    file_output.directory = str(output_directory)
else:
    file_output.base_path = str(output_directory)
tree.links.new(layers.outputs['Image'], file_output.inputs[0])
original = (bpy.data.filepath, scene.frame_current, scene.render.filepath,
            scene.render.resolution_percentage, scene.render.image_settings.file_format)

def wait(manager, job_id, observed=None):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = manager.status(job_id)
        if observed is not None:
            observed.append(status)
        if status['state'] not in {'running', 'cancelling'}:
            return status
        time.sleep(0.05)
    raise AssertionError(manager.status(job_id))

manager = module.RenderJobs()
try:
    first = manager.start(frame=3, resolution_percentage=50)
    assert first['state'] == 'running', first
    assert manager.status()['job_id'] == first['job_id']
    result = wait(manager, first['job_id'])
    assert result['state'] == 'completed', result
    assert (result['width'], result['height']) == (64, 48), result
    image = manager.image(first['job_id'], max_size=32)
    assert (image['width'], image['height']) == (32, 24), image
    assert base64.b64decode(image['image_data']).startswith(b'\\x89PNG'), image
    original_image = manager.image(first['job_id'], max_size=1000)
    assert (original_image['width'], original_image['height']) == (64, 48)
    destination = Path(__file__).parent / 'exported image.png'
    exported = manager.export(first['job_id'], str(destination))
    assert destination.read_bytes() == base64.b64decode(original_image['image_data'])
    assert exported['sha256'] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert (exported['width'], exported['height']) == (64, 48)
    assert original == (bpy.data.filepath, scene.frame_current, scene.render.filepath,
                        scene.render.resolution_percentage, scene.render.image_settings.file_format)
    assert not file_output.mute
    assert not output_directory.exists(), list(output_directory.iterdir())
    if RENDER_ENGINE == 'CYCLES':
        scene.cycles.samples = 512
        scene.cycles.use_adaptive_sampling = False
        scene.render.resolution_x = 512
        scene.render.resolution_y = 512
        observed = []
        progressive = manager.start()
        rendered = wait(manager, progressive['job_id'], observed)
        assert rendered['state'] == 'completed', rendered
        samples = [item['progress'] for item in observed if item['state'] == 'running' and item.get('progress') and item['progress']['samples_total']]
        assert samples, observed
        assert any(0 < item['sample_fraction'] < 1 for item in samples), samples
        assert any(item['remaining_seconds'] is not None for item in samples), samples
    second = manager.start()
    cancelled = manager.cancel(second['job_id'])
    assert cancelled['state'] in {'cancelling', 'cancelled'}, cancelled
    cancelled = wait(manager, second['job_id'])
    assert cancelled['state'] == 'cancelled', cancelled
    assert not cancelled['image_available']
    assert manager.image(first['job_id'])['image_data'] == original_image['image_data']
    print('RENDER_JOBS_OK', json.dumps(result), json.dumps(cancelled))
finally:
    manager.close()
assert destination.is_file()
""".replace("MODULE_PATH", repr(str(ROOT_ADDON.with_name("render_jobs.py")))).replace(
            "RENDER_ENGINE", repr(engine)
        )
    )
    checked = subprocess.run(
        [
            os.environ["BLENDER_TEST_EXECUTABLE"],
            "--background",
            "--factory-startup",
            "--offline-mode",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(script),
        ],
        env=blender_environment(tmp_path / "profile"),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "RENDER_JOBS_OK" in checked.stdout
