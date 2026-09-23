from __future__ import annotations

import json
import re
import struct
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import bpy

if TYPE_CHECKING:
    from .render_storage import FrameInfo


def write_status(directory: Path, **status: object) -> None:
    path = directory / "status.tmp"
    path.write_text(json.dumps(status), encoding="utf-8")
    path.replace(directory / "status.json")


class Progress(TypedDict):
    status_text: str
    samples_completed: int | None
    samples_total: int | None
    sample_fraction: float | None
    remaining_seconds: float | None


class RenderProgress:
    def __init__(self, directory: Path, **fields: object) -> None:
        self.directory = directory
        self.progress: Progress | None = None
        self.fields = fields

    def update(self, statistics: object, *_args: object) -> None:
        if not isinstance(statistics, str):
            return
        samples = re.search(
            r"\bSample\s+(\d+)\s*/\s*(\d+)\b", statistics, re.IGNORECASE
        )
        remaining = re.search(
            r"\bRemaining:\s*((?:\d+:){1,2}\d+(?:\.\d+)?)", statistics
        )
        seconds = None
        if remaining:
            seconds = 0.0
            for component in remaining[1].split(":"):
                seconds = seconds * 60 + float(component)
        previous = self.progress
        completed, total = (
            (int(value) for value in samples.groups())
            if samples
            else (previous["samples_completed"], previous["samples_total"])
            if previous
            else (None, None)
        )
        self.progress = {
            "status_text": statistics[:2048],
            "samples_completed": completed,
            "samples_total": total,
            "sample_fraction": min(1.0, completed / total)
            if total and completed is not None
            else None,
            "remaining_seconds": seconds,
        }
        self.write("rendering")

    def write(self, phase: str, **fields: object) -> None:
        if phase != "rendering" and self.progress:
            self.progress = {**self.progress, "remaining_seconds": None}
        self.fields.update(fields)
        write_status(self.directory, phase=phase, progress=self.progress, **self.fields)


def mute_file_outputs(
    tree: bpy.types.NodeTree | None, visited: set[bpy.types.NodeTree] | None = None
) -> None:
    if tree is None:
        return
    if visited is None:
        visited = set()
    if tree in visited:
        return
    visited.add(tree)
    for node in tree.nodes:
        if node.type == "OUTPUT_FILE":
            node.mute = True
        if node.type == "GROUP":
            mute_file_outputs(getattr(node, "node_tree", None), visited)


def configure_scene(scene: bpy.types.Scene, resolution_percentage: int | None) -> None:
    if resolution_percentage is not None:
        scene.render.resolution_percentage = resolution_percentage
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.use_multiview = False
    for item in bpy.data.scenes:
        mute_file_outputs(getattr(item, "node_tree", None))
        mute_file_outputs(getattr(item, "compositing_node_group", None))


def render_frame(scene: bpy.types.Scene, progress: RenderProgress) -> None:
    progress.progress = None
    progress.write("rendering")
    bpy.app.handlers.render_stats.append(progress.update)
    try:
        result = bpy.ops.render.render(write_still=False, scene=scene.name)
    finally:
        bpy.app.handlers.render_stats.remove(progress.update)
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender render did not finish: {result}")
    progress.write("saving")


def render_animation(directory: Path, attempt_id: str) -> None:
    from .render_storage import (
        checksum,
        frame_path,
        frame_range,
        job_lock,
        png_dimensions,
        read_frames,
        read_request,
        valid_frame,
        write_json,
    )

    with job_lock(directory):
        progress = RenderProgress(directory, attempt_id=attempt_id)
        try:
            request = read_request(directory)
            frames = frame_range(
                request["frame_start"], request["frame_end"], request["frame_step"]
            )
            progress.write("validating", frames_total=len(frames), frames_completed=0)
            saved = read_frames(directory)
            completed = {
                str(frame): saved[str(frame)]
                for frame in frames
                if str(frame) in saved
                and valid_frame(directory, frame, saved[str(frame)])
            }
            write_json(directory / "frames.json", completed)
            last = int(next(reversed(completed))) if completed else None
            progress.write(
                "starting",
                frame=last if last is not None else request["frame_start"],
                frames_completed=len(completed),
                last_completed_frame=last,
            )
            scene = bpy.data.scenes[request["scene"]]
            configure_scene(scene, request["resolution_percentage"])
            started = time.monotonic()
            rendered = 0
            for frame in frames:
                if str(frame) in completed:
                    continue
                scene.frame_set(frame)
                progress.fields["frame"] = frame
                render_frame(scene, progress)
                path = frame_path(directory, frame)
                temporary = path.with_suffix(".partial.png")
                bpy.data.images["Render Result"].save_render(
                    str(temporary), scene=scene
                )
                width, height = png_dimensions(temporary)
                info: FrameInfo = {
                    "width": width,
                    "height": height,
                    "size_bytes": temporary.stat().st_size,
                    "sha256": checksum(temporary),
                }
                temporary.replace(path)
                completed[str(frame)] = info
                write_json(directory / "frames.json", completed)
                rendered += 1
                progress.write(
                    "rendering",
                    frames_completed=len(completed),
                    last_completed_frame=frame,
                    remaining_frames_seconds=(time.monotonic() - started)
                    / rendered
                    * (len(frames) - len(completed)),
                )
            progress.write("completed", remaining_frames_seconds=None)
        except Exception as error:
            progress.write(
                "failed", error=str(error)[:4096], remaining_frames_seconds=None
            )
            raise


def render(directory: Path) -> None:
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    scene = bpy.data.scenes[request["scene"]]
    scene.frame_set(request["frame"])
    configure_scene(scene, request["resolution_percentage"])
    progress = RenderProgress(directory)
    render_frame(scene, progress)
    path = directory / "render.png"
    bpy.data.images["Render Result"].save_render(str(path), scene=scene)
    with path.open("rb") as image:
        header = image.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Render output is not a PNG image")
    width, height = struct.unpack(">II", header[16:24])
    progress.write("completed", width=width, height=height)


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--") + 1 :]
    directory = Path(arguments[0])
    animation = len(arguments) == 2
    try:
        if animation:
            package = type(sys)("_blender_mcp_render")
            package.__path__ = [str(Path(__file__).parent)]
            sys.modules[package.__name__] = package
            __package__ = package.__name__
            render_animation(directory, arguments[1])
        else:
            render(directory)
    except Exception as error:
        if not animation:
            write_status(directory, phase="failed", error=str(error)[:4096])
        traceback.print_exc()
        raise
