from __future__ import annotations

import logging
from typing import cast

import bpy

from .context import current_scene
from .geometry import world_bounding_box

logger = logging.getLogger(__name__)


def get_scene_info(
    offset: int = 0,
    limit: int = 20,
    name_filter: str | None = None,
    object_type: str | None = None,
    selected_only: bool = False,
) -> dict[str, object]:
    """Get information about the current Blender scene"""
    try:
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if name_filter is not None and not isinstance(name_filter, str):
            raise ValueError("name_filter must be a string or null")
        if object_type is not None and (
            not isinstance(object_type, str) or not object_type.isupper()
        ):
            raise ValueError(
                "object_type must be an uppercase Blender object type or null"
            )
        if type(selected_only) is not bool:
            raise ValueError("selected_only must be a boolean")
        if object_type is not None and object_type not in {
            item.identifier
            for item in cast(
                "bpy.types.EnumProperty", bpy.types.Object.bl_rna.properties["type"]
            ).enum_items
        }:
            raise ValueError(f"Unsupported object_type: {object_type}")

        scene = current_scene()
        name_filter = name_filter.casefold() if name_filter is not None else None
        matching_objects = sorted(
            (
                obj
                for obj in scene.objects
                if (name_filter is None or name_filter in obj.name.casefold())
                and (object_type is None or obj.type == object_type)
                and (not selected_only or obj.select_get())
            ),
            key=lambda obj: obj.name,
        )
        page = matching_objects[offset : offset + limit]
        next_offset = offset + len(page)
        if next_offset >= len(matching_objects):
            next_offset = None
        active_object = bpy.context.active_object
        objects: list[dict[str, object]] = []
        scene_info: dict[str, object] = {
            "name": scene.name,
            "object_count": len(scene.objects),
            "objects": objects,
            "materials_count": len(bpy.data.materials),
            "matching_objects": len(matching_objects),
            "returned_count": len(page),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "has_more": next_offset is not None,
            "details_omitted": False,
            "active_object": active_object.name if active_object else None,
            "mode": bpy.context.mode,
            "camera": scene.camera.name if scene.camera else None,
            "frame": scene.frame_current,
            "render_engine": scene.render.engine,
            "unit_settings": {
                "system": scene.unit_settings.system,
                "scale_length": scene.unit_settings.scale_length,
            },
            "filepath": bpy.data.filepath,
        }

        for obj in page:
            obj_info: dict[str, object] = {
                "name": obj.name,
                "type": obj.type,
                "location": [
                    round(float(obj.location.x), 2),
                    round(float(obj.location.y), 2),
                    round(float(obj.location.z), 2),
                ],
                "dimensions": [float(value) for value in obj.dimensions],
                "selected": obj.select_get(),
                "visible": obj.visible_get(),
            }
            objects.append(obj_info)

        return scene_info
    except Exception as e:
        logger.exception("Error in get_scene_info")
        return {"error": str(e)}


def get_object_info(
    name: str, evaluated: bool = False, details: bool = False
) -> dict[str, object]:
    """Get detailed information about a specific object"""
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    if type(evaluated) is not bool:
        raise ValueError("evaluated must be a boolean")
    if type(details) is not bool:
        raise ValueError("details must be a boolean")
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object not found: {name}")

    materials: list[str] = []
    obj_info: dict[str, object] = {
        "name": obj.name,
        "type": obj.type,
        "location": [obj.location.x, obj.location.y, obj.location.z],
        "rotation": [
            obj.rotation_euler.x,
            obj.rotation_euler.y,
            obj.rotation_euler.z,
        ],
        "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
        "dimensions": [float(value) for value in obj.dimensions],
        "rotation_mode": obj.rotation_mode,
        "matrix_world": [list(row) for row in obj.matrix_world],
        "world_location": list(obj.matrix_world.translation),
        "parent": obj.parent.name if obj.parent else None,
        "collections": [collection.name for collection in obj.users_collection],
        "modifiers": [
            {
                "name": modifier.name,
                "type": modifier.type,
                "show_viewport": modifier.show_viewport,
                "show_render": modifier.show_render,
            }
            for modifier in obj.modifiers
        ],
        "selected": obj.select_get(),
        "visible": obj.visible_get(),
        "materials": materials,
    }

    if obj.type == "MESH":
        bounding_box = world_bounding_box(obj)
        obj_info["world_bounding_box"] = bounding_box

    for slot in obj.material_slots:
        if slot.material:
            materials.append(slot.material.name)

    if obj.type == "MESH" and obj.data:
        mesh = cast("bpy.types.Mesh", obj.data)
        obj_info["mesh"] = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
        }

    if obj.type == "CAMERA" and obj.data:
        from .view import camera_info

        obj_info["camera"] = camera_info(obj)

    if evaluated:
        from .geometry import evaluated_geometry

        obj_info["evaluated"] = evaluated_geometry(obj)

    if details:
        from .inspection import object_details

        obj_info["details"] = object_details(obj)

    return obj_info
