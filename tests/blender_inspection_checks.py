from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blender_types import ColorSpace, IDCollection, NodeInterface

    from extension.inspection import ModifierProperties

import json
from typing import cast

import bpy


def run_checks(server):
    scene = bpy.context.scene
    assert bpy.context.view_layer is not None
    assert scene is not None
    frame = (scene.frame_current, scene.frame_subframe)
    selection = list(bpy.context.selected_objects or ())
    active = bpy.context.view_layer.objects.active
    original_compositor = scene.compositing_node_group
    groups = [
        bpy.data.objects,
        bpy.data.meshes,
        bpy.data.materials,
        bpy.data.node_groups,
        bpy.data.images,
        bpy.data.cameras,
        bpy.data.actions,
    ]
    original = [set(group) for group in groups]
    try:
        mesh = bpy.data.meshes.new("Inspection.Mesh")
        mesh.from_pydata([(0, 0, 0)], [], [])
        obj = bpy.data.objects.new("Inspection.Object", mesh)
        scene.collection.objects.link(obj)
        target = bpy.data.objects.new("Inspection.Target", None)
        scene.collection.objects.link(target)
        material = bpy.data.materials.new("Inspection.Material")
        material.use_nodes = True
        mesh.materials.append(material)
        mesh.materials.append(None)
        assert material.node_tree is not None
        shader = next(
            node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        cast(bpy.types.NodeSocketFloat, shader.inputs["Metallic"]).default_value = 0.75
        cast(bpy.types.NodeSocketFloat, shader.inputs["Roughness"]).default_value = 0.2
        shader.inputs["Roughness"].keyframe_insert("default_value", frame=1)
        cast(bpy.types.NodeSocketFloat, shader.inputs["Roughness"]).default_value = 0.8
        shader.inputs["Roughness"].keyframe_insert("default_value", frame=120)
        ramp = cast(
            bpy.types.ShaderNodeValToRGB,
            material.node_tree.nodes.new("ShaderNodeValToRGB"),
        )
        assert ramp.color_ramp is not None
        assert ramp.outputs is not None
        ramp.color_ramp.elements[0].color = (1, 0.1, 0.2, 1)
        material.node_tree.links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
        texture = cast(
            bpy.types.ShaderNodeTexImage,
            material.node_tree.nodes.new("ShaderNodeTexImage"),
        )
        image = bpy.data.images.new("Inspection.Image", width=1, height=1)
        assert image.colorspace_settings is not None
        cast("ColorSpace", image.colorspace_settings).name = "Non-Color"
        texture.image = image
        group = cast(
            bpy.types.ShaderNodeTree,
            bpy.data.node_groups.new("Inspection.ShaderGroup", "ShaderNodeTree"),
        )
        cast("NodeInterface", group.interface).new_socket(
            name="Factor", in_out="INPUT", socket_type="NodeSocketFloat"
        )
        cast(
            bpy.types.NodeSocketFloat, group.nodes.new("ShaderNodeValue").outputs[0]
        ).default_value = 0.42
        nested = cast(
            bpy.types.ShaderNodeGroup, material.node_tree.nodes.new("ShaderNodeGroup")
        )
        nested.node_tree = group
        array = cast(bpy.types.ArrayModifier, obj.modifiers.new("Copies", "ARRAY"))
        array.count = 7
        array.relative_offset_displace = (2, 0, 0)
        array.offset_object = target
        geo = bpy.data.node_groups.new("Inspection.Geometry", "GeometryNodeTree")
        cast("NodeInterface", geo.interface).new_socket(
            name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry"
        )
        cast("NodeInterface", geo.interface).new_socket(
            name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        factor = cast("NodeInterface", geo.interface).new_socket(
            name="Height", in_out="INPUT", socket_type="NodeSocketFloat"
        )
        layer_socket = cast("NodeInterface", geo.interface).new_socket(
            name="Selection", in_out="INPUT", socket_type="NodeSocketBool"
        )
        output_socket = cast("NodeInterface", geo.interface).new_socket(
            name="Weight", in_out="OUTPUT", socket_type="NodeSocketFloat"
        )
        node_mod = cast(
            bpy.types.NodesModifier, obj.modifiers.new("Procedural", "NODES")
        )
        node_mod.node_group = geo
        input_property = getattr(
            cast("ModifierProperties", node_mod.properties).inputs, factor.identifier
        )
        input_property.value = 3.75
        input_property.type = "ATTRIBUTE"
        input_property.attribute_name = "height"
        layer_property = getattr(
            cast("ModifierProperties", node_mod.properties).inputs,
            layer_socket.identifier,
        )
        layer_property.type = "LAYER"
        layer_property.layer_name = "Ink"
        output_property = getattr(
            cast("ModifierProperties", node_mod.properties).outputs,
            output_socket.identifier,
        )
        output_property.attribute_name = "weight"

        compositor = bpy.data.node_groups.new(
            "Inspection.Compositor", "CompositorNodeTree"
        )
        scene.compositing_node_group = compositor
        compositor_value = compositor.nodes.new("ShaderNodeValue")
        compositor_value.outputs[0].keyframe_insert("default_value", frame=1)

        first = geo.nodes.new("GeometryNodeMeshCube")
        second = geo.nodes.new("GeometryNodeMeshUVSphere")
        join = geo.nodes.new("GeometryNodeJoinGeometry")
        geo.links.new(first.outputs["Mesh"], join.inputs["Geometry"])
        geo.links.new(second.outputs["Mesh"], join.inputs["Geometry"])
        action = bpy.data.actions.new("Inspection.SharedAction")
        own_slot = action.slots.new("OBJECT", obj.name)
        other_slot = action.slots.new("OBJECT", target.name)
        layer = action.layers.new("Layer")
        strip = cast(bpy.types.ActionKeyframeStrip, layer.strips.new(type="KEYFRAME"))
        own_curve = strip.channelbag(own_slot, ensure=True).fcurves.new(
            "location", index=0
        )
        own_curve.keyframe_points.insert(1, 0).interpolation = "LINEAR"
        own_curve.keyframe_points.insert(120, 10).interpolation = "BEZIER"
        own_curve.modifiers.new("CYCLES")
        other_curve = strip.channelbag(other_slot, ensure=True).fcurves.new(
            "scale", index=2
        )
        other_curve.keyframe_points.insert(9, 2)
        other_curve.keyframe_points.insert(20, 4)
        other_curve.convert_to_samples(start=9, end=21)
        obj.animation_data_create().action = action
        assert obj.animation_data is not None
        obj.animation_data.action_slot = own_slot
        target.animation_data_create().action = action
        assert target.animation_data is not None
        target.animation_data.action_slot = other_slot
        driver = cast(bpy.types.FCurve, obj.driver_add("rotation_euler", 2)).driver
        assert driver is not None
        driver.expression = "source * 2"
        variable = driver.variables.new()
        variable.name = "source"
        variable.type = "SINGLE_PROP"
        variable.targets[0].id = target
        variable.targets[0].data_path = "location.x"
        track = obj.animation_data.nla_tracks.new()
        track.name = "Motion"
        nla_action = action.copy()
        nla = track.strips.new("Repeat", 10, nla_action)
        nla.action_slot = nla_action.slots[0]
        nla.repeat = 2
        nla.scale = 1.5
        nla.mute = True
        obj.shape_key_add(name="Basis")
        key = obj.shape_key_add(name="Grow")
        key.value = 0.2
        key.keyframe_insert("value", frame=5)
        camera_data = bpy.data.cameras.new("Inspection.CameraData")
        camera = bpy.data.objects.new("Inspection.Camera", camera_data)
        scene.collection.objects.link(camera)
        camera_data.keyframe_insert("lens", frame=7)

        basic = server.handlers["get_object_info"](obj.name)
        detailed = server.handlers["get_object_info"](obj.name, details=True)
        assert "details" not in basic
        assert {
            key: value for key, value in detailed.items() if key != "details"
        } == basic
        slots = detailed["details"]["material_slots"]
        assert slots["count"] == 2 and slots["items"][1]["material"] is None
        assert slots["items"][0]["material"]["name"] == material.name
        settings = server.handlers["get_modifier_info"](
            object_name=obj.name, modifier_name="Copies"
        )["settings"]["values"]
        assert settings["count"] == 7 and settings["relative_offset_displace"] == [
            2,
            0,
            0,
        ]
        assert settings["offset_object"]["name"] == target.name
        inputs = server.handlers["get_modifier_info"](
            object_name=obj.name, modifier_name="Procedural"
        )["inputs"]["items"]
        height = next(item for item in inputs if item["name"] == "Height")
        assert (
            height["value"] == 3.75
            and height.get("type", "ATTRIBUTE") == "ATTRIBUTE"
            and height["attribute_name"] == "height"
        )
        selection_input = next(item for item in inputs if item["name"] == "Selection")
        assert (
            selection_input["type"] == "LAYER"
            and selection_input["layer_name"] == "Ink"
        ), selection_input
        output_info = server.handlers["get_modifier_info"](
            object_name=obj.name, modifier_name="Procedural"
        )["outputs"]["items"]
        assert (
            next(item for item in output_info if item["name"] == "Weight")[
                "attribute_name"
            ]
            == "weight"
        ), output_info
        geometry_group = server.handlers["get_node_group_info"](
            node_group_name=geo.name
        )
        join_info = next(
            node
            for node in geometry_group["nodes"]["items"]
            if node["name"] == join.name
        )
        assert join_info["inputs"]["items"][0]["is_multi_input"], join_info
        join_links = [
            link
            for link in geometry_group["incoming_links"]["items"]
            if link["to_node"] == join.name
        ]
        assert len({link["multi_input_sort_id"] for link in join_links}) == 2, (
            join_links
        )
        compositor_animation = server.handlers["get_animation_info"](
            data_name=scene.name, data_type="COMPOSITOR_NODES"
        )
        assert compositor_animation["counts"]["ACTION_CHANNEL"] == 1, (
            compositor_animation
        )
        material_result = server.handlers["get_material_info"](
            material_name=material.name, limit=100
        )
        nodes = {
            node["name"]: node
            for node in material_result["node_tree"]["nodes"]["items"]
        }
        socket = next(
            item
            for item in nodes[shader.name]["inputs"]["items"]
            if item["name"] == "Metallic"
        )
        assert abs(socket["default_value"] - 0.75) < 1e-6
        assert nodes[texture.name]["image"]["colorspace"] == "Non-Color"
        assert nodes[nested.name]["node_group"]["name"] == group.name
        assert nodes[ramp.name]["color_ramp"]["elements"]["count"] == 2
        assert any(
            link["from_node"] == ramp.name and link["to_node"] == shader.name
            for link in material_result["node_tree"]["incoming_links"]["items"]
        )
        offset, names = 0, []
        while offset is not None:
            page = server.handlers["get_material_info"](
                material_name=material.name, offset=offset, limit=2
            )["node_tree"]["nodes"]
            names.extend(node["name"] for node in page["items"])
            assert page["has_more"] == (page["next_offset"] is not None), page
            assert "truncated" not in page
            offset = page["next_offset"]
        assert names == sorted(nodes)
        node_group = server.handlers["get_node_group_info"](node_group_name=group.name)
        assert node_group["interface"]["items"][0]["name"] == "Factor"
        assert (
            abs(
                node_group["nodes"]["items"][0]["outputs"]["items"][0]["default_value"]
                - 0.42
            )
            < 1e-6
        )
        result = server.handlers["get_animation_info"](data_name=obj.name, limit=100)
        entries = result["entries"]["items"]
        channels = [entry for entry in entries if entry["kind"] == "ACTION_CHANNEL"]
        assert len(channels) == 1 and channels[0]["data_path"] == "location", result
        assert channels[0]["frame_range"] == [1, 120]
        assert channels[0]["interpolation_counts"] == {"BEZIER": 1, "LINEAR": 1}
        assert channels[0]["modifiers"]["items"][0]["type"] == "CYCLES"
        driver_result = next(
            entry["driver"] for entry in entries if entry["kind"] == "DRIVER"
        )
        assert driver_result["expression"] == "source * 2"
        assert (
            driver_result["variables"]["items"][0]["targets"][0]["values"]["id"]["name"]
            == target.name
        )
        nla_result = next(entry for entry in entries if entry["kind"] == "NLA_STRIP")
        assert (
            nla_result["repeat"] == 2
            and nla_result["scale"] == 1.5
            and nla_result["mute"]
        )
        assert all(
            entry["data_path"] == "location"
            for entry in entries
            if entry["kind"] == "NLA_ACTION_CHANNEL"
        )
        other = server.handlers["get_animation_info"](data_name=target.name)["entries"][
            "items"
        ]
        assert len(other) == 1 and other[0]["data_path"] == "scale"
        assert other[0]["sample_count"] == 12 and other[0]["keyframe_count"] == 0, other
        assert other[0]["frame_range"] == [9, 20]
        pages, offset = [], 0
        while offset is not None:
            page = server.handlers["get_animation_info"](
                data_name=obj.name, offset=offset, limit=2
            )["entries"]
            pages.extend(page["items"])
            assert page["has_more"] == (page["next_offset"] is not None), page
            assert "truncated" not in page
            offset = page["next_offset"]
        assert pages == entries
        assert (
            server.handlers["get_animation_info"](
                data_name=obj.name, data_type="SHAPE_KEYS"
            )["counts"]["ACTION_CHANNEL"]
            == 1
        )
        assert (
            server.handlers["get_animation_info"](
                data_name=camera.name, data_type="OBJECT_DATA"
            )["entries"]["items"][0]["data_path"]
            == "lens"
        )
        assert (
            server.handlers["get_animation_info"](
                data_name=material.name, data_type="MATERIAL"
            )["entry_count"]
            == 0
        )
        shader_anim = server.handlers["get_animation_info"](
            data_name=material.name, data_type="MATERIAL_NODES"
        )
        assert shader_anim["counts"]["ACTION_CHANNEL"] == 1
        assert shader_anim["entries"]["items"][0]["data_path"].endswith(
            ".default_value"
        )
        assert (
            server.handlers["get_animation_info"](
                data_name=group.name, data_type="NODE_GROUP"
            )["entry_count"]
            == 0
        )
        json.dumps([detailed, material_result, node_group, result], allow_nan=False)
        assert frame == (scene.frame_current, scene.frame_subframe)
        assert list(bpy.context.selected_objects or ()) == selection
        assert bpy.context.view_layer.objects.active == active
        return {
            "material_nodes": len(nodes),
            "animation_entries": len(entries),
            "slot_isolation": True,
            "geometry_node_inputs": True,
            "read_only": True,
        }
    finally:
        scene.compositing_node_group = original_compositor
        for group, before in zip(groups, original):
            for item in set(group) - before:
                cast("IDCollection", group).remove(item, do_unlink=True)
