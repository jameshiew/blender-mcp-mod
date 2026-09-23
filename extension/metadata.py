from __future__ import annotations

import json
import tomllib
from functools import cache
from pathlib import Path
from typing import TypedDict, cast


class AddonManifest(TypedDict):
    name: str
    version: str


@cache
def addon_metadata() -> tuple[AddonManifest, int]:
    directory = Path(__file__).parent
    with (directory / "blender_manifest.toml").open("rb") as file:
        manifest = cast(AddonManifest, tomllib.load(file))
    protocol: int = json.loads((directory / "protocol.json").read_text())["version"]
    return manifest, protocol
