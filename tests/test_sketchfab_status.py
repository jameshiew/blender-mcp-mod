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

    def fail_import(filepath):
        assert Path(filepath).is_file()
        raise RuntimeError("import failed")

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
        name="Root", type="EMPTY", parent=None, scale=(1, 1, 1), children=[]
    )
    mesh = types.SimpleNamespace(
        name="Mesh", type="MESH", parent=root, scale=(1, 1, 1), children=[]
    )
    root.children.append(mesh)
    bpy.context.selected_objects = [root, mesh]
    bpy.ops.import_scene.gltf = lambda **_kwargs: None
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
    assert updates == [True]
    assert list(tmp_path.iterdir()) == []
