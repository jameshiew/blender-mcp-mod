import math
from itertools import product

import bpy
from mathutils import Vector

from .geometry import evaluated_geometry


def _number(name, value, low, high):
    if value is not None and (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a number between {low} and {high}")


def _boolean(name, value):
    if value is not None and type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _choice(name, value, choices):
    if value is not None and (not isinstance(value, str) or value not in choices):
        raise ValueError(f"{name} must be one of {', '.join(choices)}")


def _targets(object_names=None, frame=None, viewport=None):
    if object_names is not None:
        if (
            not isinstance(object_names, list)
            or not object_names
            or any(not isinstance(name, str) or not name for name in object_names)
        ):
            raise ValueError(
                "object_names must be a non-empty list of exact object names"
            )
        objects = []
        for name in dict.fromkeys(object_names):
            obj = bpy.context.view_layer.objects.get(name)
            if obj is None:
                raise ValueError(f"Object not in the active view layer: {name}")
            if viewport is not None and not obj.visible_get(viewport=viewport):
                raise ValueError(f"Object is not visible in this viewport: {name}")
            objects.append(obj)
    else:
        objects = [
            obj
            for obj in bpy.context.view_layer.objects
            if obj.visible_get(viewport=viewport)
            and (frame == "ALL" or obj.select_get())
        ]
    if not objects:
        raise ValueError("No objects to frame")
    return objects


def _points(objects, padding):
    points = []
    for obj in objects:
        bounds = evaluated_geometry(obj)["world_bounding_box"]
        if bounds is None:
            points.append(obj.matrix_world.translation.copy())
        else:
            points.extend(Vector(point) for point in product(*zip(*bounds)))
    low = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center = (low + high) / 2
    return [center + (point - center) * padding for point in points], center


def camera_info(obj):
    data = obj.data
    return {
        "data_name": data.name,
        "projection": data.type,
        "lens": data.lens,
        "lens_unit": data.lens_unit,
        "angle_x": data.angle_x,
        "angle_y": data.angle_y,
        "ortho_scale": data.ortho_scale,
        "sensor_fit": data.sensor_fit,
        "sensor_width": data.sensor_width,
        "sensor_height": data.sensor_height,
        "shift_x": data.shift_x,
        "shift_y": data.shift_y,
        "clip_start": data.clip_start,
        "clip_end": data.clip_end,
        "is_active": bpy.context.scene.camera == obj,
        "dof": {
            "use_dof": data.dof.use_dof,
            "focus_object": data.dof.focus_object.name
            if data.dof.focus_object
            else None,
            "focus_distance": data.dof.focus_distance,
            "aperture_fstop": data.dof.aperture_fstop,
        },
    }


def set_camera(
    object_name,
    projection=None,
    lens=None,
    ortho_scale=None,
    shift_x=None,
    shift_y=None,
    clip_start=None,
    clip_end=None,
    object_names=None,
    padding=1.1,
    make_active=False,
):
    if not isinstance(object_name, str) or not object_name:
        raise ValueError("object_name must be a non-empty string")
    _choice("projection", projection, ("PERSP", "ORTHO"))
    for name, value, low, high in (
        ("lens", lens, 1, 5000),
        ("ortho_scale", ortho_scale, 0.000001, 1000000),
        ("shift_x", shift_x, -10, 10),
        ("shift_y", shift_y, -10, 10),
        ("clip_start", clip_start, 0.000001, 1000000),
        ("clip_end", clip_end, 0.000001, 1000000),
        ("padding", padding, 1, 10),
    ):
        _number(name, value, low, high)
    _boolean("make_active", make_active)
    obj = bpy.context.scene.objects.get(object_name)
    if obj is None or obj.type != "CAMERA":
        raise ValueError(f"Camera not found in the active scene: {object_name}")
    data = obj.data
    if not obj.is_editable or not data.is_editable:
        raise ValueError("Camera and camera data must be editable")
    if (clip_start if clip_start is not None else data.clip_start) >= (
        clip_end if clip_end is not None else data.clip_end
    ):
        raise ValueError("clip_start must be less than clip_end")
    settings = {
        name: value
        for name, value in {
            "type": projection,
            "lens": lens,
            "ortho_scale": ortho_scale,
            "shift_x": shift_x,
            "shift_y": shift_y,
            "clip_start": clip_start,
            "clip_end": clip_end,
        }.items()
        if value is not None
    }
    location = None
    framed = []
    if object_names is not None:
        if (projection or data.type) not in {"PERSP", "ORTHO"}:
            raise ValueError("Camera framing supports PERSP and ORTHO projections")
        if any(
            not constraint.mute and constraint.influence
            for constraint in obj.constraints
        ):
            raise ValueError("Mute camera constraints before framing")
        objects = _targets(object_names)
        if obj in objects:
            raise ValueError("A camera cannot frame itself")
        points, _ = _points(objects, padding)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        if not evaluated.is_evaluated:
            raise ValueError("Camera is not in the active view layer dependency graph")
        temporary = obj.copy()
        temporary.data = data.copy()
        temporary_data = temporary.data
        try:
            temporary.matrix_world = evaluated.matrix_world.copy()
            for name, value in settings.items():
                setattr(temporary_data, name, value)
            location, scale = temporary.camera_fit_coords(
                depsgraph, [coordinate for point in points for coordinate in point]
            )
            if temporary_data.type == "ORTHO":
                settings["ortho_scale"] = max(scale, 0.000001)
        finally:
            bpy.data.objects.remove(temporary)
            bpy.data.cameras.remove(temporary_data)
        framed = [target.name for target in objects]
    for name, value in settings.items():
        setattr(data, name, value)
    if location is not None:
        matrix = obj.matrix_world.copy()
        matrix.translation = location
        obj.matrix_world = matrix
    if make_active:
        bpy.context.scene.camera = obj
    bpy.context.view_layer.update()
    return {
        "object_name": obj.name,
        "camera": camera_info(obj),
        "matrix_world": [list(row) for row in obj.matrix_world],
        "framed_objects": framed,
    }


def viewport_context(viewport_index=0):
    if type(viewport_index) is not int or viewport_index < 0:
        raise ValueError("viewport_index must be a non-negative integer")
    if bpy.app.background:
        raise ValueError("Viewport controls require a Blender window")
    windows = list(bpy.context.window_manager.windows)
    if bpy.context.window in windows:
        windows.remove(bpy.context.window)
        windows.insert(0, bpy.context.window)
    viewports = []
    for window in windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    viewports.append((window, area, region))
    if viewport_index >= len(viewports):
        raise ValueError(
            f"Viewport {viewport_index} unavailable; found {len(viewports)} 3D viewports"
        )
    window, area, region = viewports[viewport_index]
    if area.spaces.active.region_quadviews:
        raise ValueError(
            "Quad view is not supported; switch this area to a single view"
        )
    return window, area, region


def viewport_info(area, viewport_index):
    space = area.spaces.active
    region = space.region_3d
    camera = space.camera if space.use_local_camera else bpy.context.scene.camera
    return {
        "viewport_index": viewport_index,
        "scene": bpy.context.scene.name,
        "view_layer": bpy.context.view_layer.name,
        "projection": region.view_perspective,
        "location": list(region.view_location),
        "rotation": list(region.view_rotation),
        "distance": region.view_distance,
        "camera_zoom": region.view_camera_zoom,
        "camera": camera.name if camera else None,
        "shading": space.shading.type,
        "overlays": space.overlay.show_overlays,
        "gizmos": space.show_gizmo,
    }


def set_viewport(
    viewport_index=0,
    view=None,
    projection=None,
    frame=None,
    object_names=None,
    padding=1.1,
    distance=None,
    camera_zoom=None,
    shading=None,
    overlays=None,
    gizmos=None,
):
    directions = {
        "FRONT": (0, -1, 0),
        "BACK": (0, 1, 0),
        "LEFT": (-1, 0, 0),
        "RIGHT": (1, 0, 0),
        "TOP": (0, 0, 1),
        "BOTTOM": (0, 0, -1),
        "ISO": (1, -1, 1),
    }
    _choice("view", view, (*directions, "CAMERA"))
    _choice("projection", projection, ("PERSP", "ORTHO"))
    _choice("frame", frame, ("ALL", "SELECTED"))
    _choice("shading", shading, ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED"))
    _number("padding", padding, 1, 10)
    _number("distance", distance, 0.000001, 1000000)
    _number("camera_zoom", camera_zoom, -30, 600)
    _boolean("overlays", overlays)
    _boolean("gizmos", gizmos)
    framing = frame is not None or object_names is not None
    if frame is not None and object_names is not None:
        raise ValueError("Use frame or object_names, not both")
    if framing and distance is not None:
        raise ValueError("Use framing or distance, not both")
    if view == "CAMERA" and (projection is not None or framing or distance is not None):
        raise ValueError(
            "Camera view cannot be combined with projection, framing, or distance"
        )
    window, area, region = viewport_context(viewport_index)
    with bpy.context.temp_override(window=window, area=area, region=region):
        space = area.spaces.active
        r3d = space.region_3d
        perspective = projection or (
            "CAMERA"
            if view == "CAMERA"
            else "PERSP"
            if view == "ISO"
            else "ORTHO"
            if view in directions
            else "PERSP"
            if framing and r3d.view_perspective == "CAMERA"
            else r3d.view_perspective
        )
        if perspective == "CAMERA":
            camera = space.camera if space.use_local_camera else window.scene.camera
            if camera is None and (view == "CAMERA" or camera_zoom is not None):
                raise ValueError("No camera for this viewport")
            if distance is not None:
                raise ValueError("Use camera_zoom for camera view display zoom")
        elif camera_zoom is not None:
            raise ValueError("camera_zoom requires camera view")
        objects = _targets(object_names, frame, space) if framing else []
        points, center = _points(objects, padding) if framing else (None, None)
        if view in directions:
            r3d.view_rotation = (-Vector(directions[view])).to_track_quat("-Z", "Y")
        r3d.view_perspective = perspective
        if framing:
            radius = max(max((point - center).length for point in points), 0.01)
            r3d.view_location = center
            r3d.view_distance = 1
            r3d.update()
            factor = max(abs(r3d.window_matrix[0][0]), abs(r3d.window_matrix[1][1]))
            r3d.view_distance = radius * (
                math.sqrt(1 + factor * factor) if perspective == "PERSP" else factor
            )
        if distance is not None:
            r3d.view_distance = distance
        if camera_zoom is not None:
            r3d.view_camera_zoom = camera_zoom
        if shading is not None:
            space.shading.type = shading
        if overlays is not None:
            space.overlay.show_overlays = overlays
        if gizmos is not None:
            space.show_gizmo = gizmos
        r3d.update()
        area.tag_redraw()
        return {
            **viewport_info(area, viewport_index),
            "framed_objects": [obj.name for obj in objects],
        }


def capture_viewport(max_size, filepath, format, viewport_index=0, camera_only=False):
    window, area, region = viewport_context(viewport_index)
    with bpy.context.temp_override(window=window, area=area, region=region):
        space = area.spaces.active
        r3d = space.region_3d
        r3d.update()
        scene = window.scene
        camera = scene.camera if camera_only else None
        if camera_only and (
            camera is None or camera.data.type not in {"PERSP", "ORTHO"}
        ):
            raise ValueError(
                "Camera capture requires an active PERSP or ORTHO scene camera"
            )
        if camera_only:
            render = scene.render
            source_width = render.resolution_x * render.pixel_aspect_x
            source_height = render.resolution_y * render.pixel_aspect_y
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated_camera = camera.evaluated_get(depsgraph)
            view_matrix = evaluated_camera.matrix_world.normalized().inverted()
            projection = evaluated_camera.calc_matrix_camera(
                depsgraph,
                x=render.resolution_x,
                y=render.resolution_y,
                scale_x=render.pixel_aspect_x,
                scale_y=render.pixel_aspect_y,
            )
        else:
            source_width, source_height = region.width, region.height
            view_matrix, projection = r3d.view_matrix, r3d.window_matrix
        scale = min(1, max_size / max(source_width, source_height))
        width = max(1, round(source_width * scale))
        height = max(1, round(source_height * scale))
        method = "offscreen"
        image = None
        try:
            try:
                import gpu
                import numpy as np

                offscreen = gpu.types.GPUOffScreen(width, height)
                try:
                    offscreen.draw_view3d(
                        scene,
                        window.view_layer,
                        space,
                        region,
                        view_matrix,
                        projection,
                        do_color_management=True,
                    )
                    buffer = offscreen.texture_color.read()
                    buffer.dimensions = width * height * 4
                    pixels = np.asarray(buffer, dtype=np.float32) / 255.0
                finally:
                    offscreen.free()
                image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
                image.pixels.foreach_set(pixels.ravel())
            except Exception as error:
                if image is not None:
                    bpy.data.images.remove(image)
                    image = None
                if camera_only:
                    raise RuntimeError(
                        f"Camera viewport capture failed: {error}"
                    ) from error
                print(
                    f"[BlenderMCP] offscreen capture failed ({error}); using window grab",
                    flush=True,
                )
                method = "window_grab"
                bpy.ops.screen.screenshot_area(filepath=filepath)
                image = bpy.data.images.load(filepath)
                source_width, source_height = image.size
                scale = min(1, max_size / max(source_width, source_height))
                width = max(1, round(source_width * scale))
                height = max(1, round(source_height * scale))
                image.scale(width, height)
            image.filepath_raw = filepath
            image.file_format = format.upper()
            image.save()
        finally:
            if image is not None:
                bpy.data.images.remove(image)
        return {
            "success": True,
            "width": width,
            "height": height,
            "filepath": filepath,
            "method": method,
            "capture_mode": "CAMERA" if camera_only else "VIEWPORT",
            "camera": camera.name if camera else None,
            "viewport": viewport_info(area, viewport_index),
        }
