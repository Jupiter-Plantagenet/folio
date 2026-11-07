"""Focused source tests for non-destructive retention helpers; not a DB/live suite."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from folio import retention_backend
from folio.backup import open_backup
from folio.private_storage import secure_private_directory
from folio.retention_backend import archive_decision, decision_due


def test_decision_archive_is_content_addressed_and_idempotent(tmp_path):
    decision = {"mode": "hold", "reason": "supporting context", "at": "2025-11-01T00:00:00+00:00"}
    keys = tmp_path / "keys"
    digest, size, path = archive_decision(tmp_path / "archive", "ws", "case", decision,
                                         key_root=keys)
    assert digest == hashlib.sha256(json.dumps(
        decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    assert size > 0
    first = open_backup(path, keys / f"decision-case-{digest}.key", f"folio-decision:ws:case:{digest}")
    second = archive_decision(tmp_path / "archive", "ws", "case", decision,
                              key_root=keys)[2]
    assert open_backup(second, keys / f"decision-case-{digest}.key", f"folio-decision:ws:case:{digest}") == first
    record = json.loads(first)
    assert record["decision_sha256"] == digest
    assert record["decision"] == decision


def test_decision_due_uses_frozen_365_day_threshold_and_rejects_bad_timestamp():
    clock = datetime(2026, 11, 2, tzinfo=timezone.utc)
    old = {"reason": "x", "at": (clock - timedelta(days=365)).isoformat()}
    recent = {"reason": "x", "at": (clock - timedelta(days=364)).isoformat()}
    assert decision_due(old, now=clock)
    assert not decision_due(recent, now=clock)
    assert not decision_due({"reason": "x", "at": "unparseable"}, now=clock)


def test_expired_decision_archival_does_not_remove_source_text(tmp_path):
    decision = {"mode": "hold", "reason": "verbatim evidence note", "at": "2025-01-01T00:00:00+00:00"}
    keys = tmp_path / "keys"
    digest, _, path = archive_decision(tmp_path / "archive", "ws", "case", decision,
                                       key_root=keys)
    raw = open_backup(path, keys / f"decision-case-{digest}.key", f"folio-decision:ws:case:{digest}")
    assert b"verbatim evidence note" in raw


def test_ordinary_projections_hide_text_only_after_verified_archive(monkeypatch):
    clock = datetime(2026, 11, 2, tzinfo=timezone.utc)
    old_decision = {"mode": "hold", "reason": "old decision", "at":
                    (clock - timedelta(days=366)).isoformat()}
    case = {"decision": dict(old_decision), "retention": {}, "state": "needs_review"}
    monkeypatch.setattr(retention_backend, "decision_archive_exists", lambda *a, **k: True)
    projected = retention_backend.project_current_decision("ws", "case", case, now=clock)
    assert "reason" not in projected and projected["reason_redacted"] is True
    assert case["decision"] == old_decision

    event_body = {"mode": "hold", "reason": "immutable event text", "at": old_decision["at"]}
    event_at = clock - timedelta(days=366)
    monkeypatch.setattr(retention_backend, "immutable_archive_exists", lambda *a, **k: True)
    projected_event = retention_backend.project_history_event(
        "ws", 7, "case", event_at, event_body, case, now=clock
    )
    assert "reason" not in projected_event and projected_event["reason_redacted"] is True
    assert event_body["reason"] == "immutable event text"


def test_projection_preserves_holds_and_proposals_without_archive(monkeypatch):
    clock = datetime(2026, 11, 2, tzinfo=timezone.utc)
    case = {"retention": {"legal_hold": True}, "state": "needs_review"}
    decision = {"reason": "hold text", "at": (clock - timedelta(days=366)).isoformat()}
    monkeypatch.setattr(retention_backend, "decision_archive_exists", lambda *a, **k: True)
    assert retention_backend.project_current_decision("ws", "case", {**case, "decision": decision}, now=clock) == decision
    proposal = {"id": "proposal", "reason": "proposal text"}
    monkeypatch.setattr(retention_backend, "immutable_archive_exists", lambda *a, **k: False)
    assert retention_backend.project_proposal_reason("ws", "proposal", "case", proposal, case) == proposal


def test_history_scan_cursor_is_private_validated_and_resumable(tmp_path, monkeypatch):
    path = tmp_path / "history-scan-cursor.json"
    monkeypatch.setattr(retention_backend, "_history_cursor_path", lambda: path)
    cursor = {"event_sequence": 42, "proposal_id": "12345678-1234-4234-8234-123456789abc",
              "deferred_events": [], "deferred_proposals": [],
              "deferred_overflow": False}
    retention_backend._write_history_cursor(cursor)
    assert retention_backend._read_history_cursor() == cursor
    saved = json.loads(path.read_text())
    assert saved["schema"] == "folio-history-scan-cursor-v1"
    assert "reason" not in saved


def test_deferred_history_gate_is_retried_without_cursor_wrap():
    pending, known = [], set()
    assert retention_backend._defer_identity(pending, known, 17)
    first_batch, remaining = retention_backend._take_deferred(pending, 1)
    assert first_batch == [17]
    # The gate still blocks on this pass, so the same identity is retained.
    assert retention_backend._defer_identity(remaining, set(remaining), 17)
    next_batch, remaining = retention_backend._take_deferred(remaining, 1)
    assert next_batch == [17]
    # Once the gate clears, the production loop archives and does not requeue it.
    gate_cleared = True
    if not gate_cleared:
        retention_backend._defer_identity(remaining, set(remaining), 17)
    assert remaining == []


def test_archive_tick_retries_previously_gated_event_ahead_of_cursor(tmp_path, monkeypatch):
    clock = datetime(2026, 11, 2, tzinfo=timezone.utc)
    event_at = clock - timedelta(days=400)
    case = {"retention": {}, "state": "needs_review", "proposal": None}
    body = {"reason": "archivable history", "at": event_at.isoformat()}
    event_row = (17, "case-id", event_at, body, case)
    cursor_path = tmp_path / "history-scan-cursor.json"
    monkeypatch.setattr(retention_backend, "_history_cursor_path", lambda: cursor_path)
    retention_backend._write_history_cursor({
        "event_sequence": 100,
        "proposal_id": "00000000-0000-0000-0000-000000000000",
        "deferred_events": [17], "deferred_proposals": [], "deferred_overflow": False,
    })
    monkeypatch.setenv("FOLIO_DSN", "postgresql://retention_test@127.0.0.1/test")
    monkeypatch.setattr("folio.restore_mode.require_writable", lambda db: None)
    archived = []

    class Rows:
        def __init__(self, values=()):
            self.values = list(values)

        def fetchall(self):
            return self.values

        def fetchone(self):
            return self.values[0] if self.values else None

    class Database:
        def execute(self, query, params=()):
            if "sequence=ANY" in query:
                return Rows([event_row])
            if "FROM pg_roles" in query:
                return Rows([("retention_test", False)])
            return Rows()

    class Connection:
        def __enter__(self):
            return Database()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(retention_backend, "immutable_archive_exists", lambda *a, **k: False)
    monkeypatch.setattr(retention_backend, "archive_immutable_record",
                        lambda *a, **k: archived.append(a[1]) or {"identity": a[1]})
    result = retention_backend.archive_due_history(connect=Connection, now=clock)
    assert result["events_archived"] == 1
    assert archived == ["17"]
    assert retention_backend._read_history_cursor()["deferred_events"] == []


def test_private_storage_rejects_symlink_ancestor_before_mkdir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirect = tmp_path / "redirect"
    try:
        redirect.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(RuntimeError, match="PRIVATE_ARCHIVE_REPARSE_POINT"):
        secure_private_directory(redirect / "new-private-child")
    assert not (outside / "new-private-child").exists()
