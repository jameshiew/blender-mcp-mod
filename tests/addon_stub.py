import importlib.util
import sys
import types
from typing import Any

from conftest import BLENDER_VERSION, BLENDER_VERSION_MIN, ROOT_ADDON

loaded_packages = set()


class StubModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)


def _install_bpy_stubs(monkeypatch, scene=None, selected_objects=()):
    if scene is None:
        scene = _scene()
    bpy = StubModule("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        selected_objects=list(selected_objects),
        view_layer=types.SimpleNamespace(update=lambda: None),
        mode="OBJECT",
        preferences=types.SimpleNamespace(addons={}),
    )
    bpy.data = types.SimpleNamespace(filepath="", is_saved=False, is_dirty=False)
    bpy.ops = types.SimpleNamespace(
        import_scene=types.SimpleNamespace(gltf=_unexpected_import),
        wm=types.SimpleNamespace(obj_import=_unexpected_import),
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    props = StubModule("bpy.props")
    for name in (
        "BoolProperty",
        "EnumProperty",
        "FloatProperty",
        "IntProperty",
        "StringProperty",
    ):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = StubModule("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.load_pre = []
    handlers.undo_pre = []
    handlers.redo_pre = []
    handlers.exit_pre = []
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = StubModule("bpy.app")
    app.version = BLENDER_VERSION
    app.version_string = BLENDER_VERSION_MIN
    app.background = False
    app.binary_path = "/test/blender"
    app.online_access = True
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    mathutils = StubModule("mathutils")
    mathutils.Vector = tuple
    monkeypatch.setitem(sys.modules, "mathutils", mathutils)

    requests = StubModule("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    return bpy


def load_addon_package(monkeypatch, path=ROOT_ADDON, name="blender_mcp_test"):
    loaded_packages.add(name)
    parts = name.split(".")
    for index in range(1, len(parts)):
        parent_name = ".".join(parts[:index])
        if parent_name not in sys.modules:
            parent = types.ModuleType(parent_name)
            parent.__path__ = []
            monkeypatch.setitem(sys.modules, parent_name, parent)
    for module_name in list(sys.modules):
        if module_name == name or module_name.startswith(name + "."):
            monkeypatch.delitem(sys.modules, module_name)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    addon = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, addon)
    spec.loader.exec_module(addon)
    return addon


def _load_addon(monkeypatch, scene=None, selected_objects=()):
    _install_bpy_stubs(monkeypatch, scene, selected_objects)
    return load_addon_package(monkeypatch)


def _unexpected_import(**_kwargs):
    raise AssertionError("unexpected model import")


def _scene():
    return types.SimpleNamespace(
        blendermcp_use_sketchfab=False,
    )
