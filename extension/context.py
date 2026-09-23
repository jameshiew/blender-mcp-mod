from __future__ import annotations

from typing import TYPE_CHECKING, cast

import bpy

if TYPE_CHECKING:

    class MCPScene(bpy.types.Scene):
        blendermcp_port: int
        blendermcp_server_running: bool
        blendermcp_auto_start_server: bool
        blendermcp_use_sketchfab: bool
        blendermcp_sketchfab_api_key: str


def current_scene(context: bpy.types.Context | None = None) -> MCPScene:
    scene = (context or bpy.context).scene
    if scene is None:
        raise RuntimeError("No active Blender scene")
    return cast("MCPScene", scene)


def current_view_layer() -> bpy.types.ViewLayer:
    layer = bpy.context.view_layer
    if layer is None:
        raise RuntimeError("No active Blender view layer")
    return layer
