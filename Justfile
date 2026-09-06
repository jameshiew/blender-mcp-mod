fmt:
    cargo fmt
    uv run ruff format

verify:
    cargo fmt --all -- --check
    uv run --locked ruff format --check
    cargo clippy --locked --all-targets -- -D warnings
    cargo test --locked
    uv run --locked pytest -q
