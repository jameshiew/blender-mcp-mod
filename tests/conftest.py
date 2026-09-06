"""Shared paths for the test suite.

These tests read the root addon.py as a source file (it cannot be imported
without bpy), so they need the repo root rather than the tests directory.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import ssl
import subprocess
import tempfile

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_ADDON = REPO_ROOT / "addon.py"


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


def create_credentials(binary, directory):
    subprocess.run(
        [str(binary), "setup-connection"],
        env=dict(os.environ, BLENDER_MCP_CONFIG_DIR=str(directory)),
        check=True,
        capture_output=True,
    )
    return directory


@pytest.fixture(scope="session")
def connection_credentials(binary, tmp_path_factory):
    return create_credentials(binary, tmp_path_factory.mktemp("connection"))


@pytest.fixture(autouse=True)
def configured_transport(connection_credentials, monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_CONFIG_DIR", str(connection_credentials))


def client_tls_context(directory, identity=None):
    credentials = json.loads((directory / "credentials.json").read_text())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_verify_locations(cadata=credentials["ca"])
    if identity is not False:
        identity = credentials if identity is None else identity
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            chain = Path(temporary) / "client.pem"
            chain.write_text(identity["client_cert"] + identity["client_key"])
            context.load_cert_chain(chain)
    return context
