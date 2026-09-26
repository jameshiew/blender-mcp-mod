from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager
from itertools import islice
from typing import Any, cast

import bpy

logger = logging.getLogger(__name__)

SOCKET_LIMIT = 100
SETTING_VALUE_LIMIT = 100
VARIANT_LIMIT = 256
TREE_TYPES = (
    "ShaderNodeTree",
    "GeometryNodeTree",
    "CompositorNodeTree",
    "TextureNodeTree",
)
FAMILY_TREE_TYPES = {
    "ShaderNode": "ShaderNodeTree",
    "GeometryNode": "GeometryNodeTree",
    "FunctionNode": "GeometryNodeTree",
    "CompositorNode": "CompositorNodeTree",
    "TextureNode": "TextureNodeTree",
}

NODE_TEMPLATE_FUNCTIONS = ("input_template", "output_template")

SocketKey = tuple[str, str, str]
Setting = tuple[str, str, list[object]]


def _value(value: Any) -> object:
    if isinstance(value, float):
        return float(f"{value:.7g}") if math.isfinite(value) else None
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, str):
        return value[:2048]
    return value


def _property(prop: bpy.types.Property) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": "PROPERTY",
        "identifier": prop.identifier,
        "name": prop.name,
        "description": prop.description[:2048],
        "type": prop.type,
        "subtype": prop.subtype,
        "readonly": prop.is_readonly,
        "animatable": prop.is_animatable,
        "required": prop.is_required,
        "output": prop.is_output,
        "never_none": prop.is_never_none,
    }
    omitted = len(prop.description) > 2048
    if isinstance(
        prop, (bpy.types.BoolProperty, bpy.types.IntProperty, bpy.types.FloatProperty)
    ):
        result["array_length"] = prop.array_length
        if prop.is_array:
            result["array_dimensions"] = list(prop.array_dimensions)
            result["default"] = [
                _value(item) for item in islice(prop.default_array, 64)
            ]
            omitted |= prop.array_length > 64
        else:
            result["default"] = _value(prop.default)
    if isinstance(prop, (bpy.types.IntProperty, bpy.types.FloatProperty)):
        result.update(
            minimum=_value(prop.hard_min), maximum=_value(prop.hard_max), unit=prop.unit
        )
    elif isinstance(prop, bpy.types.StringProperty):
        result.update(default=_value(prop.default), max_length=prop.length_max)
        omitted |= len(prop.default) > 2048
    elif isinstance(prop, bpy.types.EnumProperty):
        items = prop.enum_items_static
        entries = [
            {
                "identifier": item.identifier,
                "name": item.name,
                "description": item.description[:2048],
            }
            for item in islice(items, 100)
        ]
        result.update(
            enum_items=entries,
            enum_items_total=len(items),
            enum_items_static_only=True,
            enum_flag=prop.is_enum_flag,
            default=_value(prop.default_flag if prop.is_enum_flag else prop.default),
        )
        omitted |= len(items) > 100 or any(
            len(item.description) > 2048 for item in islice(items, 100)
        )
    elif isinstance(prop, (bpy.types.PointerProperty, bpy.types.CollectionProperty)):
        result["fixed_type"] = prop.fixed_type.identifier if prop.fixed_type else None
    result["details_omitted"] = omitted
    return result


def _function(function: bpy.types.Function) -> dict[str, object]:
    parameters = [_property(prop) for prop in islice(function.parameters, 64)]
    return {
        "kind": "FUNCTION",
        "identifier": function.identifier,
        "description": function.description[:2048],
        "parameters": parameters,
        "parameters_total": len(function.parameters),
        "details_omitted": len(function.parameters) > 64
        or len(function.description) > 2048
        or any(parameter["details_omitted"] for parameter in parameters),
    }


def _is_node_type(rna: bpy.types.Struct) -> bool:
    base = rna.base
    while base is not None:
        if base.identifier == "Node":
            return True
        base = base.base
    return False


def _tree_types(rna: bpy.types.Struct) -> list[str]:
    preferred = []
    base = rna.base
    while base is not None:
        if base.identifier in FAMILY_TREE_TYPES:
            preferred.append(FAMILY_TREE_TYPES[base.identifier])
        base = base.base
    custom = [
        tree_type.bl_idname
        for tree_type in bpy.types.NodeTree.__subclasses__()
        if isinstance(getattr(tree_type, "bl_idname", None), str)
    ]
    return list(dict.fromkeys([*preferred, *TREE_TYPES, *custom]))


def _socket_value(value: Any) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return _value(value)
    if isinstance(value, bpy.types.ID):
        return value.name
    try:
        return [_socket_value(item) for item in islice(value, 16)]
    except TypeError:
        return None


def _menu_items(socket: bpy.types.NodeSocket) -> list[str] | None:
    # Menu socket items are dynamic, and Python sees them only in this error.
    try:
        cast("bpy.types.NodeSocketMenu", socket).default_value = "\x01"
    except TypeError as error:
        found = re.search(r" not found in \((.*)\)$", str(error), re.DOTALL)
        if found:
            return re.findall(r"'([^']*)'", found.group(1))
    return None


def _socket(socket: bpy.types.NodeSocket, index: int) -> dict[str, object]:
    entry: dict[str, object] = {
        "index": index,
        "identifier": socket.identifier,
        "name": socket.name,
        "type": socket.bl_idname,
        "enabled": socket.enabled,
        "is_inactive": socket.is_inactive,
    }
    menu = socket.type == "MENU"
    items = _menu_items(socket) if menu else None
    if hasattr(socket, "default_value") and not (menu and items == []):
        entry["default_value"] = _socket_value(socket.default_value)
    if menu:
        entry["menu_items"] = items
        if items is None:
            entry["details_omitted"] = True
    if socket.hide_value:
        entry["hide_value"] = True
    if socket.is_multi_input:
        entry["is_multi_input"] = True
    if socket.description:
        entry["description"] = socket.description[:2048]
    return entry


def _sockets(
    node: bpy.types.Node,
) -> Iterator[tuple[SocketKey, bpy.types.NodeSocket, int]]:
    for in_out, sockets in (("INPUT", node.inputs), ("OUTPUT", node.outputs)):
        for index, socket in enumerate(sockets):
            if socket.bl_idname != "NodeSocketVirtual":
                yield (in_out, socket.identifier, socket.bl_idname), socket, index


def _settings(node: bpy.types.Node, common: set[str]) -> list[Setting]:
    settings: list[Setting] = []
    for prop in node.bl_rna.properties:
        if prop.identifier in common or prop.is_readonly:
            continue
        if isinstance(prop, bpy.types.EnumProperty) and not prop.is_enum_flag:
            values: list[object] = [item.identifier for item in prop.enum_items]
            settings.append(("property", prop.identifier, values))
        elif isinstance(prop, bpy.types.BoolProperty) and not prop.is_array:
            settings.append(("property", prop.identifier, [False, True]))
    for socket in node.inputs:
        if socket.type == "MENU" and socket.enabled:
            items: list[object] = list(_menu_items(socket) or [])
            settings.append(("input", socket.identifier, items))
    return settings


def _apply(node: bpy.types.Node, setting: Setting, value: object) -> bool:
    kind, key, _ = setting
    target = (
        node
        if kind == "property"
        else next((item for item in node.inputs if item.identifier == key), None)
    )
    try:
        setattr(target, key if kind == "property" else "default_value", value)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return False
    return True


def _inspect_node(
    data: bpy.types.BlendData,
    node_type: str,
    tree_types: list[str],
    common: set[str],
    needle: str | None,
) -> dict[str, object]:
    accepted: list[bpy.types.NodeTree] = []
    for tree_type in tree_types:
        try:
            tree = data.node_groups.new("API lookup", cast(Any, tree_type))
            tree.nodes.remove(tree.nodes.new(node_type))
        except (RuntimeError, TypeError, ValueError):
            continue
        accepted.append(tree)
    if not accepted:
        return {"tree_types": [], "details_omitted": False}
    tree = accepted[0]
    sockets: dict[SocketKey, dict[str, object]] = {}
    defaults: list[SocketKey] = []
    node = tree.nodes.new(node_type)
    try:
        for key, socket, index in _sockets(node):
            sockets[key] = _socket(socket, index)
            defaults.append(key)
        settings = _settings(node, common)
    finally:
        tree.nodes.remove(node)
    variants: list[SocketKey] = []
    applied: list[list[object]] = [[] for _ in settings]
    enabled: dict[SocketKey, list[list[object]]] = {}
    used: dict[SocketKey, list[list[object]]] = {}
    remaining = VARIANT_LIMIT
    omitted = False
    for position, setting in enumerate(settings):
        values = setting[2]
        omitted |= len(values) > min(SETTING_VALUE_LIMIT, remaining)
        for value in values[: min(SETTING_VALUE_LIMIT, remaining)]:
            remaining -= 1
            node = tree.nodes.new(node_type)
            try:
                if not _apply(node, setting, value):
                    continue
                applied[position].append(value)
                for key, socket, index in _sockets(node):
                    if key not in sockets:
                        sockets[key] = _socket(socket, index)
                        variants.append(key)
                    if socket.enabled:
                        states = enabled.setdefault(key, [[] for _ in settings])
                        states[position].append(value)
                        if not socket.is_inactive:
                            states = used.setdefault(key, [[] for _ in settings])
                            states[position].append(value)
            finally:
                tree.nodes.remove(node)
    unset: list[list[object]] = [[] for _ in settings]
    for key in [*defaults, *variants]:
        enabled_when = []
        used_when = []
        for position, (kind, setting_key, _) in enumerate(settings):
            available = enabled.get(key, unset)[position]
            active = used.get(key, unset)[position]
            if available and len(available) < len(applied[position]):
                enabled_when.append({kind: setting_key, "values": available})
            if active and active != available:
                used_when.append({kind: setting_key, "values": active})
        if enabled_when:
            sockets[key]["enabled_when"] = enabled_when
        if used_when:
            sockets[key]["used_when"] = used_when
    variants = [key for key in variants if "enabled_when" in sockets[key]]

    def listed(keys: list[SocketKey], in_out: str) -> list[dict[str, object]]:
        nonlocal omitted
        entries = [
            sockets[key]
            for key in keys
            if key[0] == in_out
            and (
                needle is None
                or needle in key[1].casefold()
                or needle in str(sockets[key]["name"]).casefold()
            )
        ]
        omitted |= len(entries) > SOCKET_LIMIT or any(
            entry.get("details_omitted") for entry in entries
        )
        return entries[:SOCKET_LIMIT]

    return {
        "tree_types": [tree.bl_idname for tree in accepted],
        "inputs": listed(defaults, "INPUT"),
        "outputs": listed(defaults, "OUTPUT"),
        "variant_inputs": listed(variants, "INPUT"),
        "variant_outputs": listed(variants, "OUTPUT"),
        "scope": f"Sockets of a node created by nodes.new with default settings in an empty {tree.bl_idname} and a new scene. Each setting is a node property or a menu input's default_value, changed alone from the defaults. enabled_when lists the values that make the socket available; used_when lists the values under which Blender infers that it affects the output, when these differ. Variant sockets exist only under other settings. node.inputs[key] finds enabled sockets by identifier, except on Mix and Map Range nodes, and then by name. Nodes that take sockets from node groups, zones, items, or scene data can differ.",
        "details_omitted": omitted,
    }


def _node_info(
    node_type: str, rna: bpy.types.Struct, common: set[str], needle: str | None
) -> dict[str, object]:
    # Blender frees temporary data on exit, so no exception or struct may leave.
    # Some node init functions take the context scene and leak a user on it.
    error = None
    with bpy.data.temp_data() as data:
        try:
            scene = data.scenes.new("API lookup")
            with cast(
                AbstractContextManager[object],
                bpy.context.temp_override(blend_data=data, scene=scene),
            ):
                info = _inspect_node(data, node_type, _tree_types(rna), common, needle)
        except Exception as exception:
            logger.exception("Failed to inspect node type %s", node_type)
            info = None
            error = f"{type(exception).__name__}: {exception}"[:2048]
    return info or {"tree_types": [], "error": error, "details_omitted": True}


def get_blender_api_info(
    identifier: str,
    kind: str = "TYPE",
    query: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, object]:
    if kind not in {"TYPE", "OPERATOR"}:
        raise ValueError("kind must be TYPE or OPERATOR")
    pattern = (
        r"[A-Za-z][A-Za-z0-9_]*"
        if kind == "TYPE"
        else r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*"
    )
    if not isinstance(identifier, str) or re.fullmatch(pattern, identifier) is None:
        raise ValueError(
            "Use a type name such as Camera or an operator such as mesh.primitive_cube_add"
        )
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError(
            "offset must be nonnegative and limit must be between 1 and 100"
        )
    if query is not None and not isinstance(query, str):
        raise TypeError("query must be a string")
    result: dict[str, object] = {
        "kind": kind,
        "identifier": identifier,
        "blender_version": bpy.app.version_string,
    }
    if kind == "TYPE":
        target = getattr(bpy.types, identifier, None)
        rna = getattr(target, "bl_rna", None)
        if rna is None:
            raise ValueError(f"Unknown Blender RNA type: {identifier}")
    else:
        namespace, name = identifier.split(".")
        operator = getattr(getattr(bpy.ops, namespace), name)
        try:
            rna = operator.get_rna_type()
        except (AttributeError, KeyError, RuntimeError) as error:
            raise ValueError(f"Unknown Blender operator: {identifier}") from error
        try:
            result["poll"] = operator.poll()
        except RuntimeError as error:
            result["poll"] = False
            result["poll_error"] = str(error)
        result["poll_context"] = (
            "Current MCP timer context; availability can change with mode, selection, and context overrides"
        )
    result.update(
        name=rna.name,
        description=rna.description[:2048],
        base=rna.base.identifier if rna.base else None,
    )
    members = [prop for prop in rna.properties if prop.identifier != "rna_type"]
    if kind == "TYPE":
        members.extend(rna.functions)
    node_type = kind == "TYPE" and _is_node_type(rna)
    common: set[str] = set()
    if node_type:
        node_rna = cast("bpy.types.Struct", bpy.types.Node.bl_rna)
        common = {
            *node_rna.properties.keys(),
            *node_rna.functions.keys(),
            *NODE_TEMPLATE_FUNCTIONS,
        }
        own = [member for member in members if member.identifier not in common]
        result["node_members_omitted"] = len(members) - len(own)
        members = own
    needle = query.casefold() if query else None
    if needle:
        members = [
            member for member in members if needle in member.identifier.casefold()
        ]
    members.sort(key=lambda member: member.identifier)
    entries = [
        _function(member)
        if isinstance(member, bpy.types.Function)
        else _property(member)
        for member in members[offset : offset + limit]
    ]
    next_offset = offset + len(entries)
    result.update(
        entries=entries,
        total=len(members),
        offset=offset,
        has_more=next_offset < len(members),
        next_offset=next_offset if next_offset < len(members) else None,
        details_omitted=len(rna.description) > 2048
        or any(entry["details_omitted"] for entry in entries),
    )
    if node_type and offset == 0:
        node = _node_info(identifier, rna, common, needle)
        result["node"] = node
        result["details_omitted"] = bool(
            result["details_omitted"] or node["details_omitted"]
        )
    return result
