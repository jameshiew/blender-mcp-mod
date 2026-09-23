import logging

import bpy

from . import inspection, scene, view
from .execution import ExecutionSession
from .metadata import addon_metadata
from .preferences import sketchfab_api_key
from .render_jobs import RenderJobs
from .sketchfab import SketchfabService
from .transport import CommandServer

logger = logging.getLogger(__name__)


class BlenderMCPServer(CommandServer):
    def __init__(self, host="127.0.0.1", port=9876, config_dir=None, *, renders=None):
        super().__init__(host, port, config_dir)
        self.execution = ExecutionSession(config_dir, lambda: self.running)
        self.renders = renders if renders is not None else RenderJobs()
        self.sketchfab = SketchfabService(
            sketchfab_api_key, lambda: bpy.context.scene.blendermcp_use_sketchfab
        )
        self.poll = self.renders.poll
        self.handlers = {
            "get_addon_info": self.get_addon_info,
            "get_scene_info": scene.get_scene_info,
            "get_object_info": scene.get_object_info,
            "get_material_info": inspection.material_info,
            "get_node_group_info": inspection.node_group_info,
            "get_modifier_info": inspection.modifier_info,
            "get_animation_info": inspection.animation_info,
            "set_camera": view.set_camera,
            "set_viewport": view.set_viewport,
            "get_viewport_screenshot": view.get_viewport_screenshot,
            **{
                name: getattr(self.execution, name)
                for name in (
                    "execute_code",
                    "create_checkpoint",
                    "list_checkpoints",
                    "restore_checkpoint",
                    "delete_checkpoint",
                    "get_execution_result",
                    "get_execution_changes",
                )
            },
            "start_render": self.renders.start,
            "get_render_status": self.renders.status,
            "cancel_render": self.renders.cancel,
            "get_render_image": self.renders.image,
            "export_render": self.renders.export,
        }
        self.sketchfab_handlers = {
            name: getattr(self.sketchfab, name)
            for name in (
                "search_sketchfab_models",
                "get_sketchfab_model_preview",
                "download_sketchfab_model",
            )
        }

    def execute_command(self, command):
        try:
            command_type = command.get("type")
            if command_type == "ping":
                return {"status": "success", "result": {"pong": True}}
            if command_type == "get_sketchfab_status":
                handler = self.sketchfab.get_sketchfab_status
            else:
                handler = self.handlers.get(command_type)
                if handler is None and bpy.context.scene.blendermcp_use_sketchfab:
                    handler = self.sketchfab_handlers.get(command_type)
            if handler is None:
                raise ValueError(f"Unknown command type: {command_type}")
            return {"status": "success", "result": handler(**command.get("params", {}))}
        except Exception as error:
            logger.exception("Error executing command")
            return {"status": "error", "message": str(error)}

    def get_addon_info(self):
        manifest, protocol = addon_metadata()
        version = manifest["version"]
        return {
            "name": manifest["name"],
            "addon_version": [
                int(part) for part in version.split("+")[0].split("-")[0].split(".")
            ],
            "addon_build_version": version,
            "protocol_version": protocol,
            "capabilities": sorted(self.handlers),
            "blender_version": bpy.app.version_string,
        }

    def stop(self):
        super().stop()
        self.execution.close()
        self.renders.close()
