from typing import Literal, Protocol

import bpy

# These interfaces cover dynamic Blender APIs missing from the upstream stubs.

class NodeInterface(Protocol):
    def new_socket(
        self, *, name: str, in_out: Literal["INPUT", "OUTPUT"], socket_type: str
    ) -> bpy.types.NodeTreeInterfaceSocket: ...

class IDCollection(Protocol):
    def remove(self, item: bpy.types.ID, *, do_unlink: bool = True) -> None: ...

class ColorSpace(Protocol):
    name: str

class RenderEngine(Protocol):
    engine: str

class SceneRegistration(Protocol):
    blendermcp_server_running: object
