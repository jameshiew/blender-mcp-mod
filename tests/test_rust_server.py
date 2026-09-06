import base64
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from test_server_threading import BlenderMCPServer

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def binary():
    build = subprocess.run(
        [
            os.environ.get("CARGO", "cargo"),
            "build",
            "--locked",
            "--message-format=json",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    artifacts = [json.loads(line) for line in build.stdout.splitlines()]
    return next(
        Path(artifact["executable"])
        for artifact in artifacts
        if artifact.get("reason") == "compiler-artifact"
        and artifact["target"]["name"] == "blender-mcp"
        and artifact.get("executable")
    )


class Client:
    def __init__(self, binary, directory, safe_mode=False):
        self.commands = []
        self.errors = queue.Queue()
        self.responses = queue.Queue()
        self.stopped = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.scene = types.SimpleNamespace(name="Scene")
        self.executor = BlenderMCPServer()
        env = dict(
            os.environ,
            BLENDER_HOST="127.0.0.1",
            BLENDER_PORT=str(self.listener.getsockname()[1]),
            BLENDER_MCP_SAFE_MODE="1" if safe_mode else "0",
            BLENDER_USER_ADDONS=str(directory),
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
                with connection:
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
                result = self.executor.execute_code(params["code"])
            except Exception as error:
                logger.exception("Blender rejected test script")
                return {"status": "error", "message": str(error)}
        elif name == "get_addon_info":
            result = {
                "protocol_version": 6,
                "addon_version": [1, 6],
                "capabilities": ["execute_code"],
                "blender_version": "test",
            }
        elif name in {"get_viewport_screenshot", "get_sketchfab_model_preview"}:
            result = {
                "image_data": base64.b64encode(b"image-bytes").decode(),
                "format": "png",
            }
        elif name == "create_rodin_job":
            result = {
                "submit_time": 1,
                "uuid": "task",
                "jobs": {"subscription_key": "subscription"},
            }
        elif name == "create_hunyuan_job":
            result = {"Response": {"JobId": "123"}}
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
    ("get_addon_status", {}, "get_addon_info", {}),
    ("get_scene_info", {}, "get_scene_info", {}),
    ("get_object_info", {"object_name": "Cube"}, "get_object_info", {"name": "Cube"}),
    ("get_viewport_screenshot", {}, "get_viewport_screenshot", {"max_size": 1000}),
    (
        "execute_blender_code",
        {"code": "print('ok')"},
        "execute_code",
        {"code": "print('ok')"},
    ),
    (
        "get_polyhaven_categories",
        {},
        "get_polyhaven_categories",
        {"asset_type": "hdris"},
    ),
    (
        "search_polyhaven_assets",
        {},
        "search_polyhaven_assets",
        {"asset_type": "all", "categories": None},
    ),
    (
        "download_polyhaven_asset",
        {"asset_id": "chair", "asset_type": "models"},
        "download_polyhaven_asset",
        {
            "asset_id": "chair",
            "asset_type": "models",
            "resolution": "1k",
            "file_format": None,
        },
    ),
    (
        "set_texture",
        {"object_name": "Cube", "texture_id": "wood"},
        "set_texture",
        {"object_name": "Cube", "texture_id": "wood"},
    ),
    ("get_polyhaven_status", {}, "get_polyhaven_status", {}),
    ("get_hyper3d_status", {}, "get_hyper3d_status", {}),
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
    ("get_polypizza_status", {}, "get_polypizza_status", {}),
    (
        "search_polypizza_models",
        {"category": "Animals", "licence": "CC0"},
        "search_polypizza_models",
        {"query": "", "category": 7, "licence": 1, "animated": False, "limit": 20},
    ),
    (
        "download_polypizza_model",
        {"model_id": "pizza"},
        "download_polypizza_model",
        {"model_id": "pizza", "normalize_size": False, "target_size": 1.0},
    ),
    (
        "generate_hyper3d_model_via_text",
        {"text_prompt": "chair", "bbox_condition": [1.0, 2.0, 1.0]},
        "create_rodin_job",
        {"text_prompt": "chair", "images": None, "bbox_condition": [50, 100, 50]},
    ),
    (
        "generate_hyper3d_model_via_images",
        {"input_image_urls": ["https://example.com/a.png"]},
        "create_rodin_job",
        {
            "text_prompt": None,
            "images": ["https://example.com/a.png"],
            "bbox_condition": None,
        },
    ),
    (
        "poll_rodin_job_status",
        {"request_id": "request"},
        "poll_rodin_job_status",
        {"request_id": "request"},
    ),
    (
        "import_generated_asset",
        {"name": "Chair", "task_uuid": "task"},
        "import_generated_asset",
        {"name": "Chair", "task_uuid": "task"},
    ),
    ("get_hunyuan3d_status", {}, "get_hunyuan3d_status", {}),
    (
        "generate_hunyuan3d_model",
        {"text_prompt": "chair"},
        "create_hunyuan_job",
        {"text_prompt": "chair", "image": None},
    ),
    (
        "poll_hunyuan_job_status",
        {"job_id": "job_123"},
        "poll_hunyuan_job_status",
        {"job_id": "job_123"},
    ),
    (
        "import_generated_asset_hunyuan",
        {"name": "Chair", "zip_file_url": "https://example.com/chair.glb"},
        "import_generated_asset_hunyuan",
        {"name": "Chair", "zip_file_url": "https://example.com/chair.glb"},
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
        if name in {"get_viewport_screenshot", "get_sketchfab_model_preview"}:
            assert result["content"][0]["type"] == "image"
            assert result["content"][0]["mimeType"] == "image/png"
            assert base64.b64decode(result["content"][0]["data"]) == b"image-bytes"
        elif name.startswith("generate_hyper3d"):
            assert json.loads(result["content"][0]["text"]) == {
                "task_uuid": "task",
                "subscription_key": "subscription",
            }
        elif name == "generate_hunyuan3d_model":
            assert json.loads(result["content"][0]["text"]) == {"job_id": "job_123"}
        elif name == "get_addon_status":
            assert json.loads(result["content"][0]["text"])["up_to_date"] is True
    assert len(client.commands) == len(CASES)
    prompts = client.rpc("prompts/list", {})["result"]["prompts"]
    assert [prompt["name"] for prompt in prompts] == ["asset_creation_strategy"]
    prompt = client.rpc("prompts/get", {"name": "asset_creation_strategy"})["result"]
    assert "record_trajectory_feedback" not in json.dumps(prompt)
    assert "get_viewport_screenshot" in json.dumps(prompt)


def test_errors_are_visible_and_server_remains_usable(client):
    for name, arguments in [
        ("execute_blender_code", {"code": 42}),
        ("get_viewport_screenshot", {"max_size": 0}),
        ("search_polypizza_models", {"category": "invalid"}),
        ("generate_hyper3d_model_via_images", {"input_image_urls": []}),
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


def test_rust_safe_mode_validates_before_running_user_code(
    binary, tmp_path, monkeypatch
):
    client = Client(binary, tmp_path, safe_mode=True)
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(scene=client.scene)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    try:
        result = client.call(
            "execute_blender_code",
            {"code": "import bpy\nbpy.context.scene.name = 'Safe'\nprint('done')"},
        )
        assert not result.get("isError"), result
        assert client.scene.name == "Safe"
        assert json.loads(result["content"][0]["text"])["result"] == "done\n"
        result = client.call(
            "execute_blender_code",
            {"code": "import bpy\nbpy.context.scene.name = 'Unsafe'\nimport os"},
        )
        assert result["isError"] is True
        assert client.scene.name == "Safe"
    finally:
        client.close()


def test_local_rodin_images_are_encoded(client, tmp_path):
    path = tmp_path / "input.png"
    path.write_bytes(b"local-image")
    result = client.call(
        "generate_hyper3d_model_via_images", {"input_image_paths": [str(path)]}
    )
    assert not result.get("isError"), result
    assert client.commands[-1]["params"]["images"] == [
        [".png", base64.b64encode(b"local-image").decode()]
    ]


def test_installer_binary_contains_addon(binary, tmp_path):
    subprocess.run(
        [str(binary), "install-addon", "--addons-dir", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    assert (tmp_path / "blendermcp.py").read_bytes() == (
        REPO_ROOT / "addon.py"
    ).read_bytes()
