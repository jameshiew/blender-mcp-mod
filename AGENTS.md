# Version and protocol rules

- Use `Cargo.toml`'s `package.version` as the release version. Increment the
  patch for fixes, the minor version for compatible features, and the major
  version for incompatible public behavior. Preserve the `+mod` build suffix.
- Keep `addon.py`'s `ADDON_VERSION` and `pyproject.toml`'s `project.version`
  identical to the full Cargo version, including `+mod`. Update the affected
  lock files when changing a release version.
- Set `addon.py`'s `bl_info["version"]` to the release's numeric
  `(major, minor, patch)` tuple. For `1.9.1+mod`, use `(1, 9, 1)`.
- Increment `addon.py`'s `ADDON_PROTOCOL_VERSION` and `src/addon.rs`'s
  `PROTOCOL_VERSION` together when wire framing, authentication, or required
  command parameters or response fields change. Never decrease these numbers.
- Keep the protocol number unchanged for fixes and features that preserve
  the existing command and response contract. A release version change does
  not require a protocol change.
- Run `just verify` before committing version changes. Its Rust tests check
  that the bundled add-on version, numeric tuple, and protocol match the server.
