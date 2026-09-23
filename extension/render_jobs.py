import atexit
import base64
import hashlib
from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import bpy


@dataclass
class RenderJob:
    job_id: str
    directory: Path
    process: subprocess.Popen
    scene: str
    frame: int
    engine: str
    started: float
    state: str = "running"
    finished: float | None = None
    cancel_requested: float | None = None
    kill_sent: bool = False
    error: str | None = None


class RenderJobs:
    max_jobs = 8

    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="blender-mcp-renders-")
        self._jobs = {}
        atexit.register(self.close)

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")

    @staticmethod
    def _worker_status(job):
        try:
            status = json.loads(
                (job.directory / "status.json").read_text(encoding="utf-8")
            )
            return status if isinstance(status, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _log_tail(job):
        with (job.directory / "worker.log").open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 4096))
            return log.read().decode("utf-8", errors="replace").strip()

    def poll(self):
        for job in self._jobs.values():
            if job.finished is not None:
                continue
            code = job.process.poll()
            if code is None:
                if (
                    job.cancel_requested is not None
                    and time.monotonic() - job.cancel_requested >= 2
                    and not job.kill_sent
                ):
                    with suppress(ProcessLookupError):
                        job.process.kill()
                    job.kill_sent = True
                continue
            job.finished = time.monotonic()
            status = self._worker_status(job)
            if job.cancel_requested is not None:
                job.state = "cancelled"
            elif (
                code == 0
                and status.get("phase") == "completed"
                and (job.directory / "render.png").is_file()
            ):
                job.state = "completed"
            else:
                job.state = "failed"
                job.error = status.get("error") or (
                    f"Blender render worker exited with code {code}. "
                    + self._log_tail(job)
                )
            (job.directory / "scene.blend").unlink(missing_ok=True)

    def start(self, scene_name=None, frame=None, resolution_percentage=None):
        self.poll()
        for job in self._jobs.values():
            if job.finished is None:
                raise RuntimeError(f"Render job {job.job_id} is still {job.state}")
        if scene_name is not None and (
            not isinstance(scene_name, str) or not scene_name
        ):
            raise ValueError("scene_name must be a non-empty string")
        scene = (
            bpy.context.scene if scene_name is None else bpy.data.scenes.get(scene_name)
        )
        if scene is None:
            raise ValueError(f"Scene not found: {scene_name}")
        if scene.camera is None or scene.camera.type != "CAMERA":
            raise ValueError(f"Scene {scene.name} has no active camera")
        if frame is None:
            frame = scene.frame_current
        self._integer(frame, "frame", -1048574, 1048574)
        if resolution_percentage is not None:
            self._integer(resolution_percentage, "resolution_percentage", 1, 100)
        if not bpy.app.binary_path:
            raise RuntimeError("Blender executable path is unavailable")

        job_id = uuid.uuid4().hex
        directory = Path(self._temporary.name) / job_id
        directory.mkdir()
        try:
            bpy.data.libraries.write(
                str(directory / "scene.blend"), {scene}, path_remap="ABSOLUTE"
            )
            (directory / "request.json").write_text(
                json.dumps(
                    {
                        "scene": scene.name,
                        "frame": frame,
                        "resolution_percentage": resolution_percentage,
                    }
                ),
                encoding="utf-8",
            )
            with (directory / "worker.log").open("wb") as log:
                process = subprocess.Popen(
                    [
                        bpy.app.binary_path,
                        "--background",
                        "--offline-mode",
                        "--disable-autoexec",
                        str(directory / "scene.blend"),
                        "--python-exit-code",
                        "1",
                        "--python",
                        str(Path(__file__).with_name("render_worker.py")),
                        "--",
                        str(directory),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        except Exception:
            self._remove_directory(directory)
            raise
        self._jobs[job_id] = RenderJob(
            job_id,
            directory,
            process,
            scene.name,
            frame,
            scene.render.engine,
            time.monotonic(),
        )
        while len(self._jobs) > self.max_jobs:
            oldest = next(iter(self._jobs))
            self._remove_directory(self._jobs.pop(oldest).directory)
        return self.status(job_id)

    @staticmethod
    def _remove_directory(directory):
        import shutil

        shutil.rmtree(directory)

    def _get(self, job_id):
        if job_id is None:
            if not self._jobs:
                raise ValueError("No render jobs in this server session")
            job_id = next(reversed(self._jobs))
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise ValueError(f"Unknown or expired render job: {job_id}")
        return self._jobs[job_id]

    def status(self, job_id=None):
        self.poll()
        job = self._get(job_id)
        worker = self._worker_status(job)
        result = {
            "job_id": job.job_id,
            "state": job.state,
            "phase": worker.get("phase", "starting")
            if job.state == "running"
            else job.state,
            "scene": job.scene,
            "frame": job.frame,
            "engine": job.engine,
            "elapsed_seconds": round(
                (job.finished or time.monotonic()) - job.started, 3
            ),
            "image_available": job.state == "completed",
            "progress": worker.get("progress"),
        }
        if job.state == "completed":
            result.update(width=worker["width"], height=worker["height"])
        if job.error:
            result["error_message"] = job.error
        if result["progress"] is not None and job.state != "running":
            result["progress"] = {**result["progress"], "remaining_seconds": None}
        return result

    def cancel(self, job_id):
        self.poll()
        job = self._get(job_id)
        if job.finished is None and job.cancel_requested is None:
            with suppress(ProcessLookupError):
                job.process.terminate()
            job.cancel_requested = time.monotonic()
            job.state = "cancelling"
        return self.status(job.job_id)

    def image(self, job_id, max_size=1000):
        self._integer(max_size, "max_size", 1, 4096)
        status = self.status(job_id)
        if status["state"] != "completed":
            raise ValueError(
                f"Render job {status['job_id']} is {status['state']}; no image available"
            )
        job = self._get(job_id)
        path = job.directory / "render.png"
        width, height = status["width"], status["height"]
        if max(width, height) > max_size:
            image = bpy.data.images.load(str(path), check_existing=False)
            try:
                scale = max_size / max(width, height)
                width, height = max(1, int(width * scale)), max(1, int(height * scale))
                image.scale(width, height)
                image.filepath_raw = str(job.directory / "preview.png")
                image.file_format = "PNG"
                image.save()
                data = Path(image.filepath_raw).read_bytes()
            finally:
                bpy.data.images.remove(image)
        else:
            data = path.read_bytes()
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("Render image exceeds 16 MiB; request a smaller max_size")
        return {
            **status,
            "source_width": status["width"],
            "source_height": status["height"],
            "width": width,
            "height": height,
            "format": "png",
            "image_data": base64.b64encode(data).decode("ascii"),
        }

    def export(self, job_id, filepath, overwrite=False):
        if not isinstance(filepath, str) or not filepath:
            raise ValueError("filepath must be a non-empty absolute PNG path")
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        destination = Path(filepath).expanduser()
        if not destination.is_absolute() or destination.suffix.lower() != ".png":
            raise ValueError("filepath must be an absolute path ending in .png")
        if destination.resolve().is_relative_to(Path(self._temporary.name).resolve()):
            raise ValueError("Export outside the temporary render storage")
        status = self.status(job_id)
        if status["state"] != "completed":
            raise ValueError(
                f"Render job {status['job_id']} is {status['state']}; no image available"
            )
        source = self._get(job_id).directory / "render.png"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".blender-mcp-export-", delete=False
            ) as output:
                temporary = Path(output.name)
                with source.open("rb") as image:
                    shutil.copyfileobj(image, output)
                output.flush()
                os.fsync(output.fileno())
            with temporary.open("rb") as image:
                checksum = hashlib.file_digest(image, "sha256").hexdigest()
            size = temporary.stat().st_size
            if overwrite:
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination)
            return {
                "job_id": job_id,
                "filepath": str(destination),
                "format": "png",
                "width": status["width"],
                "height": status["height"],
                "size_bytes": size,
                "sha256": checksum,
            }
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def close(self):
        for job in self._jobs.values():
            if job.process.poll() is None:
                with suppress(ProcessLookupError):
                    job.process.kill()
                job.process.wait(timeout=5)
        self._jobs.clear()
        self._temporary.cleanup()
        atexit.unregister(self.close)
