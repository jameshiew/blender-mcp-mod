import base64
from pathlib import Path

from addon_stub import _load_addon, _scene


def test_inline_screenshot_cleans_up_its_temporary_file(monkeypatch):
    addon = _load_addon(monkeypatch, _scene())
    server = addon.BlenderMCPServer()
    paths = []

    def save(max_size, filepath, format):
        assert max_size == 1000
        assert format == "png"
        path = Path(filepath)
        paths.append(path)
        path.write_bytes(b"png-data")
        return {"success": True, "filepath": filepath, "width": 1000, "height": 500}

    monkeypatch.setattr(server, "_save_viewport_screenshot", save)
    result = server.get_viewport_screenshot(max_size=1000)
    assert result["format"] == "png"
    assert base64.b64decode(result["image_data"]) == b"png-data"
    assert "filepath" not in result
    assert not paths[0].parent.exists()


def test_inline_screenshot_cleans_up_after_failure(monkeypatch):
    addon = _load_addon(monkeypatch, _scene())
    server = addon.BlenderMCPServer()
    paths = []

    def save(max_size, filepath, format):
        paths.append(Path(filepath))
        Path(filepath).write_bytes(b"incomplete")
        return {"error": "No viewport"}

    monkeypatch.setattr(server, "_save_viewport_screenshot", save)
    assert server.get_viewport_screenshot() == {"error": "No viewport"}
    assert not paths[0].parent.exists()


def test_explicit_screenshot_path_remains_compatible(monkeypatch, tmp_path):
    addon = _load_addon(monkeypatch, _scene())
    server = addon.BlenderMCPServer()
    expected = {"success": True, "filepath": str(tmp_path / "image.png")}
    monkeypatch.setattr(
        server, "_save_viewport_screenshot", lambda size, path, format: expected
    )
    assert server.get_viewport_screenshot(filepath=expected["filepath"]) == expected
    assert "error" in server.get_viewport_screenshot(max_size=0)
