"""Operator model pause: health says paused, agent runs fail closed, supervisor needs the lock."""

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paused(tmp_path, monkeypatch):
    marker = tmp_path / "model.paused"
    marker.write_text("{}")
    monkeypatch.setenv("FOLIO_MODEL_PAUSE_MARKER", str(marker))

    def no_network(*a, **k):
        raise AssertionError("no model request may be attempted while paused")

    monkeypatch.setattr(httpx, "Client", no_network)
    return marker


def test_health_reports_paused_not_ready(paused):
    from folio.workbench import model_readiness

    assert model_readiness() == "paused"


def test_agent_run_fails_closed_without_any_model_request(paused):
    from folio.agent import local_generate, run_bounded

    with pytest.raises(RuntimeError, match="MODEL_PAUSED"):
        local_generate([])
    result = run_bounded({"sources": []}, lambda: {})
    assert result["status"] == "blocked" and result["error"] == "MODEL_PAUSED"
    assert result["target_writes"] == 0 and result["quality_gate_passed"] is False


def test_marker_absent_is_not_paused(tmp_path, monkeypatch):
    from folio.workbench import model_readiness

    monkeypatch.setenv("FOLIO_MODEL_PAUSE_MARKER", str(tmp_path / "absent"))
    assert model_readiness() in ("ready-local-loopback", "unavailable")


@pytest.mark.parametrize("command", ["pause-model", "resume-model"])
def test_supervisor_pause_resume_require_build_lock_holder(command):
    marker = ROOT / ".local/live-round4/model.paused"
    before = marker.read_bytes() if marker.exists() else None
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/live_supervisor.py"),
            command,
            "--lock-owner",
            "not-the-holder-x9",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode != 0 and "BUILD_LOCK_HOLDER_REQUIRED" in r.stderr
    after = marker.read_bytes() if marker.exists() else None
    assert after == before
