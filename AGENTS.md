# Version and protocol rules

- Use `Cargo.toml`'s `package.version` as the release version. Increment the
  patch for fixes, the minor version for compatible features, and the major
  version for incompatible public behavior. Preserve the `+mod` build suffix.
- `build.rs` generates the extension manifest, protocol metadata, and ZIP.
  Do not add release versions to Python sources or `pyproject.toml`.
  Update `Cargo.lock` when changing the release version.
- Increment `Cargo.toml`'s `package.metadata.blender.protocol-version` when
  wire framing, authentication, or required command parameters or response
  fields change. Never decrease this number.
- Keep the protocol number unchanged for fixes and features that preserve
  the existing command and response contract. A release version change does
  not require a protocol change.
- Run `just verify` before committing version changes. Its tests check that
  the packaged extension version, numeric tuple, and protocol match the server.
- After changing add-on dependencies, run `uv lock` and `just sync-wheels`.
  Commit the wheels selected from `uv.lock`; the build checks their hashes.

# Validation and test fixtures

- Run `just verify-native BLENDER_PATH` before committing extension packaging
  or registration changes. Supply the Blender executable path and run with
  a GUI display available. This runs all verification checks and tests,
  including native package validation, installation, and registration checks.
- Use the shared `addon` and `server_class` fixtures in `tests/conftest.py`
  for add-on unit tests. For custom scenes or package namespaces, use the
  package-loading helpers in `tests/addon_stub.py`. Extend these shared
  helpers when needed; do not duplicate Blender stubs or extract classes
  from source instead of importing the package.
