"""Round 6: supervisor resilience (no live processes are touched; STATE is a temp dir)."""

import errno
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("live_supervisor_under_test", ROOT / "scripts/live_supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path)
    monkeypatch.setattr(module, "LOG", tmp_path / "supervisor.log")
    monkeypatch.setattr(module, "STOPPED", tmp_path / "supervisor.stopped")
    monkeypatch.setattr(module, "MARKER", tmp_path / "model.paused")
    return module


def events(sup):
    return [json.loads(line)["event"] for line in sup.LOG.read_text().splitlines()]


def test_restart_cap_is_time_windowed_and_escalates(sup):
    comp = {"name": "worker", "restart_times": []}
    now = 1_000_000.0
    for i in range(5):
        assert sup.restart_allowed(comp, now + i)
        comp["restart_times"].append(now + i)
    assert not sup.restart_allowed(comp, now + 10)
    assert comp["escalation"] == "RESTART_CAP_REACHED"
    assert comp["suppressed_until_epoch"] == now + sup.RESTART_WINDOW_S
    assert not sup.restart_allowed(comp, now + 100)  # still inside the window
    # Once the oldest restart leaves the 10-minute window the component is retried.
    assert sup.restart_allowed(comp, now + sup.RESTART_WINDOW_S + 0.5)
    assert "escalation" not in comp and len(comp["restart_times"]) == 4
    assert events(sup) == ["restart_cap_reached", "restart_cap_window_reopened"]


def test_state_write_survives_disk_full(sup, monkeypatch):
    real = Path.write_text

    def full(self, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "write_text", full)
    assert sup.write_state({"components": {}}) is False  # the round-4 supervisor died here
    monkeypatch.setattr(Path, "write_text", real)
    assert events(sup) == ["state_write_failed"]
    assert sup.write_state({"components": {}}) is True
    assert "heartbeat_utc" in json.loads((sup.STATE / "state.json").read_text())


def test_state_write_falls_back_when_replace_is_refused(sup, monkeypatch):
    def refused(self, target):
        raise PermissionError(errno.EACCES, "sharing violation")

    monkeypatch.setattr(Path, "replace", refused)
    assert sup.write_state({"components": {}, "x": 1}) is True
    assert json.loads((sup.STATE / "state.json").read_text())["x"] == 1


def write(sup, **state):
    (sup.STATE / "state.json").write_text(json.dumps({"components": {}, **state}))


def test_watchdog_decisions(sup, monkeypatch, capsys):
    import psutil

    started = []
    monkeypatch.setattr(sup, "start_detached", lambda argv: started.append(argv) or "4242")
    me = psutil.Process()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=1)).isoformat()

    def check():
        sup.watchdog()
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])["watchdog"]

    write(sup, supervisor_pid=os.getpid(), supervisor_created=me.create_time(),
          started_utc=old, heartbeat_utc=now.isoformat())
    assert check() == "healthy"
    sup.STOPPED.write_text("{}")
    assert check() == "operator_stopped_noop"
    sup.STOPPED.unlink()
    # Dead supervisor (PID gone): restarted with --adopt, then debounced for 5 minutes.
    write(sup, supervisor_pid=2**22 + 7, supervisor_created=1.0, started_utc=old, heartbeat_utc=old)
    assert check() == "restarted"
    assert started[-1][-2:] == ["run", "--adopt"]
    assert check() == "start_in_progress_noop"
    (sup.STATE / "watchdog.starting").unlink()
    # PID reused by an unrelated process (create time differs) counts as dead.
    write(sup, supervisor_pid=os.getpid(), supervisor_created=1.0, started_utc=old, heartbeat_utc=old)
    assert check() == "restarted"
    (sup.STATE / "watchdog.starting").unlink()
    # Alive but booting (waiting for the model) is never treated as hung.
    write(sup, supervisor_pid=os.getpid(), supervisor_created=me.create_time(),
          started_utc=now.isoformat(), heartbeat_utc=old)
    assert check() == "healthy"
    assert len(started) == 2
    assert "watchdog_restarted_supervisor" in events(sup)


def test_hung_supervisor_is_killed_alone_and_replaced(sup, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(sup, "start_detached", lambda argv: started.append(argv) or "4243")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        import psutil

        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        write(sup, supervisor_pid=sleeper.pid, supervisor_created=psutil.Process(sleeper.pid).create_time(),
              started_utc=old, heartbeat_utc=old)
        sup.watchdog()
        assert sleeper.wait(10) is not None  # the hung supervisor process was killed
        assert json.loads(capsys.readouterr().out.strip())["watchdog"] == "restarted"
        assert "watchdog_killed_hung_supervisor" in events(sup)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_adopted_handle_detects_exit_and_pid_reuse(sup):
    import psutil

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    handle = sup.Adopted(sleeper.pid, psutil.Process(sleeper.pid).create_time())
    assert handle.poll() is None
    sleeper.kill()
    sleeper.wait(10)
    assert handle.poll() == -1
    assert sup.Adopted(os.getpid(), 1.0).poll() == -1  # wrong create time = not ours
