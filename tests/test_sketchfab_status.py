import base64
import io
import tempfile
import types
import zipfile
from pathlib import Path

import pytest


def sketchfab_service(addon, enabled=True, api_key="saved-key"):
    module = addon.sketchfab
    bpy = module.bpy
    bpy.context.scene.blendermcp_use_sketchfab = enabled
    service = module.SketchfabService(
        lambda: api_key, lambda: bpy.context.scene.blendermcp_use_sketchfab
    )
    return service, module, bpy


def response(data=None, *, status=200, content=b"", headers=None):
    return types.SimpleNamespace(
        status_code=status,
        content=content,
        headers=headers or {},
        json=lambda: data,
    )


def archive_bytes(entries):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return content.getvalue()


@pytest.mark.parametrize("data", [[], "unexpected", 42])
def test_search_rejects_non_object_json(addon, monkeypatch, data):
    service, module, _ = sketchfab_service(addon)
    monkeypatch.setattr(
        module.requests, "get", lambda *_args, **_kwargs: response(data), raising=False
    )
    result = service.search_sketchfab_models("chair")
    assert "expected an object" in result["error"]


@pytest.mark.parametrize(
    "thumbnail",
    [{"url": 42}, {"width": "wide", "url": "https://images.example/preview.png"}],
)
def test_preview_validates_thumbnail_fields(addon, monkeypatch, thumbnail):
    service, module, _ = sketchfab_service(addon)
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        if len(requests) == 1:
            return response({"thumbnails": {"images": [thumbnail]}})
        return response(content=b"image")

    monkeypatch.setattr(module.requests, "get", get, raising=False)
    result = service.get_sketchfab_model_preview("chair")
    if isinstance(thumbnail["url"], str):
        assert result["success"]
        assert len(requests) == 2
    else:
        assert result == {"error": "Thumbnail URL not found"}
        assert len(requests) == 1


def local_download(monkeypatch, module, tmp_path, content):
    temporary_directory = tempfile.TemporaryDirectory
    monkeypatch.setattr(
        module.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: temporary_directory(dir=tmp_path, **kwargs),
    )
    responses = iter(
        [
            response({"gltf": {"url": "https://models.example/model.zip"}}),
            response(content=content),
        ]
    )
    monkeypatch.setattr(
        module.requests, "get", lambda *_args, **_kwargs: next(responses), raising=False
    )


def test_disabled_sketchfab_does_not_report_a_saved_key_as_ready(addon, monkeypatch):
    service, module, bpy = sketchfab_service(addon, enabled=False)

    def request_should_not_run(*_args, **_kwargs):
        raise AssertionError("must not validate a disabled integration")

    monkeypatch.setattr(module.requests, "get", request_should_not_run, raising=False)
    status = service.get_sketchfab_status()
    assert status["enabled"] is False
    assert "currently disabled" in status["message"]

    server = addon.server.BlenderMCPServer()
    assert server.execute_command({"type": "search_sketchfab_models"}) == {
        "status": "error",
        "message": "Unknown command type: search_sketchfab_models",
    }
    bpy.context.scene.blendermcp_use_sketchfab = True
    server.sketchfab._api_key = lambda: ""
    assert server.execute_command(
        {"type": "search_sketchfab_models", "params": {"query": "chair"}}
    ) == {
        "status": "success",
        "result": {"error": "Sketchfab API key is not configured"},
    }


def test_enabled_sketchfab_reports_a_valid_key_as_ready(addon, monkeypatch):
    service, module, _ = sketchfab_service(addon)
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *_args, **_kwargs: response({"username": "artist"}),
        raising=False,
    )
    assert service.get_sketchfab_status() == {
        "enabled": True,
        "message": "Sketchfab integration is enabled and ready to use. Logged in as: artist",
    }


@pytest.mark.parametrize("status", [401, 403, 500])
def test_rejected_key_does_not_report_ready(addon, monkeypatch, status):
    service, module, _ = sketchfab_service(addon)
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *_args, **_kwargs: response(status=status),
        raising=False,
    )
    assert service.get_sketchfab_status() == {
        "enabled": False,
        "message": f"Sketchfab API key seems invalid. Status code: {status}",
    }


@pytest.mark.parametrize(
    "command,arguments",
    [
        ("search_sketchfab_models", {"query": "chair"}),
        ("get_sketchfab_model_preview", {"uid": "model"}),
        ("download_sketchfab_model", {"uid": "model"}),
    ],
)
def test_commands_require_a_key_and_respect_blender_online_access(
    addon, command, arguments
):
    service, _, bpy = sketchfab_service(addon, api_key="")
    operation = getattr(service, command)
    assert operation(**arguments) == {"error": "Sketchfab API key is not configured"}
    service._api_key = lambda: "saved-key"
    bpy.app.online_access = False
    assert "Online access is disabled" in operation(**arguments)["error"]


def test_preview_selects_medium_thumbnail_without_sending_credentials_to_asset_host(
    addon,
    monkeypatch,
):
    service, module, _ = sketchfab_service(addon)
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        if len(requests) == 1:
            return response(
                {
                    "name": "Chair",
                    "user": {"username": "artist"},
                    "thumbnails": {
                        "images": [
                            {"width": 128, "url": "https://images.example/small.jpg"},
                            {
                                "width": 640,
                                "height": 480,
                                "url": "https://images.example/medium.png",
                            },
                        ]
                    },
                }
            )
        return response(content=b"image", headers={"Content-Type": "image/png"})

    monkeypatch.setattr(module.requests, "get", get, raising=False)
    assert service.get_sketchfab_model_preview("chair") == {
        "success": True,
        "image_data": base64.b64encode(b"image").decode("ascii"),
        "format": "png",
        "model_name": "Chair",
        "author": "artist",
        "uid": "chair",
        "thumbnail_width": 640,
        "thumbnail_height": 480,
    }
    assert requests[0][1]["headers"] == {"Authorization": "Token saved-key"}
    assert requests[1] == ("https://images.example/medium.png", {"timeout": 30})


@pytest.mark.parametrize("entry", ["../escape.gltf", "..\\escape.gltf", "/escape.gltf"])
def test_download_rejects_archive_traversal_and_removes_temporary_files(
    addon, monkeypatch, tmp_path, entry
):
    service, module, _ = sketchfab_service(addon)
    local_download(monkeypatch, module, tmp_path, archive_bytes({entry: "{}"}))
    result = service.download_sketchfab_model("model")
    assert "Security issue: Zip contains files" in result["error"]
    assert list(tmp_path.iterdir()) == []


def test_failed_import_removes_temporary_files(addon, monkeypatch, tmp_path):
    service, module, bpy = sketchfab_service(addon)
    local_download(monkeypatch, module, tmp_path, archive_bytes({"scene.gltf": "{}"}))

    def fail_import(filepath, **options):
        assert Path(filepath).is_file()
        raise RuntimeError("import failed")

    bpy.data.objects = []
    bpy.ops.import_scene.gltf = fail_import
    assert service.download_sketchfab_model("model") == {
        "error": "Failed to download model: import failed"
    }
    assert list(tmp_path.iterdir()) == []


def test_download_normalizes_only_roots_and_recalculates_combined_bounds(
    addon, monkeypatch, tmp_path
):
    service, module, bpy = sketchfab_service(addon)
    local_download(monkeypatch, module, tmp_path, archive_bytes({"scene.gltf": "{}"}))
    root = types.SimpleNamespace(
        name="Root",
        type="EMPTY",
        parent=None,
        scale=(1, 1, 1),
        location=(0, 0, 0),
        children=[],
        session_uid=1,
    )
    mesh = types.SimpleNamespace(
        name="Mesh",
        type="MESH",
        parent=root,
        scale=(1, 1, 1),
        children=[],
        session_uid=2,
    )
    root.children.append(mesh)
    bpy.context.selected_objects = [root, mesh]
    bpy.data.objects = []

    def import_model(**kwargs):
        assert kwargs["import_pack_images"]
        bpy.data.objects.extend([root, mesh])
        return {"FINISHED"}

    bpy.ops.import_scene.gltf = import_model
    updates = []
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: updates.append(True))
    monkeypatch.setattr(
        module,
        "world_bounding_box",
        lambda _mesh: [
            [0, 0, 0],
            [4 * root.scale[0], 2 * root.scale[1], root.scale[2]],
        ],
    )
    assert service.download_sketchfab_model("model", normalize_size=True) == {
        "success": True,
        "message": "Model imported successfully",
        "imported_objects": ["Root", "Mesh"],
        "world_bounding_box": [[0, 0, 0], [1, 0.5, 0.25]],
        "dimensions": [1, 0.5, 0.25],
        "scale_applied": 0.25,
        "normalized": True,
    }
    assert root.scale == (0.25, 0.25, 0.25)
    assert mesh.scale == (1, 1, 1)
    assert updates == [True, True]
    assert list(tmp_path.iterdir()) == []


def test_cancelled_import_does_not_report_or_normalize_existing_selection(addon):
    module, bpy = addon.sketchfab, addon.sketchfab.bpy
    existing = types.SimpleNamespace(session_uid=7, scale=(1, 1, 1))
    bpy.data.objects = [existing]
    bpy.context.selected_objects = [existing]
    bpy.ops.import_scene.gltf = lambda **kwargs: {"CANCELLED"}
    with pytest.raises(module.SketchfabError, match="cancelled"):
        module._import_archive(archive_bytes({"scene.gltf": "{}"}), True, 1)
    assert existing.scale == (1, 1, 1)


def test_import_bounds_ignore_empty_meshes(addon, monkeypatch):
    module = addon.sketchfab
    bounds = [[-1, -2, -3], [1, 2, 3]]
    monkeypatch.setattr(module, "world_bounding_box", lambda obj: obj)
    assert module._mesh_bounds([None, bounds, None]) == bounds
    assert module._mesh_bounds([None]) is None


@pytest.mark.parametrize("size", [0, -1, float("nan"), float("inf"), True, "large"])
def test_invalid_normalization_size_is_rejected_before_import(addon, size):
    with pytest.raises(addon.sketchfab.SketchfabError, match="positive finite"):
        addon.sketchfab._import_archive(b"", True, size)


def test_nested_archive_and_multiple_root_offsets_are_normalized(addon, monkeypatch):
    module, bpy = addon.sketchfab, addon.sketchfab.bpy
    bpy.data.objects = [types.SimpleNamespace(session_uid=1)]
    existing = bpy.data.objects[0]
    roots = [
        types.SimpleNamespace(
            name=f"Root{index}",
            type="MESH",
            parent=None,
            scale=(1, 1, 1),
            location=(x, 0, 0),
            children=[],
            session_uid=index + 2,
        )
        for index, x in enumerate((-10, 10))
    ]

    def import_model(filepath, **options):
        assert Path(filepath).name == "scene.GLTF"
        bpy.data.objects.extend(roots)
        bpy.context.selected_objects = [existing]
        return {"FINISHED"}

    bpy.ops.import_scene.gltf = import_model
    monkeypatch.setattr(
        module,
        "world_bounding_box",
        lambda obj: [
            [value - scale for value, scale in zip(obj.location, obj.scale)],
            [value + scale for value, scale in zip(obj.location, obj.scale)],
        ],
    )
    result = module._import_archive(
        archive_bytes({"model/scene.GLTF": "{}"}),
        True,
        2,
    )
    assert result["imported_objects"] == ["Root0", "Root1"]
    assert result["dimensions"][0] == 2
    assert roots[0].location[0] == pytest.approx(-10 / 11)
    assert roots[1].location[0] == pytest.approx(10 / 11)
