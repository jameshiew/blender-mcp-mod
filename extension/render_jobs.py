from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

import bpy

from .render_storage import (
    AnimationRequest,
    checksum,
    frame_path,
    frame_range,
    integer,
    job_lock,
    read_frames,
    read_request,
    valid_frame,
    write_json,
)

if TYPE_CHECKING:
    from .render_worker import Progress


class WorkerStatus(TypedDict, total=False):
    phase: str
    error: str
    width: int
    height: int
    progress: Progress | None
    attempt_id: str
    frame: int
    remaining_frames_seconds: float | None


class RenderStatus(TypedDict):
    job_id: str
    state: str
    phase: str
    scene: str
    frame: int
    engine: str
    elapsed_seconds: float
    image_available: bool
    progress: Progress | None
    width: NotRequired[int]
    height: NotRequired[int]
    error_message: NotRequired[str]
    kind: NotRequired[str]
    output_directory: NotRequired[str]
    frame_start: NotRequired[int]
    frame_end: NotRequired[int]
    frame_step: NotRequired[int]
    frames_total: NotRequired[int]
    frames_completed: NotRequired[int]
    frame_fraction: NotRequired[float]
    last_completed_frame: NotRequired[int | None]
    remaining_frames_seconds: NotRequired[float | None]
    fps: NotRequired[float]


@dataclass
class RenderJob:
    job_id: str
    directory: Path
    process: subprocess.Popen[bytes]
    scene: str
    frame: int
    engine: str
    started: float
    state: Literal["running", "cancelling", "cancelled", "completed", "failed"] = (
        "running"
    )
    finished: float | None = None
    cancel_requested: float | None = None
    kill_sent: bool = False
    error: str | None = None
    animation: AnimationRequest | None = None
    attempt_id: str | None = None


class RenderJobs:
    max_jobs = 8

    def __init__(self) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._jobs: dict[str, RenderJob] = {}
        self._registered = False

    @staticmethod
    def _integer(value: int, name: str, minimum: int, maximum: int) -> None:
        integer(value, name, minimum, maximum)

    @staticmethod
    def _worker_status(job: RenderJob) -> WorkerStatus:
        try:
            status = json.loads(
                (job.directory / "status.json").read_text(encoding="utf-8")
            )
            if not isinstance(status, dict):
                return {}
            if (
                job.attempt_id is not None
                and status.get("attempt_id") != job.attempt_id
            ):
                return {}
            return cast(WorkerStatus, status)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _log_tail(job: RenderJob) -> str:
        name = f"worker-{job.attempt_id}.log" if job.attempt_id else "worker.log"
        with (job.directory / name).open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 4096))
            return log.read().decode("utf-8", errors="replace").strip()

    def poll(self) -> None:
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
                and self._output_complete(job)
            ):
                job.state = "completed"
            else:
                job.state = "failed"
                job.error = status.get("error") or (
                    f"Blender render worker exited with code {code}. "
                    + self._log_tail(job)
                )
            if job.animation is None:
                (job.directory / "scene.blend").unlink(missing_ok=True)

    @staticmethod
    def _output_complete(job: RenderJob) -> bool:
        if job.animation is None:
            return (job.directory / "render.png").is_file()
        request = job.animation
        completed = read_frames(job.directory)
        return all(
            str(frame) in completed and frame_path(job.directory, frame).is_file()
            for frame in frame_range(
                request["frame_start"], request["frame_end"], request["frame_step"]
            )
        )

    def _ensure_idle(self) -> None:
        self.poll()
        for job in self._jobs.values():
            if job.finished is None:
                raise RuntimeError(f"Render job {job.job_id} is still {job.state}")

    @staticmethod
    def _scene(scene_name: str | None) -> bpy.types.Scene:
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
        if not bpy.app.binary_path:
            raise RuntimeError("Blender executable path is unavailable")
        return scene

    @staticmethod
    def _launch(
        directory: Path, attempt_id: str | None = None
    ) -> subprocess.Popen[bytes]:
        name = f"worker-{attempt_id}.log" if attempt_id else "worker.log"
        with (directory / name).open("wb") as log:
            return subprocess.Popen(
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
                    *([attempt_id] if attempt_id is not None else []),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def _remember(self, job: RenderJob) -> RenderStatus:
        if not self._registered:
            atexit.register(self.close)
            self._registered = True
        self._jobs.pop(job.job_id, None)
        self._jobs[job.job_id] = job
        while len(self._jobs) > self.max_jobs:
            oldest = self._jobs.pop(next(iter(self._jobs)))
            if oldest.animation is None:
                self._remove_directory(oldest.directory)
        return self.status(job.job_id)

    def start(
        self,
        scene_name: str | None = None,
        frame: int | None = None,
        resolution_percentage: int | None = None,
    ) -> RenderStatus:
        self._ensure_idle()
        scene = self._scene(scene_name)
        if frame is None:
            frame = scene.frame_current
        self._integer(frame, "frame", -1048574, 1048574)
        if resolution_percentage is not None:
            self._integer(resolution_percentage, "resolution_percentage", 1, 100)
        job_id = uuid.uuid4().hex
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="blender-mcp-renders-")
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
            process = self._launch(directory)
        except Exception:
            self._remove_directory(directory)
            raise
        return self._remember(
            RenderJob(
                job_id,
                directory,
                process,
                scene.name,
                frame,
                scene.render.engine,
                time.monotonic(),
            )
        )

    def _output_directory(self, output_directory: str) -> Path:
        if not isinstance(output_directory, str) or not output_directory:
            raise ValueError("output_directory must be a non-empty absolute path")
        directory = Path(output_directory).expanduser()
        if not directory.is_absolute():
            raise ValueError("output_directory must be an absolute path")
        directory = directory.resolve()
        if self._temporary is not None and directory.is_relative_to(
            Path(self._temporary.name).resolve()
        ):
            raise ValueError(
                "Store animation output outside the temporary render storage"
            )
        return directory

    def start_animation(
        self,
        output_directory: str,
        scene_name: str | None = None,
        frame_start: int | None = None,
        frame_end: int | None = None,
        frame_step: int | None = None,
        resolution_percentage: int | None = None,
    ) -> RenderStatus:
        self._ensure_idle()
        scene = self._scene(scene_name)
        start = scene.frame_start if frame_start is None else frame_start
        end = scene.frame_end if frame_end is None else frame_end
        step = scene.frame_step if frame_step is None else frame_step
        frame_range(start, end, step)
        if resolution_percentage is not None:
            integer(resolution_percentage, "resolution_percentage", 1, 100)
        directory = self._output_directory(output_directory)
        directory.mkdir()
        try:
            (directory / "frames").mkdir()
            bpy.data.libraries.write(
                str(directory / "scene.blend"), {scene}, path_remap="ABSOLUTE"
            )
            request: AnimationRequest = {
                "kind": "animation",
                "version": 1,
                "job_id": uuid.uuid4().hex,
                "scene": scene.name,
                "engine": scene.render.engine,
                "frame_start": start,
                "frame_end": end,
                "frame_step": step,
                "resolution_percentage": resolution_percentage,
                "fps": scene.render.fps / scene.render.fps_base / step,
                "snapshot_sha256": checksum(directory / "scene.blend"),
            }
            write_json(directory / "request.json", request)
        except Exception:
            self._remove_directory(directory)
            raise
        return self._start_animation_worker(directory, request)

    def _start_animation_worker(
        self, directory: Path, request: AnimationRequest
    ) -> RenderStatus:
        attempt_id = uuid.uuid4().hex
        process = self._launch(directory, attempt_id)
        return self._remember(
            RenderJob(
                request["job_id"],
                directory,
                process,
                request["scene"],
                request["frame_start"],
                request["engine"],
                time.monotonic(),
                animation=request,
                attempt_id=attempt_id,
            )
        )

    def resume_animation(self, output_directory: str) -> RenderStatus:
        self._ensure_idle()
        directory = self._output_directory(output_directory)
        if not bpy.app.binary_path:
            raise RuntimeError("Blender executable path is unavailable")
        with job_lock(directory):
            request = read_request(directory)
        return self._start_animation_worker(directory, request)

    @staticmethod
    def _remove_directory(directory: Path) -> None:
        import shutil

        shutil.rmtree(directory)

    def _get(self, job_id: str | None) -> RenderJob:
        if job_id is None:
            if not self._jobs:
                raise ValueError("No render jobs in this server session")
            job_id = next(reversed(self._jobs))
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise ValueError(f"Unknown or expired render job: {job_id}")
        return self._jobs[job_id]

    def status(self, job_id: str | None = None) -> RenderStatus:
        self.poll()
        job = self._get(job_id)
        worker = self._worker_status(job)
        result: RenderStatus = {
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
        if job.state == "completed" and job.animation is None:
            result.update(width=worker["width"], height=worker["height"])
        if job.error:
            result["error_message"] = job.error
        if result["progress"] is not None and job.state != "running":
            result["progress"] = {**result["progress"], "remaining_seconds": None}
        if job.animation is not None:
            request = job.animation
            frames = frame_range(
                request["frame_start"], request["frame_end"], request["frame_step"]
            )
            completed = read_frames(job.directory)
            completed = {
                str(f): completed[str(f)] for f in frames if str(f) in completed
            }
            last = int(next(reversed(completed))) if completed else None
            result.update(
                kind="animation",
                output_directory=str(job.directory),
                frame_start=request["frame_start"],
                frame_end=request["frame_end"],
                frame_step=request["frame_step"],
                frames_total=len(frames),
                frames_completed=len(completed),
                fps=request["fps"],
                frame_fraction=len(completed) / len(frames),
                last_completed_frame=last,
                image_available=last is not None,
                frame=worker.get("frame", job.frame),
                remaining_frames_seconds=worker.get("remaining_frames_seconds")
                if job.state == "running"
                else None,
            )
            if last is not None:
                result.update(
                    width=completed[str(last)]["width"],
                    height=completed[str(last)]["height"],
                )
        return result

    def cancel(self, job_id: str) -> RenderStatus:
        self.poll()
        job = self._get(job_id)
        if job.finished is None and job.cancel_requested is None:
            with suppress(ProcessLookupError):
                job.process.terminate()
            job.cancel_requested = time.monotonic()
            job.state = "cancelling"
        return self.status(job.job_id)

    def _image_source(
        self, job_id: str, frame: int | None
    ) -> tuple[RenderStatus, Path]:
        if frame is not None:
            integer(frame, "frame", -1048574, 1048574)
        status = self.status(job_id)
        job = self._get(job_id)
        if job.animation is not None:
            if frame is None:
                frame = status["last_completed_frame"]
            request = job.animation
            if frame is not None and frame not in frame_range(
                request["frame_start"], request["frame_end"], request["frame_step"]
            ):
                raise ValueError(
                    f"Animation frame {frame} is not complete or is outside the requested range"
                )
            info = read_frames(job.directory).get(str(frame))
            if (
                frame is None
                or info is None
                or not valid_frame(job.directory, frame, info)
            ):
                raise ValueError(
                    f"Animation frame {frame} is not complete or has changed; resume the job to render it"
                )
            status.update(
                frame=frame,
                width=info["width"],
                height=info["height"],
                image_available=True,
            )
            return status, frame_path(job.directory, frame)
        if frame is not None and frame != job.frame:
            raise ValueError(f"Still render job contains only frame {job.frame}")
        if status["state"] != "completed":
            raise ValueError(
                f"Render job {status['job_id']} is {status['state']}; no image available"
            )
        return status, job.directory / "render.png"

    def image(
        self, job_id: str, max_size: int = 1000, frame: int | None = None
    ) -> dict[str, object]:
        self._integer(max_size, "max_size", 1, 4096)
        status, path = self._image_source(job_id, frame)
        width, height = status["width"], status["height"]
        if max(width, height) > max_size:
            image = bpy.data.images.load(str(path), check_existing=False)
            try:
                scale = max_size / max(width, height)
                width, height = max(1, int(width * scale)), max(1, int(height * scale))
                image.scale(width, height)
                image.filepath_raw = str(self._get(job_id).directory / "preview.png")
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

    def export(
        self,
        job_id: str,
        filepath: str,
        overwrite: bool = False,
        frame: int | None = None,
    ) -> dict[str, object]:
        if not isinstance(filepath, str) or not filepath:
            raise ValueError("filepath must be a non-empty absolute PNG path")
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        destination = Path(filepath).expanduser()
        if not destination.is_absolute() or destination.suffix.lower() != ".png":
            raise ValueError("filepath must be an absolute path ending in .png")
        if self._temporary is not None and destination.resolve().is_relative_to(
            Path(self._temporary.name).resolve()
        ):
            raise ValueError("Export outside the temporary render storage")
        job = self._get(job_id)
        if job.animation is not None and destination.resolve().is_relative_to(
            job.directory
        ):
            raise ValueError("Export outside the animation render directory")
        status, source = self._image_source(job_id, frame)
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

    def close(self) -> None:
        for job in self._jobs.values():
            if job.process.poll() is None:
                with suppress(ProcessLookupError):
                    job.process.kill()
                job.process.wait(timeout=5)
        self._jobs.clear()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        atexit.unregister(self.close)
        self._registered = False
