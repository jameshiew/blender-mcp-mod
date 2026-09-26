from __future__ import annotations

import base64
import io
import json
import logging
import math
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import TypedDict, Unpack

import bpy
import requests
from mathutils import Vector

from .context import current_view_layer, push_undo_step
from .geometry import Bounds, world_bounding_box

logger = logging.getLogger(__name__)
API_URL = "https://api.sketchfab.com/v3"


class SketchfabError(Exception):
    pass


class RequestOptions(TypedDict, total=False):
    timeout: float
    headers: dict[str, str]
    params: dict[str, str | int | bool] | None


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SketchfabError(
            "Unexpected response format from Sketchfab API: expected an object"
        )
    return value


def _medium_thumbnail(thumbnail: dict[str, object]) -> bool:
    width = thumbnail.get("width", 0)
    return isinstance(width, (int, float)) and 400 <= width <= 800


def _http_get(url: str, **kwargs: Unpack[RequestOptions]) -> requests.Response:
    if not bpy.app.online_access:
        raise RuntimeError("Online access is disabled in Blender preferences")
    return requests.get(url, **kwargs)


def _mesh_bounds(meshes: Sequence[bpy.types.Object]) -> Bounds | None:
    bounds = None
    for mesh in meshes:
        mesh_bounds = world_bounding_box(mesh)
        if mesh_bounds is None:
            continue
        if bounds is None:
            bounds = mesh_bounds
        else:
            for axis in range(3):
                bounds[0][axis] = min(bounds[0][axis], mesh_bounds[0][axis])
                bounds[1][axis] = max(bounds[1][axis], mesh_bounds[1][axis])
    return bounds


def _import_archive(
    content: bytes, normalize_size: bool, target_size: float
) -> dict[str, object]:
    if bpy.context.mode != "OBJECT":
        raise SketchfabError("Model import requires Object mode")
    if type(normalize_size) is not bool:
        raise SketchfabError("normalize_size must be a boolean")
    if (
        isinstance(target_size, bool)
        or not isinstance(target_size, (int, float))
        or not math.isfinite(target_size)
        or target_size <= 0
    ):
        raise SketchfabError("target_size must be a positive finite number")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        directory = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if path.is_absolute():
                    raise SketchfabError(
                        "Security issue: Zip contains files with path traversal attempt"
                    )
                if ".." in path.parts:
                    raise SketchfabError(
                        "Security issue: Zip contains files with directory traversal sequence"
                    )
            archive.extractall(directory)
        main_file = next(
            (
                path
                for path in sorted(directory.rglob("*"))
                if path.suffix.lower() in {".gltf", ".glb"} and path.is_file()
            ),
            None,
        )
        if main_file is None:
            raise SketchfabError("No glTF file found in the downloaded model")
        try:
            return _import_model(main_file, normalize_size, target_size)
        finally:
            push_undo_step("MCP: Import Sketchfab Model")


def _import_model(
    main_file: Path, normalize_size: bool, target_size: float
) -> dict[str, object]:
    existing = {obj.session_uid for obj in bpy.data.objects}
    status = bpy.ops.import_scene.gltf(filepath=str(main_file), import_pack_images=True)
    if status != {"FINISHED"}:
        raise SketchfabError("Blender cancelled the glTF import")
    imported = [obj for obj in bpy.data.objects if obj.session_uid not in existing]

    roots = [obj for obj in imported if obj.parent is None]
    meshes = []
    pending = list(roots)
    while pending:
        obj = pending.pop()
        if obj.type == "MESH":
            meshes.append(obj)
        pending.extend(obj.children)

    current_view_layer().update()
    bounds = _mesh_bounds(meshes)
    scale_applied = 1.0
    if bounds is not None and normalize_size:
        max_dimension = max(high - low for low, high in zip(*bounds))
        if max_dimension > 0:
            scale_applied = target_size / max_dimension
            center = [(low + high) / 2 for low, high in zip(*bounds)]
            for root in roots:
                root.location = Vector(
                    tuple(
                        origin + (component - origin) * scale_applied
                        for component, origin in zip(root.location[:], center)
                    )
                )
                root.scale = Vector(
                    tuple(component * scale_applied for component in root.scale)
                )
            current_view_layer().update()
            bounds = _mesh_bounds(meshes)

    result: dict[str, object] = {
        "success": True,
        "message": "Model imported successfully",
        "imported_objects": [obj.name for obj in imported],
    }
    if bounds is not None:
        result["world_bounding_box"] = bounds
        result["dimensions"] = [round(high - low, 4) for low, high in zip(*bounds)]
    if normalize_size:
        result.update(scale_applied=round(scale_applied, 6), normalized=True)
    return result


class SketchfabService:
    def __init__(self, api_key: Callable[[], str], enabled: Callable[[], bool]) -> None:
        self._api_key = api_key
        self._enabled = enabled

    def _request_json(
        self,
        path: str,
        failure: str,
        *,
        missing: str | None = None,
        params: dict[str, str | int | bool] | None = None,
    ) -> dict[str, object] | None:
        api_key = self._api_key()
        if not api_key:
            raise SketchfabError("Sketchfab API key is not configured")
        response = _http_get(
            f"{API_URL}/{path}",
            headers={"Authorization": f"Token {api_key}"},
            timeout=30,
            params=params,
        )
        if response.status_code == 401:
            raise SketchfabError("Authentication failed (401). Check your API key.")
        if response.status_code == 404 and missing is not None:
            raise SketchfabError(missing)
        if response.status_code != 200:
            raise SketchfabError(f"{failure}{response.status_code}")
        data: object = response.json()
        return _json_object(data) if data is not None else None

    def get_sketchfab_status(self) -> dict[str, object]:
        if not self._enabled():
            return {
                "enabled": False,
                "message": "Sketchfab integration is currently disabled. Enable Sketchfab in the MCP for Blender panel and enter your API key.",
            }
        api_key = self._api_key()
        if not api_key:
            return {
                "enabled": False,
                "message": "Sketchfab integration is currently enabled, but API key is not given. Enter your Sketchfab API key in the MCP for Blender panel.",
            }
        try:
            response = _http_get(
                f"{API_URL}/me",
                headers={"Authorization": f"Token {api_key}"},
                timeout=30,
            )
            if response.status_code != 200:
                return {
                    "enabled": False,
                    "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}",
                }
            username = _json_object(response.json()).get("username", "Unknown user")
            return {
                "enabled": True,
                "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}",
            }
        except requests.exceptions.Timeout:
            return {
                "enabled": False,
                "message": "Timeout connecting to Sketchfab API. Check your internet connection.",
            }
        except Exception as error:
            logger.exception("Error testing Sketchfab API key")
            return {
                "enabled": False,
                "message": f"Error testing Sketchfab API key: {error!s}",
            }

    def search_sketchfab_models(
        self,
        query: str,
        categories: str | None = None,
        count: int = 20,
        downloadable: bool = True,
    ) -> dict[str, object]:
        try:
            params: dict[str, str | int | bool] = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False,
            }
            if categories:
                params["categories"] = categories
            data = self._request_json(
                "search", "API request failed with status code ", params=params
            )
            if data is None:
                raise SketchfabError("Received empty response from Sketchfab API")
            if not isinstance(data.get("results", []), list):
                raise SketchfabError(
                    f"Unexpected response format from Sketchfab API: {data}"
                )
            return data
        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as error:
            return {"error": f"Invalid JSON response from Sketchfab API: {error!s}"}
        except SketchfabError as error:
            return {"error": str(error)}
        except Exception as error:
            logger.exception("Error searching Sketchfab models")
            return {"error": str(error)}

    def get_sketchfab_model_preview(self, uid: str) -> dict[str, object]:
        try:
            data = self._request_json(
                f"models/{uid}",
                "Failed to get model info: ",
                missing=f"Model not found: {uid}",
            )
            if data is None:
                raise SketchfabError("Received empty response from Sketchfab API")
            images = _json_object(data.get("thumbnails", {})).get("images", [])
            if not images:
                raise SketchfabError("No thumbnail available for this model")
            if not isinstance(images, list):
                raise SketchfabError("Unexpected thumbnail list from Sketchfab API")
            thumbnails = [_json_object(item) for item in images]
            thumbnail = next(
                (item for item in thumbnails if _medium_thumbnail(item)),
                thumbnails[0],
            )
            url = thumbnail.get("url")
            if not isinstance(url, str) or not url:
                raise SketchfabError("Thumbnail URL not found")
            response = _http_get(url, timeout=30)
            if response.status_code != 200:
                raise SketchfabError(
                    f"Failed to download thumbnail: {response.status_code}"
                )
            content_type = response.headers.get("Content-Type", "")
            return {
                "success": True,
                "image_data": base64.b64encode(response.content).decode("ascii"),
                "format": "png"
                if "png" in content_type or url.endswith(".png")
                else "jpeg",
                "model_name": data.get("name", "Unknown"),
                "author": _json_object(data.get("user", {})).get("username", "Unknown"),
                "uid": uid,
                "thumbnail_width": thumbnail.get("width"),
                "thumbnail_height": thumbnail.get("height"),
            }
        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except SketchfabError as error:
            return {"error": str(error)}
        except Exception as error:
            logger.exception("Error getting Sketchfab model preview")
            return {"error": f"Failed to get model preview: {error!s}"}

    def download_sketchfab_model(
        self, uid: str, normalize_size: bool = False, target_size: float = 1.0
    ) -> dict[str, object]:
        try:
            data = self._request_json(
                f"models/{uid}/download", "Download request failed with status code "
            )
            if data is None:
                raise SketchfabError(
                    "Received empty response from Sketchfab API for download request"
                )
            gltf = data.get("gltf")
            if not gltf:
                raise SketchfabError(
                    f"No gltf download URL available for this model. Response: {data}"
                )
            url = _json_object(gltf).get("url")
            if not isinstance(url, str) or not url:
                raise SketchfabError(
                    "No download URL available for this model. Make sure the model is downloadable and you have access."
                )
            response = _http_get(url, timeout=60)
            if response.status_code != 200:
                raise SketchfabError(
                    f"Model download failed with status code {response.status_code}"
                )
            return _import_archive(response.content, normalize_size, target_size)
        except requests.exceptions.Timeout:
            return {
                "error": "Request timed out. Check your internet connection and try again with a simpler model."
            }
        except json.JSONDecodeError as error:
            return {"error": f"Invalid JSON response from Sketchfab API: {error!s}"}
        except SketchfabError as error:
            return {"error": str(error)}
        except Exception as error:
            logger.exception("Error downloading Sketchfab model")
            return {"error": f"Failed to download model: {error!s}"}
