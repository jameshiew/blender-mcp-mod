fmt:
    cargo fmt
    uv run ruff format

package-addon:
    cargo run --locked -- package-addon

sync-wheels:
    uv run --locked tools/sync_wheels.py

verify-addon blender="blender":
    addon_package="$(cargo run --locked --quiet -- package-addon)" && "{{blender}}" --command extension validate "$addon_package"

verify-native $blender:
    BLENDER_TEST_EXECUTABLE="${blender:?Pass the path to the Blender executable}" just verify

test:
    cargo test --locked
    uv run --locked pytest -q

typecheck:
    uv run --locked ty check

verify:
    cargo fmt --all -- --check
    uv run --locked ruff format --check
    uv run --locked ruff check
    just typecheck
    cargo clippy --locked --all-targets -- -D warnings
    just test

pin-actions:
    pinact run --verify-comment

check-actions:
    pinact run --check --verify-comment
