import json
import os
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest

from conftest import RELEASE_VERSION, blender_environment, client_tls_context
from test_rust_server import Client


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for the native Blender GUI test",
)
def test_native_blender_transport(binary, tmp_path, connection_credentials):
    environment = blender_environment(tmp_path / "profile")
    subprocess.run(
        [
            str(binary),
            "install-addon",
            "--blender",
            os.environ["BLENDER_TEST_EXECUTABLE"],
        ],
        env=environment,
        check=True,
        capture_output=True,
    )
    ready = tmp_path / "ready.json"
    stop = tmp_path / "stop"
    forbidden = tmp_path / "unauthorized.txt"
    allowed = tmp_path / "authorized.txt"
    script = tmp_path / "start.py"
    script.write_text(f"""import bpy
import addon_utils
import json
import sys
from pathlib import Path

name = "bl_ext.user_default.blender_mcp"
addon_utils.enable(name, default_set=True)
addon = sys.modules[name]
addon_utils.disable(name, default_set=True)
server = addon.BlenderMCPServer(port=0)
bpy.types.blendermcp_server = server
addon_utils.enable(name, default_set=True)
Path({str(ready)!r}).write_text(json.dumps({{"port": server.port, "running": server.running, "error": server.last_error, "version": bpy.app.version_string}}))

def finish():
    if Path({str(stop)!r}).exists():
        addon_utils.disable(name, default_set=True)
        bpy.ops.wm.quit_blender()
        return None
    return 0.1

bpy.app.timers.register(finish)
""")
    with (tmp_path / "blender.log").open("w+") as log:
        process = subprocess.Popen(
            [
                os.environ["BLENDER_TEST_EXECUTABLE"],
                "--factory-startup",
                "--disable-autoexec",
                "--offline-mode",
                "--python-exit-code",
                "1",
                "--python",
                str(script),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        client = None
        try:
            deadline = time.monotonic() + 30
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    pytest.fail(log.read())
                time.sleep(0.1)
            state = json.loads(ready.read_text())
            assert state["running"], state
            command = json.dumps(
                {
                    "type": "execute_code",
                    "params": {
                        "code": f"from pathlib import Path; Path({str(forbidden)!r}).touch()"
                    },
                }
            ).encode()
            for context in (
                None,
                client_tls_context(connection_credentials, identity=False),
            ):
                with socket.create_connection(
                    ("127.0.0.1", state["port"]), timeout=3
                ) as tcp:
                    try:
                        if context is None:
                            tcp.sendall(command)
                            assert tcp.recv(1) == b""
                        else:
                            with context.wrap_socket(
                                tcp, server_hostname="blender-mcp.local"
                            ) as tls:
                                tls.sendall(command)
                                assert tls.recv(1) == b""
                    except OSError:
                        pass
            client = Client(
                binary, tmp_path, server=SimpleNamespace(port=state["port"])
            )
            status = json.loads(
                client.call("get_addon_status", {})["content"][0]["text"]
            )
            assert status["up_to_date"], status
            assert status["addon_build_version"] == RELEASE_VERSION
            result = client.call(
                "execute_blender_code",
                {
                    "code": f"from pathlib import Path\nPath({str(allowed)!r}).write_text('full Python works')\nprint(bpy.app.version_string)"
                },
            )
            assert not result.get("isError"), result
            assert allowed.read_text() == "full Python works"
            assert not forbidden.exists()
            assert (
                state["version"] in json.loads(result["content"][0]["text"])["result"]
            )
            print(
                f"Native Blender {state['version']}: rejected plaintext and missing certificate; Rust MCP executed unrestricted Python over TLS"
            )
        finally:
            if client is not None:
                client.close()
            stop.touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
                pytest.fail("Blender did not stop after the transport test")
            assert process.returncode == 0
