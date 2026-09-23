from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast


class AnimationRequest(TypedDict):
    kind: str
    version: int
    job_id: str
    scene: str
    engine: str
    frame_start: int
    frame_end: int
    frame_step: int
    resolution_percentage: int | None
    snapshot_sha256: str
    fps: float


class FrameInfo(TypedDict):
    width: int
    height: int
    size_bytes: int
    sha256: str


def integer(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")


def frame_range(start: int, end: int, step: int) -> range:
    integer(start, "frame_start", -1048574, 1048574)
    integer(end, "frame_end", -1048574, 1048574)
    integer(step, "frame_step", 1, 1048574)
    frames = range(start, end + 1, step)
    if not 1 <= len(frames) <= 10000:
        raise ValueError(
            "Frame range must contain 1 to 10000 frames, with end >= start"
        )
    return frames


def checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def frame_path(directory: Path, frame: int) -> Path:
    return directory / "frames" / f"frame_{frame:07d}.png"


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as image:
        header = image.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Render output is not a PNG image")
    width, height = struct.unpack(">II", header[16:24])
    if not width or not height:
        raise ValueError("Render output has invalid dimensions")
    return width, height


def read_request(directory: Path) -> AnimationRequest:
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    if (
        not isinstance(request, dict)
        or request.get("kind") != "animation"
        or request.get("version") != 1
        or not isinstance(request.get("job_id"), str)
        or not re.fullmatch("[0-9a-f]{32}", request["job_id"])
        or not isinstance(request.get("scene"), str)
        or not request["scene"]
        or not isinstance(request.get("engine"), str)
        or not request["engine"]
    ):
        raise ValueError("Directory does not contain a supported animation render job")
    frame_range(
        request.get("frame_start"), request.get("frame_end"), request.get("frame_step")
    )
    resolution = request.get("resolution_percentage")
    if resolution is not None:
        integer(resolution, "resolution_percentage", 1, 100)
    fps = request.get("fps")
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("Animation request has invalid fps")
    snapshot = directory / "scene.blend"
    if snapshot.is_symlink() or checksum(snapshot) != request.get("snapshot_sha256"):
        raise ValueError("Animation snapshot checksum changed; start a new render job")
    if not (directory / "frames").is_dir() or (directory / "frames").is_symlink():
        raise ValueError("Animation frames directory is missing or is a symlink")
    return cast(AnimationRequest, request)


def read_frames(directory: Path) -> dict[str, FrameInfo]:
    try:
        data = json.loads((directory / "frames.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                key: cast(FrameInfo, info)
                for key, info in data.items()
                if isinstance(info, dict)
                and all(
                    type(info.get(field)) is int and info[field] > 0
                    for field in ("width", "height", "size_bytes")
                )
                and isinstance(info.get("sha256"), str)
            }
    except (OSError, ValueError):
        pass
    return {}


def valid_frame(directory: Path, frame: int, info: FrameInfo) -> bool:
    path = frame_path(directory, frame)
    try:
        return (
            not path.is_symlink()
            and path.stat().st_size == info["size_bytes"]
            and checksum(path) == info["sha256"]
            and png_dimensions(path) == (info["width"], info["height"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


@contextmanager
def job_lock(directory: Path) -> Iterator[None]:
    with (directory / "worker.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            if not (hasattr(msvcrt, "locking") and hasattr(msvcrt, "LK_NBLCK")):
                raise RuntimeError("File locking is unavailable on this platform")

            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError(
                    "Animation directory is in use by another worker"
                ) from error
        else:
            import fcntl

            if not (
                hasattr(fcntl, "flock")
                and hasattr(fcntl, "LOCK_EX")
                and hasattr(fcntl, "LOCK_NB")
            ):
                raise RuntimeError("File locking is unavailable on this platform")

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "Animation directory is in use by another worker"
                ) from error
        yield
