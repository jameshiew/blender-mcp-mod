from __future__ import annotations

import json
import os
import ssl
import stat
import tempfile
from pathlib import Path


def config_directory(directory: str | os.PathLike[str] | None = None) -> Path:
    return Path(
        directory
        if directory is not None
        else os.environ.get("BLENDER_MCP_CONFIG_DIR", str(Path.home() / ".blender-mcp"))
    )


def load_tls_context(directory: str | os.PathLike[str] | None = None) -> ssl.SSLContext:
    directory = config_directory(directory)
    if not directory.is_absolute():
        raise ValueError("BLENDER_MCP_CONFIG_DIR must be an absolute path")
    path = directory / "credentials.json"
    for item, is_directory in ((directory, True), (path, False)):
        metadata = item.lstat()
        valid_type = (
            stat.S_ISDIR(metadata.st_mode)
            if is_directory
            else stat.S_ISREG(metadata.st_mode)
        )
        if not valid_type:
            raise ValueError(
                f"{item} must be a regular {'directory' if is_directory else 'file'} (no symlinks)"
            )
        # ty does not narrow platform-specific APIs through os.name.
        if os.name == "posix" and (
            metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077  # ty: ignore[possibly-missing-attribute]
        ):
            raise ValueError(
                f"{item} must be owned by the current user with no group/other permissions"
            )
    with path.open(encoding="utf-8") as file:
        credentials = json.load(file)
    if credentials.get("version") != 1:
        raise ValueError("Unsupported Blender connection credentials")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=credentials["ca"])
    context.num_tickets = 0
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        chain = Path(temporary) / "server.pem"
        with chain.open("x", encoding="ascii") as file:
            file.write(credentials["server_cert"] + credentials["server_key"])
        context.load_cert_chain(chain)
    return context
