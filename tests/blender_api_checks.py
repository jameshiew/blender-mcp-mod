import json

import bpy


def _socket(sockets, identifier, socket_type=None):
    return next(
        item
        for item in sockets
        if item["identifier"] == identifier and socket_type in {None, item["type"]}
    )


def _node_types():
    for name in dir(bpy.types):
        rna = getattr(getattr(bpy.types, name), "bl_rna", None)
        base = rna.base if rna is not None else None
        while base is not None and base.identifier != "Node":
            base = base.base
        if base is not None:
            yield name


def check_nodes(api):
    scene = bpy.context.scene
    assert scene is not None
    state = (scene.users, len(bpy.data.node_groups), bpy.data.is_dirty)
    glare = api(identifier="CompositorNodeGlare")
    assert glare["node_members_omitted"] > 50
    assert {"bl_icon", "location", "input_template"}.isdisjoint(
        entry["identifier"] for entry in glare["entries"]
    )
    node = glare["node"]
    assert node["tree_types"] == ["CompositorNodeTree"]
    glare_type = _socket(node["inputs"], "Type")
    assert glare_type["type"] == "NodeSocketMenu"
    assert {"Bloom", "Fog Glow", "Streaks"} <= set(glare_type["menu_items"])
    assert glare_type["default_value"] in glare_type["menu_items"]
    size = _socket(node["inputs"], "Size")
    assert "enabled_when" not in size
    [condition] = size["used_when"]
    assert condition["input"] == "Type"
    assert "Fog Glow" in condition["values"]
    assert "Streaks" not in condition["values"]
    assert _socket(node["inputs"], "Highlights Threshold")["name"] == "Threshold"
    assert _socket(node["outputs"], "Glare")["type"] == "NodeSocketColor"

    random = api(identifier="FunctionNodeRandomValue")
    assert [entry["identifier"] for entry in random["entries"]] == ["data_type"]
    random = random["node"]
    assert _socket(random["inputs"], "Min", "NodeSocketFloat")["enabled_when"] == [
        {"property": "data_type", "values": ["FLOAT"]}
    ]
    assert _socket(random["variant_inputs"], "Min", "NodeSocketInt")[
        "enabled_when"
    ] == [{"property": "data_type", "values": ["INT"]}]
    assert _socket(random["variant_inputs"], "Probability")["enabled_when"] == [
        {"property": "data_type", "values": ["BOOLEAN"]}
    ]
    assert _socket(random["variant_outputs"], "Value", "NodeSocketBool")

    noise = api(identifier="ShaderNodeTexNoise")
    assert "noise_dimensions" in {entry["identifier"] for entry in noise["entries"]}
    factor = noise["node"]["outputs"][0]
    assert (factor["identifier"], factor["name"]) == ("Fac", "Factor")
    w = _socket(noise["node"]["inputs"], "W")
    assert w["enabled"] is False
    [condition] = w["enabled_when"]
    assert condition["property"] == "noise_dimensions"
    assert {"1D", "4D"} <= set(condition["values"])

    mix = api(identifier="ShaderNodeMix")["node"]
    color = _socket(mix["inputs"], "A_Color")
    assert color["index"] == 6
    assert color["enabled_when"] == [{"property": "data_type", "values": ["RGBA"]}]

    coat = api(identifier="ShaderNodeBsdfPrincipled", query="coat")
    assert coat["entries"] == []
    assert coat["node"]["inputs"]
    assert all("coat" in item["name"].casefold() for item in coat["node"]["inputs"])

    assert "node" not in api(identifier="ShaderNodeMath", offset=1, limit=1)
    base = api(identifier="Node", query="bl_icon")
    assert "node" not in base
    assert base["entries"][0]["identifier"] == "bl_icon"
    assert api(identifier="ShaderNode")["node"]["tree_types"] == []

    names = sorted(_node_types())
    for name in names:
        info = api(identifier=name, limit=1)
        json.dumps(info, allow_nan=False)
        assert "error" not in info["node"], (name, info["node"])
    assert (scene.users, len(bpy.data.node_groups), bpy.data.is_dirty) == state
    return len(names)


def run_checks(api):
    before = set(bpy.data.objects)
    lens = api(identifier="Camera", query="lens")
    assert all("lens" in item["identifier"] for item in lens["entries"])
    prop = next(item for item in lens["entries"] if item["identifier"] == "lens")
    assert prop["type"] == "FLOAT"
    assert prop["minimum"] == 1
    assert prop["default"] == 50
    assert prop["animatable"] is True
    projection = api(identifier="Camera", query="type")["entries"]
    prop = next(item for item in projection if item["identifier"] == "type")
    assert {item["identifier"] for item in prop["enum_items"]} == {
        "PERSP",
        "ORTHO",
        "PANO",
        "CUSTOM",
    }
    assert prop["enum_items_static_only"] is True
    transform = api(identifier="Object", query="matrix_world")["entries"][0]
    assert transform["array_length"] == 16
    assert transform["array_dimensions"][:2] == [4, 4]
    function = api(identifier="Object", query="to_mesh")["entries"][0]
    assert function["kind"] == "FUNCTION"
    assert any(parameter["output"] for parameter in function["parameters"])
    assert any(
        parameter["identifier"] == "depsgraph" for parameter in function["parameters"]
    )
    pointer = api(identifier="Object", query="data")["entries"]
    assert (
        next(item for item in pointer if item["identifier"] == "data")["fixed_type"]
        == "ID"
    )
    operator = api(identifier="mesh.primitive_cube_add", kind="OPERATOR")
    assert isinstance(operator["poll"], bool)
    assert any(item["identifier"] == "size" for item in operator["entries"])
    count = 0
    for identifier in (
        "Camera",
        "CameraDOFSettings",
        "Object",
        "NodesModifier",
        "Scene",
        "RenderSettings",
        "NodeTree",
        "Mesh",
        "Curves",
        "PointCloud",
        "ShaderNodeMix",
        "CompositorNodeGlare",
    ):
        offset = 0
        identifiers = []
        while True:
            page = api(identifier=identifier, offset=offset, limit=17)
            json.dumps(page, allow_nan=False)
            identifiers.extend(item["identifier"] for item in page["entries"])
            if not page["has_more"]:
                assert page["next_offset"] is None
                assert len(identifiers) == page["total"]
                assert identifiers == sorted(set(identifiers))
                count += len(identifiers)
                break
            offset = page["next_offset"]
    for identifier, kind in (
        ("NoSuchType", "TYPE"),
        ("mesh.no_such_operator", "OPERATOR"),
        ("__dict__", "TYPE"),
        ("bpy.data.objects[0]", "TYPE"),
    ):
        try:
            api(identifier=identifier, kind=kind)
        except ValueError:
            pass
        else:
            raise AssertionError(identifier)
    node_types = check_nodes(api)
    assert set(bpy.data.objects) == before
    return {
        "members_checked": count,
        "node_types_checked": node_types,
        "version": bpy.app.version_string,
    }
