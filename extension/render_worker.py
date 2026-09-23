from __future__ import annotations

import json
import re
import struct
import sys
import traceback
from pathlib import Path
from typing import TypedDict

import bpy


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
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.progress: Progress | None = None

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
        write_status(self.directory, phase=phase, progress=self.progress, **fields)


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


def render(directory: Path) -> None:
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    scene = bpy.data.scenes[request["scene"]]
    scene.frame_set(request["frame"])
    if request["resolution_percentage"] is not None:
        scene.render.resolution_percentage = request["resolution_percentage"]
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.use_multiview = False
    for item in bpy.data.scenes:
        mute_file_outputs(getattr(item, "node_tree", None))
        mute_file_outputs(getattr(item, "compositing_node_group", None))
    progress = RenderProgress(directory)
    progress.write("rendering")
    bpy.app.handlers.render_stats.append(progress.update)
    try:
        result = bpy.ops.render.render(write_still=False, scene=scene.name)
    finally:
        bpy.app.handlers.render_stats.remove(progress.update)
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender render did not finish: {result}")
    progress.write("saving")
    path = directory / "render.png"
    bpy.data.images["Render Result"].save_render(str(path), scene=scene)
    with path.open("rb") as image:
        header = image.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Render output is not a PNG image")
    width, height = struct.unpack(">II", header[16:24])
    progress.write("completed", width=width, height=height)


if __name__ == "__main__":
    directory = Path(sys.argv[sys.argv.index("--") + 1])
    try:
        render(directory)
    except Exception as error:
        write_status(directory, phase="failed", error=str(error)[:4096])
        traceback.print_exc()
        raise
