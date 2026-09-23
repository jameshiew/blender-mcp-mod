from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING, TypedDict, cast

import bpy
from mathutils import Vector

from .context import current_scene

if TYPE_CHECKING:
    from mathutils import Matrix

Bounds = list[list[float]]


class InstanceSource(TypedDict):
    name: str
    library: str | None
    type: str
    count: int
    mesh: dict[str, int] | None


class Instances(TypedDict):
    count: int
    sources: list[InstanceSource]


class EvaluatedGeometry(TypedDict):
    frame: int
    depsgraph_mode: str
    mesh: dict[str, int] | None
    mesh_including_instances: dict[str, int]
    world_bounding_box: Bounds | None
    dimensions: list[float] | None
    instance_collection: str | None
    instances: Instances


_CONVERTIBLE_TYPES = {"CURVE", "SURFACE", "FONT", "META"}


def _mesh_geometry(
    obj: bpy.types.Object,
) -> tuple[dict[str, int] | None, Bounds | None]:
    if obj.type != "MESH" and obj.type not in _CONVERTIBLE_TYPES:
        return None, None
    try:
        mesh = obj.to_mesh()
        if mesh is None:
            return None, None
        counts = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
        }
        bounds = None
        for vertex in mesh.vertices:
            point = vertex.co
            if bounds is None:
                bounds = [list(point[:]), list(point[:])]
            else:
                for axis in range(3):
                    bounds[0][axis] = min(bounds[0][axis], point[axis])
                    bounds[1][axis] = max(bounds[1][axis], point[axis])
        return counts, bounds
    finally:
        obj.to_mesh_clear()


def _include_bounds(
    bounds: Bounds | None, local_bounds: Bounds | None, matrix: Matrix
) -> Bounds | None:
    if local_bounds is None:
        return bounds
    for corner in product(*zip(*local_bounds)):
        point = matrix @ Vector(corner)
        if bounds is None:
            bounds = [list(point[:]), list(point[:])]
        else:
            for axis in range(3):
                bounds[0][axis] = min(bounds[0][axis], point[axis])
                bounds[1][axis] = max(bounds[1][axis], point[axis])
    return bounds


def _geometry_key(obj: bpy.types.Object) -> tuple[int, str, int | None]:
    # Instance object wrappers can change or reuse addresses during iteration.
    return (
        cast("bpy.types.Object", obj.original).as_pointer(),
        obj.type,
        obj.data.as_pointer() if obj.data is not None else None,
    )


def evaluated_geometry(obj: bpy.types.Object) -> EvaluatedGeometry:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    if not evaluated.is_evaluated:
        raise ValueError(
            f"Object is not in the active view layer dependency graph: {obj.name}"
        )
    mesh, local_bounds = _mesh_geometry(evaluated)
    bounds = _include_bounds(None, local_bounds, evaluated.matrix_world)
    total = (
        dict(mesh) if mesh is not None else {"vertices": 0, "edges": 0, "polygons": 0}
    )
    cache = {_geometry_key(evaluated): (mesh, local_bounds)}
    sources: dict[tuple[int, str, int | None], InstanceSource] = {}
    instance_count = 0
    converted_paths = set()
    if evaluated.type in _CONVERTIBLE_TYPES and mesh is not None:
        converted_paths.add((obj.as_pointer(), ()))
    for instance in depsgraph.object_instances:
        if (
            not instance.is_instance
            or instance.parent is None
            or instance.parent.original != obj
        ):
            continue
        source = instance.object
        if source is None:
            continue
        original_pointer = cast("bpy.types.Object", source.original).as_pointer()
        path = tuple(index for index in instance.persistent_id if index != 2147483647)
        # Blender also emits a converted object's own mesh as component zero.
        if (
            source.type == "MESH"
            and path
            and path[0] == 0
            and (original_pointer, path[1:]) in converted_paths
        ):
            continue
        key = _geometry_key(source)
        if key not in cache:
            cache[key] = _mesh_geometry(source)
        instance_mesh, instance_bounds = cache[key]
        if source.type in _CONVERTIBLE_TYPES and instance_mesh is not None:
            converted_paths.add((original_pointer, path))
        if key not in sources:
            original = cast("bpy.types.Object", source.original)
            sources[key] = {
                "name": original.name,
                "library": original.library.filepath if original.library else None,
                "type": source.type,
                "count": 0,
                "mesh": instance_mesh,
            }
        sources[key]["count"] += 1
        instance_count += 1
        if instance_mesh is not None:
            for field in total:
                total[field] += instance_mesh[field]
        bounds = _include_bounds(bounds, instance_bounds, instance.matrix_world)
    return {
        "frame": current_scene().frame_current,
        "depsgraph_mode": depsgraph.mode,
        "mesh": mesh,
        "mesh_including_instances": total,
        "world_bounding_box": bounds,
        "dimensions": [high - low for low, high in zip(*bounds)]
        if bounds is not None
        else None,
        "instance_collection": obj.instance_collection.name
        if obj.instance_collection is not None
        else None,
        "instances": {
            "count": instance_count,
            "sources": sorted(
                sources.values(),
                key=lambda source: (source["name"], source["library"] or ""),
            ),
        },
    }


def world_bounding_box(obj: bpy.types.Object) -> Bounds:
    if obj.type != "MESH":
        raise TypeError("Object must be a mesh")
    local_bbox_corners = [Vector(corner) for corner in obj.bound_box]
    world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]
    min_corner = Vector(tuple(map(min, zip(*world_bbox_corners))))
    max_corner = Vector(tuple(map(max, zip(*world_bbox_corners))))
    return [[*min_corner], [*max_corner]]
