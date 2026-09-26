import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_scene_info import Vector


@pytest.fixture
def view(addon, monkeypatch):
    bpy = addon.server.bpy
    module = addon.view
    monkeypatch.setattr(module, "Vector", Vector)
    monkeypatch.setattr(addon.geometry, "Vector", Vector)
    return module, bpy


@pytest.mark.parametrize(
    "options",
    [
        {"lens": True},
        {"lens": float("nan")},
        {"ortho_scale": 0},
        {"projection": "UNKNOWN"},
        {"projection": []},
        {"make_active": 1},
        {"sensor_fit": "VERTICALS"},
        {"sensor_width": 0.5},
        {"sensor_height": float("inf")},
        {"use_dof": 1},
        {"focus_object": []},
        {"focus_distance": -1},
        {"aperture_fstop": float("nan")},
        {"panorama_type": "UNKNOWN"},
    ],
)
def test_camera_validates_before_context_access(view, options):
    module, bpy = view
    bpy.context = None
    with pytest.raises(ValueError):
        module.set_camera("Camera", **options)


def test_camera_changes_record_an_undo_step(view):
    module, bpy = view
    dof = SimpleNamespace(
        use_dof=False,
        focus_object=None,
        focus_subtarget="",
        focus_distance=10.0,
        aperture_fstop=2.8,
    )
    data = SimpleNamespace(
        name="Camera",
        type="PERSP",
        is_editable=True,
        dof=dof,
        clip_start=0.1,
        clip_end=100.0,
        **dict.fromkeys(
            (
                "panorama_type",
                "lens",
                "lens_unit",
                "angle_x",
                "angle_y",
                "ortho_scale",
                "sensor_fit",
                "sensor_width",
                "sensor_height",
                "shift_x",
                "shift_y",
            )
        ),
    )
    camera = SimpleNamespace(
        name="Camera", type="CAMERA", data=data, is_editable=True, matrix_world=[]
    )
    bpy.context.scene.objects = {"Camera": camera}
    bpy.context.scene.camera = None
    with pytest.raises(ValueError, match="Camera not found"):
        module.set_camera("Missing", lens=50)
    assert bpy.ops.ed.undo_steps == []
    assert module.set_camera("Camera", lens=50)["camera"]["lens"] == 50
    assert bpy.ops.ed.undo_steps == ["MCP: Set Camera"]


@pytest.mark.parametrize(
    "options",
    [
        {"view": "UNKNOWN"},
        {"projection": []},
        {"shading": "preview"},
        {"distance": float("inf")},
        {"distance": True},
        {"camera_zoom": -31},
        {"overlays": 1},
        {"frame": "ALL", "object_names": ["Cube"]},
        {"view": "CAMERA", "projection": "ORTHO"},
        {"frame": "ALL", "distance": 2},
    ],
)
def test_viewport_validates_before_context_access(view, options):
    module, bpy = view
    bpy.context = None
    with pytest.raises(ValueError):
        module.set_viewport(**options)


def test_viewport_uses_requested_window_and_reports_missing_or_quad_view(view):
    module, bpy = view

    def window():
        region = SimpleNamespace(type="WINDOW")
        area = SimpleNamespace(
            type="VIEW_3D",
            regions=[region],
            spaces=SimpleNamespace(active=SimpleNamespace(region_quadviews=[])),
        )
        return SimpleNamespace(screen=SimpleNamespace(areas=[area])), area, region

    first, first_area, _first_region = window()
    second, _second_area, _second_region = window()
    bpy.context.window = second
    bpy.context.window_manager = SimpleNamespace(windows=[first, second])
    first.name = "First"
    second.name = "Second"
    assert module.viewport_context(0)[0] is second
    assert module.viewport_context(1)[0] is first
    with pytest.raises(ValueError, match="found 2"):
        module.viewport_context(2)
    first_area.spaces.active.region_quadviews = [object()]
    with pytest.raises(ValueError, match="Quad view"):
        module.viewport_context(1)
    bpy.app.background = True
    with pytest.raises(ValueError, match="require a Blender window"):
        module.viewport_context(0)


@pytest.mark.parametrize(
    ("name", "right", "up"),
    [
        ("FRONT", (1, 0, 0), (0, 0, 1)),
        ("BACK", (-1, 0, 0), (0, 0, 1)),
        ("LEFT", (0, -1, 0), (0, 0, 1)),
        ("RIGHT", (0, 1, 0), (0, 0, 1)),
        ("TOP", (1, 0, 0), (0, 1, 0)),
        ("BOTTOM", (1, 0, 0), (0, -1, 0)),
    ],
)
def test_axis_views_match_blender_numpad_orientation(
    view, monkeypatch, name, right, up
):
    module, bpy = view
    region = SimpleNamespace(
        view_rotation=(0, 0, 0, 1),
        view_perspective="PERSP",
        view_location=(0, 0, 0),
        view_distance=10,
        view_camera_zoom=0,
        update=lambda: None,
    )
    space = SimpleNamespace(
        region_3d=region,
        camera=None,
        use_local_camera=False,
        shading=SimpleNamespace(type="SOLID"),
        overlay=SimpleNamespace(show_overlays=True),
        show_gizmo=True,
    )
    area = SimpleNamespace(
        spaces=SimpleNamespace(active=space), tag_redraw=lambda: None
    )
    monkeypatch.setattr(module, "viewport_context", lambda _: (None, area, None))
    bpy.context.temp_override = lambda **_kw: nullcontext()
    bpy.context.scene.name = "Scene"
    bpy.context.scene.camera = None
    bpy.context.view_layer.name = "ViewLayer"
    result = module.set_viewport(view=name)
    assert result["projection"] == "ORTHO"
    w, x, y, z = result["rotation"]
    assert (
        1 - 2 * (y * y + z * z),
        2 * (x * y + w * z),
        2 * (x * z - w * y),
    ) == pytest.approx(right)
    assert (
        2 * (x * y - w * z),
        1 - 2 * (x * x + z * z),
        2 * (y * z + w * x),
    ) == pytest.approx(up)


def test_camera_capture_does_not_fall_back_to_unrelated_window_image(view, monkeypatch):
    module, bpy = view
    matrix = SimpleNamespace(normalized=lambda: SimpleNamespace(inverted=lambda: None))
    camera = SimpleNamespace(
        data=SimpleNamespace(type="PERSP"),
        matrix_world=matrix,
        calc_matrix_camera=lambda *_a, **_kw: None,
    )
    camera.evaluated_get = lambda _: camera
    render = SimpleNamespace(
        resolution_x=100, resolution_y=100, pixel_aspect_x=1, pixel_aspect_y=1
    )
    window = SimpleNamespace(
        scene=SimpleNamespace(camera=camera, render=render), view_layer=None
    )
    area = SimpleNamespace(
        spaces=SimpleNamespace(
            active=SimpleNamespace(region_3d=SimpleNamespace(update=lambda: None))
        )
    )
    monkeypatch.setattr(module, "viewport_context", lambda _: (window, area, None))
    bpy.context.temp_override = lambda **_kw: nullcontext()
    bpy.context.evaluated_depsgraph_get = lambda: None

    def fail(*_a, **_kw):
        raise RuntimeError("GPU unavailable")

    monkeypatch.setitem(
        sys.modules, "gpu", SimpleNamespace(types=SimpleNamespace(GPUOffScreen=fail))
    )
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    with pytest.raises(
        RuntimeError, match="Camera viewport capture failed: GPU unavailable"
    ):
        module.capture_viewport(100, "/unused.png", "png", camera_only=True)
