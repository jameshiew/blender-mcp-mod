from __future__ import annotations

import base64
import logging
import math
import os
import tempfile
from collections.abc import Sequence
from contextlib import AbstractContextManager
from itertools import product
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import bpy
from mathutils import Vector

from .context import current_scene, current_view_layer
from .geometry import evaluated_geometry

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import ImageTypeAllItems

logger = logging.getLogger(__name__)

_FLOAT_MAX = 3.4028234663852886e38
_PANORAMA_TYPES = (
    "EQUIRECTANGULAR",
    "EQUIANGULAR_CUBEMAP_FACE",
    "MIRRORBALL",
    "FISHEYE_EQUIDISTANT",
    "FISHEYE_EQUISOLID",
    "FISHEYE_LENS_POLYNOMIAL",
    "CENTRAL_CYLINDRICAL",
)
_AXIS_VIEW_ROTATIONS = {
    "FRONT": (math.sqrt(0.5), math.sqrt(0.5), 0, 0),
    "BACK": (0, 0, math.sqrt(0.5), math.sqrt(0.5)),
    "LEFT": (0.5, 0.5, -0.5, -0.5),
    "RIGHT": (0.5, 0.5, 0.5, 0.5),
    "TOP": (1, 0, 0, 0),
    "BOTTOM": (0, 1, 0, 0),
}


class CaptureOptions(TypedDict, total=False):
    viewport_index: int
    camera_only: bool


def _number(name: str, value: float | None, low: float, high: float) -> None:
    if value is not None and (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a number between {low} and {high}")


def _boolean(name: str, value: bool | None) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _choice(name: str, value: str | None, choices: Sequence[str]) -> None:
    if value is not None and (not isinstance(value, str) or value not in choices):
        raise ValueError(f"{name} must be one of {', '.join(choices)}")


def _targets(
    object_names: list[str] | None = None,
    frame: str | None = None,
    viewport: bpy.types.SpaceView3D | None = None,
) -> list[bpy.types.Object]:
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
            obj = current_view_layer().objects.get(name)
            if obj is None:
                raise ValueError(f"Object not in the active view layer: {name}")
            if viewport is not None and not obj.visible_get(viewport=viewport):
                raise ValueError(f"Object is not visible in this viewport: {name}")
            objects.append(obj)
    else:
        objects = [
            obj
            for obj in current_view_layer().objects
            if obj.visible_get(viewport=viewport)
            and (frame == "ALL" or obj.select_get())
        ]
    if not objects:
        raise ValueError("No objects to frame")
    return objects


def _points(
    objects: Sequence[bpy.types.Object], padding: float
) -> tuple[list[Vector], Vector]:
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


def camera_info(obj: bpy.types.Object) -> dict[str, object]:
    data = cast("bpy.types.Camera", obj.data)
    dof = data.dof
    if dof is None:
        raise ValueError("Camera has no depth-of-field settings")
    return {
        "data_name": data.name,
        "projection": data.type,
        "panorama_type": data.panorama_type,
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
        "is_active": current_scene().camera == obj,
        "dof": {
            "use_dof": dof.use_dof,
            "focus_object": dof.focus_object.name if dof.focus_object else None,
            "focus_subtarget": dof.focus_subtarget,
            "focus_distance": dof.focus_distance,
            "aperture_fstop": dof.aperture_fstop,
        },
    }


def set_camera(
    object_name: str,
    projection: Literal["PERSP", "ORTHO", "PANO"] | None = None,
    lens: float | None = None,
    ortho_scale: float | None = None,
    shift_x: float | None = None,
    shift_y: float | None = None,
    clip_start: float | None = None,
    clip_end: float | None = None,
    object_names: list[str] | None = None,
    padding: float = 1.1,
    make_active: bool = False,
    sensor_fit: Literal["AUTO", "HORIZONTAL", "VERTICAL"] | None = None,
    sensor_width: float | None = None,
    sensor_height: float | None = None,
    use_dof: bool | None = None,
    focus_object: str | None = None,
    focus_distance: float | None = None,
    aperture_fstop: float | None = None,
    panorama_type: str | None = None,
) -> dict[str, object]:
    if not isinstance(object_name, str) or not object_name:
        raise ValueError("object_name must be a non-empty string")
    _choice("projection", projection, ("PERSP", "ORTHO", "PANO"))
    _choice("sensor_fit", sensor_fit, ("AUTO", "HORIZONTAL", "VERTICAL"))
    _choice("panorama_type", panorama_type, _PANORAMA_TYPES)
    for name, value, low, high in (
        ("lens", lens, 1, 5000),
        ("ortho_scale", ortho_scale, 0.000001, 1000000),
        ("shift_x", shift_x, -10, 10),
        ("shift_y", shift_y, -10, 10),
        ("clip_start", clip_start, 0.000001, 1000000),
        ("clip_end", clip_end, 0.000001, 1000000),
        ("padding", padding, 1, 10),
        ("sensor_width", sensor_width, 1, _FLOAT_MAX),
        ("sensor_height", sensor_height, 1, _FLOAT_MAX),
        ("focus_distance", focus_distance, 0, _FLOAT_MAX),
        ("aperture_fstop", aperture_fstop, 0, _FLOAT_MAX),
    ):
        _number(name, value, low, high)
    _boolean("make_active", make_active)
    _boolean("use_dof", use_dof)
    if focus_object is not None and not isinstance(focus_object, str):
        raise ValueError(
            "focus_object must be an object name or an empty string to clear it"
        )
    obj = current_scene().objects.get(object_name)
    if obj is None or obj.type != "CAMERA":
        raise ValueError(f"Camera not found in the active scene: {object_name}")
    data = cast("bpy.types.Camera", obj.data)
    dof = data.dof
    if dof is None:
        raise ValueError("Camera has no depth-of-field settings")
    if not obj.is_editable or not data.is_editable:
        raise ValueError("Camera and camera data must be editable")
    if (clip_start if clip_start is not None else data.clip_start) >= (
        clip_end if clip_end is not None else data.clip_end
    ):
        raise ValueError("clip_start must be less than clip_end")
    if panorama_type is not None and (projection or data.type) != "PANO":
        raise ValueError("panorama_type requires a PANO camera")
    focus = None
    if focus_object:
        focus = current_scene().objects.get(focus_object)
        if focus is None:
            raise ValueError(
                f"Focus object not found in the active scene: {focus_object}"
            )
        if focus == obj:
            raise ValueError("A camera cannot use itself as its focus object")
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
            "sensor_fit": sensor_fit,
            "sensor_width": sensor_width,
            "sensor_height": sensor_height,
            "panorama_type": panorama_type,
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
    for name, value in {
        "use_dof": use_dof,
        "focus_distance": focus_distance,
        "aperture_fstop": aperture_fstop,
    }.items():
        if value is not None:
            setattr(dof, name, value)
    if focus_object is not None:
        dof.focus_object = focus
        dof.focus_subtarget = ""
    if location is not None:
        matrix = obj.matrix_world.copy()
        matrix.translation = location
        obj.matrix_world = matrix
    if make_active:
        current_scene().camera = obj
    current_view_layer().update()
    return {
        "object_name": obj.name,
        "camera": camera_info(obj),
        "matrix_world": [list(row) for row in obj.matrix_world],
        "framed_objects": framed,
    }


def viewport_context(
    viewport_index: int = 0,
) -> tuple[bpy.types.Window, bpy.types.Area, bpy.types.Region]:
    if type(viewport_index) is not int or viewport_index < 0:
        raise ValueError("viewport_index must be a non-negative integer")
    if bpy.app.background:
        raise ValueError("Viewport controls require a Blender window")
    manager = bpy.context.window_manager
    if manager is None:
        raise ValueError("Viewport controls require a Blender window manager")
    windows = list(manager.windows)
    if bpy.context.window in windows:
        windows.remove(bpy.context.window)
        windows.insert(0, bpy.context.window)
    viewports = []
    for window in windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
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
    if cast("bpy.types.SpaceView3D", area.spaces.active).region_quadviews:
        raise ValueError(
            "Quad view is not supported; switch this area to a single view"
        )
    return window, area, region


def viewport_info(area: bpy.types.Area, viewport_index: int) -> dict[str, object]:
    space = cast("bpy.types.SpaceView3D", area.spaces.active)
    region = space.region_3d
    if region is None:
        raise ValueError("Viewport has no 3D region")
    camera = space.camera if space.use_local_camera else current_scene().camera
    return {
        "viewport_index": viewport_index,
        "scene": current_scene().name,
        "view_layer": current_view_layer().name,
        "projection": region.view_perspective,
        "location": list(region.view_location[:]),
        "rotation": list(region.view_rotation[:]),
        "distance": region.view_distance,
        "camera_zoom": region.view_camera_zoom,
        "camera": camera.name if camera else None,
        "shading": space.shading.type,
        "overlays": space.overlay.show_overlays,
        "gizmos": space.show_gizmo,
    }


def set_viewport(
    viewport_index: int = 0,
    view: str | None = None,
    projection: Literal["PERSP", "ORTHO"] | None = None,
    frame: str | None = None,
    object_names: list[str] | None = None,
    padding: float = 1.1,
    distance: float | None = None,
    camera_zoom: float | None = None,
    shading: Literal["WIREFRAME", "SOLID", "MATERIAL", "RENDERED"] | None = None,
    overlays: bool | None = None,
    gizmos: bool | None = None,
) -> dict[str, object]:
    _choice("view", view, (*_AXIS_VIEW_ROTATIONS, "ISO", "CAMERA"))
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
    # Blender's stubs omit ContextTempOverride's context-manager methods.
    with cast(
        AbstractContextManager[object],
        bpy.context.temp_override(window=window, area=area, region=region),
    ):
        space = cast("bpy.types.SpaceView3D", area.spaces.active)
        r3d = space.region_3d
        if r3d is None:
            raise ValueError("Viewport has no 3D region")
        perspective = projection or (
            "CAMERA"
            if view == "CAMERA"
            else "PERSP"
            if view == "ISO"
            else "ORTHO"
            if view in _AXIS_VIEW_ROTATIONS
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
        if view in _AXIS_VIEW_ROTATIONS:
            r3d.view_rotation = _AXIS_VIEW_ROTATIONS[view]
        elif view == "ISO":
            r3d.view_rotation = Vector((-1, 1, -1)).to_track_quat("-Z", "Y")
        r3d.view_perspective = perspective
        if framing:
            points, center = _points(objects, padding)
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


def capture_viewport(
    max_size: int,
    filepath: str,
    format: str,
    viewport_index: int = 0,
    camera_only: bool = False,
) -> dict[str, object]:
    window, area, region = viewport_context(viewport_index)
    with cast(
        AbstractContextManager[object],
        bpy.context.temp_override(window=window, area=area, region=region),
    ):
        space = cast("bpy.types.SpaceView3D", area.spaces.active)
        r3d = space.region_3d
        if r3d is None:
            raise ValueError("Viewport has no 3D region")
        r3d.update()
        scene = window.scene
        camera = scene.camera if camera_only else None
        if camera_only and (
            camera is None
            or cast("bpy.types.Camera", camera.data).type not in {"PERSP", "ORTHO"}
        ):
            raise ValueError(
                "Camera capture requires an active PERSP or ORTHO scene camera"
            )
        if camera is not None:
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
                    buffer.dimensions = [width * height * 4]
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
            image.file_format = cast("ImageTypeAllItems", format.upper())
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


def get_viewport_screenshot(
    max_size: int = 800,
    filepath: str | None = None,
    format: str = "png",
    viewport_index: int = 0,
    camera_only: bool = False,
) -> dict[str, object]:
    if type(max_size) is not int or not 1 <= max_size <= 4096:
        return {"error": "max_size must be an integer between 1 and 4096"}
    if type(viewport_index) is not int or viewport_index < 0:
        return {"error": "viewport_index must be a non-negative integer"}
    if type(camera_only) is not bool:
        return {"error": "camera_only must be a boolean"}
    options: CaptureOptions = (
        {"viewport_index": viewport_index, "camera_only": camera_only}
        if viewport_index or camera_only
        else {}
    )
    if filepath:
        return _save_viewport_screenshot(max_size, filepath, format, **options)
    with tempfile.TemporaryDirectory(prefix="blender_mcp_") as directory:
        path = os.path.join(directory, "viewport.png")
        result = _save_viewport_screenshot(max_size, path, "png", **options)
        if result.get("success"):
            with open(path, "rb") as image:
                result["image_data"] = base64.b64encode(image.read()).decode("ascii")
            result["format"] = "png"
            result.pop("filepath", None)
        return result


def _save_viewport_screenshot(
    max_size: int = 800,
    filepath: str | None = None,
    format: str = "png",
    viewport_index: int = 0,
    camera_only: bool = False,
) -> dict[str, object]:
    try:
        if not filepath:
            return {"error": "No filepath provided"}
        return capture_viewport(max_size, filepath, format, viewport_index, camera_only)
    except Exception as error:
        logger.exception("Error saving viewport screenshot")
        return {"error": str(error)}
