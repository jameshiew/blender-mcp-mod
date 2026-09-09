---
name: blender-mcp
description: Create and edit Blender scenes through the Blender MCP tools, with task-specific Python state and visual verification.
---

# Blender MCP

Check `get_addon_status` when connecting, then inspect the scene before changing it.
Use the tools advertised by the connected server; integrations vary by installation.

## Python state

When `execute_blender_code` offers `namespace`, choose a unique ID for the task
(for example, a UUID) and reuse it across calls. Imports, variables, and helper
functions persist within that ID. Omit `namespace` for a fresh namespace on each
call. Older installations without this parameter require self-contained code.

Different IDs separate Python globals, not Blender scenes, data, or imported
modules. The same ID shares globals across clients. Use task-specific object or
collection names when several tasks share Blender, and inspect current state
before editing. A stored object reference can become invalid after deletion,
undo, or loading another file.

Use `reset_namespace: true` with the task's ID to start its Python state over.
Send empty `code` with that option when finished to release stored variables.
Reset does not undo scene changes or cancel callbacks registered with Blender.
State is lost when the add-on server is recreated and is not saved in `.blend`
files.

## Execution and verification

Execute manageable steps and inspect the results. After an error or timeout,
check the scene before retrying: earlier statements may already have run, and
a timed-out command may still be running.

Use `get_viewport_screenshot` to verify geometry and framing. For a rendered
deliverable, inspect the saved render too. Save the `.blend` file to the agreed
location and report its path. Credit imported assets when their license requires
attribution.
