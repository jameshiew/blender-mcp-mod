from __future__ import annotations

import math
import re
from itertools import islice
from typing import Any

import bpy


def _value(value: Any) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
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
    if query:
        needle = query.casefold()
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
    return result
