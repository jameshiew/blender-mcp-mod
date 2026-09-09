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
- Run `just verify-addon PATH_TO_BLENDER` for extension packaging changes.
