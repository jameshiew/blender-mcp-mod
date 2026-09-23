from typing import TYPE_CHECKING, Literal, Protocol, cast

import bpy

from .context import current_scene
from .metadata import addon_metadata
from .preferences import get_preferences
from .server import BlenderMCPServer

if TYPE_CHECKING:

    class ServerRegistry(Protocol):
        blendermcp_server: BlenderMCPServer


class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or ""

    if TYPE_CHECKING:
        sketchfab_api_key: str
    else:
        # Blender evaluates property annotations during registration.
        sketchfab_api_key: bpy.props.StringProperty(
            name="Sketchfab API Key",
            subtype="PASSWORD",
            description="Persistent Sketchfab API Key",
            default="",
        )

    def draw(self, context: "bpy.types.Context | None") -> None:
        self.layout.prop(self, "sketchfab_api_key")


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "MCP for Blender"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP for Blender"

    def draw(self, context: "bpy.types.Context | None") -> None:
        layout = self.layout
        if layout is None:
            return
        scene = current_scene(context)
        server = cast(
            BlenderMCPServer | None, getattr(bpy.types, "blendermcp_server", None)
        )
        box = layout.box()
        if server is not None and server.running:
            box.label(text=f"Connected on port {server.port}", icon="CHECKMARK")
            box.operator("blendermcp.stop_server", text="Disconnect", icon="X")
        else:
            box.label(text="Not connected", icon="RADIOBUT_OFF")
            if server and server.last_error:
                box.label(text="Run blender-mcp setup-connection", icon="ERROR")
            box.prop(scene, "blendermcp_port")
            box.operator(
                "blendermcp.start_server", text="Connect to MCP server", icon="PLAY"
            )

        layout.separator()
        box = layout.box()
        box.prop(
            scene, "blendermcp_use_sketchfab", text="Sketchfab", icon="MESH_MONKEY"
        )
        if scene.blendermcp_use_sketchfab:
            preferences = get_preferences(context)
            if preferences:
                box.prop(preferences, "sketchfab_api_key", text="API Key")
            else:
                box.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")


def start_server(port: int) -> BlenderMCPServer:
    server = cast(
        BlenderMCPServer | None, getattr(bpy.types, "blendermcp_server", None)
    )
    if server is None:
        server = BlenderMCPServer(port=port)
        cast("ServerRegistry", bpy.types).blendermcp_server = server
    server.start()
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        scene.blendermcp_server_running = server.running
    return server


def stop_server() -> None:
    server = cast(
        BlenderMCPServer | None, getattr(bpy.types, "blendermcp_server", None)
    )
    if server is not None:
        server.stop()
        delattr(bpy.types, "blendermcp_server")
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        scene.blendermcp_server_running = False


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to MCP server"
    bl_description = "Start the MCP for Blender listener"

    def execute(
        self, context: "bpy.types.Context | None"
    ) -> set[
        Literal["RUNNING_MODAL", "CANCELLED", "FINISHED", "PASS_THROUGH", "INTERFACE"]
    ]:
        server = start_server(current_scene(context).blendermcp_port)
        if not server.running:
            self.report({"ERROR"}, server.last_error or "MCP server could not start")
            return {"CANCELLED"}
        return {"FINISHED"}


class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect from MCP server"
    bl_description = "Stop the MCP for Blender listener"

    def execute(
        self, context: "bpy.types.Context | None"
    ) -> set[
        Literal["RUNNING_MODAL", "CANCELLED", "FINISHED", "PASS_THROUGH", "INTERFACE"]
    ]:
        stop_server()
        return {"FINISHED"}


CLASSES = (
    BLENDERMCP_AddonPreferences,
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)
SCENE_PROPERTIES = {
    "blendermcp_port": bpy.props.IntProperty(
        name="Port",
        description="Port for the MCP for Blender server",
        default=9876,
        min=1024,
        max=65535,
    ),
    "blendermcp_server_running": bpy.props.BoolProperty(
        name="Server Running", default=False
    ),
    "blendermcp_auto_start_server": bpy.props.BoolProperty(
        name="Auto-Start Server",
        description="Automatically start the MCP server when Blender loads",
        default=True,
    ),
    "blendermcp_use_sketchfab": bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False,
    ),
    "blendermcp_sketchfab_api_key": bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="API Key provided by Sketchfab",
        default="",
    ),
}


def register() -> None:
    addon_metadata()
    for name, prop in SCENE_PROPERTIES.items():
        setattr(bpy.types.Scene, name, prop)
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    scene = getattr(bpy.context, "scene", None)
    if getattr(scene, "blendermcp_auto_start_server", True):
        start_server(getattr(scene, "blendermcp_port", 9876))


def unregister() -> None:
    stop_server()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for name in SCENE_PROPERTIES:
        delattr(bpy.types.Scene, name)
