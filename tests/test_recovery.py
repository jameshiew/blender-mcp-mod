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
    result = module.summarize_changes(module.compare_objects(before, after), limit=1)
    assert result["counts"] == {"created": 2, "changed": 1, "removed": 1}
    assert result["created"]["has_more"]
    assert result["created"]["items"] == [
        {"name": "Added", "type": "MESH", "library": None}
    ]
    assert result["removed"]["items"][0]["name"] == "Replaced"
    assert result["changed"]["items"][0]["previous_name"] == "Old"
    assert result["changed"]["items"][0]["changed_fields"] == ["geometry", "name"]


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
    assert not result["succeeded"]
    assert result["started"] and not result["succeeded"]
    assert result["partial_changes"] is True
    assert result["result"] == "before failure\n"
    assert "ValueError: deliberate" in result["error_message"]
    assert result["checkpoint"]["checkpoint_id"] == "saved"
    assert result["changes"]["created"]["items"][0]["name"] == "Left behind"
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
    assert not result["succeeded"]
    assert result["partial_changes"] is None
    assert "original" in result["error_message"]
    assert result["summary_error"] == "Object mode required"


def test_checkpoint_failure_prevents_code_and_namespace_reset(recovery, monkeypatch):
    server, _, _ = recovery
    server.execute_code("value = 7", namespace="task")

    def fail(**kw):
        raise OSError("Disk full")

    monkeypatch.setattr(server, "create_checkpoint", fail)
    result = server.execute_code(
        "value = 0", namespace="task", reset_namespace=True, checkpoint=True
    )
    assert not result["started"] and not result["succeeded"]
    assert result["partial_changes"] is False
    assert "Disk full" in result["error_message"]
    assert server.get_execution_result(result["execution_id"]) == result
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
    assert len(server._execution_changes) == 8
    with pytest.raises(ValueError, match="not found"):
        server.get_execution_changes(results[0]["execution_id"], "created")


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


def test_full_changes_are_pageable_after_later_scene_edits(recovery, monkeypatch):
    server, module, _ = recovery
    captures = iter([{}, {i: item(f"Object {i:03}") for i in range(214)}])
    monkeypatch.setattr(module, "capture_objects", lambda: next(captures))
    result = server.execute_code(
        "raise RuntimeError('partial')", summarize_changes=True
    )
    assert result["changes"]["counts"]["created"] == 214
    assert len(result["changes"]["created"]["items"]) == 100
    assert result["changes"]["created"]["next_offset"] == 100
    server.execute_code("later = True")
    names = []
    offset = 0
    while True:
        page = server.get_execution_changes(
            result["execution_id"], "created", offset, 80
        )["changes"]
        names.extend(row["name"] for row in page["items"])
        assert not page["details_omitted"]
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]
    assert names == [f"Object {i:03}" for i in range(214)]
    beyond = server.get_execution_changes(result["execution_id"], "created", 999)[
        "changes"
    ]
    assert beyond["items"] == [] and not beyond["has_more"]
    assert (
        server.get_execution_changes(result["execution_id"], "removed")["changes"][
            "count"
        ]
        == 0
    )
    for options in [
        {"category": "invalid"},
        {"category": "created", "offset": True},
        {"category": "created", "limit": 101},
    ]:
        with pytest.raises(ValueError):
            server.get_execution_changes(result["execution_id"], **options)
    server.stop()
    assert not server._execution_changes


def test_preparation_and_syntax_failures_remain_retrievable(recovery, monkeypatch):
    server, module, bpy = recovery
    syntax = server.execute_code("if True", summarize_changes=True)
    assert not syntax["started"] and syntax["partial_changes"] is False
    assert "SyntaxError" in syntax["error_message"]
    assert server.get_execution_result() == syntax
    bpy.context.mode = "EDIT_MESH"
    preparation = server.execute_code(
        "raise AssertionError('ran')", summarize_changes=True
    )
    assert not preparation["started"]
    assert "Object mode" in preparation["error_message"]
    assert server.get_execution_result() == preparation
    with pytest.raises(ValueError, match="No change summary"):
        server.get_execution_changes(preparation["execution_id"], "created")
