import base64
import io
import json
import logging
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import bpy
import requests

from .geometry import world_bounding_box

logger = logging.getLogger(__name__)
API_URL = "https://api.sketchfab.com/v3"


class SketchfabError(Exception):
    pass


def _http_get(url, **kwargs):
    if not bpy.app.online_access:
        raise RuntimeError("Online access is disabled in Blender preferences")
    return requests.get(url, **kwargs)


def _mesh_bounds(meshes):
    bounds = None
    for mesh in meshes:
        mesh_bounds = world_bounding_box(mesh)
        if bounds is None:
            bounds = mesh_bounds
        else:
            for axis in range(3):
                bounds[0][axis] = min(bounds[0][axis], mesh_bounds[0][axis])
                bounds[1][axis] = max(bounds[1][axis], mesh_bounds[1][axis])
    return bounds


def _import_archive(content, normalize_size, target_size):
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
            (path for path in directory.iterdir() if path.suffix in {".gltf", ".glb"}),
            None,
        )
        if main_file is None:
            raise SketchfabError("No glTF file found in the downloaded model")
        bpy.ops.import_scene.gltf(filepath=str(main_file))
        imported = list(bpy.context.selected_objects)

    roots = [obj for obj in imported if obj.parent is None]
    meshes = []
    pending = list(roots)
    while pending:
        obj = pending.pop()
        if obj.type == "MESH":
            meshes.append(obj)
        pending.extend(obj.children)

    bounds = _mesh_bounds(meshes)
    scale_applied = 1.0
    if bounds is not None and normalize_size:
        max_dimension = max(high - low for low, high in zip(*bounds))
        if max_dimension > 0:
            scale_applied = target_size / max_dimension
            for root in roots:
                root.scale = tuple(
                    component * scale_applied for component in root.scale
                )
            bpy.context.view_layer.update()
            bounds = _mesh_bounds(meshes)

    result = {
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
    def __init__(self, api_key, enabled):
        self._api_key = api_key
        self._enabled = enabled

    def _request_json(self, path, failure, *, missing=None, **kwargs):
        api_key = self._api_key()
        if not api_key:
            raise SketchfabError("Sketchfab API key is not configured")
        response = _http_get(
            f"{API_URL}/{path}",
            headers={"Authorization": f"Token {api_key}"},
            timeout=30,
            **kwargs,
        )
        if response.status_code == 401:
            raise SketchfabError("Authentication failed (401). Check your API key.")
        if response.status_code == 404 and missing is not None:
            raise SketchfabError(missing)
        if response.status_code != 200:
            raise SketchfabError(f"{failure}{response.status_code}")
        return response.json()

    def get_sketchfab_status(self):
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
            username = response.json().get("username", "Unknown user")
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
        self, query, categories=None, count=20, downloadable=True
    ):
        try:
            params = {
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

    def get_sketchfab_model_preview(self, uid):
        try:
            data = self._request_json(
                f"models/{uid}",
                "Failed to get model info: ",
                missing=f"Model not found: {uid}",
            )
            thumbnails = data.get("thumbnails", {}).get("images", [])
            if not thumbnails:
                raise SketchfabError("No thumbnail available for this model")
            thumbnail = next(
                (item for item in thumbnails if 400 <= item.get("width", 0) <= 800),
                thumbnails[0],
            )
            url = thumbnail.get("url")
            if not url:
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
                "author": data.get("user", {}).get("username", "Unknown"),
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

    def download_sketchfab_model(self, uid, normalize_size=False, target_size=1.0):
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
            url = gltf.get("url")
            if not url:
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
