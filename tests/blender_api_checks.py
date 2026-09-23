import json

import bpy


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
    assert set(bpy.data.objects) == before
    return {"members_checked": count, "version": bpy.app.version_string}
