import base64
import json
import logging
import os
import queue
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import PROTOCOL_VERSION, RELEASE_TUPLE, RELEASE_VERSION, ROOT_ADDON
from test_server_threading import BlenderMCPServer

logger = logging.getLogger(__name__)


class Client:
    def __init__(self, binary, directory, server=None):
        self.commands = []
        self.errors = queue.Queue()
        self.responses = queue.Queue()
        self.stopped = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.executor = BlenderMCPServer()
        self.tls_context = self.executor._load_tls_context()
        env = dict(
            os.environ,
            BLENDER_HOST="127.0.0.1",
            BLENDER_PORT=str(server.port if server else self.listener.getsockname()[1]),
        )
        self.process = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.reader = threading.Thread(target=self.read_responses, daemon=True)
        self.worker.start()
        self.reader.start()
        self.next_id = 0
        initialized = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "migration-test", "version": "1"},
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "blender-mcp"
        assert "tools" in initialized["result"]["capabilities"]
        self.notify("notifications/initialized")

    def read_responses(self):
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except json.JSONDecodeError as error:
                self.responses.put(error)

    def serve(self):
        try:
            while not self.stopped.is_set():
                try:
                    connection, _ = self.listener.accept()
                except TimeoutError:
                    continue
                connection.settimeout(5)
                with self.tls_context.wrap_socket(
                    connection, server_side=True
                ) as connection:
                    connection.settimeout(0.1)
                    buffer = b""
                    while not self.stopped.is_set():
                        try:
                            chunk = connection.recv(8192)
                        except TimeoutError:
                            continue
                        if not chunk:
                            break
                        buffer += chunk
                        try:
                            command = json.loads(buffer)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        buffer = b""
                        self.commands.append(command)
                        result = self.respond(command)
                        connection.sendall(
                            json.dumps(result, ensure_ascii=False).encode()
                        )
        except Exception as error:
            if not self.stopped.is_set():
                logger.exception("Fake Blender server failed")
                self.errors.put(error)

    def respond(self, command):
        name, params = command["type"], command["params"]
        if name == "execute_code":
            try:
                result = self.executor.execute_code(**params)
            except Exception as error:
                logger.exception("Blender rejected test script")
                return {"status": "error", "message": str(error)}
        elif name == "get_addon_info":
            result = {
                "protocol_version": PROTOCOL_VERSION,
                "addon_version": RELEASE_TUPLE,
                "addon_build_version": RELEASE_VERSION,
                "capabilities": ["execute_code"],
                "blender_version": "test",
            }
        elif name in {
            "get_viewport_screenshot",
            "get_sketchfab_model_preview",
            "get_render_image",
        }:
            result = {
                "image_data": base64.b64encode(b"image-bytes").decode(),
                "format": "png",
                "width": 640,
                "height": 480,
                "method": "offscreen",
            }
        elif params.get("name") == "missing":
            result = {"error": "Object not found"}
        else:
            result = {"echo": params, "name": "立方体 🧊"}
        return {"status": "success", "result": result}

    def notify(self, method):
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method}) + "\n"
        )
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.next_id += 1
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.next_id,
                    "method": method,
                    "params": params,
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        response = self.responses.get(timeout=10)
        assert isinstance(response, dict), response
        assert response["id"] == self.next_id
        return response

    def call(self, name, arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self):
        self.process.stdin.close()
        try:
            assert self.process.wait(timeout=5) == 0, self.process.stderr.read()
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.stopped.set()
            self.worker.join(timeout=2)
            self.listener.close()
        assert self.errors.empty(), list(self.errors.queue)


@pytest.fixture
def client(binary, tmp_path):
    client = Client(binary, tmp_path)
    try:
        yield client
    finally:
        client.close()


CASES = [
    (
        "get_material_info",
        {"material_name": "Glass", "offset": 2, "limit": 5},
        "get_material_info",
        {"material_name": "Glass", "offset": 2, "limit": 5},
    ),
    (
        "get_node_group_info",
        {"node_group_name": "Plants"},
        "get_node_group_info",
        {"node_group_name": "Plants"},
    ),
    (
        "get_modifier_info",
        {"object_name": "Cube", "modifier_name": "Array"},
        "get_modifier_info",
        {"object_name": "Cube", "modifier_name": "Array"},
    ),
    (
        "get_animation_info",
        {"data_name": "Cube", "data_type": "OBJECT"},
        "get_animation_info",
        {"data_name": "Cube", "data_type": "OBJECT"},
    ),
    (
        "get_object_info",
        {"object_name": "Cube", "details": True},
        "get_object_info",
        {"name": "Cube", "details": True},
    ),
    (
        "create_checkpoint",
        {"label": "Before edits"},
        "create_checkpoint",
        {"label": "Before edits"},
    ),
    ("list_checkpoints", {}, "list_checkpoints", {}),
    (
        "restore_checkpoint",
        {"checkpoint_id": "a" * 32},
        "restore_checkpoint",
        {"checkpoint_id": "a" * 32},
    ),
    (
        "delete_checkpoint",
        {"checkpoint_id": "a" * 32},
        "delete_checkpoint",
        {"checkpoint_id": "a" * 32},
    ),
    ("get_execution_result", {}, "get_execution_result", {}),
    (
        "get_execution_changes",
        {"execution_id": "a" * 32, "category": "created", "offset": 100},
        "get_execution_changes",
        {"execution_id": "a" * 32, "category": "created", "offset": 100, "limit": 100},
    ),
    (
        "export_render",
        {"job_id": "job", "filepath": "/tmp/full image.png"},
        "export_render",
        {"job_id": "job", "filepath": "/tmp/full image.png", "overwrite": False},
    ),
    ("get_addon_status", {}, "get_addon_info", {}),
    ("get_scene_info", {}, "get_scene_info", {}),
    (
        "set_camera",
        {"object_name": "Camera", "lens": 85},
        "set_camera",
        {"object_name": "Camera", "lens": 85},
    ),
    (
        "set_viewport",
        {"view": "TOP", "frame": "ALL", "shading": "SOLID"},
        "set_viewport",
        {"view": "TOP", "frame": "ALL", "shading": "SOLID"},
    ),
    ("get_object_info", {"object_name": "Cube"}, "get_object_info", {"name": "Cube"}),
    (
        "get_object_info",
        {"object_name": "Cube", "evaluated": True},
        "get_object_info",
        {"name": "Cube", "evaluated": True},
    ),
    ("get_viewport_screenshot", {}, "get_viewport_screenshot", {"max_size": 1000}),
    (
        "get_viewport_screenshot",
        {"viewport_index": 1, "camera_only": True},
        "get_viewport_screenshot",
        {"max_size": 1000, "viewport_index": 1, "camera_only": True},
    ),
    ("start_render", {}, "start_render", {}),
    ("get_render_status", {}, "get_render_status", {}),
    ("cancel_render", {"job_id": "job"}, "cancel_render", {"job_id": "job"}),
    (
        "get_render_image",
        {"job_id": "job"},
        "get_render_image",
        {"job_id": "job", "max_size": 1000},
    ),
    (
        "execute_blender_code",
        {"code": "print('ok')"},
        "execute_code",
        {"code": "print('ok')"},
    ),
    ("get_sketchfab_status", {}, "get_sketchfab_status", {}),
    (
        "search_sketchfab_models",
        {"query": "chair"},
        "search_sketchfab_models",
        {"query": "chair", "categories": None, "count": 20, "downloadable": True},
    ),
    (
        "get_sketchfab_model_preview",
        {"uid": "model"},
        "get_sketchfab_model_preview",
        {"uid": "model"},
    ),
    (
        "download_sketchfab_model",
        {"uid": "model", "target_size": 1.7},
        "download_sketchfab_model",
        {"uid": "model", "target_size": 1.7, "normalize_size": True},
    ),
]


def test_all_tools_over_stdio_and_tcp(client):
    catalog = client.rpc("tools/list", {})["result"]["tools"]
    assert {tool["name"] for tool in catalog} == {case[0] for case in CASES}
    assert "user_prompt" not in json.dumps(catalog)
    for name, arguments, command, params in CASES:
        result = client.call(name, arguments)
        assert not result.get("isError"), (name, result)
        assert client.commands[-1] == {"type": command, "params": params}
        if name in {
            "get_viewport_screenshot",
            "get_sketchfab_model_preview",
            "get_render_image",
        }:
            assert result["content"][0]["type"] == "image"
            assert result["content"][0]["mimeType"] == "image/png"
            assert base64.b64decode(result["content"][0]["data"]) == b"image-bytes"
            metadata = result["structuredContent"]
            assert metadata == json.loads(result["content"][1]["text"])
            assert metadata["method"] == "offscreen"
            assert (metadata["width"], metadata["height"]) == (640, 480)
            assert "image_data" not in metadata
        elif name == "get_addon_status":
            assert json.loads(result["content"][0]["text"])["up_to_date"] is True
        if name not in {
            "get_viewport_screenshot",
            "get_sketchfab_model_preview",
            "get_render_image",
        }:
            assert result["structuredContent"] == json.loads(
                result["content"][0]["text"]
            )
    assert len(client.commands) == len(CASES)
    prompts = client.rpc("prompts/list", {})["result"]["prompts"]
    assert [prompt["name"] for prompt in prompts] == ["asset_creation_strategy"]
    prompt = client.rpc("prompts/get", {"name": "asset_creation_strategy"})["result"]
    assert "record_trajectory_feedback" not in json.dumps(prompt)
    assert "get_viewport_screenshot" in json.dumps(prompt)


def test_structured_execution_failure_survives_mcp_transport(client, monkeypatch):
    outcome = {
        "started": True,
        "succeeded": False,
        "partial_changes": True,
        "error_message": "deliberate",
        "execution_id": "a" * 32,
        "checkpoint": {"checkpoint_id": "b" * 32},
        "changes": {"created": [{"name": "Left behind"}]},
    }
    monkeypatch.setattr(
        client, "respond", lambda command: {"status": "success", "result": outcome}
    )
    result = client.call(
        "execute_blender_code",
        {
            "code": "raise ValueError('deliberate')",
            "checkpoint": True,
            "summarize_changes": True,
        },
    )
    assert result["isError"]
    assert result["structuredContent"] == outcome
    assert json.loads(result["content"][0]["text"]) == outcome
    assert client.commands[-1]["params"]["checkpoint"] is True


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("export_render", {"job_id": "job"}),
        ("export_render", {"job_id": "job", "filepath": ""}),
        (
            "export_render",
            {"job_id": "job", "filepath": "/tmp/image.png", "overwrite": "yes"},
        ),
        ("get_execution_changes", {"execution_id": "a" * 32, "category": "all"}),
        (
            "get_execution_changes",
            {"execution_id": "a" * 32, "category": "created", "offset": -1},
        ),
        (
            "get_execution_changes",
            {"execution_id": "a" * 32, "category": "created", "limit": 101},
        ),
    ],
)
def test_export_and_change_page_schemas_reject_invalid_arguments(
    client, name, arguments
):
    assert client.call(name, arguments)["isError"]
    assert not client.commands


def test_tool_annotations_distinguish_inspection_edits_and_network(client):
    catalog = {
        tool["name"]: tool for tool in client.rpc("tools/list", {})["result"]["tools"]
    }
    for name in (
        "get_addon_status",
        "get_material_info",
        "get_node_group_info",
        "get_modifier_info",
        "get_animation_info",
        "get_scene_info",
        "get_object_info",
        "get_viewport_screenshot",
        "get_render_status",
        "get_render_image",
    ):
        assert catalog[name]["annotations"]["readOnlyHint"] is True
        assert catalog[name]["annotations"]["openWorldHint"] is False
    for name in (
        "get_sketchfab_status",
        "search_sketchfab_models",
        "get_sketchfab_model_preview",
    ):
        assert catalog[name]["annotations"]["readOnlyHint"] is True
        assert catalog[name]["annotations"]["openWorldHint"] is True
    assert catalog["execute_blender_code"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert catalog["download_sketchfab_model"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert catalog["start_render"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert catalog["cancel_render"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    for name in ("set_camera", "set_viewport"):
        assert catalog[name]["annotations"] == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("set_camera", {}),
        ("set_camera", {"object_name": "Camera", "lens": 0}),
        ("set_camera", {"object_name": "Camera", "lens": True}),
        ("set_camera", {"object_name": "Camera", "object_names": []}),
        ("set_viewport", {"view": "INVALID"}),
        ("set_viewport", {"viewport_index": -1}),
        ("set_viewport", {"overlays": 0}),
        ("set_viewport", {"camera_zoom": 601}),
        ("get_viewport_screenshot", {"camera_only": "yes"}),
        ("get_viewport_screenshot", {"viewport_index": True}),
    ],
)
def test_camera_viewport_schema_validation(client, name, arguments):
    assert client.call(name, arguments)["isError"]
    assert not client.commands


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("get_object_info", {"object_name": "Cube", "details": 1}),
        ("get_material_info", {"material_name": ""}),
        ("get_material_info", {"material_name": "Glass", "limit": 101}),
        ("get_node_group_info", {"node_group_name": "Plants", "offset": -1}),
        ("get_modifier_info", {"object_name": "Cube"}),
        ("get_animation_info", {"data_name": "Cube", "data_type": "ACTION"}),
        ("get_animation_info", {"data_name": "Cube", "offset": True}),
    ],
)
def test_inspection_schema_validation(client, name, arguments):
    assert client.call(name, arguments)["isError"]
    assert not client.commands


@pytest.mark.parametrize("evaluated", [None, 0, "true", []])
def test_object_rejects_invalid_evaluated_over_mcp(client, evaluated):
    result = client.call(
        "get_object_info", {"object_name": "Cube", "evaluated": evaluated}
    )
    assert result["isError"]
    assert not client.commands


def test_render_arguments_and_failures_over_mcp(client):
    arguments = {"scene_name": "Scene", "frame": 25, "resolution_percentage": 50}
    assert not client.call("start_render", arguments).get("isError")
    assert client.commands[-1] == {"type": "start_render", "params": arguments}
    count = len(client.commands)
    for name, arguments in [
        ("start_render", {"frame": True}),
        ("start_render", {"resolution_percentage": 0}),
        ("start_render", {"resolution_percentage": 101}),
        ("start_render", {"scene_name": ""}),
        ("cancel_render", {}),
        ("get_render_image", {"job_id": ""}),
        ("get_render_image", {"job_id": "job", "max_size": 4097}),
    ]:
        assert client.call(name, arguments)["isError"]
    assert len(client.commands) == count
    status = {"job_id": "job", "state": "failed", "error_message": "Render failed"}
    client.respond = lambda command: {"status": "success", "result": status}
    response = client.call("get_render_status", {"job_id": "job"})
    assert not response.get("isError")
    assert response["structuredContent"] == status


def test_scene_filters_are_forwarded_and_invalid_inputs_stay_local(client):
    arguments = {
        "offset": 20,
        "limit": 10,
        "name_filter": "Cube",
        "object_type": "MESH",
        "selected_only": True,
    }
    result = client.call("get_scene_info", arguments)
    assert not result.get("isError"), result
    assert client.commands[-1] == {"type": "get_scene_info", "params": arguments}
    count = len(client.commands)
    for arguments in ({"offset": -1}, {"limit": 101}, {"selected_only": "yes"}):
        assert client.call("get_scene_info", arguments)["isError"]
    assert client.call("execute_blender_code", {"code": "", "reset_namespace": True})[
        "isError"
    ]
    assert len(client.commands) == count


def test_execution_diagnostics_reach_mcp_and_allow_recovery(client):
    result = client.call(
        "execute_blender_code",
        {
            "code": "import sys\nprint('before failure')\nprint('warning', file=sys.stderr)\nvalue = 42\nraise ValueError('fix me')",
            "namespace": "diagnostic",
        },
    )
    assert result["isError"]
    message = result["structuredContent"]["error_message"]
    assert 'File "<blender-mcp>", line 5' in message
    assert "ValueError: fix me" in message
    assert "before failure\n" in message
    assert "warning\n" in message
    result = client.call(
        "execute_blender_code", {"code": "print(value)", "namespace": "diagnostic"}
    )
    assert not result.get("isError"), result
    assert result["structuredContent"]["result"] == "42\n"


def test_errors_are_visible_and_server_remains_usable(client):
    for name, arguments in [
        ("execute_blender_code", {"code": 42}),
        ("get_viewport_screenshot", {"max_size": 0}),
    ]:
        assert client.call(name, arguments)["isError"] is True
    assert not client.commands
    assert client.call("get_object_info", {"object_name": "missing"})["isError"] is True
    assert (
        client.rpc("tools/call", {"name": "disable_telemetry", "arguments": {}})[
            "error"
        ]["code"]
        == -32601
    )
    assert not client.call(
        "get_scene_info", {"user_prompt": "must not be forwarded"}
    ).get("isError")
    assert client.commands[-1]["params"] == {}


def test_persistent_namespaces_over_stdio_and_tcp(client):
    def run(code, **params):
        result = client.call("execute_blender_code", {"code": code, **params})
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])["result"]

    run("value = 4\ndef helper():\n    return value", namespace="task-a")
    run("value = 8", namespace="task-b")
    assert run("print(helper())", namespace="task-a") == "4\n"
    assert run("print(value)", namespace="task-b") == "8\n"
    assert run("print('value' in globals())") == "False\n"
    run("", namespace="task-a", reset_namespace=True)
    assert run("print('helper' in globals())", namespace="task-a") == "False\n"
    assert run("print(value)", namespace="task-b") == "8\n"

    count = len(client.commands)
    for params in [
        {"namespace": ""},
        {"namespace": "a" * 129},
        {"reset_namespace": "yes"},
    ]:
        assert client.call("execute_blender_code", {"code": "", **params})["isError"]
    assert len(client.commands) == count


def test_binary_contains_addon(unpacked_addon):
    assert (unpacked_addon / "__init__.py").read_bytes() == ROOT_ADDON.read_bytes()


def test_removed_integrations_are_not_dispatched(client, monkeypatch):
    from addon_stub import _load_addon, _scene

    addon = _load_addon(monkeypatch, _scene())
    server = addon.BlenderMCPServer()
    for name in (
        "get_polyhaven_status",
        "download_polyhaven_asset",
        "set_texture",
        "get_hyper3d_status",
        "generate_hyper3d_model_via_text",
        "create_rodin_job",
        "import_generated_asset",
        "get_polypizza_status",
        "search_polypizza_models",
        "get_hunyuan3d_status",
        "generate_hunyuan3d_model",
        "create_hunyuan_job",
        "import_generated_asset_hunyuan",
    ):
        response = client.rpc("tools/call", {"name": name, "arguments": {}})
        assert response["error"]["code"] == -32601
        response = server.execute_command({"type": name, "params": {}})
        assert response["status"] == "error"
        assert "Unknown command type" in response["message"]
    assert not client.commands


@pytest.mark.parametrize(
    "version",
    [None, "1.6.0", "2.0.0+mod", RELEASE_VERSION.split("+")[0], RELEASE_VERSION],
)
def test_addon_status_checks_build_version(client, version):
    result = {"protocol_version": PROTOCOL_VERSION, "addon_version": RELEASE_TUPLE}
    if version is not None:
        result["addon_build_version"] = version
    client.respond = lambda command: {"status": "success", "result": result}

    status = json.loads(client.call("get_addon_status", {})["content"][0]["text"])

    assert status["up_to_date"] is (version == RELEASE_VERSION)
    assert status["expected_addon_version"] == RELEASE_VERSION
