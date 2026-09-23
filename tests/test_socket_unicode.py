from __future__ import annotations

import json
import ssl

import pytest


class ScriptedSocket:
    def __init__(self, chunks, *, write_limit=None):
        self.chunks = list(chunks)
        self.sent = bytearray()
        self.write_limit = write_limit
        self.closed = False

    def do_handshake(self):
        pass

    def recv(self, size):
        if not self.chunks:
            raise ssl.SSLWantReadError()
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def send(self, data):
        count = len(data) if self.write_limit is None else self.write_limit
        self.sent.extend(data[:count])
        return min(count, len(data))

    def close(self):
        self.closed = True


def add_client(addon, server, client):
    state = addon.transport.ClientState(client, None, handshaking=False)
    server._clients[client] = state
    server.running = True
    return state


@pytest.mark.parametrize("label", ["café ☕ 日本語", "emoji test 🎨"])
def test_fragmented_utf8_and_following_request_are_executed_once(addon, label):
    server = addon.server.BlenderMCPServer()
    commands = []
    server.execute_command = lambda command: (
        commands.append(command) or {"status": "success", "result": {}}
    )
    payload = json.dumps({"type": "ping", "note": label}, ensure_ascii=False).encode()
    split = next(index + 1 for index, value in enumerate(payload) if value >= 0xC0)
    with pytest.raises(UnicodeDecodeError):
        payload[:split].decode()
    client = ScriptedSocket([payload[:split], ssl.SSLWantReadError()])
    state = add_client(addon, server, client)
    server._tick()
    assert not commands
    assert state.incoming == payload[:split]
    client.chunks.append(payload[split:])
    server._tick()
    assert commands == [{"type": "ping", "note": label}]
    server._tick()
    assert json.loads(client.sent)["status"] == "success"
    client.chunks.append(b'{"type":"ping"}')
    server._tick()
    assert commands == [{"type": "ping", "note": label}, {"type": "ping"}]


def test_partial_writes_never_repeat_execution_or_read_next_command_early(addon):
    server = addon.server.BlenderMCPServer()
    commands = []
    server.execute_command = lambda command: (
        commands.append(command)
        or {"status": "success", "result": {"output": "x" * 1000}}
    )
    client = ScriptedSocket([b'{"type":"ping"}'], write_limit=7)
    state = add_client(addon, server, client)
    server._tick()
    client.chunks.append(b'{"type":"next"}')
    for _ in range(100):
        assert len(commands) == 1
        if not state.outgoing:
            break
        server._tick()
    assert not state.outgoing
    assert json.loads(client.sent)["result"]["output"] == "x" * 1000
    server._tick()
    assert commands == [{"type": "ping"}, {"type": "next"}]


def test_incomplete_large_command_is_scanned_once_without_json_reparsing(
    addon, monkeypatch
):
    server = addon.server.BlenderMCPServer()
    server.execute_command = lambda command: {"status": "success", "result": {}}
    client = ScriptedSocket([b'{"text":"' + b"x" * 65536])
    state = add_client(addon, server, client)
    original = addon.transport.json.loads
    parsed = []

    def loads(data):
        parsed.append(len(data))
        return original(data)

    monkeypatch.setattr(addon.transport.json, "loads", loads)
    for _ in range(10):
        server._tick()
    assert not parsed
    client.chunks.append(b'\\"[}]\\\\"}')
    server._tick()
    assert len(parsed) == 1
    assert state.outgoing and not client.closed


@pytest.mark.parametrize("payload", [b'{"x":' + b"[" * 128, b'{"x":]', b"[]", b"{}{}"])
def test_invalid_framing_and_excessive_json_depth_are_rejected(addon, payload):
    server = addon.server.BlenderMCPServer()
    client = ScriptedSocket([payload])
    add_client(addon, server, client)
    server._tick()
    assert client.closed


def test_oversized_response_returns_bounded_error_without_replaying(addon):
    server = addon.server.BlenderMCPServer()
    server.max_message_bytes = 256
    calls = []
    server.execute_command = lambda command: (
        calls.append(command) or {"data": "x" * 1000}
    )
    client = ScriptedSocket([b'{"type":"ping"}'])
    state = add_client(addon, server, client)
    server._tick()
    assert len(state.outgoing) < server.max_message_bytes
    assert json.loads(state.outgoing)["status"] == "error"
    server._tick()
    assert len(calls) == 1
