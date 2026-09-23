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
