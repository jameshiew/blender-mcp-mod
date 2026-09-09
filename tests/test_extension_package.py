import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tomllib
from types import SimpleNamespace
import zipfile

import pytest

from conftest import (
    PROTOCOL_VERSION,
    RELEASE_TUPLE,
    RELEASE_VERSION,
    REPO_ROOT,
    ROOT_ADDON,
    blender_environment,
)
from test_hunyuan_import_security import _install_bpy_stubs, _load_addon


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
        assert manifest["blender_version_min"] == "4.2.0"
        assert manifest["license"] == ["SPDX:MIT"]
        assert set(manifest["permissions"]) == {"network", "files"}
        assert json.loads(archive.read("protocol.json"))["version"] == PROTOCOL_VERSION
        assert archive.read("LICENSE") == (REPO_ROOT / "LICENSE").read_bytes()
        assert archive.read("__init__.py") == ROOT_ADDON.read_bytes()
        for removed in (
            "telemetry",
            "trajectory",
            "UserEditRecorder",
            "uuid.getnode",
            "drain_human_activity",
        ):
            assert removed not in archive.read("__init__.py").decode()
        assert set(archive.namelist()) == {
            "__init__.py",
            "blender_manifest.toml",
            "protocol.json",
            "LICENSE",
        } | {name for name, _ in expected.values()}
        assert set(manifest["wheels"]) == {"./" + name for name, _ in expected.values()}
        for name, checksum in expected.values():
            assert (
                "sha256:" + hashlib.sha256(archive.read(name)).hexdigest() == checksum
            )


def test_installed_namespace_preferences_and_handshake(unpacked_addon, monkeypatch):
    bpy = _install_bpy_stubs(monkeypatch, SimpleNamespace())
    name = "bl_ext.custom_repository.blender_mcp"
    spec = importlib.util.spec_from_file_location(name, unpacked_addon / "__init__.py")
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    preferences = object()
    bpy.context.preferences = SimpleNamespace(
        addons={name: SimpleNamespace(preferences=preferences)}
    )
    assert addon.BLENDERMCP_AddonPreferences.bl_idname == name
    assert addon.get_blendermcp_addon_preferences() is preferences
    info = addon.BlenderMCPServer().get_addon_info()
    assert info["addon_build_version"] == RELEASE_VERSION
    assert info["addon_version"] == RELEASE_TUPLE
    assert info["protocol_version"] == PROTOCOL_VERSION
    assert not hasattr(addon, "bl_info")


def test_running_extension_keeps_loaded_version(unpacked_addon, tmp_path, monkeypatch):
    _install_bpy_stubs(monkeypatch, SimpleNamespace())
    directory = tmp_path / "extension"
    shutil.copytree(unpacked_addon, directory)
    spec = importlib.util.spec_from_file_location(
        "bl_ext.test.blender_mcp", directory / "__init__.py"
    )
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    addon._addon_metadata()
    manifest = directory / "blender_manifest.toml"
    manifest.write_text(manifest.read_text().replace(RELEASE_VERSION, "99.0.0+mod"))
    (directory / "protocol.json").write_text('{"version":999}')
    info = addon.BlenderMCPServer().get_addon_info()
    assert info["addon_build_version"] == RELEASE_VERSION
    assert info["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.parametrize("method", ["get", "post"])
def test_http_respects_online_access(monkeypatch, method):
    addon, bpy = _load_addon(monkeypatch)
    calls = []
    monkeypatch.setattr(
        addon.requests, method, lambda *a, **kw: calls.append((a, kw)), raising=False
    )
    request = getattr(addon, "_http_" + method)
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
    tree = ast.parse(ROOT_ADDON.read_text())
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
    )
    assert install.returncode == 0, install.stdout + install.stderr
    script = tmp_path / "check.py"
    script.write_text(f"""import addon_utils
import bpy
import sys
from pathlib import Path

sys.path[:] = [path for path in sys.path if not path.endswith("site-packages")]
for module in list(sys.modules):
    if module.split(".")[0] in {{"requests", "urllib3", "certifi", "charset_normalizer", "idna"}}:
        del sys.modules[module]
name = "bl_ext.user_default.blender_mcp"
addon_utils.enable(name, default_set=True)
addon = sys.modules[name]
assert addon.get_blendermcp_addon_preferences() is not None
assert addon.BLENDERMCP_AddonPreferences.bl_idname == name
info = addon.BlenderMCPServer().get_addon_info()
assert info["addon_build_version"] == {RELEASE_VERSION!r}, info
assert info["addon_version"] == {RELEASE_TUPLE!r}, info
assert info["protocol_version"] == {PROTOCOL_VERSION!r}, info
assert Path(addon.requests.__file__).resolve().is_relative_to(Path({str(tmp_path)!r}).resolve()), addon.requests.__file__
assert not bpy.app.online_access
try:
    addon._http_get("https://example.invalid")
except RuntimeError as error:
    assert "Online access is disabled" in str(error)
else:
    raise AssertionError("Offline request was allowed")
addon_utils.disable(name, default_set=True)
assert not hasattr(bpy.types.Scene, "blendermcp_port")
addon_utils.enable(name, default_set=True)
assert addon.get_blendermcp_addon_preferences() is not None
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
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "EXTENSION_OK" in checked.stdout
