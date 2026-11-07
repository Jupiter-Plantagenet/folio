"""Bounded private retention tasks for the supervised Folio runtime.

Business/evidence bytes are archived before access redaction. No key destruction
or business-row deletion is performed here.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

DECISION_DAYS = 365
DECISION_BATCH = 20
WORKSPACE = "11111111-1111-4111-8111-111111111111"
HISTORY_SCAN_PAGE = 50


def _history_cursor_path():
    from folio.private_storage import secure_private_directory

    directory = secure_private_directory(Path(__file__).absolute().parents[1] /
                                         ".local/retention")
    return directory / "history-scan-cursor.json"


def _read_history_cursor():
    path = _history_cursor_path()
    default = {"event_sequence": 0,
               "proposal_id": "00000000-0000-0000-0000-000000000000",
               "deferred_events": [], "deferred_proposals": [],
               "deferred_overflow": False}
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return default
    from folio.private_storage import secure_private_file

    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise RuntimeError("RETENTION_CURSOR_REPARSE_POINT")
    secure_private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("schema") != "folio-history-scan-cursor-v1" or
                value.get("workspace") != WORKSPACE or
                type(value.get("event_sequence")) is not int or value["event_sequence"] < 0 or
                str(UUID(value.get("proposal_id", ""))) != value["proposal_id"] or
                not isinstance(value.get("deferred_events", []), list) or
                len(value.get("deferred_events", [])) > 5000 or
                any(type(item) is not int or item < 1 for item in value.get("deferred_events", [])) or
                not isinstance(value.get("deferred_proposals", []), list) or
                len(value.get("deferred_proposals", [])) > 5000 or
                any(str(UUID(item)) != item for item in value.get("deferred_proposals", [])) or
                type(value.get("deferred_overflow", False)) is not bool):
            raise ValueError
        return {"event_sequence": value["event_sequence"],
                "proposal_id": value["proposal_id"],
                "deferred_events": value.get("deferred_events", []),
                "deferred_proposals": value.get("deferred_proposals", []),
                "deferred_overflow": value.get("deferred_overflow", False)}
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("RETENTION_CURSOR_INVALID") from error


def _write_history_cursor(value):
    from folio.private_storage import secure_private_file

    path = _history_cursor_path()
    try:
        current_metadata = path.lstat()
    except FileNotFoundError:
        current_metadata = None
    if current_metadata is not None and (
            path.is_symlink() or getattr(current_metadata, "st_file_attributes", 0) & 0x400):
        raise RuntimeError("RETENTION_CURSOR_REPARSE_POINT")
    payload = {"schema": "folio-history-scan-cursor-v1", "workspace": WORKSPACE,
               **value}
    temp = path.with_suffix(".tmp")
    if temp.exists():
        if temp.is_symlink() or getattr(temp.lstat(), "st_file_attributes", 0) & 0x400:
            raise RuntimeError("RETENTION_CURSOR_REPARSE_POINT")
        temp.unlink()
    with temp.open("xb") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        stream.flush()
        os.fsync(stream.fileno())
    secure_private_file(temp, inherited=True)
    os.replace(temp, path)
    secure_private_file(path, inherited=True)


def _defer_identity(queue, known, identity):
    if identity in known:
        return True
    if len(queue) >= 5000:
        return False
    queue.append(identity)
    known.add(identity)
    return True


def _take_deferred(queue, limit):
    """Retry a bounded prefix now; preserve the rest for the next tick."""
    return list(queue[:limit]), list(queue[limit:])


def archive_decision(root, workspace, case_id, decision, *, key_root=None):
    """Durably archive encrypted canonical decision bytes with separate key custody."""
    from folio.backup import open_backup, seal_backup
    from folio.private_storage import secure_private_directory, secure_private_file

    raw = json.dumps(decision, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    record = json.dumps({
        "schema": "folio-decision-archive-v1",
        "workspace": str(workspace),
        "case_id": str(case_id),
        "decision_sha256": digest,
        "decision_bytes": len(raw),
        "decision": decision,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    root = secure_private_directory(root)
    destination = root / f"decision-{case_id}-{digest}.json.enc"
    if key_root is None:
        key_root = Path(__file__).absolute().parents[1] / ".local/backup-keys/decision-retention"
    key_root = secure_private_directory(key_root)
    key_path = key_root / f"decision-{case_id}-{digest}.key"
    context = f"folio-decision:{workspace}:{case_id}:{digest}"
    if destination.exists():
        if destination.is_symlink() or getattr(destination.lstat(), "st_file_attributes", 0) & 0x400:
            raise RuntimeError("RETENTION_ARCHIVE_REPARSE_POINT")
        if not key_path.is_file() or key_path.is_symlink():
            raise RuntimeError("RETENTION_ARCHIVE_COLLISION")
        secure_private_file(destination)
        secure_private_file(key_path)
        existing = open_backup(destination, key_path, context)
        if existing != record:
            raise RuntimeError("RETENTION_ARCHIVE_COLLISION")
        return digest, len(raw), destination
    seal_backup(record, destination, key_path, context)
    secure_private_file(destination, inherited=True)
    secure_private_file(key_path, inherited=True)
    return digest, len(raw), destination


def archive_immutable_record(kind, identity, workspace, case_id, created_at, payload,
                            *, archive_root, key_root):
    """Encrypt an immutable event/proposal projection source without rewriting it."""
    from folio.backup import open_backup, seal_backup
    from folio.private_storage import secure_private_directory, secure_private_file

    if kind not in {"event", "proposal"}:
        raise ValueError("RETENTION_HISTORY_ID_INVALID")
    identity = str(identity)
    if not (identity.isdigit() or re.fullmatch(r"[0-9a-fA-F-]{36}", identity)):
        raise ValueError("RETENTION_HISTORY_ID_INVALID")
    source = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False).encode("utf-8")
    source_digest = hashlib.sha256(source).hexdigest()
    record = json.dumps({
        "schema": "folio-decision-history-archive-v1",
        "kind": kind,
        "identity": str(identity),
        "workspace": str(workspace),
        "case_id": str(case_id),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        "source_sha256": source_digest,
        "source_bytes": len(source),
        "source_payload": payload,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    archive_root = secure_private_directory(archive_root)
    key_root = secure_private_directory(key_root)
    base = f"{kind}-{identity}-{source_digest}"
    encrypted, key_path = archive_root / (base + ".json.enc"), key_root / (base + ".key")
    context = f"folio-decision-history:{workspace}:{case_id}:{kind}:{identity}:{source_digest}"
    if encrypted.exists() or key_path.exists():
        if not encrypted.is_file() or not key_path.is_file():
            raise RuntimeError("RETENTION_HISTORY_ARCHIVE_INCOMPLETE")
        secure_private_file(encrypted, inherited=True)
        secure_private_file(key_path, inherited=True)
        if open_backup(encrypted, key_path, context) != record:
            raise RuntimeError("RETENTION_HISTORY_ARCHIVE_COLLISION")
    else:
        seal_backup(record, encrypted, key_path, context)
        secure_private_file(encrypted)
        secure_private_file(key_path)
        if open_backup(encrypted, key_path, context) != record:
            raise RuntimeError("RETENTION_HISTORY_ARCHIVE_READBACK_FAILED")
    return {"kind": kind, "identity": str(identity), "source_sha256": source_digest,
            "source_bytes": len(source), "archive_ref": encrypted.name}


def immutable_archive_exists(kind, identity, workspace, case_id, payload, *, archive_root,
                             key_root):
    """Return true only when a matching encrypted archive authenticates and opens."""
    from folio.backup import open_backup

    source = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False).encode("utf-8")
    source_digest = hashlib.sha256(source).hexdigest()
    base = f"{kind}-{identity}-{source_digest}"
    from folio.private_storage import _validate_existing_ancestors

    encrypted = Path(archive_root) / (base + ".json.enc")
    key_path = Path(key_root) / (base + ".key")
    # Validate every existing ancestor before is_file/lstat can follow a junction.
    _validate_existing_ancestors(encrypted)
    _validate_existing_ancestors(key_path)
    if not encrypted.is_file() or not key_path.is_file():
        return False
    if (encrypted.is_symlink() or key_path.is_symlink() or
            getattr(encrypted.lstat(), "st_file_attributes", 0) & 0x400 or
            getattr(key_path.lstat(), "st_file_attributes", 0) & 0x400):
        return False
    context = f"folio-decision-history:{workspace}:{case_id}:{kind}:{identity}:{source_digest}"
    record = json.loads(open_backup(encrypted, key_path, context))
    return record.get("source_sha256") == source_digest and record.get("source_payload") == payload


def decision_archive_exists(workspace, case_id, decision, *, archive_root=None, key_root=None):
    """Authenticate a content-addressed current-decision archive for read projection."""
    from folio.backup import open_backup
    from folio.private_storage import _validate_existing_ancestors

    source = json.dumps(decision, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(source).hexdigest()
    root = Path(archive_root or (Path(__file__).absolute().parents[1] /
                                 ".local/retention/decisions"))
    keys = Path(key_root or (Path(__file__).absolute().parents[1] /
                             ".local/backup-keys/decision-retention"))
    encrypted = root / f"decision-{case_id}-{digest}.json.enc"
    key_path = keys / f"decision-{case_id}-{digest}.key"
    _validate_existing_ancestors(encrypted)
    _validate_existing_ancestors(key_path)
    if not encrypted.is_file() or not key_path.is_file():
        return False
    if (encrypted.is_symlink() or key_path.is_symlink() or
            getattr(encrypted.lstat(), "st_file_attributes", 0) & 0x400 or
            getattr(key_path.lstat(), "st_file_attributes", 0) & 0x400):
        return False
    context = f"folio-decision:{workspace}:{case_id}:{digest}"
    record = json.loads(open_backup(encrypted, key_path, context))
    return (record.get("decision_sha256") == digest and
            record.get("decision") == decision and
            record.get("workspace") == str(workspace) and
            record.get("case_id") == str(case_id))


def project_current_decision(workspace, case_id, case, *, now=None):
    """Return only the redacted read projection after authenticated archive exists."""
    decision = case.get("decision")
    if not decision_due(decision, now=now) or not _safe_to_redact(case):
        return decision
    if not decision_archive_exists(workspace, case_id, decision):
        return decision
    projected = dict(decision)
    projected.pop("reason", None)
    projected.update({"reason_redacted": True,
                      "archive_sha256": hashlib.sha256(json.dumps(
                          decision, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")).hexdigest()})
    return projected


def project_history_event(workspace, sequence, case_id, at, body, case, *, now=None):
    """Read-only projection for one immutable event; the DB row is never rewritten."""
    if not isinstance(body, dict) or not isinstance(body.get("reason"), str):
        return body
    if not timestamp_due(at, now=now) or not _safe_to_redact(case):
        return body
    payload = {"kind": "DOC-decision-saved", "at": at.isoformat(), "body": body}
    archive_root = Path(__file__).absolute().parents[1] / ".local/retention/decision-history"
    key_root = Path(__file__).absolute().parents[1] / ".local/backup-keys/decision-history"
    if not immutable_archive_exists("event", str(sequence), workspace, case_id,
                                    payload, archive_root=archive_root, key_root=key_root):
        return body
    projected = dict(body)
    projected.pop("reason", None)
    projected.update({"reason_redacted": True,
                      "archive_sha256": hashlib.sha256(json.dumps(
                          body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")).hexdigest()})
    return projected


def project_proposal_reason(workspace, proposal_id, case_id, proposal, case):
    """Redact only a proposal copy whose exact body has a verified due archive."""
    if not isinstance(proposal, dict) or not isinstance(proposal.get("reason"), str):
        return proposal
    if not _safe_to_redact(case):
        return proposal
    source_digest = hashlib.sha256(json.dumps(
        proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    payload = {"body": proposal, "source_body_sha256": source_digest}
    archive_root = Path(__file__).absolute().parents[1] / ".local/retention/decision-history"
    key_root = Path(__file__).absolute().parents[1] / ".local/backup-keys/decision-history"
    if not immutable_archive_exists("proposal", str(proposal_id), workspace, case_id,
                                    payload, archive_root=archive_root, key_root=key_root):
        return proposal
    projected = dict(proposal)
    projected.pop("reason", None)
    projected.update({"reason_redacted": True, "archive_sha256": source_digest})
    return projected


def decision_due(decision, *, now=None):
    """Return true only for a well-formed decision older than the frozen 365 days."""
    if not isinstance(decision, dict) or not isinstance(decision.get("reason"), str):
        return False
    stamp = decision.get("at")
    if not isinstance(stamp, str):
        return False
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        return False
    now = now or datetime.now(timezone.utc)
    return at <= now - timedelta(days=DECISION_DAYS)


def timestamp_due(at, *, now=None):
    if not isinstance(at, datetime) or at.tzinfo is None:
        return False
    return at <= (now or datetime.now(timezone.utc)) - timedelta(days=DECISION_DAYS)


def _safe_to_redact(case):
    retention = case.get("retention") or {}
    if retention.get("legal_hold") or retention.get("pinned_release_evidence"):
        return False
    # Keep text accessible while the business effect may still need reconciliation.
    proposal = case.get("proposal")
    if proposal and proposal.get("gate") != "applied":
        return False
    if case.get("state") in {"seal_pending", "sealed", "uncertain", "verified",
                              "awaiting_approval", "dispatching"}:
        return False
    return True


def _assert_runtime_identity(db):
    configured = os.getenv("FOLIO_DSN", "")
    expected = urlsplit(configured).username if configured else None
    identity = db.execute(
        "SELECT current_user,rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if not identity or not expected or identity[0] != expected or identity[1]:
        raise RuntimeError("RETENTION_RUNTIME_IDENTITY_REFUSED")


def redact_due_decisions(limit=DECISION_BATCH, *, archive_root=None, connect=None):
    """Archive then redact old case decision text, rechecking holds under row lock.

    Only the mutable current case-body projection is changed. Immutable event and
    proposal payloads are separately archived; their response projections consult
    those exact encrypted archives and leave verifier-chain bytes unchanged.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("RETENTION_BATCH_LIMIT_INVALID")
    if archive_root is None:
        from pathlib import Path

        archive_root = Path(__file__).absolute().parents[1] / ".local/retention/decisions"
    if connect is None:
        from folio.workbench import connect as connect
    from psycopg.types.json import Jsonb

    from folio.restore_mode import require_writable

    changed = 0
    with connect() as db:
        require_writable(db)
        _assert_runtime_identity(db)
        rows = db.execute(
            "SELECT workspace,id,body FROM cases "
            "WHERE workspace=%s AND body->'decision'->>'reason' IS NOT NULL "
            "AND octet_length(body::text)<=1048576 "
            "AND body->'decision'->>'at' ~ '^\\d{4}-\\d{2}-\\d{2}T' "
            "AND coalesce(body->'retention'->>'legal_hold','false')!='true' "
            "AND coalesce(body->'retention'->>'pinned_release_evidence','false')!='true' "
            "AND body->>'state' NOT IN ('seal_pending','sealed','uncertain','verified','awaiting_approval','dispatching') "
            "AND (body->'proposal' IS NULL OR body->'proposal'='null'::jsonb OR body->'proposal'->>'gate'='applied') "
            "ORDER BY body->'decision'->>'at',id LIMIT %s FOR UPDATE SKIP LOCKED",
            (WORKSPACE, min(200, limit * 2))
        ).fetchall()
        for workspace, case_id, case in rows:
            decision = case.get("decision")
            if not decision_due(decision) or not _safe_to_redact(case):
                continue
            digest, length, archive = archive_decision(archive_root, workspace, case_id, decision)
            redacted = dict(decision)
            redacted.pop("reason", None)
            redacted.update({"reason_redacted": True, "archive_sha256": digest,
                             "archive_bytes": length})
            retention = dict(case.get("retention") or {})
            retention["decision_text_archived_at"] = datetime.now(timezone.utc).isoformat()
            retention["decision_text_archive_sha256"] = digest
            case["retention"] = retention
            case["decision"] = redacted
            case["version"] = int(case.get("version", 0)) + 1
            db.execute("UPDATE cases SET body=%s WHERE workspace=%s AND id=%s",
                       (Jsonb(case), workspace, case_id))
            db.execute(
                "INSERT INTO events(workspace,case_id,kind,actor,body) VALUES(%s,%s,%s,%s,%s)",
                (workspace, case_id, "DOC-decision-text-archived", "retention-worker",
                 Jsonb({"archive_sha256": digest, "archive_bytes": length,
                        "reason": "365-day decision-text retention", "private_archive": True})),
            )
            changed += 1
            if changed >= limit:
                break
    return changed


def archive_due_history(limit=DECISION_BATCH, *, workspace=WORKSPACE, connect=None,
                        now=None):
    """Archive old immutable decision event/proposal bodies; never UPDATE/DELETE them."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("RETENTION_BATCH_LIMIT_INVALID")
    if workspace != WORKSPACE:
        raise ValueError("RETENTION_WORKSPACE_SCOPE_REFUSED")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DECISION_DAYS)
    root = Path(__file__).absolute().parents[1] / ".local/retention/decision-history"
    keys = Path(__file__).absolute().parents[1] / ".local/backup-keys/decision-history"
    if connect is None:
        from folio.workbench import connect as connect
    from folio.restore_mode import require_writable

    archived_events, archived_proposals = [], []
    bad_timestamps = 0
    cursor = _read_history_cursor()
    next_cursor = dict(cursor)
    due_decisions = []
    with connect() as db:
        require_writable(db)
        _assert_runtime_identity(db)
        # Workspace is explicit: never enumerate other tenants through the
        # retention service identity. `events.at` is typed timestamptz, so no
        # untrusted JSON timestamp cast can abort the maintenance transaction.
        deferred_events = list(next_cursor["deferred_events"])
        retry_events, pending_events = _take_deferred(deferred_events, limit)
        known_events = set(pending_events)
        if retry_events:
            retry_rows = db.execute(
                "SELECT e.sequence,e.case_id,e.at,e.body,c.body FROM events e "
                "JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
                "WHERE e.workspace=%s AND e.kind='DOC-decision-saved' AND e.sequence=ANY(%s::bigint[]) "
                "AND e.at<=%s AND e.body ? 'reason' AND octet_length(e.body::text)<=1048576 "
                "FOR UPDATE OF c SKIP LOCKED",
                (workspace, retry_events, cutoff),
            ).fetchall()
            rows_by_id = {int(row[0]): row for row in retry_rows}
            for sequence in retry_events:
                row = rows_by_id.get(sequence)
                if row is None:
                    _defer_identity(pending_events, known_events, sequence)
                    continue
                _, case_id, at, body, case = row
                if not timestamp_due(at, now=now) or not isinstance(body.get("reason"), str):
                    continue
                if not _safe_to_redact(case):
                    _defer_identity(pending_events, known_events, sequence)
                    continue
                payload = {"kind": "DOC-decision-saved", "at": at.isoformat(), "body": body}
                if not immutable_archive_exists("event", str(sequence), workspace, case_id, payload,
                                                archive_root=root, key_root=keys):
                    archived_events.append(archive_immutable_record(
                        "event", str(sequence), workspace, case_id, at, payload,
                        archive_root=root, key_root=keys))
                    due_decisions.append((case_id, body, at))

        events = db.execute(
            "SELECT e.sequence,e.case_id,e.at,e.body,c.body FROM events e "
            "JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
            "WHERE e.workspace=%s AND e.kind='DOC-decision-saved' AND e.at<=%s "
            "AND e.body ? 'reason' "
            "AND octet_length(e.body::text)<=1048576 "
            "AND e.sequence>%s ORDER BY e.sequence LIMIT %s FOR UPDATE OF c SKIP LOCKED",
            (workspace, cutoff, cursor["event_sequence"], HISTORY_SCAN_PAGE),
        ).fetchall()
        scanned_events = 0
        for sequence, case_id, at, body, case in events:
            if len(archived_events) >= 2 * limit:
                break
            next_cursor["event_sequence"] = int(sequence)
            scanned_events += 1
            if not timestamp_due(at, now=now):
                bad_timestamps += 1
                continue
            if not isinstance(body.get("reason"), str):
                continue
            payload = {"kind": "DOC-decision-saved", "at": at.isoformat(), "body": body}
            if immutable_archive_exists("event", str(sequence), workspace, case_id, payload,
                                        archive_root=root, key_root=keys):
                continue
            if not _safe_to_redact(case):
                if not _defer_identity(pending_events, known_events, int(sequence)):
                    next_cursor["deferred_overflow"] = True
                continue
            archived_events.append(archive_immutable_record(
                "event", str(sequence), workspace, case_id, at, payload,
                archive_root=root, key_root=keys))
            due_decisions.append((case_id, body, at))
        next_cursor["deferred_events"] = pending_events
        if scanned_events == len(events) and len(events) < HISTORY_SCAN_PAGE:
            next_cursor["event_sequence"] = 0

        # Proposal rows have no created_at column, but proposal creation writes
        # expires = creation + exactly 15 minutes. Parse that timestamp in Python
        # (never cast untrusted JSON text in SQL) and derive the creation instant.
        deferred_proposals = list(next_cursor["deferred_proposals"])
        retry_proposals, pending_proposals = _take_deferred(deferred_proposals, limit)
        known_proposals = set(pending_proposals)
        if retry_proposals:
            retry_rows = db.execute(
                "SELECT p.workspace,p.id,p.case_id,p.body,c.body FROM proposals p "
                "JOIN cases c ON c.workspace=p.workspace AND c.id=p.case_id "
                "WHERE p.workspace=%s AND p.id=ANY(%s::uuid[]) AND p.body->>'reason' IS NOT NULL "
                "AND octet_length(p.body::text)<=1048576 FOR UPDATE OF c SKIP LOCKED",
                (workspace, retry_proposals),
            ).fetchall()
            rows_by_id = {str(row[1]): row for row in retry_rows}
            for proposal_id in retry_proposals:
                row = rows_by_id.get(proposal_id)
                if row is None:
                    _defer_identity(pending_proposals, known_proposals, proposal_id)
                    continue
                proposal_workspace, _, case_id, proposal, case = row
                try:
                    expires = datetime.fromisoformat(str(proposal["expires"]).replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    bad_timestamps += 1
                    continue
                if expires.tzinfo is None:
                    bad_timestamps += 1
                    continue
                created_at = expires - timedelta(minutes=15)
                if not timestamp_due(created_at, now=now) or not isinstance(proposal.get("reason"), str):
                    continue
                if not _safe_to_redact(case):
                    _defer_identity(pending_proposals, known_proposals, proposal_id)
                    continue
                archive_payload = {"body": proposal,
                                   "source_body_sha256": hashlib.sha256(json.dumps(
                                       proposal, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")).hexdigest()}
                if not immutable_archive_exists("proposal", proposal_id, workspace, case_id,
                                                archive_payload, archive_root=root, key_root=keys):
                    archived_proposals.append(archive_immutable_record(
                        "proposal", proposal_id, proposal_workspace, case_id, created_at,
                        archive_payload, archive_root=root, key_root=keys))

        proposals = db.execute(
            "SELECT p.workspace,p.id,p.case_id,p.body,c.body FROM proposals p "
            "JOIN cases c ON c.workspace=p.workspace AND c.id=p.case_id "
            "WHERE p.workspace=%s AND p.body->>'reason' IS NOT NULL "
            "AND octet_length(p.body::text)<=1048576 "
            "AND p.id>%s::uuid ORDER BY p.id LIMIT %s FOR UPDATE OF c SKIP LOCKED",
            (workspace, cursor["proposal_id"], HISTORY_SCAN_PAGE),
        ).fetchall()
        scanned_proposals = 0
        for proposal_workspace, proposal_id, case_id, proposal, case in proposals:
            if len(archived_proposals) >= 2 * limit:
                break
            next_cursor["proposal_id"] = str(proposal_id)
            scanned_proposals += 1
            if not isinstance(proposal.get("reason"), str):
                continue
            try:
                expires = datetime.fromisoformat(str(proposal["expires"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                bad_timestamps += 1
                continue
            if expires.tzinfo is None:
                bad_timestamps += 1
                continue
            created_at = expires - timedelta(minutes=15)
            if not timestamp_due(created_at, now=now):
                continue
            archive_payload = {"body": proposal,
                               "source_body_sha256": hashlib.sha256(json.dumps(
                                   proposal, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")).hexdigest()}
            if immutable_archive_exists("proposal", str(proposal_id), workspace, case_id,
                                        archive_payload, archive_root=root, key_root=keys):
                continue
            if not _safe_to_redact(case):
                if not _defer_identity(pending_proposals, known_proposals, str(proposal_id)):
                    next_cursor["deferred_overflow"] = True
                continue
            archived_proposals.append(archive_immutable_record(
                "proposal", str(proposal_id), proposal_workspace, case_id, created_at,
                archive_payload, archive_root=root, key_root=keys))
        next_cursor["deferred_proposals"] = pending_proposals
        proposal_cycle_complete = scanned_proposals == len(proposals) and len(proposals) < HISTORY_SCAN_PAGE
        if proposal_cycle_complete:
            next_cursor["proposal_id"] = "00000000-0000-0000-0000-000000000000"
        event_cycle_complete = scanned_events == len(events) and len(events) < HISTORY_SCAN_PAGE
        if (event_cycle_complete and proposal_cycle_complete and
                not pending_events and not pending_proposals):
            next_cursor["deferred_overflow"] = False
    _write_history_cursor(next_cursor)
    return {"events_archived": len(archived_events),
            "proposals_archived": len(archived_proposals),
            "malformed_timestamps": bad_timestamps,
            "oldest_event_lag_days": max(0, (now - min((at for _, _, at in due_decisions), default=now)).days)}


def decision_retention_diagnostics(*, workspace=WORKSPACE, connect=None, now=None):
    """Bounded, workspace-scoped due/backlog visibility without unsafe SQL casts."""
    if connect is None:
        from folio.workbench import connect as connect
    from folio.restore_mode import require_writable

    now = now or datetime.now(timezone.utc)
    with connect() as db:
        require_writable(db)
        _assert_runtime_identity(db)
        rows = db.execute(
            "SELECT jsonb_build_object('decision',body->'decision','retention',body->'retention',"
            "'proposal',body->'proposal','state',body->'state') FROM cases "
            "WHERE workspace=%s AND body->'decision'->>'reason' IS NOT NULL "
            "AND coalesce(body->'retention'->>'legal_hold','false')!='true' "
            "AND coalesce(body->'retention'->>'pinned_release_evidence','false')!='true' "
            "AND body->>'state' NOT IN ('seal_pending','sealed','uncertain','verified','awaiting_approval','dispatching') "
            "AND (body->'proposal' IS NULL OR body->'proposal'='null'::jsonb OR body->'proposal'->>'gate'='applied') "
            "ORDER BY body->'decision'->>'at',id LIMIT 1000", (workspace,)
        ).fetchall()
        due, eligible, malformed, oldest = 0, 0, 0, None
        for (case,) in rows:
            decision = case.get("decision")
            if not isinstance(decision, dict) or not isinstance(decision.get("at"), str):
                malformed += 1
                continue
            try:
                timestamp = datetime.fromisoformat(decision["at"].replace("Z", "+00:00"))
            except ValueError:
                malformed += 1
                continue
            if timestamp.tzinfo is None:
                malformed += 1
                continue
            if decision_due(decision, now=now):
                due += 1
                oldest = timestamp if oldest is None or timestamp < oldest else oldest
                if _safe_to_redact(case):
                    eligible += 1
        return {"workspace": workspace, "candidates_scanned": len(rows),
                "candidate_scan_truncated": len(rows) == 1000,
                "expired_case_decisions_remaining": due,
                "eligible_case_decisions_remaining": eligible,
                "malformed_decision_timestamps": malformed,
                "oldest_decision_lag_days": max(0, (now - oldest).days) if oldest else 0}


def history_retention_diagnostics(*, workspace=WORKSPACE, connect=None, now=None):
    """Count bounded due immutable-event/proposal text without exposing its contents."""
    if connect is None:
        from folio.workbench import connect as connect
    from folio.restore_mode import require_writable

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DECISION_DAYS)
    cursor = _read_history_cursor()
    result = {"due_events_remaining": 0, "eligible_events_remaining": 0,
              "due_proposals_remaining": 0, "eligible_proposals_remaining": 0,
              "malformed_timestamps": 0, "event_scan_truncated": False,
              "proposal_scan_truncated": False, "oldest_due_lag_days": 0,
              "scan_cursor": {"event_sequence": cursor["event_sequence"],
                              "proposal_id": cursor["proposal_id"]},
              "deferred_event_retry_count": len(cursor["deferred_events"]),
              "deferred_proposal_retry_count": len(cursor["deferred_proposals"]),
              "deferred_retry_overflow": cursor["deferred_overflow"]}
    oldest = None
    with connect() as db:
        require_writable(db)
        _assert_runtime_identity(db)
        events = db.execute(
            "SELECT e.sequence,e.case_id,e.at,e.body,c.body FROM events e "
            "JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
            "WHERE e.workspace=%s AND e.kind='DOC-decision-saved' AND e.at<=%s "
            "AND e.body ? 'reason' AND octet_length(e.body::text)<=1048576 "
            "AND e.sequence>%s ORDER BY e.sequence LIMIT %s",
            (workspace, cutoff, cursor["event_sequence"], HISTORY_SCAN_PAGE + 1),
        ).fetchall()
        result["event_scan_truncated"] = len(events) > HISTORY_SCAN_PAGE
        for sequence, case_id, at, body, case in events[:HISTORY_SCAN_PAGE]:
            if not timestamp_due(at, now=now):
                result["malformed_timestamps"] += 1
                continue
            projected = project_history_event(workspace, sequence, case_id, at, body, case, now=now)
            if projected is body or projected.get("reason") is not None:
                result["due_events_remaining"] += 1
                oldest = at if oldest is None or at < oldest else oldest
            if projected is body and _safe_to_redact(case):
                result["eligible_events_remaining"] += 1

        proposals = db.execute(
            "SELECT p.id,p.case_id,p.body,c.body FROM proposals p "
            "JOIN cases c ON c.workspace=p.workspace AND c.id=p.case_id "
            "WHERE p.workspace=%s AND p.body->>'reason' IS NOT NULL AND octet_length(p.body::text)<=1048576 "
            "AND p.id>%s::uuid ORDER BY p.id LIMIT %s",
            (workspace, cursor["proposal_id"], HISTORY_SCAN_PAGE + 1),
        ).fetchall()
        result["proposal_scan_truncated"] = len(proposals) > HISTORY_SCAN_PAGE
        for proposal_id, case_id, proposal, case in proposals[:HISTORY_SCAN_PAGE]:
            try:
                expires = datetime.fromisoformat(str(proposal["expires"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                result["malformed_timestamps"] += 1
                continue
            if expires.tzinfo is None:
                result["malformed_timestamps"] += 1
                continue
            created = expires - timedelta(minutes=15)
            if not timestamp_due(created, now=now):
                continue
            projected = project_proposal_reason(workspace, proposal_id, case_id, proposal, case)
            if "reason" in projected:
                result["due_proposals_remaining"] += 1
                oldest = created if oldest is None or created < oldest else oldest
            if "reason" in projected and _safe_to_redact(case):
                result["eligible_proposals_remaining"] += 1
    result["oldest_due_lag_days"] = max(0, (now - oldest).days) if oldest else 0
    return result


def run_retention_tick(*, export_limit=20, decision_limit=20):
    """Run bounded export and decision-text expiry; exceptions belong to supervisor."""
    from folio.exportworker import expire_due
    from folio.workbench import connect

    with connect() as db:
        _assert_runtime_identity(db)
        export_due = db.execute(
            "SELECT count(*) FILTER (WHERE coalesce(c.body->'retention'->>'legal_hold','false')!='true' "
            "AND coalesce(c.body->'retention'->>'pinned_release_evidence','false')!='true'),"
            "count(*) FILTER (WHERE coalesce(c.body->'retention'->>'legal_hold','false')='true'),"
            "count(*) FILTER (WHERE coalesce(c.body->'retention'->>'pinned_release_evidence','false')='true'),"
            "min(e.expires_at) FILTER (WHERE coalesce(c.body->'retention'->>'legal_hold','false')!='true' "
            "AND coalesce(c.body->'retention'->>'pinned_release_evidence','false')!='true') "
            "FROM export_jobs e JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
            "WHERE e.workspace=%s AND e.expires_at<=clock_timestamp() AND e.state!='tombstoned'",
            (WORKSPACE,),
        ).fetchone()
    exports = expire_due(limit=export_limit, workspace=WORKSPACE)
    decisions = redact_due_decisions(limit=decision_limit)
    history = archive_due_history(limit=decision_limit)
    diagnostics = decision_retention_diagnostics()
    history_diagnostics = history_retention_diagnostics()
    return {"workspace": WORKSPACE, "exports_archived": exports,
            "due_export_inventory": int(export_due[0]),
            "legal_hold_export_inventory": int(export_due[1]),
            "pinned_release_export_inventory": int(export_due[2]),
            "oldest_due_export_at": export_due[3].isoformat() if export_due[3] else None,
            "decisions_archived": decisions, "decision_text": diagnostics,
            "decision_history": {**history, **history_diagnostics}}
