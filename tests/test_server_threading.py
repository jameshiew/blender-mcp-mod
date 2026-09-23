from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import blender_environment, client_tls_context


@pytest.fixture(autouse=True)
def timers(addon, monkeypatch):
    main_thread = threading.current_thread()
    registered = {}

    class Timers:
        def register(self, fn, first_interval=0.0, persistent=False):
            assert threading.current_thread() is main_thread
            assert persistent
            registered[id(fn)] = fn

        def unregister(self, fn):
            del registered[id(fn)]

        def is_registered(self, fn):
            return id(fn) in registered

    monkeypatch.setattr(addon.transport.bpy.app, "timers", Timers())
    return registered


def _connect(port):
    context = client_tls_context(Path(os.environ["BLENDER_MCP_CONFIG_DIR"]))
    return context.wrap_socket(
        socket.create_connection(("127.0.0.1", port), timeout=5),
        server_hostname="blender-mcp.local",
    )


def _free_port():
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def _run_client(server, operation, deadline=5):
    results, errors = [], []

    def run():
        try:
            results.append(operation())
        except (OSError, ValueError, AssertionError, RuntimeError) as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    end = time.monotonic() + deadline
    try:
        while worker.is_alive() and time.monotonic() < end:
            server._tick()
            time.sleep(0.002)
    finally:
        worker.join(timeout=1)
    assert not worker.is_alive(), "test client stalled"
    if errors:
        raise errors[0]
    return results[0]


def _roundtrip(server):
    with _connect(server.port) as client:
        client.sendall(json.dumps({"type": "ping"}).encode())
        return json.loads(client.recv(8192))


def test_listener_creates_no_background_threads(server_class, timers, monkeypatch):
    main_thread = threading.current_thread()
    threads = set(threading.enumerate())
    server = server_class(port=0)
    server.start()
    assert set(threading.enumerate()) == threads
    assert timers == {id(server._timer_callback): server._timer_callback}
    calls = []

    def execute(command):
        assert threading.current_thread() is main_thread
        calls.append(command)
        return {"status": "success", "result": {"pong": True}}

    monkeypatch.setattr(server, "execute_command", execute)
    try:
        assert _run_client(server, lambda: _roundtrip(server))["result"]["pong"]
        assert calls == [{"type": "ping"}]
    finally:
        server.stop()
    assert not timers


def test_stop_closes_clients_and_unregisters_timer(server_class, timers):
    server = server_class(port=0)
    server.start()
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
        server._tick()
        assert len(server._clients) == 1
        server.stop()
        assert not server._clients
        assert client.recv(1) == b""
    assert server.socket is None
    assert not timers
    assert server._tick() is None


def test_same_listener_can_restart_and_rebind_port(server_class, timers):
    server = server_class(port=0)
    server.start()
    port = server.port
    try:
        assert _run_client(server, lambda: _roundtrip(server))["result"]["pong"]
        server.stop()
        assert not timers
        server.start()
        assert timers == {id(server._timer_callback): server._timer_callback}
        assert server.port == port
        assert _run_client(server, lambda: _roundtrip(server))["result"]["pong"]
    finally:
        server.stop()


def test_failed_job_poll_does_not_kill_command_timer(server_class):
    server = server_class(port=0)
    server.start()

    def fail():
        raise RuntimeError("poll failed")

    server.poll = fail
    try:
        assert _run_client(server, lambda: _roundtrip(server))["result"]["pong"]
        assert server.running
    finally:
        server.stop()


@pytest.mark.skipif(
    not os.environ.get("BLENDER_TEST_EXECUTABLE"),
    reason="Set BLENDER_TEST_EXECUTABLE for native timer identity checks",
)
def test_stop_unregisters_exact_callback_in_blender(unpacked_addon, tmp_path):
    script = tmp_path / "check_timer_identity.py"
    script.write_text(f"""import importlib.util
import sys
from pathlib import Path
import bpy
directory = Path({str(unpacked_addon)!r})
sys.path.extend(str(wheel) for wheel in (directory / 'wheels').glob('*.whl'))
spec = importlib.util.spec_from_file_location('timer_native', directory / '__init__.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
server = module.server.BlenderMCPServer()
for _ in range(2):
    callback = server._timer_callback
    bpy.app.timers.register(callback, persistent=True)
    assert bpy.app.timers.is_registered(callback)
    server.stop()
    assert not bpy.app.timers.is_registered(callback)
print('TIMER_IDENTITY_OK')
""")
    checked = subprocess.run(
        [
            os.environ["BLENDER_TEST_EXECUTABLE"],
            "--background",
            "--factory-startup",
            "--offline-mode",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(script),
        ],
        env=blender_environment(tmp_path / "profile"),
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "TIMER_IDENTITY_OK" in checked.stdout
