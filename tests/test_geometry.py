import importlib.util
from types import SimpleNamespace

import pytest
from conftest import ROOT_ADDON
from extension_stub import _load_addon
from test_scene_info import Matrix, Vector


@pytest.fixture
def geometry(monkeypatch):
    addon, bpy = _load_addon(monkeypatch)
    addon.mathutils.Vector = Vector
    spec = importlib.util.spec_from_file_location(
        "geometry_test", ROOT_ADDON.with_name("geometry.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, bpy


@pytest.mark.parametrize("failure", ["to_mesh", "vertices", None])
def test_temporary_mesh_is_released_on_success_and_failure(geometry, failure):
    module, _ = geometry
    cleared = []

    def to_mesh():
        if failure == "to_mesh":
            raise RuntimeError("conversion failed")
        if failure == "vertices":
            return SimpleNamespace()
        return SimpleNamespace(vertices=[], edges=[], polygons=[])

    obj = SimpleNamespace(
        type="MESH", to_mesh=to_mesh, to_mesh_clear=lambda: cleared.append(True)
    )
    if failure:
        with pytest.raises((RuntimeError, AttributeError)):
            module._mesh_geometry(obj)
    else:
        assert module._mesh_geometry(obj) == (
            {"vertices": 0, "edges": 0, "polygons": 0},
            None,
        )
    assert cleared == [True]


def test_repeated_instances_share_conversion_and_source_summary(geometry):
    module, bpy = geometry
    conversions = []
    matrix = Matrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    original = SimpleNamespace(
        name="Plants", instance_collection=None, as_pointer=lambda: 1
    )
    emitter = SimpleNamespace(
        type="EMPTY",
        is_evaluated=True,
        as_pointer=lambda: 1,
        matrix_world=matrix,
        original=original,
        data=None,
    )
    mesh = SimpleNamespace(
        vertices=[
            SimpleNamespace(co=Vector((-1, -2, -3))),
            SimpleNamespace(co=Vector((1, 2, 3))),
        ],
        edges=[],
        polygons=[],
    )

    def to_mesh():
        conversions.append(True)
        return mesh

    source = SimpleNamespace(
        type="MESH",
        data=SimpleNamespace(as_pointer=lambda: 2),
        to_mesh=to_mesh,
        to_mesh_clear=lambda: None,
        original=SimpleNamespace(
            name="Plant",
            as_pointer=lambda: 3,
            library=SimpleNamespace(filepath="/assets/plants.blend"),
        ),
    )
    graph = SimpleNamespace(
        mode="VIEWPORT",
        object_instances=(
            SimpleNamespace(
                is_instance=True,
                parent=emitter,
                object=source,
                matrix_world=matrix,
                persistent_id=(index, 2147483647),
            )
            for index in range(1000)
        ),
    )
    original.evaluated_get = lambda _: emitter
    bpy.context.evaluated_depsgraph_get = lambda: graph
    bpy.context.scene.frame_current = 10
    result = module.evaluated_geometry(original)
    assert len(conversions) == 1
    assert result["mesh_including_instances"] == {
        "vertices": 2000,
        "edges": 0,
        "polygons": 0,
    }
    assert result["instances"] == {
        "count": 1000,
        "sources": [
            {
                "name": "Plant",
                "library": "/assets/plants.blend",
                "type": "MESH",
                "count": 1000,
                "mesh": {"vertices": 2, "edges": 0, "polygons": 0},
            }
        ],
    }
    assert result["dimensions"] == [2, 4, 6]


def test_geometry_cache_distinguishes_prototypes_and_original_objects(geometry):
    module, _ = geometry
    original = SimpleNamespace(as_pointer=lambda: 1)
    first = SimpleNamespace(
        original=original, type="MESH", data=SimpleNamespace(as_pointer=lambda: 2)
    )
    wrapper = SimpleNamespace(original=original, type="MESH", data=first.data)
    prototype = SimpleNamespace(
        original=original, type="MESH", data=SimpleNamespace(as_pointer=lambda: 3)
    )
    other = SimpleNamespace(
        original=SimpleNamespace(as_pointer=lambda: 4), type="MESH", data=first.data
    )
    assert module._geometry_key(first) == module._geometry_key(wrapper)
    assert module._geometry_key(first) != module._geometry_key(prototype)
    assert module._geometry_key(first) != module._geometry_key(other)
