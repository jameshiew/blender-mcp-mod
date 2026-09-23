from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blender_types import RenderEngine


def run_checks(manager, directory, skill_path):
    import base64
    import hashlib
    import json
    import re
    import runpy
    import time
    from typing import cast

    import bpy

    scene = bpy.context.scene
    assert scene is not None and scene.camera is not None
    cast("RenderEngine", scene.render).engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 128
    scene.render.resolution_y = 96
    scene.render.resolution_percentage = 100
    scene.render.filepath = "//untouched.png"
    scene.frame_start = 1
    scene.frame_end = 41
    scene.frame_step = 2
    scene.frame_set(7)
    camera = scene.camera.copy()
    camera_data = scene.camera.data
    assert isinstance(camera_data, bpy.types.Camera)
    copied_data = camera_data.copy()
    camera.data = copied_data
    copied_data.lens = 20
    scene.collection.objects.link(camera)
    scene.timeline_markers.new("Cut", frame=5).camera = camera
    recipe = re.search(r"```python\n(.*?)\n```", skill_path.read_text(), re.DOTALL)
    assert recipe is not None
    recipe_path = directory.parent / "compositor_recipe.py"
    recipe_path.write_text("import bpy\n" + recipe[1])
    runpy.run_path(str(recipe_path))
    assert scene.compositing_node_group is not None
    glow = next(n for n in scene.compositing_node_group.nodes if n.type == "GLARE")
    assert (
        cast(bpy.types.NodeSocketMenu, glow.inputs["Type"]).default_value == "Fog Glow"
    )

    def wait(job_id, predicate):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = manager.status(job_id)
            if predicate(status):
                return status
            assert status["state"] not in {"failed", "cancelled"}, status
            time.sleep(0.01)
        raise AssertionError(manager.status(job_id))

    original = (
        scene.frame_current,
        scene.render.filepath,
        scene.render.resolution_percentage,
        scene.camera,
    )
    try:
        result = manager.start_animation(str(directory), resolution_percentage=50)
        job_id = result["job_id"]
        partial = wait(job_id, lambda s: s["frames_completed"] > 0)
        assert partial["frames_completed"] < partial["frames_total"], partial
        image = manager.image(job_id, frame=1)
        assert (image["source_width"], image["source_height"]) == (64, 48), image
        assert base64.b64decode(image["image_data"]).startswith(b"\x89PNG")
        manager.cancel(job_id)
        cancelled = wait(job_id, lambda s: s["state"] in {"cancelled", "completed"})
        assert cancelled["state"] == "cancelled", cancelled
        assert original == (
            scene.frame_current,
            scene.render.filepath,
            scene.render.resolution_percentage,
            scene.camera,
        )
        first_path = directory / "frames" / "frame_0000001.png"
        original_bytes = first_path.read_bytes()
        original_mtime = first_path.stat().st_mtime_ns
        manager.close()
        scene.frame_set(999)
        scene.render.resolution_percentage = 10
        bpy.data.objects["Cube"].hide_render = True
        resumed = manager.resume_animation(str(directory))
        assert resumed["job_id"] == job_id
        finished = wait(job_id, lambda s: s["state"] == "completed")
        assert finished["frames_completed"] == finished["frames_total"] == 21, finished
        assert finished["frame_fraction"] == 1
        assert first_path.read_bytes() == original_bytes
        assert first_path.stat().st_mtime_ns == original_mtime
        assert scene.frame_current == 999
        assert scene.render.resolution_percentage == 10
        assert manager.image(job_id)["frame"] == 41
        after_cut = manager.image(job_id, frame=5)
        assert base64.b64decode(after_cut["image_data"]) != original_bytes
        exported = directory.parent / "animation-frame.png"
        manager.export(job_id, str(exported), frame=1)
        assert exported.read_bytes() == original_bytes
        frame_to_repair = directory / "frames" / "frame_0000005.png"
        reference_path = directory.parent / "reference-cut.png"
        reference_path.write_bytes(frame_to_repair.read_bytes())
        frame_to_repair.write_bytes(b"interrupted")
        manager.resume_animation(str(directory))
        repaired = wait(job_id, lambda s: s["state"] == "completed")
        assert repaired["frames_completed"] == 21
        saved_frames = json.loads((directory / "frames.json").read_text())
        assert (
            hashlib.sha256(frame_to_repair.read_bytes()).hexdigest()
            == saved_frames["5"]["sha256"]
        )
        reference = bpy.data.images.load(str(reference_path), check_existing=False)
        repaired_image = bpy.data.images.load(
            str(frame_to_repair), check_existing=False
        )
        try:
            difference = [
                abs(a - b)
                for a, b in zip(
                    reference.pixels[:], repaired_image.pixels[:], strict=True
                )
            ]
            assert sum(difference) / len(difference) < 1 / 255, max(difference)
        finally:
            bpy.data.images.remove(reference)
            bpy.data.images.remove(repaired_image)
        assert first_path.stat().st_mtime_ns == original_mtime
        print("ANIMATION_CHECKS_OK", json.dumps(repaired))
    finally:
        manager.close()
    assert (directory / "scene.blend").is_file()
    assert len(list((directory / "frames").glob("frame_*.png"))) == 21
