import ssl
import time

import pytest
from test_socket_unicode import ScriptedSocket, add_client


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionResetError])
def test_response_send_failure_closes_client_before_another_command(addon, failure):
    server = addon.server.BlenderMCPServer()
    commands = []
    server.execute_command = lambda command: (
        commands.append(command) or {"status": "success", "result": {}}
    )

    class Client(ScriptedSocket):
        def send(self, data):
            raise failure("partial response")

    client = Client([b'{"type":"execute_code"}'])
    add_client(addon, server, client)
    server._tick()
    client.chunks.append(b'{"type":"execute_code"}')
    server._tick()
    assert client.closed and client not in server._clients
    assert len(commands) == 1
    assert len(client.chunks) == 1


@pytest.mark.parametrize("phase", ["handshake", "read", "write"])
def test_stalled_client_deadline_closes_connection(addon, phase):
    server = addon.server.BlenderMCPServer()
    client = ScriptedSocket([])
    state = add_client(addon, server, client)
    state.handshaking = phase == "handshake"
    if phase == "read":
        state.incoming.extend(b'{"type":')
    if phase == "write":
        state.outgoing = b'{"status":"success"}'
    state.deadline = time.monotonic() - 1
    server._tick()
    assert client.closed and not server._clients


def test_slow_client_does_not_block_other_clients(addon):
    server = addon.server.BlenderMCPServer()

    class SlowClient(ScriptedSocket):
        def do_handshake(self):
            raise ssl.SSLWantReadError()

    slow = SlowClient([])
    state = add_client(addon, server, slow)
    state.handshaking = True
    fast = ScriptedSocket([b'{"type":"ping"}'])
    fast_state = add_client(addon, server, fast)
    server._tick()
    assert fast_state.outgoing
    assert not slow.closed


def test_command_size_is_bounded(addon):
    server = addon.server.BlenderMCPServer()
    server.max_message_bytes = 8
    client = ScriptedSocket([b'{"long":"unfinished'])
    add_client(addon, server, client)
    server._tick()
    assert client.closed and not server._clients


def test_locked_interface_defers_execution_and_detects_disconnection(addon):
    from types import SimpleNamespace

    server = addon.server.BlenderMCPServer()
    addon.transport.bpy.context.window_manager = SimpleNamespace(
        is_interface_locked=True
    )
    commands = []
    server.execute_command = lambda command: commands.append(command) or {}
    client = ScriptedSocket([b'{"type":"ping"}'])
    state = add_client(addon, server, client)
    server._tick()
    assert not commands
    assert state.complete
    addon.transport.bpy.context.window_manager.is_interface_locked = False
    server._tick()
    assert commands == [{"type": "ping"}]

    addon.transport.bpy.context.window_manager.is_interface_locked = True
    abandoned = ScriptedSocket([b'{"type":"ping"}'])
    add_client(addon, server, abandoned)
    server._tick()
    abandoned.chunks.append(b"")
    server._tick()
    assert abandoned.closed
    assert len(commands) == 1
