import base64
import hashlib
import importlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest


class Process:
    def __init__(self):
        self.code = None
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.code = -9

    def wait(self, timeout):
        self.waited += 1
        assert timeout == 5
        return self.code


class Scene(SimpleNamespace):
    __hash__ = object.__hash__
    __eq__ = object.__eq__


@pytest.mark.parametrize("closed", [False, True])
def test_export_without_render_storage_reports_missing_job(renders, tmp_path, closed):
    if closed:
        renders.manager.start()
        renders.manager.close()
    with pytest.raises(ValueError, match="Unknown or expired render job"):
        renders.manager.export("missing", str(tmp_path / "render.png"))
    assert not (tmp_path / "render.png").exists()


@pytest.fixture
def renders(addon, monkeypatch):
    module = addon.render_jobs
    bpy = module.bpy
    scene = Scene()
    scene.name = "植物 • Scene"
    scene.blendermcp_use_sketchfab = False
    scene.camera = SimpleNamespace(type="CAMERA")
    scene.frame_current = 12
    scene.frame_start = 1
    scene.frame_end = 6
    scene.frame_step = 2
    scene.render = SimpleNamespace(
        engine="CYCLES", resolution_percentage=80, fps=24, fps_base=1
    )
    bpy.context.scene = scene
    bpy.app.binary_path = "/Blender with spaces/blender"
    snapshots = []

    def write(path, datablocks, **kwargs):
        snapshots.append((path, datablocks, kwargs))
        Path(path).write_bytes(b"snapshot")

    bpy.data = SimpleNamespace(
        scenes={scene.name: scene},
        images=[],
        libraries=SimpleNamespace(write=write),
    )
    processes, launches = [], []

    def launch(arguments, **kwargs):
        launches.append((arguments, kwargs))
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    manager = module.RenderJobs()
    try:
        yield SimpleNamespace(
            manager=manager,
            module=module,
            addon=addon,
            scene=scene,
            snapshots=snapshots,
            processes=processes,
            launches=launches,
            now=now,
        )
    finally:
        manager.close()


def complete(renders, job_id, width=640, height=480):
    job = renders.manager._jobs[job_id]
    (job.directory / "status.json").write_text(
        json.dumps(
            {
                "phase": "completed",
                "width": width,
                "height": height,
            }
        )
    )
    (job.directory / "render.png").write_bytes(b"render-image")
    job.process.code = 0
    return job


def test_server_without_render_does_not_allocate_render_resources(renders, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("render resources allocated without a render")

    monkeypatch.setattr(renders.module.tempfile, "TemporaryDirectory", unexpected)
    monkeypatch.setattr(renders.module.atexit, "register", unexpected)
    server = renders.addon.server.BlenderMCPServer()
    server.stop()


def test_server_can_render_again_after_stopping(renders):
    server = renders.addon.server.BlenderMCPServer(renders=renders.manager)
    first = server.execute_command({"type": "start_render"})
    assert first["status"] == "success"
    first_directory = renders.manager._jobs[first["result"]["job_id"]].directory
    server.stop()
    assert not first_directory.exists()
    second = server.execute_command({"type": "start_render"})
    assert second["status"] == "success"
    assert second["result"]["job_id"] != first["result"]["job_id"]
    assert len(renders.processes) == 2
    server.stop()
    assert all(process.killed == 1 for process in renders.processes)


def test_snapshot_job_does_not_change_scene_and_blocks_overlap(renders):
    result = renders.manager.start(frame=23, resolution_percentage=25)
    assert result["state"] == "running"
    assert result["phase"] == "starting"
    assert result["scene"] == renders.scene.name
    assert result["frame"] == 23
    assert not result["image_available"]
    assert renders.scene.frame_current == 12
    assert renders.scene.render.resolution_percentage == 80
    assert renders.snapshots[0][1:] == ({renders.scene}, {"path_remap": "ABSOLUTE"})
    args, kwargs = renders.launches[0]
    assert args[0] == "/Blender with spaces/blender"
    assert "--disable-autoexec" in args
    assert "--offline-mode" in args
    assert "shell" not in kwargs
    job = renders.manager._jobs[result["job_id"]]
    request = json.loads((job.directory / "request.json").read_text())
    assert request == {
        "scene": renders.scene.name,
        "frame": 23,
        "resolution_percentage": 25,
    }
    with pytest.raises(RuntimeError, match=result["job_id"]):
        renders.manager.start()
    assert len(renders.processes) == 1
    assert renders.manager.status() == result


def test_completion_has_stable_duration_and_independent_image(renders):
    job_id = renders.manager.start()["job_id"]
    job = complete(renders, job_id)
    renders.now[0] += 3
    result = renders.manager.status(job_id)
    assert result["state"] == "completed"
    assert result["elapsed_seconds"] == 3
    assert result["image_available"]
    assert not (job.directory / "scene.blend").exists()
    renders.now[0] += 8
    assert renders.manager.status(job_id)["elapsed_seconds"] == 3
    renders.manager.start()
    image = renders.manager.image(job_id)
    assert base64.b64decode(image["image_data"]) == b"render-image"
    assert image["format"] == "png"
    assert (image["width"], image["height"]) == (640, 480)
    assert renders.manager.cancel(job_id)["state"] == "completed"
    assert not job.process.terminated


def test_cancel_is_idempotent_and_escalates_without_blocking(renders):
    job_id = renders.manager.start()["job_id"]
    process = renders.processes[0]
    assert renders.manager.cancel(job_id)["state"] == "cancelling"
    assert renders.manager.cancel(job_id)["state"] == "cancelling"
    assert process.terminated == 1
    assert process.waited == 0
    renders.now[0] += 2.1
    renders.manager.poll()
    assert process.killed == 1
    assert renders.manager.status(job_id)["state"] == "cancelled"
    assert renders.manager.cancel(job_id)["state"] == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        renders.manager.image(job_id)
    assert renders.manager.start()["job_id"] != job_id


def test_failed_worker_keeps_diagnostics_and_no_image(renders):
    job_id = renders.manager.start()["job_id"]
    job = renders.manager._jobs[job_id]
    (job.directory / "worker.log").write_text("X" * 5000 + "\nEngine failed")
    job.process.code = 1
    status = renders.manager.status(job_id)
    assert status["state"] == "failed"
    assert status["error_message"].endswith("Engine failed")
    assert len(status["error_message"]) < 4200
    with pytest.raises(ValueError, match="failed"):
        renders.manager.image(job_id)


@pytest.mark.parametrize("code", [0, 1])
def test_missing_output_never_reports_completion(renders, code):
    job_id = renders.manager.start()["job_id"]
    renders.processes[0].code = code
    assert renders.manager.status(job_id)["state"] == "failed"


def test_worker_error_and_transient_status_are_handled(renders):
    job_id = renders.manager.start()["job_id"]
    job = renders.manager._jobs[job_id]
    (job.directory / "status.json").write_text("{")
    assert renders.manager.status(job_id)["phase"] == "starting"
    (job.directory / "status.json").write_text('{"phase":"rendering"}')
    assert renders.manager.status(job_id)["phase"] == "rendering"
    (job.directory / "status.json").write_text(
        '{"phase":"failed","error":"No render engine"}'
    )
    job.process.code = 1
    assert renders.manager.status(job_id)["error_message"] == "No render engine"


def test_cache_eviction_and_close_remove_only_owned_jobs(renders):
    renders.manager.max_jobs = 2
    first = renders.manager.start()["job_id"]
    directory = complete(renders, first).directory
    second = renders.manager.start()["job_id"]
    complete(renders, second)
    third = renders.manager.start()["job_id"]
    assert not directory.exists()
    with pytest.raises(ValueError, match="expired"):
        renders.manager.status(first)
    root = Path(renders.manager._temporary.name)
    renders.manager.close()
    assert renders.processes[-1].killed == 1
    assert renders.processes[-1].waited == 1
    assert not root.exists()
    with pytest.raises(ValueError, match="expired"):
        renders.manager.status(third)


@pytest.mark.parametrize(
    "arguments",
    [
        {"frame": True},
        {"frame": 1048575},
        {"frame": 1.5},
        {"resolution_percentage": 0},
        {"resolution_percentage": 101},
        {"scene_name": "missing"},
        {"scene_name": ""},
    ],
)
def test_invalid_requests_do_not_write_snapshots(renders, arguments):
    with pytest.raises(ValueError):
        renders.manager.start(**arguments)
    assert not renders.snapshots
    assert not renders.processes


def test_camera_validation_is_left_to_blender_render_pipeline(renders):
    renders.scene.camera = None
    result = renders.manager.start()
    assert result["state"] == "running"
    assert renders.snapshots[0][1] == {renders.scene}


def test_snapshot_rejects_only_reachable_unsaved_image_pixels(renders):
    used = Scene(name="Painted texture", is_dirty=True, source="FILE")
    unused = Scene(name="Unrelated image", is_dirty=True, source="GENERATED")
    viewer = Scene(name="Render Result", is_dirty=True, source="VIEWER")
    material = Scene(name="Material")
    bpy = renders.module.bpy
    bpy.data.images = [used, unused, viewer]
    bpy.data.user_map = lambda: {
        used: {material},
        material: {renders.scene, material},
        unused: set(),
        viewer: {renders.scene},
    }
    with pytest.raises(ValueError, match="Painted texture.*Save or pack") as error:
        renders.manager.start()
    assert "Unrelated image" not in str(error.value)
    assert "Render Result" not in str(error.value)
    assert not renders.snapshots
    assert used.is_dirty
    used.is_dirty = False
    assert renders.manager.start()["state"] == "running"


def test_launch_failure_is_clean(renders, monkeypatch):

    def fail(*args, **kwargs):
        raise OSError("Cannot launch Blender")

    monkeypatch.setattr(renders.module.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="Cannot launch"):
        renders.manager.start()
    assert list(Path(renders.manager._temporary.name).iterdir()) == []


def test_image_scaling_cleans_up_datablock_and_preserves_original(renders):
    job_id = renders.manager.start()["job_id"]
    job = complete(renders, job_id, width=4000, height=2000)
    scaled, removed = [], []
    image = SimpleNamespace(
        scale=lambda *size: scaled.append(size),
        save=lambda: Path(image.filepath_raw).write_bytes(b"preview-image"),
    )
    renders.module.bpy.data.images = SimpleNamespace(
        load=lambda path, check_existing: image,
        remove=lambda item: removed.append(item),
    )
    result = renders.manager.image(job_id, max_size=800)
    assert (result["width"], result["height"]) == (800, 400)
    assert (result["source_width"], result["source_height"]) == (4000, 2000)
    assert scaled == [(800, 400)]
    assert removed == [image]
    assert base64.b64decode(result["image_data"]) == b"preview-image"
    assert (job.directory / "render.png").read_bytes() == b"render-image"

    def fail():
        raise RuntimeError("save failed")

    image.save = fail
    with pytest.raises(RuntimeError, match="save failed"):
        renders.manager.image(job_id)
    assert removed == [image, image]


def test_addon_dispatch_and_stop_own_the_render_manager(renders):
    server = renders.addon.server.BlenderMCPServer(renders=renders.manager)
    for command, params in [
        ("get_render_status", {}),
        ("cancel_render", {"job_id": "unknown"}),
        ("get_render_image", {"job_id": "unknown"}),
        ("export_render", {"job_id": "unknown", "filepath": "/export.png"}),
    ]:
        assert (
            server.execute_command({"type": command, "params": params})["status"]
            == "error"
        )
    response = server.execute_command({"type": "start_render", "params": {}})
    assert response["status"] == "success"
    job_id = response["result"]["job_id"]
    assert server.renders.status()["job_id"] == job_id
    assert server.renders.cancel(job_id)["state"] == "cancelling"
    server.stop()
    assert not server.renders._jobs
    assert renders.processes[0].killed == 1


def test_export_keeps_full_image_after_job_cleanup(renders, tmp_path):
    job_id = renders.manager.start()["job_id"]
    complete(renders, job_id, width=4000, height=2000)
    destination = tmp_path / "永久 image.png"
    server = renders.addon.server.BlenderMCPServer(renders=renders.manager)
    result = server.renders.export(job_id, str(destination))
    assert result == {
        "job_id": job_id,
        "filepath": str(destination),
        "format": "png",
        "width": 4000,
        "height": 2000,
        "size_bytes": len(b"render-image"),
        "sha256": hashlib.sha256(b"render-image").hexdigest(),
    }
    renders.manager.close()
    assert destination.read_bytes() == b"render-image"


def test_export_requires_explicit_overwrite_and_does_not_follow_symlinks(
    renders, tmp_path
):
    job_id = renders.manager.start()["job_id"]
    complete(renders, job_id)
    destination = tmp_path / "image.png"
    original = tmp_path / "original.png"
    original.write_bytes(b"original")
    destination.symlink_to(original)
    with pytest.raises(FileExistsError):
        renders.manager.export(job_id, str(destination))
    assert destination.is_symlink()
    assert original.read_bytes() == b"original"
    renders.manager.export(job_id, str(destination), overwrite=True)
    assert not destination.is_symlink()
    assert destination.read_bytes() == b"render-image"
    assert original.read_bytes() == b"original"
    assert not list(tmp_path.glob(".blender-mcp-export-*"))


def test_failed_export_preserves_destination_and_removes_partial_copy(
    renders, monkeypatch, tmp_path
):
    job_id = renders.manager.start()["job_id"]
    complete(renders, job_id)
    destination = tmp_path / "image.png"
    destination.write_bytes(b"original")

    def fail(source, output):
        output.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(renders.module.shutil, "copyfileobj", fail)
    with pytest.raises(OSError, match="disk full"):
        renders.manager.export(job_id, str(destination), overwrite=True)
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".blender-mcp-export-*"))


@pytest.mark.parametrize("filepath", ["", "relative.png", "/image.jpg", 42])
def test_export_rejects_invalid_paths(renders, filepath):
    with pytest.raises(ValueError, match="path"):
        renders.manager.export("unknown", filepath)


def test_export_rejects_unfinished_jobs_and_temporary_destinations(renders, tmp_path):
    job_id = renders.manager.start()["job_id"]
    with pytest.raises(ValueError, match="running"):
        renders.manager.export(job_id, str(tmp_path / "image.png"))
    job = complete(renders, job_id)
    with pytest.raises(ValueError, match="temporary"):
        renders.manager.export(job_id, str(job.directory / "other.png"))
    with pytest.raises(ValueError, match="boolean"):
        renders.manager.export(job_id, str(tmp_path / "image.png"), overwrite=1)
    with pytest.raises(FileNotFoundError):
        renders.manager.export(job_id, str(tmp_path / "missing" / "image.png"))


def test_progress_is_exposed_and_terminal_jobs_clear_eta(renders):
    job_id = renders.manager.start()["job_id"]
    job = renders.manager._jobs[job_id]
    assert renders.manager.status(job_id)["progress"] is None
    progress = {"samples_completed": 12, "samples_total": 64, "remaining_seconds": 3.0}
    (job.directory / "status.json").write_text(
        json.dumps({"phase": "rendering", "progress": progress})
    )
    assert renders.manager.status(job_id)["progress"] == progress
    renders.manager.cancel(job_id)
    assert renders.manager.status(job_id)["progress"]["remaining_seconds"] is None


def save_frame(renders, job, frame, payload=b"pixels"):
    storage = renders.addon.render_storage
    path = storage.frame_path(job.directory, frame)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 64, 48) + payload
    )
    frames = storage.read_frames(job.directory)
    frames[str(frame)] = {
        "width": 64,
        "height": 48,
        "size_bytes": path.stat().st_size,
        "sha256": storage.checksum(path),
    }
    storage.write_json(job.directory / "frames.json", frames)
    return path


def test_animation_cancel_restart_resume_keeps_snapshot_and_frames(renders, tmp_path):
    directory = tmp_path / "Film with spaces 植物"
    first = renders.manager.start_animation(str(directory), frame_end=8, frame_step=3)
    job_id = first["job_id"]
    job = renders.manager._jobs[job_id]
    assert first["frames_total"] == 3
    assert first["frames_completed"] == 0
    assert first["fps"] == 8
    assert first["frame_fraction"] == 0
    assert first["output_directory"] == str(directory)
    assert renders.scene.frame_current == 12
    assert renders.snapshots[0][1:] == ({renders.scene}, {"path_remap": "ABSOLUTE"})
    path = save_frame(renders, job, 1)
    original = path.read_bytes()
    status = renders.manager.status(job_id)
    assert status["frames_completed"] == 1
    assert status["image_available"]
    assert status["frame_fraction"] == 1 / 3
    assert (
        base64.b64decode(renders.manager.image(job_id, frame=1)["image_data"])
        == original
    )
    assert (
        renders.manager.export(job_id, str(tmp_path / "first.png"), frame=1)["width"]
        == 64
    )
    renders.manager.cancel(job_id)
    job.process.code = -15
    assert renders.manager.status(job_id)["state"] == "cancelled"
    assert (directory / "scene.blend").exists()
    renders.manager.close()
    assert path.read_bytes() == original
    renders.scene.camera = None
    renders.scene.frame_current = 999
    resumed = renders.manager.resume_animation(str(directory))
    assert resumed["job_id"] == job_id
    assert resumed["frames_completed"] == 1
    assert resumed["phase"] == "starting"
    assert len(renders.snapshots) == 1
    assert (directory / "request.json").is_file()
    assert renders.launches[-1][0][-2] == str(directory)
    assert renders.launches[-1][0][-1] != job.attempt_id


def test_animation_completion_eviction_and_frame_exports(renders, tmp_path):
    renders.manager.max_jobs = 1
    result = renders.manager.start_animation(
        str(tmp_path / "film"), frame_start=-1, frame_end=3, frame_step=2
    )
    job = renders.manager._jobs[result["job_id"]]
    for f in [-1, 1, 3]:
        save_frame(renders, job, f, str(f).encode())
    (job.directory / "status.json").write_text(
        json.dumps({"phase": "completed", "attempt_id": job.attempt_id})
    )
    job.process.code = 0
    status = renders.manager.status(job.job_id)
    assert status["state"] == "completed"
    assert status["frames_completed"] == status["frames_total"] == 3
    assert status["frame_fraction"] == 1
    assert status["width"] == 64
    assert renders.manager.image(job.job_id)["frame"] == 3
    with pytest.raises(ValueError, match="not complete"):
        renders.manager.image(job.job_id, frame=0)
    with pytest.raises(ValueError, match="outside"):
        renders.manager.export(job.job_id, str(job.directory / "copy.png"))
    renders.manager.start()
    assert job.directory.is_dir()
    assert (job.directory / "scene.blend").is_file()
    assert len(list((job.directory / "frames").iterdir())) == 3


@pytest.mark.parametrize(
    "arguments",
    [
        {"frame_start": True},
        {"frame_end": 1048575},
        {"frame_step": 0},
        {"frame_step": True},
        {"frame_start": 5, "frame_end": 4},
        {"frame_start": 0, "frame_end": 10000, "frame_step": 1},
        {"resolution_percentage": 101},
    ],
)
def test_invalid_animation_range_never_creates_output(renders, tmp_path, arguments):
    directory = tmp_path / "film"
    with pytest.raises(ValueError):
        renders.manager.start_animation(str(directory), **arguments)
    assert not directory.exists()
    assert not renders.snapshots


def test_animation_refuses_existing_paths_busy_jobs_and_changed_snapshot(
    renders, tmp_path
):
    directory = tmp_path / "film"
    with pytest.raises(ValueError, match="absolute"):
        renders.manager.start_animation("relative")
    with pytest.raises(FileExistsError):
        renders.manager.start_animation(str(tmp_path))
    result = renders.manager.start_animation(str(directory))
    with pytest.raises(RuntimeError, match="still running"):
        renders.manager.start()
    renders.manager.close()
    with (
        renders.addon.render_storage.job_lock(directory),
        pytest.raises(RuntimeError, match="in use"),
    ):
        renders.manager.resume_animation(str(directory))
    (directory / "scene.blend").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        renders.manager.resume_animation(str(directory))
    assert result["job_id"]


def test_animation_does_not_report_success_with_missing_frames(renders, tmp_path):
    result = renders.manager.start_animation(str(tmp_path / "film"))
    job = renders.manager._jobs[result["job_id"]]
    save_frame(renders, job, 1)
    (job.directory / "status.json").write_text(
        json.dumps({"phase": "completed", "attempt_id": job.attempt_id})
    )
    job.process.code = 0
    assert renders.manager.status(job.job_id)["state"] == "failed"
    assert renders.manager.image(job.job_id, frame=1)["frame"] == 1


def test_failed_animation_launch_retains_a_resumable_snapshot(
    renders, tmp_path, monkeypatch
):
    directory = tmp_path / "film"

    def fail(*args, **kwargs):
        raise OSError("Cannot launch Blender")

    with monkeypatch.context() as patch:
        patch.setattr(renders.module.subprocess, "Popen", fail)
        with pytest.raises(OSError, match="Cannot launch"):
            renders.manager.start_animation(str(directory))
    assert (directory / "scene.blend").read_bytes() == b"snapshot"
    result = renders.manager.resume_animation(str(directory))
    assert result["state"] == "running"
    assert len(renders.snapshots) == 1


def test_animation_cannot_use_temporary_still_storage(renders):
    job_id = renders.manager.start()["job_id"]
    complete(renders, job_id)
    directory = Path(renders.manager._temporary.name) / "animation"
    with pytest.raises(ValueError, match="outside the temporary"):
        renders.manager.start_animation(str(directory))
    assert not directory.exists()


@pytest.fixture
def animation_worker(renders, tmp_path, monkeypatch):
    result = renders.manager.start_animation(str(tmp_path / "film"))
    job = renders.manager._jobs[result["job_id"]]
    worker = importlib.import_module(renders.addon.__name__ + ".render_worker")
    rendered = []
    renders.scene.frame_set = lambda f: setattr(renders.scene, "frame_current", f)
    renders.scene.render.image_settings = SimpleNamespace()
    renders.module.bpy.app.handlers.render_stats = []
    renders.module.bpy.data.scenes = [renders.scene]

    class Scenes(list):
        def __getitem__(self, key):
            return renders.scene if isinstance(key, str) else super().__getitem__(key)

    renders.module.bpy.data.scenes = Scenes([renders.scene])

    def render(**kwargs):
        rendered.append(renders.scene.frame_current)
        return {"FINISHED"}

    def save(path, scene):
        Path(path).write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\0" * 8
            + struct.pack(">II", 64, 48)
            + str(scene.frame_current).encode()
        )

    renders.module.bpy.ops.render = SimpleNamespace(render=render)
    image = SimpleNamespace(save_render=save)
    renders.module.bpy.data.images = {"Render Result": image}
    return SimpleNamespace(worker=worker, job=job, rendered=rendered, image=image)


def test_worker_resumes_only_verified_frames_and_resets_attempt_progress(
    renders, animation_worker
):
    worker = animation_worker
    retained = save_frame(renders, worker.job, 1)
    original = retained.read_bytes()
    corrupt = save_frame(renders, worker.job, 3)
    corrupt.write_bytes(b"partial")
    (worker.job.directory / "status.json").write_text(
        json.dumps({"phase": "completed", "attempt_id": "old"})
    )
    assert renders.manager.status(worker.job.job_id)["phase"] == "starting"
    worker.worker.render_animation(worker.job.directory, worker.job.attempt_id)
    assert worker.rendered == [3, 5]
    assert retained.read_bytes() == original
    worker.job.process.code = 0
    result = renders.manager.status(worker.job.job_id)
    assert result["state"] == "completed"
    assert result["frames_completed"] == 3
    worker.rendered.clear()
    worker.worker.render_animation(worker.job.directory, "new-attempt")
    assert not worker.rendered
    assert not renders.module.bpy.app.handlers.render_stats


def test_worker_never_publishes_a_partial_frame(renders, animation_worker):
    worker = animation_worker
    save_frame(renders, worker.job, 1)

    def fail(path, scene):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("disk full")

    worker.image.save_render = fail
    with pytest.raises(RuntimeError, match="disk full"):
        worker.worker.render_animation(worker.job.directory, worker.job.attempt_id)
    assert not renders.addon.render_storage.frame_path(worker.job.directory, 3).exists()
    assert set(renders.addon.render_storage.read_frames(worker.job.directory)) == {"1"}
    worker.job.process.code = 1
    status = renders.manager.status(worker.job.job_id)
    assert status["state"] == "failed"
    assert status["error_message"] == "disk full"
    assert status["image_available"]
    assert not renders.module.bpy.app.handlers.render_stats
