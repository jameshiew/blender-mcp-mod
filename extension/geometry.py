from itertools import product

import bpy
from mathutils import Vector

_CONVERTIBLE_TYPES = {"CURVE", "SURFACE", "FONT", "META"}


def _mesh_geometry(obj):
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
                bounds = [list(point), list(point)]
            else:
                for axis in range(3):
                    bounds[0][axis] = min(bounds[0][axis], point[axis])
                    bounds[1][axis] = max(bounds[1][axis], point[axis])
        return counts, bounds
    finally:
        obj.to_mesh_clear()


def _include_bounds(bounds, local_bounds, matrix):
    if local_bounds is None:
        return bounds
    for corner in product(*zip(*local_bounds)):
        point = matrix @ Vector(corner)
        if bounds is None:
            bounds = [list(point), list(point)]
        else:
            for axis in range(3):
                bounds[0][axis] = min(bounds[0][axis], point[axis])
                bounds[1][axis] = max(bounds[1][axis], point[axis])
    return bounds


def _geometry_key(obj):
    # Instance object wrappers can change or reuse addresses during iteration.
    return (
        obj.original.as_pointer(),
        obj.type,
        obj.data.as_pointer() if obj.data is not None else None,
    )


def evaluated_geometry(obj):
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
    sources = {}
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
        original_pointer = source.original.as_pointer()
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
            original = source.original
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
        "frame": bpy.context.scene.frame_current,
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


def world_bounding_box(obj):
    if obj.type != "MESH":
        raise TypeError("Object must be a mesh")
    local_bbox_corners = [Vector(corner) for corner in obj.bound_box]
    world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]
    min_corner = Vector(map(min, zip(*world_bbox_corners)))
    max_corner = Vector(map(max, zip(*world_bbox_corners)))
    return [[*min_corner], [*max_corner]]
