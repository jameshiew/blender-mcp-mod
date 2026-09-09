fmt:
    cargo fmt
    uv run ruff format

package-addon:
    cargo run --locked -- package-addon

sync-wheels:
    uv run --locked tools/sync_wheels.py

verify-addon blender="blender":
    addon_package="$(cargo run --locked --quiet -- package-addon)" && "{{blender}}" --command extension validate "$addon_package"

test:
    cargo test --locked
    uv run --locked pytest -q

verify:
    cargo fmt --all -- --check
    uv run --locked ruff format --check
    cargo clippy --locked --all-targets -- -D warnings
    just test
