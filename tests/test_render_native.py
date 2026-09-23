import os
import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT, blender_environment


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for native render jobs",
)
@pytest.mark.parametrize("engine", ["CYCLES", "BLENDER_EEVEE", "BLENDER_WORKBENCH"])
def test_render_snapshot_image_cancellation_and_scene_preservation(
    tmp_path, engine, unpacked_addon
):
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

directory = Path(PACKAGE_PATH)
sys.path.extend(str(wheel) for wheel in (directory / 'wheels').glob('*.whl'))
spec = importlib.util.spec_from_file_location("render_jobs_native", directory / '__init__.py')
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

manager = module.render_jobs.RenderJobs()
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
""".replace("PACKAGE_PATH", repr(str(unpacked_addon))).replace(
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


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for native animation jobs",
)
def test_animation_cancel_resume_camera_cuts_and_compositor(unpacked_addon, tmp_path):
    script = tmp_path / "check_animation.py"
    script.write_text(f"""import importlib.util
import runpy
import sys
from pathlib import Path
directory = Path({str(unpacked_addon)!r})
sys.path.extend(str(wheel) for wheel in (directory / 'wheels').glob('*.whl'))
spec = importlib.util.spec_from_file_location('animation_native', directory / '__init__.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
checks = runpy.run_path({str(Path(__file__).with_name("blender_animation_checks.py"))!r})
checks['run_checks'](module.render_jobs.RenderJobs(), Path({str(tmp_path / "film")!r}), Path({str(REPO_ROOT / "skills/blender-mcp/SKILL.md")!r}))
""")
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
        timeout=180,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "ANIMATION_CHECKS_OK" in checked.stdout


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for native render eligibility checks",
)
@pytest.mark.parametrize(
    "mode", ["COMPOSITOR", "SEQUENCER", "MISSING_CAMERA", "DIRTY_IMAGE"]
)
def test_render_uses_native_camera_requirements(unpacked_addon, tmp_path, mode):
    script = tmp_path / "check_render_eligibility.py"
    script.write_text(f"""import importlib.util
import sys
import time
from pathlib import Path
import bpy

directory = Path({str(unpacked_addon)!r})
sys.path.extend(str(wheel) for wheel in (directory / 'wheels').glob('*.whl'))
spec = importlib.util.spec_from_file_location('render_eligibility', directory / '__init__.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
mode = {mode!r}
scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.render.resolution_x = 64
scene.render.resolution_y = 48
scene.render.resolution_percentage = 100
scene.camera = None
for obj in list(scene.objects):
    if obj.type == 'CAMERA':
        bpy.data.objects.remove(obj, do_unlink=True)
if mode in {{'COMPOSITOR', 'DIRTY_IMAGE'}}:
    tree = bpy.data.node_groups.new('Camera-less compositor', 'CompositorNodeTree')
    scene.compositing_node_group = tree
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    output = tree.nodes.new('NodeGroupOutput')
    if mode == 'DIRTY_IMAGE':
        image = bpy.data.images.new('Unsaved compositor pixels', width=64, height=48)
        image.pixels.foreach_set([0.2, 0.4, 0.8, 1] * (64 * 48))
        unrelated = bpy.data.images.new('Unrelated dirty pixels', width=1, height=1)
        unrelated.pixels[:] = [1, 0, 0, 1]
        node = tree.nodes.new('CompositorNodeImage')
        node.image = image
        tree.links.new(node.outputs['Image'], output.inputs[0])
    else:
        color = tree.nodes.new('CompositorNodeRGB')
        color.outputs[0].default_value = (0.2, 0.4, 0.8, 1)
        tree.links.new(color.outputs[0], output.inputs[0])
elif mode == 'SEQUENCER':
    editor = scene.sequence_editor_create()
    strip = editor.strips.new_effect('Color', type='COLOR', channel=1, frame_start=1, length=9)
    strip.color = (0.2, 0.4, 0.8)
    scene.render.use_sequencer = True
manager = module.render_jobs.RenderJobs()
try:
    if mode == 'DIRTY_IMAGE':
        try:
            manager.start(frame=1)
        except ValueError as error:
            assert image.name in str(error) and 'Save or pack' in str(error), error
            assert unrelated.name not in str(error), error
        else:
            raise AssertionError('Unsaved compositor pixels must fail before snapshotting')
        assert image.is_dirty and unrelated.is_dirty
        assert manager._temporary is None
        image.pack()
        assert not image.is_dirty and unrelated.is_dirty
    result = manager.start(frame=1)
    deadline = time.monotonic() + 45
    while result['state'] in {{'running', 'cancelling'}} and time.monotonic() < deadline:
        time.sleep(0.05)
        result = manager.status(result['job_id'])
    if mode == 'MISSING_CAMERA':
        assert result['state'] == 'failed', result
        assert 'camera' in result['error_message'].lower(), result
        assert not result['image_available'], result
    else:
        assert result['state'] == 'completed', result
        assert (result['width'], result['height']) == (64, 48), result
        assert manager.image(result['job_id'])['image_data']
        if mode == 'DIRTY_IMAGE':
            rendered = bpy.data.images.load(str(manager._jobs[result['job_id']].directory / 'render.png'))
            assert rendered.pixels[2] > rendered.pixels[0] > 0.1, list(rendered.pixels[:4])
            bpy.data.images.remove(rendered)
            assert unrelated.is_dirty
    assert scene.camera is None
    print('RENDER_ELIGIBILITY_OK', result)
finally:
    manager.close()
""")
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
        timeout=60,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "RENDER_ELIGIBILITY_OK" in checked.stdout
