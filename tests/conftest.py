"""Shared paths for the test suite."""

from __future__ import annotations

from pathlib import Path
import json
import os
import ssl
import subprocess
import tempfile
import tomllib
import zipfile

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_ADDON = REPO_ROOT / "extension" / "__init__.py"
CARGO_PACKAGE = tomllib.loads((REPO_ROOT / "Cargo.toml").read_text())["package"]
RELEASE_VERSION = CARGO_PACKAGE["version"]
RELEASE_TUPLE = [
    int(part) for part in RELEASE_VERSION.split("+")[0].split("-")[0].split(".")
]
PROTOCOL_VERSION = CARGO_PACKAGE["metadata"]["blender"]["protocol-version"]


@pytest.fixture(scope="session")
def addon_package(binary, tmp_path_factory):
    path = tmp_path_factory.mktemp("package") / "addon.zip"
    subprocess.run(
        [str(binary), "package-addon", "--output", str(path)],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(scope="session")
def unpacked_addon(addon_package, tmp_path_factory):
    directory = tmp_path_factory.mktemp("extension")
    with zipfile.ZipFile(addon_package) as archive:
        archive.extractall(directory)
    return directory


def blender_environment(directory):
    environment = dict(os.environ, BLENDER_USER_RESOURCES=str(directory))
    for suffix in ("CONFIG", "SCRIPTS", "EXTENSIONS", "DATAFILES"):
        environment["BLENDER_USER_" + suffix] = str(directory / suffix.lower())
    return environment


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
