def run_checks(server):
    import math

    import bpy
    from mathutils import Matrix

    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    data_groups = (
        bpy.data.objects,
        bpy.data.collections,
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.node_groups,
        bpy.data.actions,
    )
    original_data = [set(group) for group in data_groups]

    def inspect(obj):
        return server.get_object_info(obj.name, evaluated=True)["evaluated"]

    def cube(name, collection):
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(
            [
                (-1, -1, -1),
                (-1, -1, 1),
                (-1, 1, -1),
                (-1, 1, 1),
                (1, -1, -1),
                (1, -1, 1),
                (1, 1, -1),
                (1, 1, 1),
            ],
            [],
            [
                (0, 4, 6, 2),
                (1, 3, 7, 5),
                (0, 1, 5, 4),
                (2, 6, 7, 3),
                (0, 2, 3, 1),
                (4, 5, 7, 6),
            ],
        )
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
        return obj

    def instance(name, source, collection):
        obj = bpy.data.objects.new(name, None)
        obj.instance_type = "COLLECTION"
        obj.instance_collection = source
        collection.objects.link(obj)
        return obj

    def close_bounds(actual, expected):
        assert actual is not None, expected
        assert all(
            abs(a - b) < 1e-5
            for row, other in zip(actual, expected)
            for a, b in zip(row, other)
        ), (actual, expected)

    try:
        collection = bpy.data.collections.new("GeometryCheck.Scene")
        scene.collection.children.link(collection)
        obj = cube("GeometryCheck.Array", collection)
        array = obj.modifiers.new("Array", "ARRAY")
        array.count = 2
        base = server.get_object_info(obj.name)
        assert base["mesh"] == {"vertices": 8, "edges": 12, "polygons": 6}, base
        assert "evaluated" not in base
        result = inspect(obj)
        assert result["mesh"] == {"vertices": 16, "edges": 24, "polygons": 12}, result
        assert result["mesh_including_instances"] == result["mesh"], result
        assert result["depsgraph_mode"] == "VIEWPORT", result
        close_bounds(result["world_bounding_box"], [[-1, -1, -1], [3, 1, 1]])
        array.show_viewport = False
        assert inspect(obj)["mesh"]["vertices"] == 8
        array.show_viewport = True
        array.count = 2
        array.keyframe_insert("count", frame=1)
        array.count = 3
        array.keyframe_insert("count", frame=2)
        scene.frame_set(1)
        assert inspect(obj)["mesh"]["vertices"] == 16
        scene.frame_set(2)
        animated = inspect(obj)
        assert animated["mesh"]["vertices"] == 24 and animated["frame"] == 2, animated

        source = bpy.data.collections.new("GeometryCheck.Source")
        source.instance_offset = (1, 0, 0)
        plant = cube("GeometryCheck.Plant", source)
        plant.location = (2, 0, 0)
        plant.modifiers.new("Array", "ARRAY").count = 2
        nested = bpy.data.collections.new("GeometryCheck.Nested")
        inner = instance("GeometryCheck.Inner", source, nested)
        inner.location = (0, 4, 0)
        inner2 = instance("GeometryCheck.Inner2", source, nested)
        inner2.location = (0, -4, 0)
        outer = instance("GeometryCheck.Outer", nested, collection)
        outer.matrix_world = (
            Matrix.Translation((10, 0, 0))
            @ Matrix.Rotation(math.pi / 2, 4, "Z")
            @ Matrix.Diagonal((-2, 3, 1, 1))
        )
        unrelated = instance("GeometryCheck.Unrelated", source, collection)
        unrelated.location = (100, 100, 100)
        outer_info = server.get_object_info(outer.name, evaluated=True)
        assert outer_info["dimensions"] == [0, 0, 0], outer_info
        instanced = outer_info["evaluated"]
        assert instanced["mesh"] is None, instanced
        assert instanced["mesh_including_instances"] == {
            "vertices": 32,
            "edges": 48,
            "polygons": 24,
        }, instanced
        assert instanced["instances"]["count"] == 4, instanced
        assert {s["name"]: s["count"] for s in instanced["instances"]["sources"]} == {
            inner.name: 1,
            inner2.name: 1,
            plant.name: 2,
        }, instanced
        plant_info = next(
            s for s in instanced["instances"]["sources"] if s["name"] == plant.name
        )
        assert plant_info == {
            "name": plant.name,
            "library": None,
            "type": "MESH",
            "count": 2,
            "mesh": {"vertices": 16, "edges": 24, "polygons": 12},
        }, plant_info
        assert instanced["instance_collection"] == nested.name
        close_bounds(instanced["world_bounding_box"], [[-5, -8, -1], [25, 0, 1]])

        points = bpy.data.meshes.new("GeometryCheck.Points")
        points.from_pydata([(0, 0, 0), (5, 0, 0)], [], [])
        emitter = bpy.data.objects.new("GeometryCheck.Nodes", points)
        collection.objects.link(emitter)
        group = bpy.data.node_groups.new("GeometryCheck.Instances", "GeometryNodeTree")
        group.interface.new_socket(
            name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry"
        )
        group.interface.new_socket(
            name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        inputs = group.nodes.new("NodeGroupInput")
        outputs = group.nodes.new("NodeGroupOutput")
        source_node = group.nodes.new("GeometryNodeObjectInfo")
        source_node.inputs["Object"].default_value = plant
        source_node.inputs["As Instance"].default_value = True
        instances = group.nodes.new("GeometryNodeInstanceOnPoints")
        group.links.new(inputs.outputs["Geometry"], instances.inputs["Points"])
        group.links.new(source_node.outputs["Geometry"], instances.inputs["Instance"])
        group.links.new(instances.outputs["Instances"], outputs.inputs["Geometry"])
        emitter.modifiers.new("Instances", "NODES").node_group = group
        nodes = inspect(emitter)
        assert nodes["mesh"] == {"vertices": 0, "edges": 0, "polygons": 0}, nodes
        assert nodes["instances"]["count"] == 2, nodes
        assert nodes["mesh_including_instances"]["vertices"] == 32, nodes
        assert nodes["instances"]["sources"][0]["name"] == plant.name, nodes
        assert len(nodes["instances"]["sources"]) == 1, nodes
        assert nodes["instances"]["sources"][0]["count"] == 2, nodes
        close_bounds(nodes["world_bounding_box"], [[-1, -1, -1], [8, 1, 1]])
        realize = group.nodes.new("GeometryNodeRealizeInstances")
        group.links.new(instances.outputs["Instances"], realize.inputs["Geometry"])
        group.links.new(realize.outputs["Geometry"], outputs.inputs["Geometry"])
        realized = inspect(emitter)
        assert realized["mesh"]["vertices"] == 32, realized
        assert realized["instances"]["count"] == 0, realized
        assert realized["mesh_including_instances"] == realized["mesh"], realized
        close_bounds(realized["world_bounding_box"], nodes["world_bounding_box"])

        primitive = group.nodes.new("GeometryNodeMeshCube")
        primitive.inputs["Size"].default_value = (2, 2, 2)
        group.links.new(primitive.outputs["Mesh"], instances.inputs["Instance"])
        group.links.new(instances.outputs["Instances"], outputs.inputs["Geometry"])
        procedural = inspect(emitter)
        assert procedural["mesh"]["vertices"] == 0, procedural
        assert procedural["instances"]["count"] == 2, procedural
        assert procedural["mesh_including_instances"]["vertices"] == 16, procedural
        close_bounds(procedural["world_bounding_box"], [[-1, -1, -1], [6, 1, 1]])
        join = group.nodes.new("GeometryNodeJoinGeometry")
        group.links.new(inputs.outputs["Geometry"], join.inputs["Geometry"])
        group.links.new(instances.outputs["Instances"], join.inputs["Geometry"])
        group.links.new(join.outputs["Geometry"], outputs.inputs["Geometry"])
        mixed = inspect(emitter)
        assert mixed["mesh"]["vertices"] == 2, mixed
        assert mixed["mesh_including_instances"]["vertices"] == 18, mixed

        curve = bpy.data.curves.new("GeometryCheck.Curve", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.2
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        spline.points[0].co = (0, 0, 0, 1)
        spline.points[1].co = (0, 0, 2, 1)
        curve_obj = bpy.data.objects.new("GeometryCheck.Curve", curve)
        collection.objects.link(curve_obj)
        curve_info = inspect(curve_obj)
        assert curve_info["mesh"]["vertices"] > 0, curve_info
        assert curve_info["mesh_including_instances"] == curve_info["mesh"], curve_info
        assert curve_info["instances"]["count"] == 0, curve_info
        assert curve_info["dimensions"][2] == 2, curve_info
        curve_source = bpy.data.collections.new("GeometryCheck.CurveSource")
        curve_source.objects.link(curve_obj)
        collection.objects.unlink(curve_obj)
        curve_nested = bpy.data.collections.new("GeometryCheck.CurveNested")
        instance("GeometryCheck.CurveCopyA", curve_source, curve_nested)
        instance("GeometryCheck.CurveCopyB", curve_source, curve_nested)
        curve_outer = instance("GeometryCheck.CurveOuter", curve_nested, collection)
        curve_instances = inspect(curve_outer)
        assert curve_instances["mesh_including_instances"] == {
            field: count * 2 for field, count in curve_info["mesh"].items()
        }, curve_instances
        assert curve_instances["instances"]["count"] == 4, curve_instances
        curve_summary = next(
            s
            for s in curve_instances["instances"]["sources"]
            if s["name"] == curve_obj.name
        )
        assert (
            curve_summary["count"] == 2 and curve_summary["mesh"] == curve_info["mesh"]
        ), curve_summary
        empty = bpy.data.objects.new("GeometryCheck.Empty", None)
        collection.objects.link(empty)
        empty_info = inspect(empty)
        assert empty_info["mesh"] is None, empty_info
        assert empty_info["world_bounding_box"] is empty_info["dimensions"] is None, (
            empty_info
        )
        zero = bpy.data.objects.new(
            "GeometryCheck.Zero", bpy.data.meshes.new("GeometryCheck.Zero")
        )
        collection.objects.link(zero)
        zero_info = inspect(zero)
        assert zero_info["mesh"] == {"vertices": 0, "edges": 0, "polygons": 0}, (
            zero_info
        )
        assert zero_info["world_bounding_box"] is zero_info["dimensions"] is None, (
            zero_info
        )
        absent = bpy.data.objects.new("GeometryCheck.Absent", None)
        try:
            inspect(absent)
        except ValueError as error:
            assert "dependency graph" in str(error), error
        else:
            raise AssertionError("Unevaluated object reported evaluated geometry")
        mesh_count = len(bpy.data.meshes)
        for _ in range(10):
            assert inspect(outer) == instanced
            assert inspect(emitter) == mixed
        assert len(bpy.data.meshes) == mesh_count
        return {
            "array": result,
            "animated": animated,
            "collection": instanced,
            "geometry_nodes": nodes,
            "realized": realized,
        }
    finally:
        for group, original in zip(data_groups, original_data):
            for item in set(group) - original:
                group.remove(item)
        scene.frame_set(original_frame, subframe=original_subframe)
