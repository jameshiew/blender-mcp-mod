import importlib.util
from types import SimpleNamespace
import sys
from contextlib import nullcontext

import pytest

from conftest import ROOT_ADDON
from extension_stub import _load_addon
from test_scene_info import Vector


@pytest.fixture
def view(monkeypatch):
    addon, bpy = _load_addon(monkeypatch)
    addon.mathutils.Vector = Vector
    monkeypatch.setitem(sys.modules, "blender_mcp_addon_test", addon)
    spec = importlib.util.spec_from_file_location(
        "blender_mcp_addon_test.view", ROOT_ADDON.with_name("view.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, bpy


@pytest.mark.parametrize(
    "options",
    [
        {"lens": True},
        {"lens": float("nan")},
        {"ortho_scale": 0},
        {"projection": "PANO"},
        {"projection": []},
        {"make_active": 1},
    ],
)
def test_camera_validates_before_context_access(view, options):
    module, bpy = view
    bpy.context = None
    with pytest.raises(ValueError):
        module.set_camera("Camera", **options)


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

    first, first_area, first_region = window()
    second, second_area, second_region = window()
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
