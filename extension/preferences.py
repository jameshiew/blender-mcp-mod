from __future__ import annotations

import os

import bpy


def get_preferences(
    context: bpy.types.Context | None = None,
) -> bpy.types.AddonPreferences | None:
    context = context or bpy.context
    preferences = context.preferences
    if preferences is None:
        return None
    addon = preferences.addons.get(__package__ or "")
    return addon.preferences if addon else None


def sketchfab_api_key() -> str:
    preferences = get_preferences()
    return (
        getattr(preferences, "sketchfab_api_key", "")
        or getattr(bpy.context.scene, "blendermcp_sketchfab_api_key", "")
        or os.getenv("BLENDERMCP_SKETCHFAB_API_KEY", "")
    )
