# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025

import bpy
import mathutils
import json
import threading
import socket
import ssl
import stat
from pathlib import Path
import queue
import time
import tomllib
import requests
import tempfile
import traceback
import os
import shutil
import zipfile
from bpy.props import IntProperty
import io
import base64
from contextlib import redirect_stdout, suppress
from functools import cache


@cache
def _addon_metadata():
    directory = Path(__file__).parent
    with (directory / "blender_manifest.toml").open("rb") as file:
        manifest = tomllib.load(file)
    protocol = json.loads((directory / "protocol.json").read_text())["version"]
    return manifest, protocol


def _http_get(*args, **kwargs):
    if not bpy.app.online_access:
        raise RuntimeError("Online access is disabled in Blender preferences")
    return requests.get(*args, **kwargs)


def get_blendermcp_addon_preferences(context=None):
    """Get add-on preferences object if available."""
    if context is None:
        context = bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


class BlenderMCPServer:
    def __init__(self, host="127.0.0.1", port=9876, config_dir=None):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        self.config_dir = config_dir
        self._tls_context = None
        self.last_error = ""
        self.handshake_timeout = 5.0
        self.max_clients = 16
        # Commands are pushed here by client threads and drained by a single
        # timer running on Blender's main thread. bpy.app.timers is not
        # thread-safe, so registering a timer per command (the previous
        # approach) could silently drop the callback - on Windows especially -
        # leaving the client blocked in recv() until its socket timeout.
        self.command_queue = queue.Queue()
        # Live client sockets, so stop() can unblock threads parked in recv().
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._execution_namespaces = {}

    def _load_tls_context(self):
        directory = self.config_dir
        if directory is None:
            directory = os.environ.get(
                "BLENDER_MCP_CONFIG_DIR", str(Path.home() / ".blender-mcp")
            )
        directory = Path(directory)
        if not directory.is_absolute():
            raise ValueError("BLENDER_MCP_CONFIG_DIR must be an absolute path")
        path = directory / "credentials.json"
        for item, is_directory in ((directory, True), (path, False)):
            metadata = item.lstat()
            valid_type = (
                stat.S_ISDIR(metadata.st_mode)
                if is_directory
                else stat.S_ISREG(metadata.st_mode)
            )
            if not valid_type:
                raise ValueError(
                    f"{item} must be a regular {'directory' if is_directory else 'file'} (no symlinks)"
                )
            if os.name == "posix" and (
                metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077
            ):
                raise ValueError(
                    f"{item} must be owned by the current user with no group/other permissions"
                )
        with path.open(encoding="utf-8") as file:
            credentials = json.load(file)
        if credentials.get("version") != 1:
            raise ValueError("Unsupported Blender connection credentials")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=credentials["ca"])
        context.num_tickets = 0
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            chain = Path(temporary) / "server.pem"
            with chain.open("x", encoding="ascii") as file:
                file.write(credentials["server_cert"] + credentials["server_key"])
            context.load_cert_chain(chain)
        return context

    def _get_config_value(self, scene_attr, pref_attr=None, env_var=None):
        """Read config in order: addon preferences -> scene -> env var."""
        prefs = get_blendermcp_addon_preferences()
        if prefs and pref_attr:
            pref_value = getattr(prefs, pref_attr, "")
            if pref_value:
                return pref_value

        scene_value = getattr(bpy.context.scene, scene_attr, "")
        if scene_value:
            return scene_value

        if env_var:
            env_value = os.getenv(env_var, "")
            if env_value:
                return env_value
        return ""

    def _get_sketchfab_api_key(self):
        return self._get_config_value(
            "blendermcp_sketchfab_api_key",
            "sketchfab_api_key",
            "BLENDERMCP_SKETCHFAB_API_KEY",
        )

    def start(self):
        if bpy.app.background:
            print(
                "BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute\n"
                "BlenderMCP: run Blender with a GUI, or use a virtual display: xvfb-run -a blender"
            )
            return

        if self.running:
            print("Server is already running")
            return

        self.running = True
        self.last_error = ""

        try:
            self._tls_context = self._load_tls_context()
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.port = self.socket.getsockname()[1]
            # Backlog of 1 meant a reconnecting client could complete the TCP
            # handshake and then never be accept()ed - a connection that looks
            # established but is never serviced.
            self.socket.listen(5)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # start() is called from the operator, i.e. the main thread, so
            # this is the only safe place to touch bpy.app.timers.
            if not bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.register(self._drain_command_queue, persistent=True)

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            self.last_error = f"Secure connection unavailable: {e}. Run blender-mcp setup-connection, then start the server again."
            print(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False

        try:
            if bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.unregister(self._drain_command_queue)
        except Exception:
            pass

        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    with self._clients_lock:
                        if not self.running or len(self._clients) >= self.max_clients:
                            client.close()
                            continue
                        try:
                            client = self._tls_context.wrap_socket(
                                client, server_side=True, do_handshake_on_connect=False
                            )
                        except Exception:
                            client.close()
                            raise
                        self._clients.add(client)

                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client, args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _drain_command_queue(self):
        """Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.
        """
        if not self.running:
            return None

        while True:
            try:
                command, response_queue = self.command_queue.get_nowait()
            except queue.Empty:
                break

            try:
                response = self.execute_command(command)
                response_json = json.dumps(response)
            except Exception as e:
                print(f"Error executing command: {str(e)}")
                traceback.print_exc()
                response_json = json.dumps({"status": "error", "message": str(e)})

            response_queue.put(response_json.encode("utf-8"))

        return 0.05

    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        buffer = b""

        try:
            client.settimeout(self.handshake_timeout)
            client.do_handshake()
            client.settimeout(1.0)
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    if len(buffer) > 32 * 1024 * 1024:
                        raise ValueError("Blender command exceeds the 32 MiB limit")
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode("utf-8"))
                        buffer = b""
                        if not isinstance(command, dict):
                            raise ValueError("Blender command must be a JSON object")

                        # Hand off to the main thread. Never call
                        # bpy.app.timers.register() from here - it is not
                        # thread-safe and the callback can be silently lost.
                        print(f"Queued command: {command.get('type')}")
                        response_queue = queue.Queue(maxsize=1)
                        self.command_queue.put((command, response_queue))
                        while self.running:
                            try:
                                response = response_queue.get(timeout=0.1)
                                break
                            except queue.Empty:
                                continue
                        else:
                            break
                        try:
                            client.sendall(response)
                        except OSError as e:
                            print(f"Error sending response: {e}")
                            break
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # Incomplete data, wait for more. A multi-byte UTF-8
                        # character can land split across a recv() chunk
                        # boundary, which fails decode() before json.loads()
                        # ever runs - that's incomplete data too, not garbage.
                        pass
                except socket.timeout:
                    # Expected; loop round and re-check self.running.
                    continue
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Trivial liveness check. Touches no bpy data, so a successful ping
        # alongside a failing command isolates data access from transport.
        if cmd_type == "ping":
            return {"status": "success", "result": {"pong": True}}

        # Base handlers that are always available
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_addon_info": self.get_addon_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "get_sketchfab_status": self.get_sketchfab_status,
        }

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "get_sketchfab_model_preview": self.get_sketchfab_model_preview,
                "download_sketchfab_model": self.download_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

    def get_addon_info(self):
        """Version/capability handshake for the MCP server (and install tooling)."""
        manifest, protocol = _addon_metadata()
        version = manifest["version"]
        return {
            "name": manifest["name"],
            "addon_version": [
                int(part) for part in version.split("+")[0].split("-")[0].split(".")
            ],
            "addon_build_version": version,
            "protocol_version": protocol,
            "capabilities": sorted(
                [
                    "get_scene_info",
                    "get_addon_info",
                    "get_object_info",
                    "get_viewport_screenshot",
                    "execute_code",
                ]
            ),
            "blender_version": bpy.app.version_string,
        }

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            # Collect minimal object information (limit to first 10 objects)
            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:  # Reduced from 20 to 10
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [
                        round(float(obj.location.x), 2),
                        round(float(obj.location.y), 2),
                        round(float(obj.location.z), 2),
                    ],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
        world_bbox_corners = [
            obj.matrix_world @ corner for corner in local_bbox_corners
        ]
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))
        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [
                obj.rotation_euler.x,
                obj.rotation_euler.y,
                obj.rotation_euler.z,
            ],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == "MESH" and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        if not isinstance(max_size, int) or not 1 <= max_size <= 4096:
            return {"error": "max_size must be an integer between 1 and 4096"}
        if filepath:
            return self._save_viewport_screenshot(max_size, filepath, format)
        with tempfile.TemporaryDirectory(prefix="blender_mcp_") as directory:
            path = os.path.join(directory, "viewport.png")
            result = self._save_viewport_screenshot(max_size, path, "png")
            if result.get("success"):
                with open(path, "rb") as image:
                    result["image_data"] = base64.b64encode(image.read()).decode(
                        "ascii"
                    )
                result["format"] = "png"
                result.pop("filepath", None)
            return result

    def _save_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension of the image
        - filepath: Path where to save the screenshot file
        - format: Image format (png, jpg, etc.)

        Returns success/error status
        """
        # screen.screenshot_area captures the OS window framebuffer, which is
        # all-black whenever the Blender window is not composited in the
        # foreground (the normal case when Blender is driven headless-style via
        # MCP). Render the viewport with gpu.types.GPUOffScreen.draw_view3d
        # instead, which is independent of window compositing state, and fall
        # back to the window grab if offscreen rendering is unavailable (e.g. no
        # GPU context). The response reports which path produced the image.
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            area = region = space = None
            for a in bpy.context.screen.areas:
                if a.type == "VIEW_3D":
                    area = a
                    space = a.spaces.active
                    region = next((r for r in a.regions if r.type == "WINDOW"), None)
                    break

            if not area or region is None or space is None:
                return {"error": "No 3D viewport found"}

            method = "offscreen"
            try:
                import gpu
                import numpy as np

                r3d = space.region_3d
                src_w, src_h = region.width, region.height
                if max(src_w, src_h) > max_size:
                    s = max_size / max(src_w, src_h)
                    width, height = max(1, int(src_w * s)), max(1, int(src_h * s))
                else:
                    width, height = src_w, src_h

                offscreen = gpu.types.GPUOffScreen(width, height)
                try:
                    offscreen.draw_view3d(
                        bpy.context.scene,
                        bpy.context.view_layer,
                        space,
                        region,
                        r3d.view_matrix,
                        r3d.window_matrix,
                        do_color_management=True,
                    )
                    buf = offscreen.texture_color.read()
                finally:
                    offscreen.free()

                buf.dimensions = width * height * 4
                pixels = (
                    np.asarray(buf, dtype=np.float32) / 255.0
                )  # GPU buffer is 0..255

                image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
                image.pixels.foreach_set(pixels.ravel())
                image.filepath_raw = filepath
                image.file_format = format.upper()
                image.save()
                bpy.data.images.remove(image)

            except Exception as offscreen_err:
                print(
                    f"[BlenderMCP] offscreen capture failed ({offscreen_err}); "
                    "falling back to window grab",
                    flush=True,
                )
                method = "window_grab"
                with bpy.context.temp_override(area=area):
                    bpy.ops.screen.screenshot_area(filepath=filepath)
                img = bpy.data.images.load(filepath)
                width, height = img.size
                if max(width, height) > max_size:
                    s = max_size / max(width, height)
                    width, height = int(width * s), int(height * s)
                    img.scale(width, height)
                    img.file_format = format.upper()
                    img.save()
                bpy.data.images.remove(img)

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath,
                "method": method,
            }

        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code, namespace=None, reset_namespace=False):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution
        try:
            if namespace is not None and (
                not isinstance(namespace, str) or not 1 <= len(namespace) <= 128
            ):
                raise ValueError("namespace must be a string of 1 to 128 characters")
            if reset_namespace and namespace is None:
                raise ValueError("reset_namespace requires a namespace")
            if namespace is None:
                execution_namespace = {"bpy": bpy}
            else:
                if reset_namespace:
                    self._execution_namespaces.pop(namespace, None)
                execution_namespace = self._execution_namespaces.setdefault(
                    namespace, {"bpy": bpy}
                )

            # Capture stdout during execution, and return it as result
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, execution_namespace)

            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")

    def get_sketchfab_status(self):
        """Get the current status of Sketchfab integration"""
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = self._get_sketchfab_api_key()

        # Test the API key if present
        if api_key and enabled:
            try:
                headers = {"Authorization": f"Token {api_key}"}

                response = _http_get(
                    "https://api.sketchfab.com/v3/me",
                    headers=headers,
                    timeout=30,  # Add timeout of 30 seconds
                )

                if response.status_code == 200:
                    user_data = response.json()
                    username = user_data.get("username", "Unknown user")
                    return {
                        "enabled": True,
                        "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}",
                    }
                else:
                    return {
                        "enabled": False,
                        "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}",
                    }
            except requests.exceptions.Timeout:
                return {
                    "enabled": False,
                    "message": "Timeout connecting to Sketchfab API. Check your internet connection.",
                }
            except Exception as e:
                return {
                    "enabled": False,
                    "message": f"Error testing Sketchfab API key: {str(e)}",
                }

        if enabled and api_key:
            return {
                "enabled": True,
                "message": "Sketchfab integration is enabled and ready to use.",
            }
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently enabled, but API key is not given. To enable it:
                            1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            2. Keep the 'Use Sketchfab' checkbox checked
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude""",
            }
        else:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Sketchfab' checkbox
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude""",
            }

    def search_sketchfab_models(
        self, query, categories=None, count=20, downloadable=True
    ):
        """Search for models on Sketchfab based on query and optional filters"""
        try:
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Build search parameters with exact fields from Sketchfab API docs
            params = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False,
            }

            if categories:
                params["categories"] = categories

            # Make API request to Sketchfab search endpoint
            # The proper format according to Sketchfab API docs for API key auth
            headers = {"Authorization": f"Token {api_key}"}

            # Use the search endpoint as specified in the API documentation
            response = _http_get(
                "https://api.sketchfab.com/v3/search",
                headers=headers,
                params=params,
                timeout=30,  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {
                    "error": f"API request failed with status code {response.status_code}"
                }

            response_data = response.json()

            # Safety check on the response structure
            if response_data is None:
                return {"error": "Received empty response from Sketchfab API"}

            # Handle 'results' potentially missing from response
            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {
                    "error": f"Unexpected response format from Sketchfab API: {response_data}"
                }

            return response_data

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"error": str(e)}

    def get_sketchfab_model_preview(self, uid):
        """Get thumbnail preview image of a Sketchfab model by its UID"""
        try:
            import base64

            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            headers = {"Authorization": f"Token {api_key}"}

            # Get model info which includes thumbnails
            response = _http_get(
                f"https://api.sketchfab.com/v3/models/{uid}",
                headers=headers,
                timeout=30,
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code == 404:
                return {"error": f"Model not found: {uid}"}

            if response.status_code != 200:
                return {"error": f"Failed to get model info: {response.status_code}"}

            data = response.json()
            thumbnails = data.get("thumbnails", {}).get("images", [])

            if not thumbnails:
                return {"error": "No thumbnail available for this model"}

            # Find a suitable thumbnail (prefer medium size ~640px)
            selected_thumbnail = None
            for thumb in thumbnails:
                width = thumb.get("width", 0)
                if 400 <= width <= 800:
                    selected_thumbnail = thumb
                    break

            # Fallback to the first available thumbnail
            if not selected_thumbnail:
                selected_thumbnail = thumbnails[0]

            thumbnail_url = selected_thumbnail.get("url")
            if not thumbnail_url:
                return {"error": "Thumbnail URL not found"}

            # Download the thumbnail image
            img_response = _http_get(thumbnail_url, timeout=30)
            if img_response.status_code != 200:
                return {
                    "error": f"Failed to download thumbnail: {img_response.status_code}"
                }

            # Encode image as base64
            image_data = base64.b64encode(img_response.content).decode("ascii")

            # Determine format from content type or URL
            content_type = img_response.headers.get("Content-Type", "")
            if "png" in content_type or thumbnail_url.endswith(".png"):
                img_format = "png"
            else:
                img_format = "jpeg"

            # Get additional model info for context
            model_name = data.get("name", "Unknown")
            author = data.get("user", {}).get("username", "Unknown")

            return {
                "success": True,
                "image_data": image_data,
                "format": img_format,
                "model_name": model_name,
                "author": author,
                "uid": uid,
                "thumbnail_width": selected_thumbnail.get("width"),
                "thumbnail_height": selected_thumbnail.get("height"),
            }

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"error": f"Failed to get model preview: {str(e)}"}

    def download_sketchfab_model(self, uid, normalize_size=False, target_size=1.0):
        """Download a model from Sketchfab by its UID

        Parameters:
        - uid: The unique identifier of the Sketchfab model
        - normalize_size: If True, scale the model so its largest dimension equals target_size
        - target_size: The target size in Blender units (meters) for the largest dimension
        """
        try:
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Use proper authorization header for API key auth
            headers = {"Authorization": f"Token {api_key}"}

            # Request download URL using the exact endpoint from the documentation
            download_endpoint = f"https://api.sketchfab.com/v3/models/{uid}/download"

            response = _http_get(
                download_endpoint,
                headers=headers,
                timeout=30,  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {
                    "error": f"Download request failed with status code {response.status_code}"
                }

            data = response.json()

            # Safety check for None data
            if data is None:
                return {
                    "error": "Received empty response from Sketchfab API for download request"
                }

            # Extract download URL with safety checks
            gltf_data = data.get("gltf")
            if not gltf_data:
                return {
                    "error": "No gltf download URL available for this model. Response: "
                    + str(data)
                }

            download_url = gltf_data.get("url")
            if not download_url:
                return {
                    "error": "No download URL available for this model. Make sure the model is downloadable and you have access."
                }

            # Download the model (already has timeout)
            model_response = _http_get(download_url, timeout=60)  # 60 second timeout

            if model_response.status_code != 200:
                return {
                    "error": f"Model download failed with status code {model_response.status_code}"
                }

            # Save to temporary file
            temp_dir = tempfile.mkdtemp()
            zip_file_path = os.path.join(temp_dir, f"{uid}.zip")

            with open(zip_file_path, "wb") as f:
                f.write(model_response.content)

            # Extract the zip file with enhanced security
            with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
                # More secure zip slip prevention
                for file_info in zip_ref.infolist():
                    # Get the path of the file
                    file_path = file_info.filename

                    # Convert directory separators to the current OS style
                    # This handles both / and \ in zip entries
                    target_path = os.path.join(temp_dir, os.path.normpath(file_path))

                    # Get absolute paths for comparison
                    abs_temp_dir = os.path.abspath(temp_dir)
                    abs_target_path = os.path.abspath(target_path)

                    # Ensure the normalized path doesn't escape the target directory
                    if not abs_target_path.startswith(abs_temp_dir):
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {
                            "error": "Security issue: Zip contains files with path traversal attempt"
                        }

                    # Additional explicit check for directory traversal
                    if ".." in file_path:
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {
                            "error": "Security issue: Zip contains files with directory traversal sequence"
                        }

                # If all files passed security checks, extract them
                zip_ref.extractall(temp_dir)

            # Find the main glTF file
            gltf_files = [
                f
                for f in os.listdir(temp_dir)
                if f.endswith(".gltf") or f.endswith(".glb")
            ]

            if not gltf_files:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
                return {"error": "No glTF file found in the downloaded model"}

            main_file = os.path.join(temp_dir, gltf_files[0])

            # Import the model
            bpy.ops.import_scene.gltf(filepath=main_file)

            # Get the imported objects
            imported_objects = list(bpy.context.selected_objects)
            imported_object_names = [obj.name for obj in imported_objects]

            # Clean up temporary files
            with suppress(Exception):
                shutil.rmtree(temp_dir)

            # Find root objects (objects without parents in the imported set)
            root_objects = [obj for obj in imported_objects if obj.parent is None]

            # Helper function to recursively get all mesh children
            def get_all_mesh_children(obj):
                """Recursively collect all mesh objects in the hierarchy"""
                meshes = []
                if obj.type == "MESH":
                    meshes.append(obj)
                for child in obj.children:
                    meshes.extend(get_all_mesh_children(child))
                return meshes

            # Collect ALL meshes from the entire hierarchy (starting from roots)
            all_meshes = []
            for obj in root_objects:
                all_meshes.extend(get_all_mesh_children(obj))

            if all_meshes:
                # Calculate combined world bounding box for all meshes
                all_min = mathutils.Vector((float("inf"), float("inf"), float("inf")))
                all_max = mathutils.Vector(
                    (float("-inf"), float("-inf"), float("-inf"))
                )

                for mesh_obj in all_meshes:
                    # Get world-space bounding box corners
                    for corner in mesh_obj.bound_box:
                        world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                        all_min.x = min(all_min.x, world_corner.x)
                        all_min.y = min(all_min.y, world_corner.y)
                        all_min.z = min(all_min.z, world_corner.z)
                        all_max.x = max(all_max.x, world_corner.x)
                        all_max.y = max(all_max.y, world_corner.y)
                        all_max.z = max(all_max.z, world_corner.z)

                # Calculate dimensions
                dimensions = [
                    all_max.x - all_min.x,
                    all_max.y - all_min.y,
                    all_max.z - all_min.z,
                ]
                max_dimension = max(dimensions)

                # Apply normalization if requested
                scale_applied = 1.0
                if normalize_size and max_dimension > 0:
                    scale_factor = target_size / max_dimension
                    scale_applied = scale_factor

                    # ✅ Only apply scale to ROOT objects (not children!)
                    # Child objects inherit parent's scale through matrix_world
                    for root in root_objects:
                        root.scale = (
                            root.scale.x * scale_factor,
                            root.scale.y * scale_factor,
                            root.scale.z * scale_factor,
                        )

                    # Update the scene to recalculate matrix_world for all objects
                    bpy.context.view_layer.update()

                    # Recalculate bounding box after scaling
                    all_min = mathutils.Vector(
                        (float("inf"), float("inf"), float("inf"))
                    )
                    all_max = mathutils.Vector(
                        (float("-inf"), float("-inf"), float("-inf"))
                    )

                    for mesh_obj in all_meshes:
                        for corner in mesh_obj.bound_box:
                            world_corner = mesh_obj.matrix_world @ mathutils.Vector(
                                corner
                            )
                            all_min.x = min(all_min.x, world_corner.x)
                            all_min.y = min(all_min.y, world_corner.y)
                            all_min.z = min(all_min.z, world_corner.z)
                            all_max.x = max(all_max.x, world_corner.x)
                            all_max.y = max(all_max.y, world_corner.y)
                            all_max.z = max(all_max.z, world_corner.z)

                    dimensions = [
                        all_max.x - all_min.x,
                        all_max.y - all_min.y,
                        all_max.z - all_min.z,
                    ]

                world_bounding_box = [
                    [all_min.x, all_min.y, all_min.z],
                    [all_max.x, all_max.y, all_max.z],
                ]
            else:
                world_bounding_box = None
                dimensions = None
                scale_applied = 1.0

            result = {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": imported_object_names,
            }

            if world_bounding_box:
                result["world_bounding_box"] = world_bounding_box
            if dimensions:
                result["dimensions"] = [round(d, 4) for d in dimensions]
            if normalize_size:
                result["scale_applied"] = round(scale_applied, 6)
                result["normalized"] = True

            return result

        except requests.exceptions.Timeout:
            return {
                "error": "Request timed out. Check your internet connection and try again with a simpler model."
            }
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"error": f"Failed to download model: {str(e)}"}

    # endregion


# Blender Addon Preferences
class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    sketchfab_api_key: bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="Persistent Sketchfab API Key",
        default="",
    )

    def draw(self, context):
        layout = self.layout

        layout.label(text="Persistent API Credentials:", icon="LOCKED")
        cred_box = layout.box()
        cred_box.prop(self, "sketchfab_api_key", text="Sketchfab API Key")


# Blender UI Panel
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "MCP for Blender"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP for Blender"

    def _integration_header(self, layout, scene, prop_name, title, icon):
        """Draw an integration as a box with a checkbox header row.
        Returns the box if the integration is enabled (for settings), else None."""
        box = layout.box()
        row = box.row()
        row.prop(scene, prop_name, text="")
        row.label(text=title, icon=icon)
        return box if getattr(scene, prop_name) else None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = get_blendermcp_addon_preferences(context)

        # Connection
        box = layout.box()
        col = box.column()
        if scene.blendermcp_server_running:
            col.label(
                text=f"Connected on port {scene.blendermcp_port}", icon="CHECKMARK"
            )
            col.operator("blendermcp.stop_server", text="Disconnect", icon="X")
        else:
            col.label(text="Not connected", icon="RADIOBUT_OFF")
            server = getattr(bpy.types, "blendermcp_server", None)
            if server and server.last_error:
                col.label(text="Run blender-mcp setup-connection", icon="ERROR")
            col.prop(scene, "blendermcp_port")
            col.operator(
                "blendermcp.start_server", text="Connect to MCP server", icon="PLAY"
            )

        # Asset libraries
        layout.separator()
        layout.label(text="Asset Libraries", icon="ASSET_MANAGER")

        sub = self._integration_header(
            layout, scene, "blendermcp_use_sketchfab", "Sketchfab", "MESH_MONKEY"
        )
        if sub:
            col = sub.column(align=True)
            if prefs:
                col.prop(prefs, "sketchfab_api_key", text="API Key")
            else:
                col.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the MCP for Blender server to connect with Claude"

    def execute(self, context):
        scene = context.scene

        # Create a new server instance
        if (
            not hasattr(bpy.types, "blendermcp_server")
            or not bpy.types.blendermcp_server
        ):
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = bpy.types.blendermcp_server.running
        if not scene.blendermcp_server_running:
            self.report(
                {"ERROR"},
                bpy.types.blendermcp_server.last_error or "MCP server could not start",
            )
            return {"CANCELLED"}

        return {"FINISHED"}


# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop the connection to Claude"
    bl_description = "Stop the connection to Claude"

    def execute(self, context):
        scene = context.scene

        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {"FINISHED"}


# Registration functions
def register():
    _addon_metadata()
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the MCP for Blender server",
        default=9876,
        min=1024,
        max=65535,
    )

    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running", default=False
    )

    bpy.types.Scene.blendermcp_auto_start_server = bpy.props.BoolProperty(
        name="Auto-Start Server",
        description="Automatically start the MCP server when Blender loads",
        default=True,
    )

    bpy.types.Scene.blendermcp_use_sketchfab = bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False,
    )

    bpy.types.Scene.blendermcp_sketchfab_api_key = bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="API Key provided by Sketchfab",
        default="",
    )

    # Register preferences class
    bpy.utils.register_class(BLENDERMCP_AddonPreferences)

    bpy.utils.register_class(BLENDERMCP_PT_Panel)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)

    # Auto-start the server so the MCP client can connect without manual UI interaction
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        port = scene.blendermcp_port
        auto_start = scene.blendermcp_auto_start_server
    else:
        port = 9876
        auto_start = True

    if auto_start and (
        not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server
    ):
        bpy.types.blendermcp_server = BlenderMCPServer(port=port)
    if auto_start and not bpy.types.blendermcp_server.running:
        bpy.types.blendermcp_server.start()
        try:
            bpy.context.scene.blendermcp_server_running = (
                bpy.types.blendermcp_server.running
            )
        except AttributeError:
            pass

    print("BlenderMCP addon registered")


def unregister():

    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    bpy.utils.unregister_class(BLENDERMCP_PT_Panel)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)
    bpy.utils.unregister_class(BLENDERMCP_AddonPreferences)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running
    del bpy.types.Scene.blendermcp_auto_start_server
    del bpy.types.Scene.blendermcp_use_sketchfab
    del bpy.types.Scene.blendermcp_sketchfab_api_key

    print("BlenderMCP addon unregistered")
