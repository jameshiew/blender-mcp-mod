from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hunyuan_import_security import _load_addon


def test_clean_import_keeps_world_transform_when_removing_parent(monkeypatch):
    addon, bpy = _load_addon(monkeypatch)

    class Mesh:
        type = "MESH"
        name = "Mesh"
        data = SimpleNamespace(name="Mesh")

        def __init__(self):
            self.matrix_world = [2, 3, 4]

        @property
        def parent(self):
            return parent

        @parent.setter
        def parent(self, value):
            self.matrix_world = [0, 0, 0]

    class Parent:
        type = "EMPTY"

    mesh = Mesh()
    parent = Parent()
    parent.children = [mesh]
    bpy.data = SimpleNamespace(objects=set())
    bpy.context.view_layer = SimpleNamespace(update=lambda: None)
    bpy.ops.import_scene.gltf = lambda **kwargs: bpy.data.objects.update([mesh, parent])

    result = addon.BlenderMCPServer._clean_imported_glb("model.glb", "Chair")

    assert result is mesh
    assert mesh.matrix_world == [2, 3, 4]
    assert bpy.data.objects == {mesh}
    assert mesh.name == mesh.data.name == "Chair"


@pytest.mark.parametrize("provider", ["main_site", "fal_ai"])
@pytest.mark.parametrize("failure", [None, "download", "import"])
def test_rodin_import_removes_downloads(monkeypatch, tmp_path, provider, failure):
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    server._get_hyper3d_api_key = lambda: "test-key"
    monkeypatch.setattr(addon.tempfile, "tempdir", str(tmp_path))
    metadata = {
        "list": [{"name": "model.glb", "url": "https://example.com/model.glb"}],
        "model_mesh": {"url": "https://example.com/model.glb"},
    }

    class Response:
        def json(self):
            return metadata

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"glTF"
            if failure == "download":
                raise OSError("download failed")

    monkeypatch.setattr(
        addon.requests, "get", lambda *a, **kw: Response(), raising=False
    )
    monkeypatch.setattr(
        addon.requests, "post", lambda *a, **kw: Response(), raising=False
    )
    imported = []

    def import_model(filepath, mesh_name):
        assert Path(filepath).read_bytes() == b"glTF"
        imported.append(filepath)
        if failure == "import":
            raise RuntimeError("import failed")
        vector = SimpleNamespace(x=0, y=0, z=0)
        return SimpleNamespace(
            name=mesh_name,
            type="EMPTY",
            location=vector,
            rotation_euler=vector,
            scale=vector,
        )

    server._clean_imported_glb = import_model

    result = getattr(server, f"import_generated_asset_{provider}")("task", "Chair")

    assert result["succeed"] is (failure is None)
    if failure:
        assert f"{failure} failed" in result["error"]
    assert bool(imported) is (failure != "download")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fails", [False, True])
def test_local_hunyuan_reports_import_outcome(monkeypatch, tmp_path, fails):
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    server._get_hunyuan3d_api_url = lambda: "http://localhost:8081"
    scene = bpy.context.scene
    scene.blendermcp_hunyuan3d_octree_resolution = 256
    scene.blendermcp_hunyuan3d_num_inference_steps = 20
    scene.blendermcp_hunyuan3d_guidance_scale = 5.0
    scene.blendermcp_hunyuan3d_texture = True
    monkeypatch.setattr(addon.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        addon.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, content=b"glTF"),
        raising=False,
    )
    imported = []

    def import_model(filepath):
        assert Path(filepath).read_bytes() == b"glTF"
        imported.append(filepath)
        if fails:
            raise RuntimeError("import failed")

    bpy.ops.import_scene.gltf = import_model

    result = server.create_hunyuan_job_local_site(text_prompt="Chair")

    assert len(imported) == 1
    if fails:
        assert "import failed" in result["error"]
    else:
        assert result["status"] == "DONE"
    assert list(tmp_path.iterdir()) == []
