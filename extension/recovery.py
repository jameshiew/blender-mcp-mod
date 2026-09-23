import hashlib
import json
import os
import re
import shutil
import uuid
from array import array
from datetime import UTC, datetime
from pathlib import Path

import bpy

SUMMARY_SCOPE = "Writable object/data properties, relationships, visibility in all scene view layers, modifier/constraint settings, material slots, and base mesh positions/topology. Shader contents, animation, mesh attributes, scene settings, external files, and Python state are not compared."


def _identity(value):
    if value is None:
        return None
    return [value.id_type, value.session_uid]


def _value(value):
    if isinstance(value, bpy.types.ID):
        return _identity(value)
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if hasattr(value, "to_list"):
        return [_value(item) for item in value.to_list()]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    try:
        return [_value(item) for item in value]
    except TypeError:
        return str(value)


def _properties(value, depth=2):
    result = {"rna_type": value.bl_rna.identifier}
    for prop in value.bl_rna.properties:
        if prop.identifier == "rna_type" or prop.is_readonly:
            continue
        item = getattr(value, prop.identifier)
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
            result[prop.identifier] = _value(
                sorted(item) if isinstance(item, set) else item
            )
        elif prop.type == "POINTER":
            if item is None or isinstance(item, bpy.types.ID):
                result[prop.identifier] = _identity(item)
            elif depth:
                result[prop.identifier] = _properties(item, depth - 1)
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _mesh_digest(mesh):
    digest = hashlib.sha256()
    for collection, name, size, kind in (
        (mesh.vertices, "co", 3, "f"),
        (mesh.edges, "vertices", 2, "i"),
        (mesh.loops, "vertex_index", 1, "i"),
        (mesh.polygons, "loop_start", 1, "i"),
        (mesh.polygons, "loop_total", 1, "i"),
        (mesh.polygons, "material_index", 1, "i"),
    ):
        values = array(kind, [0]) * (len(collection) * size)
        collection.foreach_get(name, values)
        digest.update(str(len(values)).encode() + b":" + values.tobytes())
    return digest.hexdigest()


def capture_objects():
    if bpy.context.mode != "OBJECT":
        raise ValueError("Object change summaries require Object mode")
    data_cache = {}
    result = {}
    view_layers = [layer for scene in bpy.data.scenes for layer in scene.view_layers]
    for obj in bpy.data.objects:
        fields = {
            "properties": _digest(_properties(obj)),
            "collections": _digest(sorted(c.session_uid for c in obj.users_collection)),
            "modifiers": _digest([_properties(item) for item in obj.modifiers]),
            "constraints": _digest([_properties(item) for item in obj.constraints]),
            "material_slots": _digest(
                [[_identity(slot.material), slot.link] for slot in obj.material_slots]
            ),
            "custom_properties": _digest(
                {key: _value(value) for key, value in obj.items()}
            ),
            "hidden": {
                str(layer.as_pointer()): obj.hide_get(view_layer=layer)
                for layer in view_layers
                if obj.name in layer.objects
            },
        }
        if obj.data is not None:
            key = obj.data.session_uid
            if key not in data_cache:
                data_cache[key] = {"data_properties": _digest(_properties(obj.data))}
                if obj.type == "MESH":
                    data_cache[key]["mesh_geometry"] = _mesh_digest(obj.data)
            fields.update(data_cache[key])
        result[obj.session_uid] = {
            "name": obj.name,
            "type": obj.type,
            "library": obj.library.filepath if obj.library else None,
            "fields": fields,
        }
    return result


def compare_objects(before, after):
    def description(item):
        return {key: item[key] for key in ("name", "type", "library")}

    created = [description(after[key]) for key in after.keys() - before.keys()]
    removed = [description(before[key]) for key in before.keys() - after.keys()]
    changed = []
    for key in before.keys() & after.keys():
        old, new = before[key], after[key]
        fields = sorted(
            field
            for field in old["fields"].keys() | new["fields"].keys()
            if old["fields"].get(field) != new["fields"].get(field)
        )
        if old["name"] != new["name"]:
            fields.append("name")
        if fields:
            changed.append(
                {
                    **description(new),
                    "previous_name": old["name"],
                    "changed_fields": fields,
                }
            )
    changes = {}
    for name, items in (
        ("created", created),
        ("changed", changed),
        ("removed", removed),
    ):
        changes[name] = sorted(
            items, key=lambda item: (item["name"], item["library"] or "")
        )
    return changes


def change_page(changes, category, offset=0, limit=100):
    if category not in {"created", "changed", "removed"}:
        raise ValueError("category must be created, changed, or removed")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    items = changes[category]
    end = min(offset + limit, len(items))
    more = end < len(items)
    return {
        "count": len(items),
        "offset": offset,
        "limit": limit,
        "items": items[offset:end],
        "next_offset": end if more else None,
        "has_more": more,
        "details_omitted": False,
    }


def summarize_changes(changes, limit=100):
    pages = {
        category: change_page(changes, category, limit=limit) for category in changes
    }
    return {
        "scope": SUMMARY_SCOPE,
        "counts": {category: len(items) for category, items in changes.items()},
        **pages,
    }


class Checkpoints:
    max_checkpoints = 32

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _paths(self, checkpoint_id):
        if not isinstance(checkpoint_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", checkpoint_id
        ):
            raise ValueError(
                "checkpoint_id must be an ID returned by create_checkpoint"
            )
        return (
            self.directory / f"{checkpoint_id}.blend",
            self.directory / f"{checkpoint_id}.json",
        )

    @staticmethod
    def _checksum(path):
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def _get(self, checkpoint_id):
        path, metadata_path = self._paths(checkpoint_id)
        if not path.is_file() or not metadata_path.is_file():
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        metadata = json.loads(metadata_path.read_text())
        if metadata.get("checkpoint_id") != checkpoint_id:
            raise ValueError("Checkpoint metadata does not match its ID")
        return path, metadata_path, metadata

    def list(self):
        checkpoints = []
        for metadata in sorted(self.directory.glob("*.json")):
            if re.fullmatch(r"[0-9a-f]{32}", metadata.stem):
                _, _, record = self._get(metadata.stem)
                checkpoints.append(record)
        return {
            "checkpoints": sorted(checkpoints, key=lambda item: item["created_at"]),
            "max_checkpoints": self.max_checkpoints,
        }

    def create(self, label=None):
        if label is not None and (
            not isinstance(label, str) or not 1 <= len(label) <= 128
        ):
            raise ValueError("label must be a string of 1 to 128 characters")
        if bpy.context.mode != "OBJECT":
            raise ValueError("Checkpoint creation requires Object mode")
        if len(self.list()["checkpoints"]) >= self.max_checkpoints:
            raise ValueError(
                "Checkpoint storage is full; delete an unused checkpoint first"
            )
        checkpoint_id = uuid.uuid4().hex
        path, metadata_path = self._paths(checkpoint_id)
        metadata = {
            "checkpoint_id": checkpoint_id,
            "label": label,
            "created_at": datetime.now(UTC).isoformat(),
            "original_filepath": bpy.data.filepath,
            "scene": bpy.context.scene.name,
            "frame": bpy.context.scene.frame_current,
            "object_count": len(bpy.data.objects),
            "blender_version": bpy.app.version_string,
            "filepath": str(path),
        }
        try:
            status = bpy.ops.wm.save_as_mainfile(
                filepath=str(path),
                copy=True,
                check_existing=False,
                relative_remap=True,
                compress=True,
            )
            if status != {"FINISHED"} or not path.is_file():
                raise RuntimeError("Blender did not write the checkpoint")
            metadata["size_bytes"] = path.stat().st_size
            metadata["sha256"] = self._checksum(path)
            with metadata_path.open("x", encoding="utf-8") as file:
                json.dump(metadata, file)
        except Exception:
            path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise
        return metadata

    def restore(self, checkpoint_id, clear_namespaces):
        path, _, metadata = self._get(checkpoint_id)
        if self._checksum(path) != metadata["sha256"]:
            raise ValueError("Checkpoint file changed after creation; restore refused")
        safety = self.create(label=f"Before restoring {checkpoint_id}")
        working_path = self.directory / f"restored-{uuid.uuid4().hex}.blend"
        try:
            shutil.copyfile(path, working_path)
            clear_namespaces()
            status = bpy.ops.wm.open_mainfile(
                filepath=str(working_path), load_ui=False, use_scripts=False
            )
            if status != {"FINISHED"}:
                raise RuntimeError("Blender did not open the checkpoint")
        except Exception as error:
            raise RuntimeError(
                f"Restore failed: {error}; safety checkpoint: {safety['checkpoint_id']}"
            ) from error
        return {
            "restored_checkpoint_id": checkpoint_id,
            "filepath": bpy.data.filepath,
            "original_filepath": metadata["original_filepath"],
            "safety_checkpoint": safety,
            "namespaces_cleared": True,
        }

    def delete(self, checkpoint_id):
        path, metadata_path, _ = self._get(checkpoint_id)
        if os.path.abspath(bpy.data.filepath) == str(path.resolve()):
            raise ValueError("Cannot delete the currently open checkpoint file")
        path.unlink()
        metadata_path.unlink()
        return {"deleted_checkpoint_id": checkpoint_id}
