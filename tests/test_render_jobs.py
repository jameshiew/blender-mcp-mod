import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from addon_stub import _load_addon, _scene
from conftest import ROOT_ADDON


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


@pytest.fixture
def renders(monkeypatch):
    addon = _load_addon(monkeypatch, _scene())
    name = "render_jobs_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT_ADDON.with_name("render_jobs.py")
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    scene = type("Scene", (), {})()
    scene.name = "植物 • Scene"
    scene.blendermcp_use_sketchfab = False
    scene.camera = SimpleNamespace(type="CAMERA")
    scene.frame_current = 12
    scene.render = SimpleNamespace(engine="CYCLES", resolution_percentage=80)
    addon.bpy.context.scene = scene
    addon.bpy.app.binary_path = "/Blender with spaces/blender"
    snapshots = []

    def write(path, datablocks, **kwargs):
        snapshots.append((path, datablocks, kwargs))
        Path(path).write_bytes(b"snapshot")

    addon.bpy.data = SimpleNamespace(
        scenes={scene.name: scene},
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


def test_missing_camera_and_launch_failure_are_clean(renders, monkeypatch):
    renders.scene.camera = None
    with pytest.raises(ValueError, match="camera"):
        renders.manager.start()
    renders.scene.camera = SimpleNamespace(type="CAMERA")

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
    renders.addon.bpy.data.images = SimpleNamespace(
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
    server = renders.addon.BlenderMCPServer()
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
    server._render_jobs = renders.manager
    response = server.execute_command({"type": "start_render", "params": {}})
    assert response["status"] == "success"
    job_id = response["result"]["job_id"]
    assert server.get_render_status()["job_id"] == job_id
    assert server.cancel_render(job_id)["state"] == "cancelling"
    server.stop()
    assert server._render_jobs is None
    assert renders.processes[0].killed == 1


def test_export_keeps_full_image_after_job_cleanup(renders, tmp_path):
    job_id = renders.manager.start()["job_id"]
    complete(renders, job_id, width=4000, height=2000)
    destination = tmp_path / "永久 image.png"
    server = renders.addon.BlenderMCPServer()
    server._render_jobs = renders.manager
    result = server.export_render(job_id, str(destination))
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
