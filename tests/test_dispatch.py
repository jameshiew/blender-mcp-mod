import inspect
import json
from unittest.mock import Mock

import pytest
from addon_stub import _install_bpy_stubs, load_addon_package
from conftest import REPO_ROOT

CATALOG = json.loads((REPO_ROOT / "resources" / "tools.json").read_text())
COMMAND_ALIASES = {
    "get_addon_status": "get_addon_info",
    "execute_blender_code": "execute_code",
}
CHECKPOINT_COMMANDS = {
    "create_checkpoint": ["label"],
    "list_checkpoints": [],
    "restore_checkpoint": ["checkpoint_id"],
    "delete_checkpoint": ["checkpoint_id"],
}


def catalog_commands():
    for tool in CATALOG:
        if tool["name"] == "checkpoints":
            for command, names in CHECKPOINT_COMMANDS.items():
                yield command, names, names
            continue
        schema = tool["inputSchema"]
        yield (
            COMMAND_ALIASES.get(tool["name"], tool["name"]),
            schema.get("required", []),
            list(schema["properties"]),
        )


@pytest.fixture
def packaged_addon(unpacked_addon, monkeypatch):
    _install_bpy_stubs(monkeypatch)
    return load_addon_package(monkeypatch, unpacked_addon / "__init__.py")


def test_catalog_commands_and_capabilities_match(packaged_addon, monkeypatch):
    server = packaged_addon.server.BlenderMCPServer()
    catalog = list(catalog_commands())
    commands = [command for command, _, _ in catalog]
    assert len(set(commands)) == len(commands)
    sketchfab_commands = {name for name in commands if "sketchfab" in name}
    core_commands = set(commands) - sketchfab_commands
    assert set(server.handlers) == core_commands
    assert (
        set(server.sketchfab_handlers) | {"get_sketchfab_status"} == sketchfab_commands
    )
    information = server.execute_command({"type": "get_addon_info"})
    assert information["status"] == "success"
    assert information["result"]["capabilities"] == sorted(core_commands)

    packaged_addon.server.bpy.context.scene.blendermcp_use_sketchfab = True
    for command, required, properties in catalog:
        registry = (
            server.sketchfab_handlers
            if command in sketchfab_commands
            else server.handlers
        )
        if command == "get_sketchfab_status":
            handler = server.sketchfab.get_sketchfab_status
        else:
            handler = registry[command]
        signature = inspect.signature(handler)
        parameters = {}
        for names in (required, properties):
            parameters = {
                "name"
                if command == "get_object_info" and name == "object_name"
                else name: None
                for name in names
            }
            if command == "download_sketchfab_model":
                parameters["normalize_size"] = True
            signature.bind(**parameters)

        result = {"command": command}
        record = Mock(return_value=result)

        if command == "get_sketchfab_status":
            monkeypatch.setattr(server.sketchfab, command, record)
        else:
            monkeypatch.setitem(registry, command, record)
        response = server.execute_command({"type": command, "params": parameters})
        assert response == {"status": "success", "result": result}
        record.assert_called_once_with(**parameters)


@pytest.mark.parametrize("params", [None, [], "invalid", 42])
def test_dispatch_rejects_non_object_parameters(packaged_addon, params):
    server = packaged_addon.server.BlenderMCPServer()
    handler = Mock()
    server.handlers["execute_code"] = handler
    assert server.execute_command({"type": "execute_code", "params": params}) == {
        "status": "error",
        "message": "Command params must be a JSON object",
    }
    handler.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_internal_methods_are_not_commands(packaged_addon, enabled):
    server = packaged_addon.server.BlenderMCPServer()
    packaged_addon.server.bpy.context.scene.blendermcp_use_sketchfab = enabled
    methods = {
        name
        for name in dir(server)
        if callable(getattr(server, name)) and name not in server.handlers
    }
    assert {"start", "stop", "execute_command"} <= methods
    for name in sorted(methods | {"missing_command"}):
        assert server.execute_command({"type": name}) == {
            "status": "error",
            "message": f"Unknown command type: {name}",
        }


@pytest.mark.parametrize("enabled", [False, True])
def test_sketchfab_status_is_always_dispatched(packaged_addon, monkeypatch, enabled):
    server = packaged_addon.server.BlenderMCPServer()
    packaged_addon.server.bpy.context.scene.blendermcp_use_sketchfab = enabled
    monkeypatch.setattr(server.sketchfab, "_api_key", lambda: "")
    response = server.execute_command({"type": "get_sketchfab_status"})
    assert response["status"] == "success"
    assert response["result"]["enabled"] is False
