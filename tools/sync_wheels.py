import hashlib
from pathlib import Path
import tomllib
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


def main():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = lock["package"]
    project = next(
        package for package in packages if package.get("source") == {"virtual": "."}
    )
    pending = list(project["dev-dependencies"]["addon"])
    visited = set()
    directory = ROOT / "extension" / "wheels"
    directory.mkdir(exist_ok=True)
    while pending:
        dependency = pending.pop()
        if "marker" in dependency:
            raise ValueError("Conditional wheel dependencies need platform resolution")
        name = dependency["name"]
        if name in visited:
            continue
        visited.add(name)
        matches = [p for p in packages if p["name"] == name]
        if len(matches) != 1:
            raise ValueError(f"Expected one locked version of {name}")
        package = matches[0]
        pending.extend(package.get("dependencies", []))
        wheel = next(
            w for w in package["wheels"] if w["url"].endswith("-py3-none-any.whl")
        )
        target = directory / wheel["url"].rsplit("/", 1)[1]
        expected = wheel["hash"]
        if (
            target.exists()
            and "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest() == expected
        ):
            continue
        with urlopen(wheel["url"], timeout=60) as response:
            data = response.read()
        if "sha256:" + hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Checksum mismatch for {target.name}")
        target.write_bytes(data)
        print(target.name)


if __name__ == "__main__":
    main()
