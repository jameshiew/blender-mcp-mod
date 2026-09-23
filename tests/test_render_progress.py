import importlib.util
import json

import pytest

from conftest import ROOT_ADDON
from extension_stub import _load_addon


@pytest.fixture
def progress(monkeypatch, tmp_path):
    _load_addon(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "render_worker_test", ROOT_ADDON.with_name("render_worker.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RenderProgress(tmp_path), tmp_path


@pytest.mark.parametrize(
    "statistics, count, total, remaining",
    [
        ("Remaining: 01:02.50 | Mem: 5M | Sample 12/64", 12, 64, 62.5),
        (
            "Time:00:01.00 | Remaining:01:02:03.50 | Path Tracing Sample 1/128",
            1,
            128,
            3723.5,
        ),
        ("Mem: 5M | Sample 0/32", 0, 32, None),
        ("Compiling shaders", None, None, None),
    ],
)
def test_statistics_parse_engine_samples_and_time(
    progress, statistics, count, total, remaining
):
    reporter, directory = progress
    reporter.update(statistics, None)
    result = json.loads((directory / "status.json").read_text())
    assert result["phase"] == "rendering"
    assert result["progress"] == {
        "status_text": statistics,
        "samples_completed": count,
        "samples_total": total,
        "sample_fraction": count / total if total else None,
        "remaining_seconds": remaining,
    }


def test_stage_changes_clear_stale_eta_and_samples_can_reset(progress):
    reporter, directory = progress
    reporter.update("Remaining: 00:12 | Sample 32/64")
    reporter.update("Denoising")
    assert reporter.progress["remaining_seconds"] is None
    assert reporter.progress["samples_completed"] == 32
    reporter.update("Sample 0/64")
    assert reporter.progress["sample_fraction"] == 0
    reporter.update("Remaining: 00:01 | Sample 64/64")
    reporter.write("completed", width=10, height=20)
    result = json.loads((directory / "status.json").read_text())
    assert result["progress"]["remaining_seconds"] is None
    assert result["progress"]["sample_fraction"] == 1
    assert result["width"] == 10
    assert not (directory / "status.tmp").exists()


def test_unknown_stats_stay_unknown_and_text_is_bounded(progress):
    reporter, directory = progress
    reporter.update(None)
    assert not (directory / "status.json").exists()
    reporter.update("x" * 10000)
    assert len(reporter.progress["status_text"]) == 2048
    assert reporter.progress["sample_fraction"] is None
