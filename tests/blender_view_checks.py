def run_checks(server, viewport=False):
    import base64
    import math
    from itertools import product

    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Matrix, Vector

    scene = bpy.context.scene
    original_camera = scene.camera
    render = scene.render
    render_fields = ("resolution_x", "resolution_y", "pixel_aspect_x", "pixel_aspect_y")
    render_state = {field: getattr(render, field) for field in render_fields}
    data_groups = (
        bpy.data.objects,
        bpy.data.collections,
        bpy.data.meshes,
        bpy.data.cameras,
    )
    original_data = [set(group) for group in data_groups]
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    view_state = None
    if viewport:
        area = next(area for area in bpy.context.screen.areas if area.type == "VIEW_3D")
        space = area.spaces.active
        r3d = space.region_3d
        view_state = {
            "view_rotation": r3d.view_rotation.copy(),
            "view_location": r3d.view_location.copy(),
            "view_distance": r3d.view_distance,
            "view_perspective": r3d.view_perspective,
            "view_camera_zoom": r3d.view_camera_zoom,
        }
        other_view_state = (
            space.shading.type,
            space.overlay.show_overlays,
            space.show_gizmo,
            space.lock_camera,
        )

    def failure(method, **options):
        try:
            method(**options)
        except ValueError:
            return
        raise AssertionError(f"Expected validation failure: {options}")

    def corners(obj):
        bounds = server.get_object_info(obj.name, evaluated=True)["evaluated"][
            "world_bounding_box"
        ]
        return [Vector(point) for point in product(*zip(*bounds))]

    try:
        collection = bpy.data.collections.new("ViewCheck.Source")
        mesh = bpy.data.meshes.new("ViewCheck.Mesh")
        mesh.from_pydata(
            list(product((-1, 1), repeat=3)), [], [(0, 1, 3, 2), (4, 6, 7, 5)]
        )
        obj = bpy.data.objects.new("ViewCheck.Mesh", mesh)
        collection.objects.link(obj)
        obj.modifiers.new("Array", "ARRAY").count = 3
        instancer = bpy.data.objects.new("ViewCheck.Instance", None)
        instancer.instance_type = "COLLECTION"
        instancer.instance_collection = collection
        instancer.location = (5, -2, 3)
        scene.collection.objects.link(instancer)
        camera = bpy.data.objects.new(
            "ViewCheck.Camera", bpy.data.cameras.new("ViewCheck.Camera")
        )
        scene.collection.objects.link(camera)
        camera.location = (12, -12, 10)
        camera.rotation_euler = (
            (Vector((5, -2, 3)) - camera.location).to_track_quat("-Z", "Y").to_euler()
        )
        parent = bpy.data.objects.new("ViewCheck.Parent", None)
        scene.collection.objects.link(parent)
        parent.matrix_world = Matrix.Translation((4, 3, -2)) @ Matrix.Rotation(
            0.2, 4, "Z"
        )
        bpy.context.view_layer.update()
        world = camera.matrix_world.copy()
        camera.parent = parent
        camera.matrix_world = world
        bpy.context.view_layer.update()
        rotation = camera.matrix_world.to_quaternion()
        targets = [instancer.name]
        for projection in ("PERSP", "ORTHO"):
            for width, height, pixel_x, pixel_y in (
                (800, 400, 1, 1),
                (400, 800, 1, 1),
                (600, 400, 1, 2),
            ):
                render.resolution_x, render.resolution_y = width, height
                render.pixel_aspect_x, render.pixel_aspect_y = pixel_x, pixel_y
                result = server.set_camera(
                    object_name=camera.name,
                    projection=projection,
                    lens=65,
                    shift_x=0.15,
                    shift_y=-0.1,
                    object_names=targets,
                    padding=1.15,
                    make_active=True,
                )
                assert result["camera"]["projection"] == projection, result
                assert scene.camera == camera
                assert result["framed_objects"] == targets
                assert (
                    camera.matrix_world.to_quaternion()
                    .rotation_difference(rotation)
                    .angle
                    < 0.001
                )
                for point in corners(instancer):
                    projected = world_to_camera_view(scene, camera, point)
                    assert (
                        0 <= projected.x <= 1
                        and 0 <= projected.y <= 1
                        and projected.z > 0
                    ), (projection, width, height, projected[:], result)
        inspected = server.get_object_info(camera.name)["camera"]
        assert inspected["sensor_fit"] == "AUTO" and inspected["is_active"]
        assert inspected["lens"] == 65 and inspected["dof"]["focus_object"] is None
        before = server.get_object_info(camera.name)
        for options in (
            {"lens": True},
            {"lens": 0},
            {"shift_x": math.nan},
            {"clip_start": 100, "clip_end": 10},
            {"object_names": ["Missing"]},
            {"object_names": [camera.name]},
            {"object_names": []},
        ):
            failure(server.set_camera, object_name=camera.name, **options)
            assert server.get_object_info(camera.name) == before
        constraint = camera.constraints.new("COPY_LOCATION")
        failure(
            server.set_camera, object_name=camera.name, object_names=targets, lens=30
        )
        assert camera.data.lens == 65
        camera.constraints.remove(constraint)
        server.set_camera(
            object_name=camera.name,
            projection="PERSP",
            lens=40,
            shift_x=0,
            shift_y=0,
            object_names=targets,
        )

        if viewport:
            original_selection = [o.name for o in bpy.context.selected_objects]
            original_transform = camera.matrix_world.copy()
            space.lock_camera = True
            instancer.hide_set(True)
            failure(server.set_viewport, object_names=targets)
            instancer.hide_set(False)
            for view in ("FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM", "ISO"):
                result = server.set_viewport(
                    view=view,
                    object_names=targets,
                    shading="SOLID",
                    overlays=False,
                    gizmos=False,
                )
                assert result["projection"] == (
                    "PERSP" if view == "ISO" else "ORTHO"
                ), result
                assert result["framed_objects"] == targets
                assert [
                    o.name for o in bpy.context.selected_objects
                ] == original_selection
                assert camera.matrix_world == original_transform
                for point in corners(instancer):
                    clip = r3d.perspective_matrix @ point.to_4d()
                    assert (
                        clip.w > 0
                        and abs(clip.x / clip.w) < 1
                        and abs(clip.y / clip.w) < 1
                    ), (view, clip[:], result)
            server.set_viewport(frame="ALL")
            for selected_obj in bpy.context.selected_objects:
                selected_obj.select_set(False)
            instancer.select_set(True)
            assert server.set_viewport(frame="SELECTED")["framed_objects"] == targets
            for shading in ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED"):
                assert server.set_viewport(shading=shading)["shading"] == shading
            server.set_viewport(shading="SOLID", view="CAMERA", camera_zoom=15)
            assert camera.matrix_world == original_transform
            state = server.set_viewport()
            for options in (
                {"viewport_index": 999},
                {"camera_zoom": 601},
                {"distance": 2},
                {"view": "CAMERA", "frame": "ALL"},
                {"frame": "ALL", "object_names": targets},
                {"view": "ISO", "object_names": ["Missing"]},
                {"overlays": 1},
            ):
                failure(server.set_viewport, **options)
                assert server.set_viewport() == state
            image_count = len(bpy.data.images)
            capture = server.get_viewport_screenshot(max_size=256, camera_only=True)
            assert capture.get("success"), capture
            assert (capture["width"], capture["height"]) == (192, 256), capture
            assert (
                capture["capture_mode"] == "CAMERA" and capture["camera"] == camera.name
            )
            assert base64.b64decode(capture["image_data"]).startswith(b"\x89PNG")
            assert server.set_viewport() == state
            assert len(bpy.data.images) == image_count
            scene.camera = None
            missing_camera = server.get_viewport_screenshot(camera_only=True)
            assert (
                "error" in missing_camera
                and "active PERSP or ORTHO" in missing_camera["error"]
            ), missing_camera
            scene.camera = camera
            normal = server.get_viewport_screenshot(max_size=256)
            assert normal.get("success"), normal
            assert normal["capture_mode"] == "VIEWPORT"
            return {
                "camera": inspected,
                "capture": {k: v for k, v in capture.items() if k != "image_data"},
            }
        failure(server.set_viewport)
        return {"camera": inspected}
    finally:
        scene.camera = original_camera
        for field, value in render_state.items():
            setattr(render, field, value)
        for group, original in zip(data_groups, original_data):
            for item in set(group) - original:
                group.remove(item)
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        if view_state is not None:
            for field, value in view_state.items():
                setattr(r3d, field, value)
            (
                space.shading.type,
                space.overlay.show_overlays,
                space.show_gizmo,
                space.lock_camera,
            ) = other_view_state
            r3d.update()
        bpy.context.view_layer.update()
