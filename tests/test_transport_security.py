import json
import os
import socket
import ssl
import subprocess
import threading
import time

import pytest

from conftest import client_tls_context, create_credentials
from test_rust_server import Client
from test_server_threading import BlenderMCPServer, _free_port, _connect


@pytest.fixture
def server():
    server = BlenderMCPServer(port=_free_port())
    server.start()
    assert server.running, server.last_error
    try:
        yield server
    finally:
        server.stop()


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def assert_rejected(server, context=None):
    command = json.dumps(
        {
            "type": "execute_code",
            "params": {"code": "raise RuntimeError('unauthorized execution')"},
        }
    ).encode()
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as tcp:
        try:
            if context is None:
                tcp.sendall(command)
                assert tcp.recv(8192) == b""
            else:
                with context.wrap_socket(
                    tcp, server_hostname="blender-mcp.local"
                ) as client:
                    client.sendall(command)
                    assert client.recv(8192) == b""
        except OSError:
            pass
    wait_until(lambda: not server._clients)
    assert server.command_queue.empty()


def test_plaintext_commands_never_reach_the_queue(server):
    assert_rejected(server)


def test_tls_without_client_certificate_never_reaches_the_queue(
    server, connection_credentials
):
    assert_rejected(server, client_tls_context(connection_credentials, identity=False))


def test_wrong_client_certificate_never_reaches_the_queue(
    server, binary, tmp_path, connection_credentials
):
    other = create_credentials(binary, tmp_path / "other")
    identity = json.loads((other / "credentials.json").read_text())
    assert_rejected(server, client_tls_context(connection_credentials, identity))


def test_server_certificate_cannot_be_used_as_a_client(server, connection_credentials):
    credentials = json.loads((connection_credentials / "credentials.json").read_text())
    identity = {
        "client_cert": credentials["server_cert"],
        "client_key": credentials["server_key"],
    }
    assert_rejected(server, client_tls_context(connection_credentials, identity))


def test_stalled_handshakes_expire_and_do_not_block_valid_clients(server):
    server.handshake_timeout = 0.2
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as stalled:
        stalled.sendall(b"\x16\x03\x03\x00\x80")
        with _connect(server.port):
            pass
        assert stalled.recv(1) == b""
    wait_until(lambda: not server._clients)
    assert server.command_queue.empty()


def test_unauthenticated_connection_count_is_bounded(server):
    server.max_clients = 2
    clients = []
    try:
        for count in range(1, 3):
            clients.append(
                socket.create_connection(("127.0.0.1", server.port), timeout=2)
            )
            wait_until(lambda: len(server._clients) == count)
        with socket.create_connection(
            ("127.0.0.1", server.port), timeout=2
        ) as rejected:
            assert rejected.recv(1) == b""
        assert len(server._clients) == 2
        assert server.command_queue.empty()
    finally:
        for client in clients:
            client.close()


@pytest.mark.parametrize("field", ["server_cert", "server_key"])
def test_setup_rejects_corrupt_server_identity_without_replacing_it(
    binary, tmp_path, field
):
    directory = create_credentials(binary, tmp_path / "pairing")
    path = directory / "credentials.json"
    credentials = json.loads(path.read_text())
    credentials[field] = "invalid PEM"
    path.write_text(json.dumps(credentials))
    before = path.read_bytes()
    result = subprocess.run(
        [str(binary), "setup-connection"],
        env=dict(os.environ, BLENDER_MCP_CONFIG_DIR=str(directory)),
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"left unchanged" in result.stderr
    assert path.read_bytes() == before


def test_setup_preserves_an_existing_pairing(binary, tmp_path):
    directory = create_credentials(binary, tmp_path / "pairing")
    path = directory / "credentials.json"
    before = path.read_bytes()
    create_credentials(binary, directory)
    assert path.read_bytes() == before


def test_stop_closes_an_incomplete_handshake(server):
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as stalled:
        wait_until(lambda: bool(server._clients))
        server.stop()
        assert stalled.recv(1) == b""


def test_missing_and_corrupt_credentials_never_open_the_port(binary, tmp_path):
    directory = tmp_path / "pairing"
    for corrupt in (False, True):
        if corrupt:
            create_credentials(binary, directory)
            (directory / "credentials.json").write_bytes(b"\xffcorrupt")
        candidate = BlenderMCPServer(port=_free_port(), config_dir=directory)
        candidate.start()
        assert not candidate.running
        assert candidate.socket is None
        assert "setup-connection" in candidate.last_error
    before = (directory / "credentials.json").read_bytes()
    result = subprocess.run(
        [str(binary), "setup-connection"],
        env=dict(os.environ, BLENDER_MCP_CONFIG_DIR=str(directory)),
        capture_output=True,
    )
    assert result.returncode != 0
    assert (directory / "credentials.json").read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("target", ["directory", "file", "symlink"])
def test_unsafe_credential_storage_is_rejected(binary, tmp_path, target):
    directory = create_credentials(binary, tmp_path / "pairing")
    path = directory / "credentials.json"
    if target == "directory":
        directory.chmod(0o755)
    elif target == "file":
        path.chmod(0o644)
    else:
        original = directory / "original.json"
        path.rename(original)
        path.symlink_to(original)
    candidate = BlenderMCPServer(port=_free_port(), config_dir=directory)
    candidate.start()
    assert not candidate.running
    assert candidate.socket is None
    result = subprocess.run(
        [str(binary), "setup-connection"],
        env=dict(os.environ, BLENDER_MCP_CONFIG_DIR=str(directory)),
        capture_output=True,
    )
    assert result.returncode != 0


@pytest.mark.parametrize("legacy_setting", [None, "1"])
def test_rust_client_executes_unsandboxed_python_through_real_handler(
    server, binary, tmp_path, monkeypatch, legacy_setting
):
    if legacy_setting is None:
        monkeypatch.delenv("BLENDER_MCP_SAFE_MODE", raising=False)
    else:
        monkeypatch.setenv("BLENDER_MCP_SAFE_MODE", legacy_setting)
    client = Client(binary, tmp_path, server=server)
    marker = tmp_path / "python-created.txt"
    results = []
    errors = []

    def call():
        try:
            for index in range(10):
                results.append(
                    client.call(
                        "execute_blender_code",
                        {
                            "code": f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsandboxed')\nprint('café 🧊 {index}')"
                        },
                    )
                )
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=call)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while worker.is_alive() and time.monotonic() < deadline:
            server._drain_command_queue()
            time.sleep(0.01)
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert not errors
        assert len(results) == 10
        assert all(not result.get("isError") for result in results), results
        assert marker.read_text() == "unsandboxed"
        assert "café 🧊 9" in json.loads(results[-1]["content"][0]["text"])["result"]
    finally:
        client.close()
