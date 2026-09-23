import importlib.util
import sys
from types import SimpleNamespace

import pytest
from conftest import ROOT_ADDON
from extension_stub import _load_addon


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    addon, bpy = _load_addon(monkeypatch)
    monkeypatch.setitem(sys.modules, addon.__name__, addon)
    spec = importlib.util.spec_from_file_location(
        addon.__name__ + ".recovery", ROOT_ADDON.with_name("recovery.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, spec.name, module)
    bpy.context.mode = "OBJECT"
    server = addon.BlenderMCPServer(config_dir=tmp_path)
    return server, module, bpy


def item(name, **fields):
    return {"name": name, "type": "MESH", "library": None, "fields": fields}


def test_summary_distinguishes_rename_from_replacement_and_bounds_lists(recovery):
    _, module, _ = recovery
    before = {1: item("Old", geometry="a"), 2: item("Replaced")}
    after = {1: item("New", geometry="b"), 3: item("Replaced"), 4: item("Added")}
    result = module.compare_objects(before, after, limit=1)
    assert result["counts"] == {"created": 2, "changed": 1, "removed": 1}
    assert result["truncated"]
    assert result["created"] == [{"name": "Added", "type": "MESH", "library": None}]
    assert result["removed"][0]["name"] == "Replaced"
    assert result["changed"][0]["previous_name"] == "Old"
    assert result["changed"][0]["changed_fields"] == ["geometry", "name"]


def test_failed_execution_keeps_checkpoint_output_and_summary(recovery, monkeypatch):
    server, module, _ = recovery
    captures = iter([{}, {1: item("Left behind")}])
    monkeypatch.setattr(module, "capture_objects", lambda: next(captures))
    monkeypatch.setattr(
        server, "create_checkpoint", lambda **kw: {"checkpoint_id": "saved"}
    )
    result = server.execute_code(
        "print('before failure')\nraise ValueError('deliberate')",
        checkpoint=True,
        summarize_changes=True,
    )
    assert not result["executed"]
    assert result["result"] == "before failure\n"
    assert "ValueError: deliberate" in result["error_message"]
    assert result["checkpoint"]["checkpoint_id"] == "saved"
    assert result["changes"]["created"][0]["name"] == "Left behind"
    assert server.get_execution_result(result["execution_id"]) == result
    assert server.get_execution_result() == result


def test_final_inspection_error_does_not_mask_script_result(recovery, monkeypatch):
    server, module, _ = recovery

    def capture():
        if server._execution_namespaces:
            raise ValueError("Object mode required")
        return {}

    monkeypatch.setattr(module, "capture_objects", capture)
    result = server.execute_code(
        "raise ValueError('original')", namespace="task", summarize_changes=True
    )
    assert not result["executed"]
    assert "original" in result["error_message"]
    assert result["summary_error"] == "Object mode required"


def test_checkpoint_failure_prevents_code_and_namespace_reset(recovery, monkeypatch):
    server, _, _ = recovery
    server.execute_code("value = 7", namespace="task")

    def fail(**kw):
        raise OSError("Disk full")

    monkeypatch.setattr(server, "create_checkpoint", fail)
    with pytest.raises(OSError, match="Disk full"):
        server.execute_code(
            "value = 0", namespace="task", reset_namespace=True, checkpoint=True
        )
    assert server.execute_code("print(value)", namespace="task")["result"] == "7\n"


def test_results_retain_only_eight_opted_in_calls(recovery, monkeypatch):
    server, module, _ = recovery
    monkeypatch.setattr(module, "capture_objects", dict)
    results = [server.execute_code("pass", summarize_changes=True) for _ in range(9)]
    with pytest.raises(ValueError, match="not found"):
        server.get_execution_result(results[0]["execution_id"])
    server.execute_code("pass")
    assert server.get_execution_result() == results[-1]
    assert len(server._execution_results) == 8


@pytest.mark.parametrize("options", [{"checkpoint": 1}, {"summarize_changes": "yes"}])
def test_invalid_options_prevent_execution(recovery, options):
    server, _, _ = recovery
    with pytest.raises(ValueError, match="booleans"):
        server.execute_code("raise AssertionError('executed')", **options)


def test_storage_capacity_validation_and_partial_save_cleanup(
    recovery, monkeypatch, tmp_path
):
    _, module, bpy = recovery
    store = module.Checkpoints(tmp_path / "checkpoints")
    with pytest.raises(ValueError, match="checkpoint_id"):
        store.restore("../anything", lambda: None)
    with pytest.raises(ValueError, match="label"):
        store.create("")
    store.max_checkpoints = 0
    with pytest.raises(ValueError, match="full"):
        store.create()
    store.max_checkpoints = 32
    bpy.data = SimpleNamespace(filepath="", objects=[])
    bpy.context.scene.name = "Scene"
    bpy.context.scene.frame_current = 1

    def partial_save(filepath, **kw):
        from pathlib import Path

        Path(filepath).write_bytes(b"partial")
        raise OSError("Disk full")

    monkeypatch.setattr(bpy.ops.wm, "save_as_mainfile", partial_save, raising=False)
    with pytest.raises(OSError, match="Disk full"):
        store.create()
    assert list(store.directory.iterdir()) == []


def test_summary_requires_object_mode(recovery):
    _, module, bpy = recovery
    bpy.context.mode = "EDIT_MESH"
    with pytest.raises(ValueError, match="Object mode"):
        module.capture_objects()
