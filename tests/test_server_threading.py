from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import client_tls_context


@pytest.fixture(autouse=True)
def timers(addon, monkeypatch):
    main_thread = threading.current_thread()
    registered = set()

    class Timers:
        def register(self, fn, first_interval=0.0, persistent=False):
            assert threading.current_thread() is main_thread, (
                "timer registered off main thread"
            )
            registered.add(fn)

        def unregister(self, fn):
            registered.discard(fn)

        def is_registered(self, fn):
            return fn in registered

    monkeypatch.setattr(addon.transport.bpy.app, "timers", Timers())
    return registered


def _connect(port):
    context = client_tls_context(Path(os.environ["BLENDER_MCP_CONFIG_DIR"]))
    return context.wrap_socket(
        socket.create_connection(("127.0.0.1", port), timeout=5),
        server_hostname="blender-mcp.local",
    )


def _free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _make_server(server_class):
    server = server_class(port=_free_port())
    # Stub out command execution; these tests are about transport, not bpy.
    server.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    return server


def _pump(server, deadline=3.0):
    """Act as Blender's main loop, draining the queue until timeout."""
    end = time.time() + deadline
    while time.time() < end:
        server._drain_command_queue()
        time.sleep(0.01)


def test_client_thread_never_registers_a_timer(server_class):
    """The timer stub rejects registration from a client thread."""
    server = _make_server(server_class)
    server.start()
    try:
        with _connect(server.port) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())

            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()

            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())

        assert response["status"] == "success"
        assert response["result"]["echo"] == "ping"
    finally:
        server.stop()


def test_command_is_queued_not_executed_on_client_thread(server_class):
    """Without a main-loop pump, the command waits in the queue - never lost."""
    server = _make_server(server_class)
    server.start()
    try:
        with _connect(server.port) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())

            # No pump running, so nothing should execute yet.
            deadline = time.time() + 2.0
            while time.time() < deadline and server.command_queue.empty():
                time.sleep(0.01)

            assert not server.command_queue.empty(), "command was dropped, not queued"

            # Now pump once: the queued command is serviced.
            server._drain_command_queue()
            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
            assert response["status"] == "success"
    finally:
        server.stop()


def test_stop_releases_client_threads(server_class, timers):
    """stop() must unblock handlers so they cannot outlive a restart.

    Orphaned daemon threads parked in recv() were what produced the
    WinError 10054 after toggling the addon.
    """
    server = _make_server(server_class)
    server.start()

    client = _connect(server.port)
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with server._clients_lock:
                if server._clients:
                    break
            time.sleep(0.01)

        with server._clients_lock:
            assert server._clients, "server did not track the client socket"

        server.stop()

        # Handler threads should have exited and deregistered themselves.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with server._clients_lock:
                if not server._clients:
                    break
            time.sleep(0.01)

        with server._clients_lock:
            assert not server._clients, "client sockets still tracked after stop()"

        assert server._drain_command_queue not in timers, "drain timer left registered"
    finally:
        try:
            client.close()
        except OSError:
            pass


def test_restart_rebinds_port_cleanly(server_class):
    """A stopped server must fully release the port for the next start()."""
    port = _free_port()

    first = server_class(port=port)
    first.execute_command = lambda command: {"status": "success", "result": {}}
    first.start()
    with _connect(port):
        pass
    first.stop()

    second = server_class(port=port)
    second.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    second.start()
    try:
        with _connect(port) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())
            pump = threading.Thread(target=_pump, args=(second,), daemon=True)
            pump.start()
            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
        assert response["status"] == "success"
    finally:
        second.stop()
