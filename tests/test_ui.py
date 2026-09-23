from types import SimpleNamespace

import pytest


class Layout:
    def __init__(self):
        self.labels = []
        self.operators = []

    def box(self):
        return self

    def label(self, **values):
        self.labels.append(values["text"])

    def operator(self, name, **values):
        self.operators.append(name)

    def prop(self, *args, **kwargs):
        pass

    def separator(self):
        pass


@pytest.mark.parametrize("running, stale_flag", [(True, False), (False, True)])
def test_panel_uses_listener_status_and_port_after_scene_switch(
    addon, monkeypatch, running, stale_flag
):
    server = SimpleNamespace(running=running, port=12345, last_error="")
    monkeypatch.setattr(addon.ui.bpy.types, "blendermcp_server", server, raising=False)
    scene = SimpleNamespace(
        blendermcp_server_running=stale_flag,
        blendermcp_port=9876,
        blendermcp_use_sketchfab=False,
    )
    panel = addon.ui.BLENDERMCP_PT_Panel()
    panel.layout = Layout()
    panel.draw(SimpleNamespace(scene=scene))
    assert panel.layout.labels == (
        ["Connected on port 12345"] if running else ["Not connected"]
    )
    assert panel.layout.operators == (
        ["blendermcp.stop_server"] if running else ["blendermcp.start_server"]
    )


def test_panel_does_not_treat_saved_scene_flag_as_a_live_listener(addon):
    scene = SimpleNamespace(
        blendermcp_server_running=True,
        blendermcp_port=9876,
        blendermcp_use_sketchfab=False,
    )
    panel = addon.ui.BLENDERMCP_PT_Panel()
    panel.layout = Layout()
    panel.draw(SimpleNamespace(scene=scene))
    assert panel.layout.labels == ["Not connected"]


@pytest.mark.parametrize("handler", ["load_pre", "undo_pre", "redo_pre"])
def test_history_and_file_loads_clear_namespaces_but_preserve_results(
    addon, monkeypatch, handler
):
    server = addon.server.BlenderMCPServer()
    monkeypatch.setattr(addon.ui.bpy.types, "blendermcp_server", server, raising=False)
    server.execution.execute_code("cached = bpy.context.scene", namespace="task")
    server.execution._execution_results["previous"] = {"result": "kept"}
    handlers = getattr(addon.ui.bpy.app.handlers, handler)
    callback = next(
        callback for target, callback in addon.ui.HANDLERS if target is handlers
    )
    callback(None)
    assert not server.execution._execution_namespaces
    assert server.execution.get_execution_result("previous") == {"result": "kept"}


def test_stopped_listener_uses_newly_selected_port(addon, monkeypatch):
    server = addon.server.BlenderMCPServer(port=12345)
    monkeypatch.setattr(server, "start", lambda: None)
    monkeypatch.setattr(addon.ui.bpy.types, "blendermcp_server", server, raising=False)
    assert addon.ui.start_server(23456) is server
    assert server.port == 23456


def test_exit_handler_stops_and_removes_listener(addon):
    server = addon.server.BlenderMCPServer()
    addon.ui.bpy.types.blendermcp_server = server
    server.running = True
    addon.ui.shutdown_server(False)
    assert not server.running
    assert not hasattr(addon.ui.bpy.types, "blendermcp_server")
