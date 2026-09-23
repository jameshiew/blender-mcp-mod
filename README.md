# blender-mcp-mod

> This README.md is written by AI

Control Blender from Codex through MCP. Codex launches the Rust executable,
which connects to a Python extension running inside Blender. The executable
includes the matching extension and its Python dependencies.

## Requirements

- Blender 5.2 LTS or later.
- Rust and Cargo to build the executable.
- Codex with the `codex` CLI available.

The commands below use a macOS/Linux shell.

Geometry Nodes inspection uses Blender 5.2's RNA input properties. Scripts
that change modifier inputs must also use this API, for example
`getattr(modifier.properties.inputs, socket.identifier).value = 1.0`.
See the [Blender 5.2 Python API changes](https://developer.blender.org/docs/release_notes/5.2/python_api/#geometry-nodes)
for attribute inputs and other migration details.

## Install

Clone the repository and install the executable:

```sh
git clone https://github.com/jameshiew/blender-mcp-mod.git
cd blender-mcp-mod
cargo install --locked --path . --root "$HOME/.local"
```

Install the bundled extension into Blender:

```sh
~/.local/bin/blender-mcp install-addon
```

If Blender is not on your `PATH`, specify its executable. For a standard macOS
installation:

```sh
~/.local/bin/blender-mcp install-addon \
  --blender /Applications/Blender.app/Contents/MacOS/Blender
```

The installer also creates local TLS credentials in `~/.blender-mcp`. Blender
and the Rust executable use these credentials to authenticate each other.

Open or restart Blender, then enable **MCP for Blender** in
**Preferences > Add-ons**.

## Connect Codex

Register the executable as a local MCP server:

```sh
codex mcp add blender -- "$HOME/.local/bin/blender-mcp"
```

Restart Codex after adding the server. Codex launches the executable
automatically; you do not need to run it in a separate terminal.

See the [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
for configuration options.

## Run

Keep Blender open with its GUI running. The extension starts its listener
automatically by default, on `127.0.0.1:9876`.

If it has not started, press **N** in Blender's 3D Viewport, open the
**MCP for Blender** tab, and click **Connect to MCP server**. Background mode
(`blender -b`) is not supported.

Ask Codex to check the connection:

> Use Blender's get_addon_status, then get_scene_info.

Once that succeeds, ask Codex to create or edit your scene. For example:

> Add a red cube and show me a viewport screenshot.

If the extension reports missing credentials, run
`~/.local/bin/blender-mcp setup-connection`, then start its listener again.

## Update

Upgrade to Blender 5.2 or later before installing this version of the extension.

After updating the checkout, repeat the Cargo install and `install-addon`
commands, then restart Codex and Blender. Update both components together;
`get_addon_status` reports whether their versions match.
