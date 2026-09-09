import base64
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
            for code, params, expected in [
                (
                    "import math\nobj = bpy.data.objects.new('Namespace test', None)\ndef helper():\n    return math.sqrt(16), obj.name",
                    {"namespace": "task-a"},
                    "",
                ),
                ("obj = 'other task'", {"namespace": "task-b"}, ""),
                (
                    "print(helper())",
                    {"namespace": "task-a"},
                    "(4.0, 'Namespace test')\n",
                ),
                ("print('obj' in globals())", {}, "False\n"),
                (
                    "print('obj' in globals())",
                    {"namespace": "task-a", "reset_namespace": True},
                    "False\n",
                ),
                ("print(obj)", {"namespace": "task-b"}, "other task\n"),
            ]:
                result = client.call("execute_blender_code", {"code": code, **params})
                assert not result.get("isError"), result
                assert json.loads(result["content"][0]["text"])["result"] == expected

            def call(name, arguments):
                result = client.call(name, arguments)
                assert not result.get("isError"), result
                return result["structuredContent"]

            call(
                "execute_blender_code",
                {
                    "code": """for obj in bpy.context.selected_objects:
    obj.select_set(False)
for index in reversed(range(25)):
    obj = bpy.data.objects.new(f'Inspect.{index:03}', None)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(index % 2 == 0)
bpy.context.view_layer.objects.active = bpy.data.objects['Inspect.000']
parent = bpy.data.objects['Inspect.000']
parent.location = (10, 20, 30)
bpy.ops.mesh.primitive_cube_add(size=2)
child = bpy.context.object
child.name = 'Inspect.Child'
child.parent = parent
child.location = (1, 2, 3)
child.modifiers.new('Bevel', 'BEVEL')
bpy.context.view_layer.update()
"""
                },
            )
            first = call(
                "get_scene_info",
                {"name_filter": "inspect.", "object_type": "EMPTY", "limit": 20},
            )
            assert first["matching_objects"] == 25
            assert first["returned_count"] == 20
            assert first["next_offset"] == 20
            assert first["active_object"] == "Inspect.Child"
            assert first["mode"] == "OBJECT"
            assert first["filepath"] == ""
            last = call(
                "get_scene_info",
                {
                    "name_filter": "INSPECT.",
                    "object_type": "EMPTY",
                    "offset": first["next_offset"],
                },
            )
            names = [obj["name"] for obj in first["objects"] + last["objects"]]
            assert names == [f"Inspect.{index:03}" for index in range(25)]
            assert last["next_offset"] is None
            selected = call(
                "get_scene_info",
                {"selected_only": True, "name_filter": "Inspect.Child"},
            )
            assert [obj["name"] for obj in selected["objects"]] == ["Inspect.Child"]
            child = call("get_object_info", {"object_name": "Inspect.Child"})
            assert child["parent"] == "Inspect.000"
            assert child["location"] == [1, 2, 3]
            assert child["world_location"] == [11, 22, 33]
            assert child["dimensions"] == [2, 2, 2]
            assert child["mesh"] == {"vertices": 8, "edges": 12, "polygons": 6}
            assert child["modifiers"][0]["type"] == "BEVEL"

            failed = client.call(
                "execute_blender_code",
                {
                    "code": "import sys\nprint('partial change')\nprint('diagnostic warning', file=sys.stderr)\nbpy.data.objects['Inspect.Child'].location.x = 4\nraise ValueError('native diagnostic')"
                },
            )
            assert failed["isError"]
            message = failed["content"][0]["text"]
            assert 'File "<blender-mcp>", line 5' in message
            assert "ValueError: native diagnostic" in message
            assert "partial change\n" in message
            assert "diagnostic warning\n" in message
            assert (
                call("get_object_info", {"object_name": "Inspect.Child"})["location"][0]
                == 4
            )
            output = call(
                "execute_blender_code",
                {
                    "code": "import sys\nprint('x' * 20000)\nprint('warning', file=sys.stderr)"
                },
            )
            assert output["output_truncated"] is True
            assert len(output["result"]) == 16_384
            assert output["stderr"] == "warning\n"

            screenshot = client.call("get_viewport_screenshot", {"max_size": 256})
            assert not screenshot.get("isError"), screenshot
            assert screenshot["content"][0]["type"] == "image"
            assert screenshot["structuredContent"]["method"] in {
                "offscreen",
                "window_grab",
            }
            assert (
                max(
                    screenshot["structuredContent"]["width"],
                    screenshot["structuredContent"]["height"],
                )
                <= 256
            )
            image = base64.b64decode(screenshot["content"][0]["data"])
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
            (tmp_path / "viewport.png").write_bytes(image)
            print(
                f"Native Blender {state['version']}: TLS authentication, Python namespaces/diagnostics, scene pagination, world transforms, and viewport capture passed"
            )
            print(f"Viewport capture: {tmp_path / 'viewport.png'}")
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
