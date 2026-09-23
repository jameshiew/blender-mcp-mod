import json
from pathlib import Path
import struct
import sys
import traceback

import bpy


def write_status(directory, **status):
    path = directory / "status.tmp"
    path.write_text(json.dumps(status), encoding="utf-8")
    path.replace(directory / "status.json")


def mute_file_outputs(tree, visited=None):
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
            mute_file_outputs(node.node_tree, visited)


def render(directory):
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
    write_status(directory, phase="rendering")
    result = bpy.ops.render.render(write_still=False, scene=scene.name)
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender render did not finish: {result}")
    write_status(directory, phase="saving")
    path = directory / "render.png"
    bpy.data.images["Render Result"].save_render(str(path), scene=scene)
    with path.open("rb") as image:
        header = image.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Render output is not a PNG image")
    width, height = struct.unpack(">II", header[16:24])
    write_status(directory, phase="completed", width=width, height=height)


if __name__ == "__main__":
    directory = Path(sys.argv[sys.argv.index("--") + 1])
    try:
        render(directory)
    except Exception as error:
        write_status(directory, phase="failed", error=str(error)[:4096])
        traceback.print_exc()
        raise
