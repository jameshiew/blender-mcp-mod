import os

import bpy


def get_preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def sketchfab_api_key():
    preferences = get_preferences()
    return (
        getattr(preferences, "sketchfab_api_key", "")
        or getattr(bpy.context.scene, "blendermcp_sketchfab_api_key", "")
        or os.getenv("BLENDERMCP_SKETCHFAB_API_KEY", "")
    )
