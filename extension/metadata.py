import json
import tomllib
from functools import cache
from pathlib import Path


@cache
def addon_metadata():
    directory = Path(__file__).parent
    with (directory / "blender_manifest.toml").open("rb") as file:
        manifest = tomllib.load(file)
    protocol = json.loads((directory / "protocol.json").read_text())["version"]
    return manifest, protocol
