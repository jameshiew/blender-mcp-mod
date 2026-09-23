from __future__ import annotations

import json
import logging
import os
import queue
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress

import bpy

from .connection import load_tls_context

logger = logging.getLogger(__name__)


class CommandServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9876,
        config_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.running = False
        self.socket: socket.socket | None = None
        self.server_thread: threading.Thread | None = None
        self.config_dir = config_dir
        self.poll: Callable[[], None] = lambda: None
        self._tls_context: ssl.SSLContext | None = None
        self.last_error = ""
        self.handshake_timeout = 5.0
        self.max_clients = 16
        # Only the main-thread timer may execute commands or access Blender data.
        self.command_queue: queue.Queue[
            tuple[Mapping[str, object], queue.Queue[bytes]]
        ] = queue.Queue()
        self._clients: set[ssl.SSLSocket] = set()
        self._clients_lock = threading.Lock()

    def start(self) -> None:
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
            self._tls_context = load_tls_context(self.config_dir)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.port = self.socket.getsockname()[1]
            self.socket.listen(5)

            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # Timer registration must also happen on Blender's main thread.
            if not bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.register(self._drain_command_queue, persistent=True)

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            self.last_error = f"Secure connection unavailable: {e}. Run blender-mcp setup-connection, then start the server again."
            logger.exception("Failed to start server")
            self.stop()

    def stop(self) -> None:
        self.running = False
        try:
            if bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.unregister(self._drain_command_queue)
        except RuntimeError:
            logger.exception("Failed to unregister command timer")

        if self.socket:
            with suppress(OSError):
                self.socket.close()
            self.socket = None

        # Unblock client threads before the listener can restart.
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            with suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                client.close()

        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except RuntimeError:
                logger.exception("Failed to join server thread")
            self.server_thread = None

        print("BlenderMCP server stopped")

    def execute_command(self, command: Mapping[str, object]) -> Mapping[str, object]:
        raise NotImplementedError

    def _server_loop(self) -> None:
        print("Server thread started")
        listener, tls_context = self.socket, self._tls_context
        if listener is None or tls_context is None:
            return
        listener.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                try:
                    client, _address = listener.accept()
                    with self._clients_lock:
                        if not self.running or len(self._clients) >= self.max_clients:
                            client.close()
                            continue
                        try:
                            client = tls_context.wrap_socket(
                                client, server_side=True, do_handshake_on_connect=False
                            )
                        except Exception:
                            client.close()
                            raise
                        self._clients.add(client)

                    client_thread = threading.Thread(
                        target=self._handle_client, args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except TimeoutError:
                    continue
                except Exception:
                    logger.exception("Error accepting connection")
                    time.sleep(0.5)
            except Exception:
                logger.exception("Error in server loop")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _drain_command_queue(self) -> float | None:
        """Execute queued commands on Blender's main thread."""
        if not self.running:
            return None

        self.poll()

        while True:
            try:
                command, response_queue = self.command_queue.get_nowait()
            except queue.Empty:
                break

            try:
                response = self.execute_command(command)
                response_json = json.dumps(response)
            except Exception as e:
                logger.exception("Error executing command")
                response_json = json.dumps({"status": "error", "message": str(e)})

            response_queue.put(response_json.encode("utf-8"))

        return 0.05

    def _handle_client(self, client: ssl.SSLSocket) -> None:
        print("Client handler started")
        buffer = b""

        try:
            client.settimeout(self.handshake_timeout)
            client.do_handshake()
            client.settimeout(1.0)
            while self.running:
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    if len(buffer) > 32 * 1024 * 1024:
                        raise ValueError("Blender command exceeds the 32 MiB limit")
                    try:
                        command = json.loads(buffer.decode("utf-8"))
                        buffer = b""
                        if not isinstance(command, dict):
                            raise TypeError("Blender command must be a JSON object")

                        print(f"Queued command: {command.get('type')}")
                        response_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
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
                        # JSON and UTF-8 characters can span multiple recv calls.
                        pass
                except TimeoutError:
                    continue
                except (OSError, TypeError, ValueError) as e:
                    print(f"Error receiving data: {e}")
                    break
        except OSError as e:
            print(f"Error in client handler: {e}")
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            with suppress(OSError):
                client.close()
            print("Client handler stopped")
