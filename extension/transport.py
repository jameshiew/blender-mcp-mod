from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field

import bpy

from .connection import load_tls_context

logger = logging.getLogger(__name__)


@dataclass
class ClientState:
    socket: ssl.SSLSocket
    deadline: float | None
    handshaking: bool = True
    incoming: bytearray = field(default_factory=bytearray)
    nesting: list[int] = field(default_factory=list)
    in_string: bool = False
    escaped: bool = False
    complete: bool = False
    outgoing: bytes = b""
    sent: int = 0


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
        self.config_dir = config_dir
        self.poll: Callable[[], None] = lambda: None
        self._tls_context: ssl.SSLContext | None = None
        self.last_error = ""
        self.handshake_timeout = 5.0
        self.read_timeout = 30.0
        self.write_timeout = 30.0
        self.max_clients = 16
        self.max_message_bytes = 32 * 1024 * 1024
        self.tick_budget = 0.01
        self.io_chunk_bytes = 64 * 1024
        self.io_steps = 4
        self._clients: dict[ssl.SSLSocket, ClientState] = {}
        self._timer_callback = self._tick

    def start(self) -> None:
        if bpy.app.background:
            self.last_error = "The MCP listener requires Blender's GUI event loop"
            return
        if self.running:
            return
        self.last_error = ""
        try:
            self._tls_context = load_tls_context(self.config_dir)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.port = self.socket.getsockname()[1]
            self.socket.listen(self.max_clients)
            self.socket.setblocking(False)
            self.running = True
            if not bpy.app.timers.is_registered(self._timer_callback):
                bpy.app.timers.register(self._timer_callback, persistent=True)
            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as error:
            self.last_error = f"Secure connection unavailable: {error}. Run blender-mcp setup-connection, then start the server again."
            logger.exception("Failed to start server")
            self.stop()

    def stop(self) -> None:
        self.running = False
        try:
            if bpy.app.timers.is_registered(self._timer_callback):
                bpy.app.timers.unregister(self._timer_callback)
        except (RuntimeError, ValueError):
            logger.exception("Failed to unregister command timer")
        if self.socket is not None:
            with suppress(OSError):
                self.socket.close()
            self.socket = None
        for client in list(self._clients):
            self._close_client(client)
        self._tls_context = None

    def execute_command(self, command: Mapping[str, object]) -> Mapping[str, object]:
        raise NotImplementedError

    def _close_client(self, client: ssl.SSLSocket) -> None:
        self._clients.pop(client, None)
        with suppress(OSError):
            client.close()

    def _accept_clients(self) -> None:
        listener, context = self.socket, self._tls_context
        if listener is None or context is None:
            return
        for _ in range(self.io_steps):
            try:
                client, _address = listener.accept()
            except BlockingIOError:
                break
            try:
                if len(self._clients) >= self.max_clients:
                    client.close()
                    continue
                client.setblocking(False)
                secured = context.wrap_socket(
                    client, server_side=True, do_handshake_on_connect=False
                )
                self._clients[secured] = ClientState(
                    secured, time.monotonic() + self.handshake_timeout
                )
            except Exception:
                client.close()
                logger.exception("Error accepting connection")

    def _service_client(self, state: ClientState) -> None:
        client = state.socket
        try:
            if state.deadline is not None and time.monotonic() >= state.deadline:
                raise TimeoutError("Blender client stalled")
            if state.handshaking:
                client.do_handshake()
                state.handshaking = False
                state.deadline = None
            if state.outgoing:
                for _ in range(self.io_steps):
                    chunk = state.outgoing[
                        state.sent : state.sent + self.io_chunk_bytes
                    ]
                    count = client.send(chunk)
                    if count == 0:
                        raise ConnectionError("Blender client closed during response")
                    state.sent += count
                    if state.sent == len(state.outgoing):
                        state.outgoing = b""
                        state.sent = 0
                        state.deadline = None
                        break
                return
            for _ in range(self.io_steps):
                try:
                    data = client.recv(self.io_chunk_bytes)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    break
                if not data:
                    self._close_client(client)
                    return
                if not state.incoming:
                    state.deadline = time.monotonic() + self.read_timeout
                state.incoming.extend(data)
                if len(state.incoming) > self.max_message_bytes:
                    raise ValueError("Blender command exceeds the 32 MiB limit")
                self._scan_message(state, data)
                if state.complete:
                    break
            if not state.complete:
                return
            manager = getattr(bpy.context, "window_manager", None)
            if getattr(manager, "is_interface_locked", False):
                state.deadline = None
                return
            command = json.loads(state.incoming)
            if not isinstance(command, dict):
                raise TypeError("Blender command must be a JSON object")
            state.incoming.clear()
            state.complete = False
            try:
                response = self.execute_command(command)
                response_json = json.dumps(response, allow_nan=False)
            except (Exception, SystemExit, KeyboardInterrupt) as error:
                logger.exception("Error executing command")
                response_json = json.dumps({"status": "error", "message": str(error)})
            state.outgoing = response_json.encode("utf-8")
            if len(state.outgoing) > self.max_message_bytes:
                state.outgoing = b'{"status":"error","message":"Blender response exceeds the 32 MiB limit; request fewer details"}'
            state.deadline = time.monotonic() + self.write_timeout
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
            return
        except (OSError, TypeError, ValueError):
            logger.debug("Closing Blender client", exc_info=True)
            self._close_client(client)

    @staticmethod
    def _scan_message(state: ClientState, chunk: bytes) -> None:
        for value in chunk:
            if state.complete:
                if value not in b" \t\r\n":
                    raise ValueError("Only one Blender command may be sent at a time")
            elif state.in_string:
                if state.escaped:
                    state.escaped = False
                elif value == 92:
                    state.escaped = True
                elif value == 34:
                    state.in_string = False
            elif not state.nesting:
                if value == 123:
                    state.nesting.append(125)
                elif value not in b" \t\r\n":
                    raise ValueError("Blender command must be a JSON object")
            elif value == 34:
                state.in_string = True
            elif value in (123, 91):
                state.nesting.append(125 if value == 123 else 93)
                if len(state.nesting) > 128:
                    raise ValueError("Blender command exceeds the JSON nesting limit")
            elif value in (125, 93):
                if state.nesting.pop() != value:
                    raise ValueError("Mismatched JSON container")
                state.complete = not state.nesting

    def _tick(self) -> float | None:
        if not self.running:
            return None
        try:
            self.poll()
        except Exception:
            logger.exception("Error polling Blender jobs")
        deadline = time.monotonic() + self.tick_budget
        try:
            self._accept_clients()
            for client, state in list(self._clients.items()):
                self._service_client(state)
                if not self.running:
                    return None
                if client in self._clients:
                    self._clients.pop(client)
                    self._clients[client] = state
                if time.monotonic() >= deadline:
                    break
        except Exception:
            logger.exception("Error polling Blender transport")
        return 0.01 if self._clients else 0.05
