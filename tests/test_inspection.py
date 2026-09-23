import importlib.util
import json
from types import SimpleNamespace

import pytest
from conftest import ROOT_ADDON
from extension_stub import _load_addon


@pytest.fixture
def inspection(monkeypatch):
    _, bpy = _load_addon(monkeypatch)
    bpy.types.ID = type("ID", (), {})
    spec = importlib.util.spec_from_file_location(
        "inspection_test", ROOT_ADDON.with_name("inspection.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, bpy


@pytest.mark.parametrize(
    "function, options",
    [
        ("material_info", {"material_name": ""}),
        ("material_info", {"material_name": "Glass", "offset": True}),
        ("material_info", {"material_name": "Glass", "limit": 101}),
        ("node_group_info", {"node_group_name": []}),
        ("modifier_info", {"object_name": "Cube", "modifier_name": ""}),
        ("animation_info", {"data_name": "Cube", "data_type": []}),
        ("animation_info", {"data_name": "Cube", "offset": -1}),
    ],
)
def test_validation_precedes_blender_data_access(inspection, function, options):
    module, bpy = inspection
    bpy.data = None
    with pytest.raises(ValueError):
        getattr(module, function)(**options)


def test_values_bound_arrays_strings_and_non_finite_numbers(inspection):
    module, _ = inspection
    values = module._value(list(range(100)))
    assert values == {"items": list(range(16)), "count": 100, "details_omitted": True}
    text = module._value("x" * 10000)
    assert text == {"value": "x" * 2048, "length": 10000, "details_omitted": True}
    assert module._value({"B", "A"}) == ["A", "B"]
    json.dumps(module._value([float("nan"), float("inf")]), allow_nan=False)


def test_pagination_only_describes_returned_entries(inspection):
    module, _ = inspection
    calls = []

    def describe(value):
        calls.append(value)
        return value * 2

    page = module._page(list(range(120)), 20, 10, describe)
    assert calls == list(range(20, 30))
    assert page["count"] == 120 and page["next_offset"] == 30 and page["has_more"]
    assert module._page([1], 2, 10)["items"] == []
    assert module._page([1], 2, 10)["next_offset"] is None


@pytest.mark.parametrize(
    "offset, more", [(0, True), (2, False), (3, False), (10, False)]
)
def test_page_availability_is_independent_of_omitted_details(inspection, offset, more):
    module, _ = inspection
    rows = [{"settings": {"omitted": ["unsupported"]}} for _ in range(3)]
    page = module._page(rows, offset, 2)
    assert page["has_more"] is more
    assert (page["next_offset"] is not None) is more
    assert page["details_omitted"] is (offset < 3)
    assert "truncated" not in page
    assert not module._page([1, 2, 3], offset, 2)["details_omitted"]


def test_fixed_caps_do_not_advertise_inaccessible_pages(inspection):
    module, _ = inspection
    page = module._limited(list(range(25)), 0, 20)
    assert page["count"] == 25 and len(page["items"]) == 20
    assert page["details_omitted"]
    assert not page["has_more"] and page["next_offset"] is None
    parent = module._page([{"children": page}], 0, 20)
    assert parent["details_omitted"] and not parent["has_more"]


def test_shared_action_slot_is_filtered_in_every_layer_and_strip(inspection):
    module, _ = inspection
    own = SimpleNamespace(handle=7, identifier="OBOwn", target_id_type="OBJECT")
    bags = [
        SimpleNamespace(slot_handle=8, fcurves=["unrelated"]),
        SimpleNamespace(slot_handle=7, fcurves=["owned"]),
    ]
    action = SimpleNamespace(
        name="Shared",
        id_type="ACTION",
        library=None,
        layers=[
            SimpleNamespace(name="A", strips=[SimpleNamespace(channelbags=bags)]),
            SimpleNamespace(
                name="B",
                strips=[
                    SimpleNamespace(channelbags=[]),
                    SimpleNamespace(channelbags=bags),
                ],
            ),
        ],
    )
    curves = list(module._action_curves(action, own))
    assert [curve for curve, _ in curves] == ["owned", "owned"]
    assert [context["layer_index"] for _, context in curves] == [0, 1]
    assert [context["action_strip_index"] for _, context in curves] == [0, 1]
    assert list(module._action_curves(action, None)) == []


def test_legacy_geometry_nodes_input_overrides(inspection):
    module, bpy = inspection
    bpy.types.Modifier = SimpleNamespace(bl_rna=SimpleNamespace(properties={}))
    socket = SimpleNamespace(
        item_type="SOCKET",
        in_out="INPUT",
        name="Height",
        identifier="Socket_2",
        socket_type="NodeSocketFloat",
        default_value=1,
    )

    class Modifier(dict):
        name = "Nodes"
        type = "NODES"
        show_viewport = show_render = show_in_editmode = True
        show_on_cage = False
        bl_rna = SimpleNamespace(properties=[])
        node_group = SimpleNamespace(
            name="Group",
            id_type="NODETREE",
            library=None,
            interface=SimpleNamespace(items_tree=[socket]),
        )

    mod = Modifier(
        Socket_2=4, Socket_2_use_attribute=True, Socket_2_attribute_name="height"
    )
    result = module._modifier_info(mod, 0)
    assert result["inputs"]["items"] == [
        {
            "name": "Height",
            "identifier": "Socket_2",
            "socket_type": "NodeSocketFloat",
            "value": 4,
            "use_attribute": True,
            "attribute_name": "height",
        }
    ]


def test_unsupported_settings_are_explicit_and_reads_do_not_write(inspection):
    module, _ = inspection
    props = [
        SimpleNamespace(identifier=name, type=kind, is_readonly=False)
        for name, kind in [
            ("count", "INT"),
            ("children", "COLLECTION"),
            ("broken", "STRING"),
        ]
    ]

    class Settings:
        bl_rna = SimpleNamespace(properties=props)
        count = 3
        children = ()

        @property
        def broken(self):
            raise RuntimeError("unavailable")

        def __setattr__(self, name, value):
            raise AssertionError("inspection wrote data")

    result = module._settings(Settings())
    assert result == {
        "values": {"count": 3},
        "omitted": ["children"],
        "errors": {"broken": "unavailable"},
        "details_omitted": True,
    }


def test_missing_owners_are_reported_by_name(inspection):
    module, bpy = inspection
    bpy.data = SimpleNamespace(materials={}, objects={}, node_groups={})
    for function, options in [
        (module.material_info, {"material_name": "Missing"}),
        (module.modifier_info, {"object_name": "Missing", "modifier_name": "Array"}),
        (module.animation_info, {"data_name": "Missing"}),
        (module.node_group_info, {"node_group_name": "Missing"}),
    ]:
        with pytest.raises(ValueError, match="not found: Missing"):
            function(**options)
