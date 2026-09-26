import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from addon_stub import _install_bpy_stubs, load_addon_package
from conftest import (
    BLENDER_VERSION,
    BLENDER_VERSION_MIN,
    PROTOCOL_VERSION,
    RELEASE_TUPLE,
    RELEASE_VERSION,
    REPO_ROOT,
    ROOT_ADDON,
    blender_environment,
)


def test_package_metadata_and_wheels(addon_package):
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}
    project = next(
        package
        for package in packages.values()
        if package.get("source") == {"virtual": "."}
    )
    pending = [
        dependency["name"] for dependency in project["dev-dependencies"]["addon"]
    ]
    expected = {}
    while pending:
        name = pending.pop()
        if name in expected:
            continue
        package = packages[name]
        wheel = next(
            wheel
            for wheel in package["wheels"]
            if wheel["url"].endswith("-py3-none-any.whl")
        )
        expected[name] = ("wheels/" + wheel["url"].rsplit("/", 1)[1], wheel["hash"])
        pending.extend(
            dependency["name"] for dependency in package.get("dependencies", [])
        )
    with zipfile.ZipFile(addon_package) as archive:
        assert archive.testzip() is None
        manifest = tomllib.loads(archive.read("blender_manifest.toml").decode())
        assert manifest["version"] == RELEASE_VERSION
        assert manifest["blender_version_min"] == BLENDER_VERSION_MIN
        assert manifest["license"] == ["SPDX:MIT"]
        assert set(manifest["permissions"]) == {"network", "files"}
        assert json.loads(archive.read("protocol.json"))["version"] == PROTOCOL_VERSION
        assert archive.read("LICENSE") == (REPO_ROOT / "LICENSE").read_bytes()
        sources = {
            path.relative_to(ROOT_ADDON.parent).as_posix(): path.read_bytes()
            for path in ROOT_ADDON.parent.rglob("*.py")
            if not any(
                part.startswith(".") or part in {"__pycache__", "wheels"}
                for part in path.relative_to(ROOT_ADDON.parent).parts
            )
        }
        for name, source in sources.items():
            assert archive.read(name) == source
        for removed in (
            "telemetry",
            "trajectory",
            "UserEditRecorder",
            "uuid.getnode",
            "drain_human_activity",
            "polyhaven",
            "hyper3d",
            "polypizza",
            "hunyuan",
            "bit.ly",
        ):
            assert all(removed not in source.decode() for source in sources.values())
        assert set(archive.namelist()) == {
            "blender_manifest.toml",
            "protocol.json",
            "LICENSE",
        } | sources.keys() | {name for name, _ in expected.values()}
        assert archive.namelist() == sorted(archive.namelist())
        assert set(manifest["wheels"]) == {"./" + name for name, _ in expected.values()}
        for name, checksum in expected.values():
            assert (
                "sha256:" + hashlib.sha256(archive.read(name)).hexdigest() == checksum
            )


def test_installed_namespace_preferences_and_handshake(unpacked_addon, monkeypatch):
    bpy = _install_bpy_stubs(monkeypatch, SimpleNamespace())
    name = "bl_ext.custom_repository.blender_mcp"
    addon = load_addon_package(monkeypatch, unpacked_addon / "__init__.py", name)
    preferences = object()
    bpy.context.preferences = SimpleNamespace(
        addons={name: SimpleNamespace(preferences=preferences)}
    )
    assert addon.ui.BLENDERMCP_AddonPreferences.bl_idname == name
    assert addon.preferences.get_preferences() is preferences
    info = addon.server.BlenderMCPServer().get_addon_info()
    assert info["addon_build_version"] == RELEASE_VERSION
    assert info["addon_version"] == RELEASE_TUPLE
    assert info["protocol_version"] == PROTOCOL_VERSION
    assert not hasattr(addon, "bl_info")


def test_running_extension_keeps_loaded_version(unpacked_addon, tmp_path, monkeypatch):
    _install_bpy_stubs(monkeypatch, SimpleNamespace())
    directory = tmp_path / "extension"
    shutil.copytree(unpacked_addon, directory)
    addon = load_addon_package(
        monkeypatch, directory / "__init__.py", "bl_ext.test.blender_mcp"
    )
    addon.metadata.addon_metadata()
    manifest = directory / "blender_manifest.toml"
    manifest.write_text(manifest.read_text().replace(RELEASE_VERSION, "99.0.0+mod"))
    (directory / "protocol.json").write_text('{"version":999}')
    info = addon.server.BlenderMCPServer().get_addon_info()
    assert info["addon_build_version"] == RELEASE_VERSION
    assert info["protocol_version"] == PROTOCOL_VERSION


def test_http_respects_online_access(monkeypatch):
    bpy = _install_bpy_stubs(monkeypatch)
    addon = load_addon_package(monkeypatch)
    calls = []
    monkeypatch.setattr(
        addon.sketchfab.requests,
        "get",
        lambda *a, **kw: calls.append((a, kw)),
        raising=False,
    )
    request = addon.sketchfab._http_get
    bpy.app.online_access = False
    with pytest.raises(RuntimeError, match="Online access is disabled"):
        request("https://example.invalid")
    assert not calls
    bpy.app.online_access = True
    request("https://example.invalid", timeout=30)
    assert len(calls) == 1
    bpy.app.online_access = False
    with pytest.raises(RuntimeError):
        request("https://example.invalid")
    assert len(calls) == 1


def test_provider_requests_use_online_access_guard():
    tree = ast.parse((ROOT_ADDON.parent / "sketchfab.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_http_get",
            "_http_post",
        }:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                assert not (
                    isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "requests"
                    and child.func.attr in {"get", "post", "request", "Session"}
                )


@pytest.mark.skipif(os.name == "nt", reason="Fake executable uses a POSIX shebang")
@pytest.mark.parametrize("exit_code", [0, 1])
def test_installer_uses_blender_and_propagates_failure(binary, tmp_path, exit_code):
    recorded = tmp_path / "arguments.json"
    fake = tmp_path / "blender with spaces"
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys, zipfile\nfrom pathlib import Path\nassert zipfile.is_zipfile(sys.argv[-1])\nPath({str(recorded)!r}).write_text(json.dumps(sys.argv[1:]))\nsys.exit({exit_code})\n"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        [str(binary), "install-addon", "--blender", str(fake), "--repo", "custom_repo"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (exit_code == 0)
    arguments = json.loads(recorded.read_text())
    assert arguments[:-1] == [
        "--disable-autoexec",
        "--command",
        "extension",
        "install-file",
        "--repo",
        "custom_repo",
    ]
    assert not Path(arguments[-1]).exists()
    if exit_code:
        assert "Blender extension command failed" in result.stderr


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for native extension validation",
)
def test_blender_validates_and_installs_extension(binary, addon_package, tmp_path):
    blender = os.environ["BLENDER_TEST_EXECUTABLE"]
    environment = blender_environment(tmp_path / "profile")
    subprocess.run(
        [blender, "--command", "extension", "validate", str(addon_package)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    install = subprocess.run(
        [str(binary), "install-addon", "--blender", blender],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    script = tmp_path / "check.py"
    script.write_text(f"""import addon_utils
import bpy
import io
import sys
from pathlib import Path
from types import SimpleNamespace
import zipfile

sys.path[:] = [path for path in sys.path if not path.endswith("site-packages")]
for module in list(sys.modules):
    if module.split(".")[0] in {{"requests", "urllib3", "certifi", "charset_normalizer", "idna"}}:
        del sys.modules[module]
name = "bl_ext.user_default.blender_mcp"
addon_utils.enable(name, default_set=True)
addon = sys.modules[name]
assert addon.preferences.get_preferences() is not None
assert addon.ui.BLENDERMCP_AddonPreferences.bl_idname == name
server = addon.server.BlenderMCPServer()
info = server.get_addon_info()
assert info["addon_build_version"] == {RELEASE_VERSION!r}, info
assert info["addon_version"] == {RELEASE_TUPLE!r}, info
assert info["protocol_version"] == {PROTOCOL_VERSION!r}, info
runtime = info["runtime"]
assert runtime["blender_binary"] == bpy.app.binary_path, runtime
assert runtime["background"] is True, runtime
assert runtime["online_access"] is False, runtime
assert runtime["file"]["path"] == bpy.data.filepath, runtime
assert runtime["file"]["saved"] == bpy.data.is_saved, runtime
assert runtime["file"]["dirty"] is False, runtime
assert runtime["scene"] == bpy.context.scene.name, runtime
assert runtime["listener"]["running"] is False, runtime
assert runtime["sketchfab"]["enabled"] is False, runtime
created = server.execute_command({{"type": "execute_code", "params": {{"code": "for i in range(200):\\n    bpy.context.scene.collection.objects.link(bpy.data.objects.new(f'Dirty.{{i}}', None))"}}}})
assert created["result"]["succeeded"], created
assert server.get_addon_info()["runtime"]["file"]["dirty"] is True
assert bpy.app.version >= {BLENDER_VERSION!r}
assert Path(addon.sketchfab.requests.__file__).resolve().is_relative_to(Path({str(tmp_path)!r}).resolve()), addon.sketchfab.requests.__file__
assert not bpy.app.online_access
try:
    addon.sketchfab._http_get("https://example.invalid")
except RuntimeError as error:
    assert "Online access is disabled" in str(error)
else:
    raise AssertionError("Offline request was allowed")

addon_utils.disable(name, default_set=True)
assert not hasattr(bpy.types.Scene, "blendermcp_port")
addon_utils.enable(name, default_set=True)
assert addon.preferences.get_preferences() is not None
addon_utils.disable(name, default_set=True)
print("EXTENSION_OK", bpy.app.version_string)
""")
    checked = subprocess.run(
        [
            blender,
            "--background",
            "--factory-startup",
            "--offline-mode",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(script),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "EXTENSION_OK" in checked.stdout
