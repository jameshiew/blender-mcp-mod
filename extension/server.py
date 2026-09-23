from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping

import bpy

from . import inspection, scene, view
from .context import current_scene
from .execution import ExecutionSession
from .metadata import addon_metadata
from .preferences import sketchfab_api_key
from .render_jobs import RenderJobs
from .sketchfab import SketchfabService
from .transport import CommandServer

logger = logging.getLogger(__name__)


class BlenderMCPServer(CommandServer):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9876,
        config_dir: str | os.PathLike[str] | None = None,
        *,
        renders: RenderJobs | None = None,
    ) -> None:
        super().__init__(host, port, config_dir)
        self.execution = ExecutionSession(config_dir, lambda: self.running)
        self.renders = renders if renders is not None else RenderJobs()
        self.sketchfab = SketchfabService(
            sketchfab_api_key, lambda: current_scene().blendermcp_use_sketchfab
        )
        self.poll = self.renders.poll
        self.handlers: dict[str, Callable[..., Mapping[str, object]]] = {
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
            "execute_code": self.execution.execute_code,
            "create_checkpoint": self.execution.create_checkpoint,
            "list_checkpoints": self.execution.list_checkpoints,
            "restore_checkpoint": self.execution.restore_checkpoint,
            "delete_checkpoint": self.execution.delete_checkpoint,
            "get_execution_result": self.execution.get_execution_result,
            "get_execution_changes": self.execution.get_execution_changes,
            "start_render": self.renders.start,
            "get_render_status": self.renders.status,
            "cancel_render": self.renders.cancel,
            "get_render_image": self.renders.image,
            "export_render": self.renders.export,
        }
        self.sketchfab_handlers: dict[str, Callable[..., Mapping[str, object]]] = {
            "search_sketchfab_models": self.sketchfab.search_sketchfab_models,
            "get_sketchfab_model_preview": self.sketchfab.get_sketchfab_model_preview,
            "download_sketchfab_model": self.sketchfab.download_sketchfab_model,
        }

    def execute_command(self, command: Mapping[str, object]) -> dict[str, object]:
        try:
            command_type = command.get("type")
            if not isinstance(command_type, str):
                raise TypeError(f"Unknown command type: {command_type}")
            if command_type == "ping":
                return {"status": "success", "result": {"pong": True}}
            if command_type == "get_sketchfab_status":
                handler = self.sketchfab.get_sketchfab_status
            else:
                handler = self.handlers.get(command_type)
                if handler is None and current_scene().blendermcp_use_sketchfab:
                    handler = self.sketchfab_handlers.get(command_type)
            if handler is None:
                raise ValueError(f"Unknown command type: {command_type}")
            params = command.get("params", {})
            if not isinstance(params, dict):
                raise TypeError("Command params must be a JSON object")
            return {"status": "success", "result": handler(**params)}
        except Exception as error:
            logger.exception("Error executing command")
            return {"status": "error", "message": str(error)}

    def get_addon_info(self) -> dict[str, object]:
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

    def stop(self) -> None:
        super().stop()
        self.execution.close()
        self.renders.close()
