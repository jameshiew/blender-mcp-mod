from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blender_types import SceneRegistration

import hashlib
from pathlib import Path
from typing import cast

import bpy


def run_checks(server, directory):
    server = server.execution
    directory = Path(directory)
    original = directory / "original.blend"
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    assert cube is not None
    cube.name = "Recovery.Cube"
    cast(
        bpy.types.SolidifyModifier, cube.modifiers.new("Solidify", "SOLIDIFY")
    ).thickness = 0.2
    cube["tag"] = 7
    cube.keyframe_insert(data_path="scale", frame=1)
    other = bpy.data.objects.new("Recovery.Remove", None)
    assert bpy.context.collection is not None
    bpy.context.collection.objects.link(other)
    material = bpy.data.materials.new("Recovery.Material")
    material.use_fake_user = True
    material.diffuse_color = (0.1, 0.2, 0.3, 1)
    cast(bpy.types.Mesh, cube.data).materials.append(material)
    image = bpy.data.images.new("Recovery.External", width=1, height=1)
    image.use_fake_user = True
    image.filepath = str(directory / "external.png")
    image.source = "FILE"
    assert bpy.context.scene is not None
    bpy.context.scene.frame_set(12)
    bpy.ops.wm.save_as_mainfile(filepath=str(original))
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    cast(
        "SceneRegistration", bpy.types.Scene
    ).blendermcp_server_running = bpy.props.BoolProperty(default=False)
    server.execute_code(
        "saved_object = bpy.data.objects['Recovery.Cube']", namespace="recovery"
    )
    before = server.execute_code("pass", summarize_changes=True)
    assert before["changes"]["counts"] == {"created": 0, "changed": 0, "removed": 0}, (
        before
    )
    original_scene = bpy.context.scene
    assert original_scene is not None
    other_layer = original_scene.view_layers.new("Recovery.SecondLayer")
    other_layer.update()
    switched = server.execute_code(
        "bpy.context.window.scene = bpy.data.scenes.new('Recovery.OtherScene')",
        summarize_changes=True,
    )
    assert switched["succeeded"] and switched["changes"]["counts"]["changed"] == 0, (
        switched
    )
    assert bpy.context.window is not None
    bpy.context.window.scene = original_scene
    visibility = server.execute_code(
        "bpy.data.objects['Recovery.Cube'].hide_set(True, view_layer=bpy.context.scene.view_layers['Recovery.SecondLayer'])",
        summarize_changes=True,
    )
    assert visibility["changes"]["counts"]["changed"] == 1, visibility
    assert "hidden" in visibility["changes"]["changed"]["items"][0]["changed_fields"]
    cube.hide_set(False, view_layer=other_layer)
    many = server.execute_code(
        "for i in range(214):\n    bpy.context.collection.objects.link(bpy.data.objects.new(f'Paging.{i:03}', None))",
        summarize_changes=True,
    )
    assert many["succeeded"] and many["changes"]["created"]["count"] == 214, many
    last = server.get_execution_changes(many["execution_id"], "created", offset=200)[
        "changes"
    ]
    assert len(last["items"]) == 14 and not last["has_more"], last
    for obj in list(bpy.data.objects):
        if obj.name.startswith("Paging."):
            bpy.data.objects.remove(obj, do_unlink=True)
    result = server.execute_code(
        """obj = bpy.data.objects['Recovery.Cube']
obj.name = 'Recovery.Renamed'
obj.location.x = 5
obj.data.vertices[0].co.x += 0.5
obj.modifiers[0].thickness = 0.9
obj['tag'] = 8
bpy.data.materials['Recovery.Material'].diffuse_color = (1, 0, 0, 1)
bpy.data.objects.remove(bpy.data.objects['Recovery.Remove'], do_unlink=True)
bpy.context.collection.objects.link(bpy.data.objects.new('Recovery.Created', None))
bpy.context.scene.frame_set(25)
print('partial changes')
raise ValueError('deliberate recovery test')
""",
        namespace="recovery",
        checkpoint=True,
        summarize_changes=True,
    )
    assert not result["succeeded"], result
    assert result["started"] and result["partial_changes"] is True, result
    changes = result["changes"]
    assert changes["counts"] == {"created": 1, "changed": 1, "removed": 1}, changes
    assert changes["created"]["items"][0]["name"] == "Recovery.Created"
    assert changes["removed"]["items"][0]["name"] == "Recovery.Remove"
    changed = changes["changed"]["items"][0]
    assert changed["previous_name"] == "Recovery.Cube"
    assert {
        "name",
        "properties",
        "mesh_geometry",
        "modifiers",
        "custom_properties",
    } <= set(changed["changed_fields"]), changed
    assert bpy.data.filepath == str(original)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    checkpoint_id = result["checkpoint"]["checkpoint_id"]
    restored = server.restore_checkpoint(checkpoint_id)
    assert Path(bpy.data.filepath).name.startswith("restored-")
    assert bpy.data.objects.get("Recovery.Created") is None
    assert bpy.data.objects.get("Recovery.Renamed") is None
    assert bpy.data.objects.get("Recovery.Remove") is not None
    cube = bpy.data.objects["Recovery.Cube"]
    assert cube.location.x == 0 and cube["tag"] == 7
    assert abs(cast(bpy.types.Mesh, cube.data).vertices[0].co.x + 1) < 1e-6
    assert (
        abs(cast(bpy.types.SolidifyModifier, cube.modifiers[0]).thickness - 0.2) < 1e-6
    )
    assert abs(bpy.data.materials["Recovery.Material"].diffuse_color[0] - 0.1) < 1e-6
    assert bpy.context.scene.frame_current == 12
    assert (
        Path(bpy.path.abspath(bpy.data.images["Recovery.External"].filepath)).resolve()
        == directory / "external.png"
    )
    assert not server._execution_namespaces
    assert server.get_execution_result(result["execution_id"]) == result
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    assert len(server.list_checkpoints()["checkpoints"]) == 2
    server._checkpoints = None
    assert len(server.list_checkpoints()["checkpoints"]) == 2
    assert cube.animation_data is not None
    assert cube.animation_data.action is not None
    server.restore_checkpoint(restored["safety_checkpoint"]["checkpoint_id"])
    assert bpy.data.objects.get("Recovery.Created") is not None
    assert bpy.data.objects["Recovery.Renamed"].location.x == 5
    assert bpy.context.scene.frame_current == 25
    current_path = Path(bpy.data.filepath)
    bad = server.create_checkpoint("Integrity check")
    with Path(bad["filepath"]).open("ab") as file:
        file.write(b"tampered")
    try:
        server.restore_checkpoint(bad["checkpoint_id"])
    except ValueError as error:
        assert "changed after creation" in str(error)
    else:
        raise AssertionError("Modified checkpoint accepted")
    for record in server.list_checkpoints()["checkpoints"]:
        server.delete_checkpoint(record["checkpoint_id"])
        assert not Path(record["filepath"]).exists()
    assert server.list_checkpoints()["checkpoints"] == []
    assert current_path.is_file()
    return {
        "counts": changes["counts"],
        "restore": True,
        "safety_restore": True,
        "integrity": True,
    }
