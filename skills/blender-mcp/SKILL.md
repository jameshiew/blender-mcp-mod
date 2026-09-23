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

## Animation renders

When available, use `start_animation_render` for a PNG sequence. Choose a new
absolute output directory and a frame range. Poll `get_render_status`; use
`frames_completed` and `frames_total` for overall progress. Engine sample
counts apply only to the current render stage.

Use `get_render_image` with `frame` to inspect completed frames while rendering.
`cancel_render` retains completed frames and the original scene snapshot.
Use `resume_animation_render` with the output directory after an interruption,
including a server restart. Resume verifies saved frames and renders missing or
changed frames again. Start a new job to include later scene edits.

The sequence contains images only. The status field `fps` gives its playback
rate, including the frame step. Assemble video and audio separately when
requested. External assets and simulation caches must remain available on disk.

## Blender 5.2 compositor

In Blender 5.2, glare settings are input sockets. Inspect `node.inputs` before
editing a node from a recipe for an older Blender release. For example, set
`node.inputs["Type"].default_value = "Fog Glow"`; do not assign `glare_type`.

For a new scene that has no compositor, this creates a glow pass:

```python
scene = bpy.context.scene
tree = bpy.data.node_groups.new("Optical glow", "CompositorNodeTree")
tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
layers = tree.nodes.new("CompositorNodeRLayers")
layers.scene = scene
glow = tree.nodes.new("CompositorNodeGlare")
glow.inputs["Type"].default_value = "Fog Glow"
glow.inputs["Quality"].default_value = "Medium"
glow.inputs["Threshold"].default_value = 1.6
glow.inputs["Strength"].default_value = 0.32
output = tree.nodes.new("NodeGroupOutput")
tree.links.new(layers.outputs["Image"], glow.inputs["Image"])
tree.links.new(glow.outputs["Image"], output.inputs["Image"])
scene.compositing_node_group = tree
```

Inspect and edit an existing `scene.compositing_node_group` instead of replacing
it. Verify the result with a camera render; the material viewport does not show
the final compositor output.
