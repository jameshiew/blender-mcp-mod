from itertools import product
from types import SimpleNamespace

import pytest

from extension_stub import _load_addon


class Vector(list):
    @property
    def x(self):
        return self[0]

    @property
    def y(self):
        return self[1]

    @property
    def z(self):
        return self[2]


class Matrix(list):
    @property
    def translation(self):
        return Vector(row[3] for row in self[:3])

    def __matmul__(self, vector):
        return Vector(
            sum(row[index] * vector[index] for index in range(3)) + row[3]
            for row in self[:3]
        )


def scene_object(
    name,
    object_type="MESH",
    location=(0, 0, 0),
    selected=False,
    visible=True,
    parent=None,
    world_location=None,
):
    world = location if world_location is None else world_location
    return SimpleNamespace(
        name=name,
        type=object_type,
        location=Vector(location),
        rotation_euler=Vector((0.1, 0.2, 0.3)),
        rotation_mode="XYZ",
        scale=Vector((1, 1, 1)),
        dimensions=Vector((2, 2, 2)),
        matrix_world=Matrix(
            [
                [1, 0, 0, world[0]],
                [0, 1, 0, world[1]],
                [0, 0, 1, world[2]],
                [0, 0, 0, 1],
            ]
        ),
        bound_box=list(product((-1, 1), repeat=3)),
        select_get=lambda: selected,
        visible_get=lambda: visible,
        parent=parent,
        users_collection=[SimpleNamespace(name="Collection")],
        modifiers=[],
        material_slots=[SimpleNamespace(material=SimpleNamespace(name="Material"))],
        data=SimpleNamespace(vertices=range(8), edges=range(12), polygons=range(6)),
    )


def scene_server(monkeypatch, objects=()):
    addon, bpy = _load_addon(monkeypatch)
    bpy.context.scene = SimpleNamespace(
        name="Scene",
        objects=list(objects),
        camera=None,
        frame_current=17,
        render=SimpleNamespace(engine="CYCLES"),
        unit_settings=SimpleNamespace(system="METRIC", scale_length=0.01),
        blendermcp_use_sketchfab=False,
    )
    bpy.context.active_object = None
    bpy.context.mode = "OBJECT"
    bpy.data = SimpleNamespace(
        materials=["Material"],
        objects={obj.name: obj for obj in objects},
        filepath="/tmp/scene.blend",
    )
    bpy.types.Object = SimpleNamespace(
        bl_rna=SimpleNamespace(
            properties={
                "type": SimpleNamespace(
                    enum_items=[
                        SimpleNamespace(identifier=name)
                        for name in ("MESH", "EMPTY", "CAMERA", "LIGHT", "NEW_TYPE")
                    ]
                ),
            }
        ),
    )
    addon.mathutils.Vector = Vector
    return addon.BlenderMCPServer(), bpy


def test_scene_pagination_reaches_every_object_in_name_order(monkeypatch):
    names = [f"Object.{index:03}" for index in range(25)]
    server, _ = scene_server(
        monkeypatch, [scene_object(name) for name in reversed(names)]
    )
    first = server.get_scene_info()
    assert [obj["name"] for obj in first["objects"]] == names[:20]
    assert first["object_count"] == first["matching_objects"] == 25
    assert first["returned_count"] == first["limit"] == 20
    assert first["offset"] == 0
    assert first["next_offset"] == 20
    assert first["truncated"] is True

    last = server.get_scene_info(offset=first["next_offset"])
    assert [obj["name"] for obj in last["objects"]] == names[20:]
    assert last["returned_count"] == 5
    assert last["matching_objects"] == 25
    assert last["next_offset"] is None
    assert last["truncated"] is False

    past_end = server.get_scene_info(offset=100)
    assert past_end["objects"] == []
    assert past_end["returned_count"] == 0
    assert past_end["matching_objects"] == 25
    assert past_end["offset"] == 100
    assert past_end["next_offset"] is None
    assert past_end["truncated"] is False


def test_filters_apply_before_pagination_and_keep_scene_count(monkeypatch):
    server, _ = scene_server(
        monkeypatch,
        [
            scene_object("Lamp.Shade.B", selected=True),
            scene_object("Lamp.Light", object_type="LIGHT", selected=True),
            scene_object("Lamp.Shade.A", selected=True, visible=False),
            scene_object("Lamp.Base"),
            scene_object("Table", selected=True),
        ],
    )
    options = dict(name_filter="lAmP", object_type="MESH", selected_only=True, limit=1)
    first = server.get_scene_info(**options)
    assert [obj["name"] for obj in first["objects"]] == ["Lamp.Shade.A"]
    assert first["object_count"] == 5
    assert first["matching_objects"] == 2
    assert first["next_offset"] == 1
    assert first["objects"][0]["selected"] is True
    assert first["objects"][0]["visible"] is False
    last = server.get_scene_info(offset=first["next_offset"], **options)
    assert [obj["name"] for obj in last["objects"]] == ["Lamp.Shade.B"]
    assert last["next_offset"] is None
    missing = server.get_scene_info(name_filter="missing")
    assert missing["object_count"] == 5
    assert missing["matching_objects"] == missing["returned_count"] == 0
    assert missing["objects"] == []
    assert missing["next_offset"] is None


def test_scene_reports_context_and_keeps_existing_location_precision(monkeypatch):
    obj = scene_object("Cube", location=(1.234, -2.345, 3.456), selected=True)
    camera = scene_object("Camera", object_type="CAMERA")
    server, bpy = scene_server(monkeypatch, [obj, camera])
    bpy.context.active_object = obj
    bpy.context.mode = "EDIT_MESH"
    bpy.context.scene.camera = camera
    result = server.get_scene_info(name_filter="Cube")
    assert result["name"] == "Scene"
    assert result["materials_count"] == 1
    assert result["active_object"] == "Cube"
    assert result["mode"] == "EDIT_MESH"
    assert result["camera"] == "Camera"
    assert result["frame"] == 17
    assert result["render_engine"] == "CYCLES"
    assert result["unit_settings"] == {"system": "METRIC", "scale_length": 0.01}
    assert result["filepath"] == "/tmp/scene.blend"
    assert result["objects"] == [
        {
            "name": "Cube",
            "type": "MESH",
            "location": [1.23, -2.35, 3.46],
            "dimensions": [2, 2, 2],
            "selected": True,
            "visible": True,
        }
    ]


def test_empty_unsaved_scene_has_nullable_context(monkeypatch):
    server, bpy = scene_server(monkeypatch)
    bpy.data.filepath = ""
    result = server.get_scene_info()
    assert result["object_count"] == result["matching_objects"] == 0
    assert result["objects"] == []
    assert result["active_object"] is None
    assert result["camera"] is None
    assert result["filepath"] == ""
    assert result["next_offset"] is None
    assert result["truncated"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"offset": -1},
        {"offset": True},
        {"offset": 0.5},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": "20"},
        {"name_filter": []},
        {"object_type": []},
        {"object_type": "mesh"},
        {"object_type": ""},
        {"object_type": "UNKNOWN"},
        {"selected_only": 1},
    ],
)
def test_scene_rejects_invalid_parameters_before_reading_scene(monkeypatch, options):
    server, bpy = scene_server(monkeypatch)
    bpy.context = None
    bpy.data = None
    result = server.get_scene_info(**options)
    assert "error" in result
    assert "NoneType" not in result["error"]


def test_object_type_filter_uses_running_blender_enum(monkeypatch):
    server, _ = scene_server(monkeypatch, [scene_object("Future", "NEW_TYPE")])
    result = server.get_scene_info(object_type="NEW_TYPE")
    assert [obj["name"] for obj in result["objects"]] == ["Future"]


def test_parented_object_distinguishes_local_and_world_transforms(monkeypatch):
    parent = scene_object("Parent", object_type="EMPTY", location=(10, 20, 30))
    child = scene_object(
        "Child",
        location=(1, 2, 3),
        world_location=(11, 22, 33),
        parent=parent,
        selected=True,
    )
    child.modifiers = [
        SimpleNamespace(
            name="Subdivision",
            type="SUBSURF",
            show_viewport=False,
            show_render=True,
        )
    ]
    server, _ = scene_server(monkeypatch, [parent, child])
    result = server.get_object_info("Child")
    assert result["location"] == [1, 2, 3]
    assert result["rotation"] == [0.1, 0.2, 0.3]
    assert result["scale"] == [1, 1, 1]
    assert result["dimensions"] == [2, 2, 2]
    assert result["rotation_mode"] == "XYZ"
    assert result["world_location"] == [11, 22, 33]
    assert result["matrix_world"] == [
        [1, 0, 0, 11],
        [0, 1, 0, 22],
        [0, 0, 1, 33],
        [0, 0, 0, 1],
    ]
    assert result["world_bounding_box"] == [[10, 21, 32], [12, 23, 34]]
    assert result["parent"] == "Parent"
    assert result["collections"] == ["Collection"]
    assert result["modifiers"] == [
        {
            "name": "Subdivision",
            "type": "SUBSURF",
            "show_viewport": False,
            "show_render": True,
        }
    ]
    assert result["selected"] is True
    assert result["materials"] == ["Material"]
    assert result["mesh"] == {"vertices": 8, "edges": 12, "polygons": 6}
    parent_info = server.get_object_info("Parent")
    assert parent_info["parent"] is None
    assert parent_info["modifiers"] == []
    assert "world_bounding_box" not in parent_info
    assert "mesh" not in parent_info


@pytest.mark.parametrize("name", ["", None, 42, []])
def test_object_rejects_invalid_names_before_data_access(monkeypatch, name):
    server, bpy = scene_server(monkeypatch)
    bpy.data = None
    with pytest.raises(ValueError, match="name must be a non-empty string"):
        server.get_object_info(name)


def test_object_reports_missing_name(monkeypatch):
    server, _ = scene_server(monkeypatch)
    with pytest.raises(ValueError, match="Object not found: Missing"):
        server.get_object_info("Missing")
