from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from itertools import islice
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    TypeAlias,
    TypeVar,
    cast,
    overload,
)

import bpy

from .context import current_scene

T = TypeVar("T")
if TYPE_CHECKING:

    class ModifierProperties(Protocol):
        inputs: object

    AnimationEntry: TypeAlias = (
        tuple[Literal["NLA_TRACK"], dict[str, object], bpy.types.NlaTrack]
        | tuple[Literal["NLA_STRIP"], dict[str, object], bpy.types.NlaStrip]
        | tuple[
            Literal[
                "ACTION_CHANNEL", "DRIVER", "NLA_CONTROL_CHANNEL", "NLA_ACTION_CHANNEL"
            ],
            dict[str, object],
            bpy.types.FCurve,
        ]
    )

DETAIL_LIMIT = 64
VALUE_LIMIT = 16
TEXT_LIMIT = 2048


def _name(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _pagination(offset: int, limit: int) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")


@overload
def _ref(value: bpy.types.ID) -> dict[str, object]: ...


@overload
def _ref(value: None) -> None: ...


def _ref(value: bpy.types.ID | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "name": value.name,
        "type": value.id_type,
        "library": value.library.filepath if value.library else None,
    }


def _value(value: Any) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"non_finite": str(value)}
    if isinstance(value, str):
        return (
            value
            if len(value) <= TEXT_LIMIT
            else {
                "value": value[:TEXT_LIMIT],
                "length": len(value),
                "details_omitted": True,
            }
        )
    if isinstance(value, bpy.types.ID):
        return _ref(value)
    items = sorted(value) if isinstance(value, set) else value
    values = [_value(item) for item in islice(items, VALUE_LIMIT)]
    return (
        values
        if len(items) <= VALUE_LIMIT
        else {"items": values, "count": len(items), "details_omitted": True}
    )


def _details_omitted(value: object) -> bool:
    if isinstance(value, dict):
        return bool(
            value.get("details_omitted") or value.get("omitted") or value.get("errors")
        ) or any(_details_omitted(item) for item in value.values())
    if isinstance(value, list):
        return any(_details_omitted(item) for item in value)
    return False


def _page(
    items: Sequence[T],
    offset: int,
    limit: int,
    describe: Callable[[T], object] | None = None,
    *,
    pageable: bool = True,
) -> dict[str, object]:
    total = len(items)
    end = min(offset + limit, total)
    described = [describe(item) if describe else item for item in items[offset:end]]
    remaining = end < total
    more = remaining and pageable
    return {
        "count": total,
        "offset": offset,
        "limit": limit,
        "items": described,
        "next_offset": end if more else None,
        "has_more": more,
        "details_omitted": (remaining and not pageable) or _details_omitted(described),
    }


def _limited(
    items: Sequence[T],
    offset: int,
    limit: int,
    describe: Callable[[T], object] | None = None,
) -> dict[str, object]:
    return _page(items, offset, limit, describe, pageable=False)


def _settings(
    value: bpy.types.bpy_struct, skip: Sequence[str] = (), depth: int = 1
) -> dict[str, object]:
    settings: dict[str, object] = {}
    omitted: list[str] = []
    errors: dict[str, str] = {}
    for prop in value.bl_rna.properties:
        key = prop.identifier
        if key == "rna_type" or key in skip:
            continue
        if prop.is_readonly and prop.type not in {"POINTER", "COLLECTION"}:
            continue
        if len(settings) >= DETAIL_LIMIT:
            omitted.append(key)
            continue
        try:
            item = getattr(value, key)
            if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
                settings[key] = _value(item)
            elif prop.type == "POINTER" and (
                item is None or isinstance(item, bpy.types.ID)
            ):
                settings[key] = _ref(item)
            elif prop.type == "POINTER" and depth:
                settings[key] = _settings(item, depth=depth - 1)
            else:
                omitted.append(key)
        except (AttributeError, TypeError, ValueError, RuntimeError) as error:
            errors[key] = str(error)[:256]
    return {
        "values": settings,
        "omitted": omitted,
        "errors": errors,
        "details_omitted": bool(omitted or errors) or _details_omitted(settings),
    }


def _slot(slot: bpy.types.ActionSlot | None) -> dict[str, object] | None:
    return (
        {"identifier": slot.identifier, "target_id_type": slot.target_id_type}
        if slot
        else None
    )


def _action_curves(
    action: bpy.types.Action | None, slot: bpy.types.ActionSlot | None
) -> Iterator[tuple[bpy.types.FCurve, dict[str, object]]]:
    if action is None or slot is None:
        return
    for layer_index, layer in enumerate(action.layers):
        for strip_index, strip in enumerate(layer.strips):
            bags: Sequence[bpy.types.ActionChannelbag] = getattr(
                strip, "channelbags", ()
            )
            for bag in bags:
                if bag.slot_handle == slot.handle:
                    for curve in bag.fcurves:
                        yield (
                            curve,
                            {
                                "action": _ref(action),
                                "slot": _slot(slot),
                                "layer": layer.name,
                                "layer_index": layer_index,
                                "action_strip_index": strip_index,
                            },
                        )


def _animation_entries(owner: bpy.types.ID) -> list[AnimationEntry]:
    animation: bpy.types.AnimData | None = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    entries: list[AnimationEntry] = [
        ("ACTION_CHANNEL", context, curve)
        for curve, context in _action_curves(animation.action, animation.action_slot)
    ]
    entries.extend(("DRIVER", {}, curve) for curve in animation.drivers)
    for track_index, track in enumerate(animation.nla_tracks):
        context: dict[str, object] = {"track": track.name, "track_index": track_index}
        entries.append(("NLA_TRACK", context, track))
        pending = [
            (strip, [index]) for index, strip in reversed(list(enumerate(track.strips)))
        ]
        while pending:
            strip, path = pending.pop()
            strip_context = {**context, "strip": strip.name, "strip_path": path}
            entries.append(("NLA_STRIP", strip_context, strip))
            entries.extend(
                ("NLA_CONTROL_CHANNEL", strip_context, curve) for curve in strip.fcurves
            )
            entries.extend(
                ("NLA_ACTION_CHANNEL", {**strip_context, **action_context}, curve)
                for curve, action_context in _action_curves(
                    strip.action, strip.action_slot
                )
            )
            pending.extend(
                (child, path + [index])
                for index, child in reversed(list(enumerate(strip.strips)))
            )
    return entries


@overload
def _animation_overview(
    owner: bpy.types.ID, entries: list[AnimationEntry] | None = None
) -> dict[str, object]: ...


@overload
def _animation_overview(
    owner: None, entries: list[AnimationEntry] | None = None
) -> None: ...


def _animation_overview(
    owner: bpy.types.ID | None, entries: list[AnimationEntry] | None = None
) -> dict[str, object] | None:
    if owner is None:
        return None
    animation: bpy.types.AnimData | None = getattr(owner, "animation_data", None)
    entries = _animation_entries(owner) if entries is None else entries
    return {
        "owner": _ref(owner),
        "entry_count": len(entries),
        "counts": dict(sorted(Counter(kind for kind, _, _ in entries).items())),
        "action": _ref(animation.action) if animation else None,
        "action_slot": _slot(animation.action_slot) if animation else None,
        "use_nla": animation.use_nla if animation else False,
        "action_blend_type": animation.action_blend_type if animation else None,
        "action_influence": animation.action_influence if animation else None,
        "action_extrapolation": animation.action_extrapolation if animation else None,
    }


def _curve_info(curve: bpy.types.FCurve) -> dict[str, object]:
    key_count, sample_count = len(curve.keyframe_points), len(curve.sampled_points)
    return {
        "data_path": _value(curve.data_path),
        "array_index": curve.array_index,
        "keyframe_count": key_count,
        "sample_count": sample_count,
        "frame_range": _value(curve.range()) if key_count or sample_count else None,
        "interpolation_counts": dict(
            sorted(
                Counter(point.interpolation for point in curve.keyframe_points).items()
            )
        ),
        "extrapolation": curve.extrapolation,
        "mute": curve.mute,
        "is_valid": curve.is_valid,
        "group": curve.group.name if curve.group else None,
        "modifiers": _limited(
            list(curve.modifiers),
            0,
            20,
            lambda mod: {"type": mod.type, "settings": _settings(mod)},
        ),
    }


def _describe_animation(entry: AnimationEntry) -> dict[str, object]:
    kind, context, value = entry
    result: dict[str, object] = {"kind": kind, **context}
    if kind == "NLA_TRACK":
        value = cast("bpy.types.NlaTrack", value)
        result.update(
            mute=value.mute, is_solo=value.is_solo, strip_count=len(value.strips)
        )
    elif kind == "NLA_STRIP":
        value = cast("bpy.types.NlaStrip", value)
        result.update(
            type=value.type,
            frame_range=_value([value.frame_start, value.frame_end]),
            action=_ref(value.action),
            action_slot=_slot(value.action_slot),
            action_frame_range=_value(
                [value.action_frame_start, value.action_frame_end]
            ),
        )
        for field in (
            "mute",
            "influence",
            "blend_type",
            "extrapolation",
            "repeat",
            "scale",
            "blend_in",
            "blend_out",
            "use_reverse",
            "use_animated_influence",
            "use_animated_time",
            "strip_time",
        ):
            result[field] = _value(getattr(value, field))
    else:
        value = cast("bpy.types.FCurve", value)
        result.update(_curve_info(value))
        if kind == "DRIVER":
            driver = value.driver
            if driver is None:
                raise ValueError("Driver channel has no driver")
            result["driver"] = {
                "type": driver.type,
                "expression": _value(driver.expression),
                "use_self": driver.use_self,
                "is_valid": driver.is_valid,
                "variables": _limited(
                    list(driver.variables),
                    0,
                    20,
                    lambda var: {
                        "name": var.name,
                        "type": var.type,
                        "targets": [_settings(target) for target in var.targets],
                    },
                ),
            }
    return result


def _owner(data_type: str, data_name: str) -> bpy.types.ID:
    _name(data_name, "data_name")
    collections = {
        "OBJECT": "objects",
        "OBJECT_DATA": "objects",
        "SHAPE_KEYS": "objects",
        "MATERIAL": "materials",
        "MATERIAL_NODES": "materials",
        "WORLD": "worlds",
        "WORLD_NODES": "worlds",
        "SCENE": "scenes",
        "NODE_GROUP": "node_groups",
    }
    if not isinstance(data_type, str) or data_type not in collections:
        raise ValueError(f"Unsupported data_type: {data_type}")
    owner = getattr(bpy.data, collections[data_type]).get(data_name)
    if owner is None:
        raise ValueError(f"{data_type} not found: {data_name}")
    if data_type == "OBJECT_DATA":
        owner = owner.data
    elif data_type == "SHAPE_KEYS":
        owner = getattr(owner.data, "shape_keys", None)
    elif data_type in {"MATERIAL_NODES", "WORLD_NODES"}:
        owner = owner.node_tree
    if owner is None:
        raise ValueError(f"{data_name} has no {data_type} data")
    return owner


def animation_info(
    data_name: str, data_type: str = "OBJECT", offset: int = 0, limit: int = 20
) -> dict[str, object]:
    _pagination(offset, limit)
    owner = _owner(data_type, data_name)
    entries = _animation_entries(owner)
    return {
        **_animation_overview(owner, entries),
        "data_type": data_type,
        "frame": current_scene().frame_current,
        "entries": _page(entries, offset, limit, _describe_animation),
        "scope": "Assigned action slots, drivers, and NLA tracks/strips. Channel ranges use action-local frames; NLA strip ranges use scene frames. Summaries do not evaluate drivers or change the current frame.",
    }


def _socket_info(socket: bpy.types.NodeSocket, index: int) -> dict[str, object]:
    result: dict[str, object] = {
        "index": index,
        "identifier": socket.identifier,
        "name": socket.name,
        "type": socket.bl_idname,
        "is_linked": socket.is_linked,
        "enabled": socket.enabled,
        "hide_value": socket.hide_value,
    }
    if hasattr(socket, "default_value"):
        result["default_value"] = _value(socket.default_value)
    return result


def _node_info(node: bpy.types.Node) -> dict[str, object]:
    result: dict[str, object] = {
        "name": node.name,
        "type": node.bl_idname,
        "label": node.label,
        "mute": node.mute,
        "settings": _settings(node, skip=bpy.types.Node.bl_rna.properties.keys()),
        "inputs": _limited(
            list(enumerate(node.inputs)),
            0,
            DETAIL_LIMIT,
            lambda pair: _socket_info(pair[1], pair[0]),
        ),
        "outputs": _limited(
            list(enumerate(node.outputs)),
            0,
            DETAIL_LIMIT,
            lambda pair: _socket_info(pair[1], pair[0]),
        ),
    }
    image: bpy.types.Image | None = getattr(node, "image", None)
    if image is not None:
        result["image"] = {
            **_ref(image),
            "filepath": image.filepath,
            "source": image.source,
            "colorspace": image.colorspace_settings.name
            if image.colorspace_settings
            else None,
        }
    if tree := getattr(node, "node_tree", None):
        result["node_group"] = _ref(tree)
    ramp: bpy.types.ColorRamp | None = getattr(node, "color_ramp", None)
    if ramp is not None:
        result["color_ramp"] = {
            "interpolation": ramp.interpolation,
            "color_mode": ramp.color_mode,
            "hue_interpolation": ramp.hue_interpolation,
            "elements": _limited(
                list(ramp.elements),
                0,
                DETAIL_LIMIT,
                lambda item: {"position": item.position, "color": list(item.color)},
            ),
        }
    return result


def _link_info(link: bpy.types.NodeLink) -> dict[str, object]:
    return {
        "from_node": link.from_node.name if link.from_node else None,
        "from_socket": link.from_socket.identifier if link.from_socket else None,
        "to_node": link.to_node.name if link.to_node else None,
        "to_socket": link.to_socket.identifier if link.to_socket else None,
        "is_muted": link.is_muted,
        "is_valid": link.is_valid,
    }


@overload
def _tree_info(
    tree: bpy.types.NodeTree, offset: int, limit: int
) -> dict[str, object]: ...


@overload
def _tree_info(tree: None, offset: int, limit: int) -> None: ...


def _tree_info(
    tree: bpy.types.NodeTree | None, offset: int, limit: int
) -> dict[str, object] | None:
    if tree is None:
        return None
    nodes = sorted(tree.nodes, key=lambda node: node.name)
    page_names = {node.name for node in nodes[offset : offset + limit]}
    links = [
        link
        for link in tree.links
        if link.to_node is not None and link.to_node.name in page_names
    ]
    return {
        "name": tree.name,
        "type": tree.bl_idname,
        "node_count": len(nodes),
        "link_count": len(tree.links),
        "nodes": _page(nodes, offset, limit, _node_info),
        "incoming_links": _limited(links, 0, 200, _link_info),
        "animation": _animation_overview(tree),
        "scope": "Nodes sorted by name, with incoming links for this page (up to 200). Socket defaults are stored values, not evaluated shader results. Nested groups are references; inspect them with get_node_group_info. Nested collections and unsupported settings are listed as omitted.",
    }


def material_info(
    material_name: str, offset: int = 0, limit: int = 20
) -> dict[str, object]:
    _name(material_name, "material_name")
    _pagination(offset, limit)
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise ValueError(f"Material not found: {material_name}")
    return {
        **_ref(material),
        "use_nodes": material.use_nodes,
        "settings": _settings(material, skip=bpy.types.ID.bl_rna.properties.keys()),
        "node_tree": _tree_info(material.node_tree, offset, limit),
        "animation": _animation_overview(material),
    }


def _interface_sockets(
    tree: bpy.types.NodeTree,
) -> list[bpy.types.NodeTreeInterfaceSocket]:
    interface = tree.interface
    if interface is None:
        return []
    return [
        cast("bpy.types.NodeTreeInterfaceSocket", item)
        for item in interface.items_tree
        if item.item_type == "SOCKET"
    ]


def node_group_info(
    node_group_name: str, offset: int = 0, limit: int = 20
) -> dict[str, object]:
    _name(node_group_name, "node_group_name")
    _pagination(offset, limit)
    tree = bpy.data.node_groups.get(node_group_name)
    if tree is None:
        raise ValueError(f"Node group not found: {node_group_name}")
    sockets = _interface_sockets(tree)
    return {
        **_ref(tree),
        **_tree_info(tree, offset, limit),
        "interface": _limited(
            sockets,
            0,
            DETAIL_LIMIT,
            lambda item: {
                "name": item.name,
                "identifier": item.identifier,
                "in_out": item.in_out,
                "socket_type": item.socket_type,
                "settings": _settings(item),
            },
        ),
    }


def _modifier_info(modifier: bpy.types.Modifier, index: int) -> dict[str, object]:
    result: dict[str, object] = {
        "name": modifier.name,
        "type": modifier.type,
        "index": index,
        "show_viewport": modifier.show_viewport,
        "show_render": modifier.show_render,
        "show_in_editmode": modifier.show_in_editmode,
        "show_on_cage": modifier.show_on_cage,
        "settings": _settings(
            modifier, skip=bpy.types.Modifier.bl_rna.properties.keys()
        ),
    }
    node_group: bpy.types.NodeTree | None = getattr(modifier, "node_group", None)
    if modifier.type == "NODES" and node_group:
        inputs = [
            item for item in _interface_sockets(node_group) if item.in_out == "INPUT"
        ]

        def input_info(item: bpy.types.NodeTreeInterfaceSocket) -> dict[str, object]:
            key = item.identifier
            result: dict[str, object] = {
                "name": item.name,
                "identifier": key,
                "socket_type": item.socket_type,
            }
            value = getattr(
                cast(
                    "ModifierProperties",
                    cast("bpy.types.NodesModifier", modifier).properties,
                ).inputs,
                key,
                None,
            )
            if value is not None:
                for field in ("value", "type", "attribute_name"):
                    if hasattr(value, field):
                        result[field] = _value(getattr(value, field))
                if hasattr(value, "type"):
                    result["use_attribute"] = value.type == "ATTRIBUTE"
            return result

        result["node_group"] = _ref(node_group)
        result["inputs"] = _limited(inputs, 0, DETAIL_LIMIT, input_info)
    return result


def modifier_info(object_name: str, modifier_name: str) -> dict[str, object]:
    _name(object_name, "object_name")
    _name(modifier_name, "modifier_name")
    obj = bpy.data.objects.get(object_name)
    if obj is None:
        raise ValueError(f"Object not found: {object_name}")
    for index, modifier in enumerate(obj.modifiers):
        if modifier.name == modifier_name:
            return {
                "object": _ref(obj),
                "frame": current_scene().frame_current,
                **_modifier_info(modifier, index),
            }
    raise ValueError(f"Modifier not found on {object_name}: {modifier_name}")


def object_details(obj: bpy.types.Object) -> dict[str, object]:
    def material_slot(pair: tuple[int, bpy.types.MaterialSlot]) -> dict[str, object]:
        index, slot = pair
        material = slot.material
        result: dict[str, object] = {
            "index": index,
            "link": slot.link,
            "material": _ref(material),
        }
        if material:
            result.update(
                use_nodes=material.use_nodes,
                node_count=len(material.node_tree.nodes) if material.node_tree else 0,
            )
        return result

    return {
        "material_slots": _limited(
            list(enumerate(obj.material_slots)), 0, 20, material_slot
        ),
        "modifiers": _limited(
            list(enumerate(obj.modifiers)),
            0,
            20,
            lambda pair: _modifier_info(pair[1], pair[0]),
        ),
        "animation": {
            "object": _animation_overview(obj),
            "data": _animation_overview(obj.data),
            "shape_keys": _animation_overview(getattr(obj.data, "shape_keys", None)),
        },
    }
