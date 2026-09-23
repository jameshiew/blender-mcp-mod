# blender-mcp-mod

Control Blender from Codex through MCP. Codex launches the Rust executable,
which connects to a Python extension running inside Blender. The executable
includes the matching extension and its Python dependencies.

## Requirements

- Blender 5.2 or later, with its GUI running.
- Rust and Cargo to build the executable.
- Codex with the `codex` CLI available.

The commands below use a macOS/Linux shell.

## Install

Clone the repository and install the executable:

```sh
git clone https://github.com/jameshiew/blender-mcp-mod.git
cd blender-mcp-mod
cargo install --locked --path . --root "$HOME/.local"
~/.local/bin/blender-mcp install-addon
codex mcp add blender -- "$HOME/.local/bin/blender-mcp"
```

If Blender is not on your `PATH`, specify its executable. For a standard macOS
installation:

```sh
~/.local/bin/blender-mcp install-addon \
  --blender /Applications/Blender.app/Contents/MacOS/Blender
```

Restart Blender and Codex. Enable **MCP for Blender** in Blender's
**Preferences > Add-ons**. Codex starts the Rust executable automatically.

The installer creates TLS credentials in `~/.blender-mcp` for mutual
authentication between Blender and the executable.

## Run

The extension starts its listener automatically on `127.0.0.1:9876`.

If it has not started, press **N** in Blender's 3D Viewport, open the
**MCP for Blender** tab, and click **Connect to MCP server**. Background mode
(`blender -b`) is not supported.

Ask Codex: "Use Blender's get_addon_status and get_scene_info, then add a red
cube and show me a viewport screenshot."

Tools support scene and material inspection, Python execution, cameras,
viewport capture, background render jobs, checkpoints, and Sketchfab imports.
Enable Sketchfab and enter its API key in the **MCP for Blender** panel to use it.

If the extension reports missing credentials, run
`~/.local/bin/blender-mcp setup-connection`, then start its listener again.

## Update

After updating the checkout, repeat the Cargo install and `install-addon`
commands, then restart Codex and Blender. Update both components together;
`get_addon_status` reports whether their versions match.

## Develop

`Cargo.toml` owns the release and protocol versions. The Cargo build generates
the extension manifest and packages its Python modules and locked wheels.

```sh
just verify
just verify-native /Applications/Blender.app/Contents/MacOS/Blender
```

The last command includes native Blender tests and requires a GUI display.
Replace the executable path for your installation.
