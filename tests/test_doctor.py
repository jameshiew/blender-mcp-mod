import json
import os
import socket
import subprocess
import time

import pytest
from conftest import (
    BLENDER_VERSION_MIN,
    PROTOCOL_VERSION,
    RELEASE_VERSION,
    create_credentials,
)


def run_doctor(binary, *arguments, environment=None):
    return subprocess.run(
        [str(binary), "doctor", *arguments],
        env=dict(os.environ, **(environment or {})),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def checks(result):
    assert not result.stderr, result.stderr
    report = json.loads(result.stdout)
    assert report["healthy"] is (result.returncode == 0)
    return {check["name"]: check for check in report["checks"]}


@pytest.fixture
def doctor_server(addon, monkeypatch):
    monkeypatch.setattr(
        addon.server,
        "addon_metadata",
        lambda: (
            {"name": "MCP for Blender", "version": RELEASE_VERSION},
            PROTOCOL_VERSION,
        ),
    )
    server = addon.server.BlenderMCPServer(port=0)
    server.start()
    assert server.running, server.last_error
    commands = []
    execute = server.execute_command

    def record(command):
        commands.append(command)
        return execute(command)

    monkeypatch.setattr(server, "execute_command", record)
    server.doctor_commands = commands
    try:
        yield server
    finally:
        server.stop()


def run_connected(binary, server, *arguments, drain=True, environment=None):
    env = dict(os.environ, BLENDER_HOST="127.0.0.1", BLENDER_PORT=str(server.port))
    env.update(environment or {})
    with subprocess.Popen(
        [str(binary), "doctor", *arguments],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        deadline = time.monotonic() + 10
        while process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                pytest.fail("doctor did not finish")
            if drain:
                server._drain_command_queue()
            time.sleep(0.01)
        stdout, stderr = process.communicate(timeout=1)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )


def test_doctor_reports_live_status_without_changes_or_secrets(
    binary, doctor_server, addon, connection_credentials, monkeypatch
):
    bpy = addon.server.bpy
    bpy.context.scene.name = "Scene 🧊"
    bpy.context.scene.blendermcp_use_sketchfab = True
    bpy.data.filepath = "/project/scene.blend"
    bpy.data.is_saved = True
    bpy.data.is_dirty = True
    monkeypatch.setenv("BLENDERMCP_SKETCHFAB_API_KEY", "private-api-key")
    before = (connection_credentials / "credentials.json").read_bytes()

    def unexpected_request(*args, **kwargs):
        pytest.fail("doctor must not contact Sketchfab")

    monkeypatch.setattr(
        addon.sketchfab.requests, "get", unexpected_request, raising=False
    )
    result = run_connected(binary, doctor_server, "--json")
    status = checks(result)
    assert result.returncode == 0, result.stdout
    for name in (
        "credentials",
        "tcp",
        "tls",
        "addon",
        "addon_version",
        "protocol",
        "blender_version",
    ):
        assert status[name]["status"] == "ok", status[name]
    assert status["blender_version"]["data"]["reported"] == BLENDER_VERSION_MIN
    assert status["scene"]["data"] == "Scene 🧊"
    assert status["file"]["data"] == {
        "path": "/project/scene.blend",
        "saved": True,
        "dirty": True,
    }
    assert status["listener"]["data"]["running"] is True
    assert status["sketchfab"]["data"] == {
        "enabled": True,
        "api_key_configured": True,
        "api_checked": False,
    }
    assert "private-api-key" not in result.stdout
    assert "PRIVATE KEY" not in result.stdout
    assert "CERTIFICATE" not in result.stdout
    assert (connection_credentials / "credentials.json").read_bytes() == before
    assert doctor_server.doctor_commands == [{"type": "get_addon_info", "params": {}}]


def test_doctor_text_reports_status_without_remediation(binary, doctor_server):
    result = run_connected(binary, doctor_server)
    assert result.returncode == 0, result.stdout
    assert "[ok] tls:" in result.stdout
    assert "[info] sketchfab: disabled" in result.stdout
    assert "Status: ready" in result.stdout
    for instruction in ("install-addon", "setup-connection", "Restart Blender"):
        assert instruction not in result.stdout


def test_doctor_escapes_control_characters_in_text(binary, doctor_server, addon):
    addon.server.bpy.context.scene.name = "\x1b[31mScene\n[ok] fake"
    result = run_connected(binary, doctor_server)
    assert result.returncode == 0
    assert "\x1b" not in result.stdout
    assert "\\u{1b}[31mScene\\n[ok] fake" in result.stdout


def test_doctor_reports_sketchfab_configuration_without_key(
    binary, doctor_server, addon, monkeypatch
):
    addon.server.bpy.context.scene.blendermcp_use_sketchfab = True
    monkeypatch.delenv("BLENDERMCP_SKETCHFAB_API_KEY", raising=False)
    result = run_connected(binary, doctor_server, "--json")
    status = checks(result)
    assert result.returncode == 0
    assert status["sketchfab"]["status"] == "warning"
    assert status["sketchfab"]["data"]["api_key_configured"] is False


@pytest.mark.parametrize("response", [[], {"error": "Status unavailable"}])
def test_doctor_handles_invalid_or_failed_status(binary, doctor_server, response):
    doctor_server.handlers["get_addon_info"] = lambda: response
    status = checks(run_connected(binary, doctor_server, "--json"))
    assert status["addon"]["status"] == "error"
    assert status["blender_version"]["status"] == "unavailable"


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_endpoint_still_reports_credentials(binary, tmp_path, port):
    directory = tmp_path / "missing"
    result = run_doctor(
        binary,
        "--json",
        environment={
            "BLENDER_PORT": port,
            "BLENDER_MCP_CONFIG_DIR": str(directory),
        },
    )
    status = checks(result)
    assert result.returncode == 1
    assert status["endpoint"]["status"] == "error"
    assert status["credentials"]["status"] == "error"
    assert status["tcp"]["status"] == "unavailable"
    assert status["addon"]["status"] == "unavailable"
    assert not directory.exists()


def test_missing_credentials_does_not_hide_tcp_status(binary, doctor_server, tmp_path):
    directory = tmp_path / "missing"
    status = checks(
        run_connected(
            binary,
            doctor_server,
            "--json",
            environment={
                "BLENDER_MCP_CONFIG_DIR": str(directory),
            },
        )
    )
    assert status["credentials"]["status"] == "error"
    assert status["tcp"]["status"] == "ok"
    assert status["tls"]["status"] == "unavailable"
    assert not doctor_server.doctor_commands
    assert not directory.exists()


def test_unreachable_endpoint_reports_complete_status(binary):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        result = run_doctor(
            binary,
            "--json",
            environment={
                "BLENDER_HOST": "127.0.0.1",
                "BLENDER_PORT": str(reserved.getsockname()[1]),
            },
        )
    status = checks(result)
    assert status["credentials"]["status"] == "ok"
    assert status["tcp"]["status"] == "error"
    assert status["tls"]["status"] == "unavailable"
    assert status["file"]["status"] == "unavailable"


def test_doctor_rejects_unpaired_tls_before_sending_status(
    binary, doctor_server, tmp_path
):
    directory = create_credentials(binary, tmp_path / "other")
    status = checks(
        run_connected(
            binary,
            doctor_server,
            "--json",
            environment={
                "BLENDER_MCP_CONFIG_DIR": str(directory),
            },
        )
    )
    assert status["credentials"]["status"] == "ok"
    assert status["tcp"]["status"] == "ok"
    assert status["tls"]["status"] == "error"
    assert status["addon"]["status"] == "unavailable"
    assert not doctor_server.doctor_commands


def test_doctor_bounds_wait_for_busy_blender(binary, doctor_server):
    started = time.monotonic()
    status = checks(
        run_connected(binary, doctor_server, "--json", "--timeout", "1", drain=False)
    )
    assert time.monotonic() - started < 4
    assert status["tls"]["status"] == "ok"
    assert status["addon"]["status"] == "error"
    assert "busy" in status["addon"]["detail"]


def test_doctor_bounds_tls_handshake(binary):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = run_doctor(
            binary,
            "--json",
            "--timeout",
            "1",
            environment={
                "BLENDER_HOST": "127.0.0.1",
                "BLENDER_PORT": str(listener.getsockname()[1]),
            },
        )
    status = checks(result)
    assert status["tcp"]["status"] == "ok"
    assert status["tls"]["status"] == "error"
    assert "timed out" in status["tls"]["detail"]


@pytest.mark.parametrize("protocol", [None, PROTOCOL_VERSION - 1, PROTOCOL_VERSION + 1])
def test_protocol_mismatch_still_reports_runtime(binary, doctor_server, protocol):
    handler = doctor_server.handlers["get_addon_info"]
    doctor_server.handlers["get_addon_info"] = lambda: {
        **handler(),
        "protocol_version": protocol,
    }
    status = checks(run_connected(binary, doctor_server, "--json"))
    assert status["protocol"]["status"] == "error"
    assert status["blender_version"]["status"] == "ok"
    assert status["file"]["status"] == "info"


def test_older_addon_has_unknown_runtime_and_version_warning(binary, doctor_server):
    handler = doctor_server.handlers["get_addon_info"]

    def older_status():
        info = handler()
        info.pop("runtime")
        info["addon_build_version"] = "8.0.2+mod"
        return info

    doctor_server.handlers["get_addon_info"] = older_status
    result = run_connected(binary, doctor_server, "--json")
    status = checks(result)
    assert result.returncode == 0
    assert status["addon_version"]["status"] == "warning"
    assert status["protocol"]["status"] == "ok"
    assert status["file"]["status"] == "unavailable"
    assert status["sketchfab"]["status"] == "unavailable"


@pytest.mark.parametrize("field", ["server_key", "client_key", "ca"])
def test_doctor_reports_corrupt_credentials_without_replacing_them(
    binary, tmp_path, field
):
    directory = create_credentials(binary, tmp_path / "pairing")
    path = directory / "credentials.json"
    credentials = json.loads(path.read_text())
    credentials[field] = "invalid PEM"
    path.write_text(json.dumps(credentials))
    before = path.read_bytes()
    result = run_doctor(
        binary,
        "--json",
        environment={
            "BLENDER_PORT": "0",
            "BLENDER_MCP_CONFIG_DIR": str(directory),
        },
    )
    assert checks(result)["credentials"]["status"] == "error"
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ["0", "301", "-1", "infinite"])
def test_doctor_rejects_invalid_timeout(binary, value):
    result = run_doctor(binary, f"--timeout={value}")
    assert result.returncode == 2
    assert "--timeout" in result.stderr
