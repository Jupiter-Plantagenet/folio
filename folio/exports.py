"""Permissioned metadata-only evidence snapshots. No provider/target dependencies."""

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import Response
from psycopg.types.json import Jsonb

MAX_BYTES = 262144
WRITE_ROLES = {"reviewer", "approver", "auditor"}
READ_ROLES = WRITE_ROLES | {"viewer", "admin"}
PROFILE = "audit-v1"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def install(db):
    from folio.schema_journal import install as install_migration

    install_migration(
        db, 3, Path(__file__).resolve().parents[1] / "ops/migrations/003-exports.sql"
    )
    install_migration(
        db,
        4,
        Path(__file__).resolve().parents[1]
        / "ops/migrations/004-export-actor-lock.sql",
    )


def authorized(db, req, write=False):
    from folio import workbench as w

    token = hashlib.sha256(req.cookies.get("folio_session", "").encode()).hexdigest()
    # The fixed owner capability retains both physical SHARE row locks until this
    # transaction ends, without granting any auth mutation to the runtime role.
    locked = db.execute(
        "SELECT * FROM public.folio_export_lock_actor(%s)", (token,)
    ).fetchone()
    if not locked:
        w.fail("SESSION_REQUIRED", 401)
    # Re-read after lock waits: current role/active/expiry, not the pre-wait snapshot.
    row = db.execute(
        "SELECT u.username,u.roles,s.csrf FROM sessions s JOIN users u USING(username) "
        "WHERE s.token=%s AND s.expires>clock_timestamp() AND u.active",
        (token,),
    ).fetchone()
    if not row:
        w.fail("SESSION_REQUIRED", 401)
    if not set(row[1]) & (WRITE_ROLES if write else READ_ROLES):
        w.fail("EXPORT_DENIED", 403)
    if write and (
        req.headers.get("origin") != w.ORIGIN
        or not hmac.compare_digest(req.headers.get("x-csrf-token", ""), row[2])
    ):
        w.fail("CSRF_DENIED", 403)
    return {"username": row[0], "roles": row[1]}


def identifier(value):
    from folio.commands import canonical_uuid

    try:
        return canonical_uuid(value)
    except (ValueError, TypeError, AttributeError):
        from folio.workbench import fail

        fail("CANONICAL_UUID_REQUIRED", 422)


def digest_field(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value)
        else None
    )


def revision(value):
    return value if type(value) is int and 0 < value < 2**63 else None


def bounded_input(value):
    from folio.workbench import fail

    pending = [(value, 0)]
    total = nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 20000 or depth > 30:
            fail("EXPORT_INPUT_TOO_LARGE", 422)
        if isinstance(item, str):
            if len(item) > 1048576:
                fail("EXPORT_INPUT_TOO_LARGE", 422)
            total += len(item.encode())
        elif isinstance(item, dict):
            if len(item) > 2000:
                fail("EXPORT_INPUT_TOO_LARGE", 422)
            pending.extend((x, depth + 1) for pair in item.items() for x in pair)
        elif isinstance(item, list):
            if len(item) > 2000:
                fail("EXPORT_INPUT_TOO_LARGE", 422)
            pending.extend((x, depth + 1) for x in item)
        if total > 1048576:
            fail("EXPORT_INPUT_TOO_LARGE", 422)


def snapshot(c, proposal):
    """Allowlist structural evidence only, never free text, credentials or grant objects."""
    from folio.provenance import FIELDS

    bounded_input([c, proposal])
    sources = [
        {
            "id": identifier(s["id"]),
            "version": revision(s.get("version")),
            "sha256": digest_field(s.get("sha256")),
            "snapshot_sha256": sha(s),
            "original_deleted": s.get("original_deleted") is True,
        }
        for s in c.get("sources", [])
    ]
    provenance = {}
    for field in sorted(FIELDS):
        entries = []
        for item in c.get("field_provenance", {}).get(field, []):
            source = item.get("source") or {}
            entries.append(
                {
                    "version": revision(item.get("version")),
                    "kind": item.get("kind")
                    if item.get("kind") in {"human", "computed", "extracted", "lookup"}
                    else "omitted",
                    "entry_sha256": sha(item),
                    "source_sha256": digest_field(source.get("source_sha256")),
                }
            )
        if entries:
            provenance[field] = entries
    verification = c.get("verification")
    result = {
        "schema_version": "folio-evidence-v2",
        "redaction_profile": PROFILE,
        "scope": "case-evidence-metadata-originals-excluded",
        "release_ready": False,
        "case": {
            "id": c["id"],
            "version": c["version"],
            "snapshot_sha256": sha(c),
            "source_digest": digest_field(c.get("source_digest")),
        },
        "sources": sources,
        "field_provenance": provenance,
        "proposal": {
            "id": proposal["id"],
            "version": revision(proposal.get("version")),
            "snapshot_sha256": sha(proposal),
        }
        if proposal
        else None,
        "verification": {
            "status": "recorded-not-reverified" if verification else "incomplete",
            "snapshot_sha256": sha(verification) if verification else None,
            "version": revision((verification or {}).get("version")),
            "verifier_version_sha256": sha((verification or {}).get("verifier")),
        },
        "omissions": [
            "originals",
            "source text and filenames",
            "business field values and quotes",
            "private paths",
            "provider secrets",
            "grants",
            "auth headers and cookies",
            "proposal effect payload",
            "verifier observation payload",
        ],
        "limitations": [
            "Metadata-only audit subset; no new verification or model claim",
            "Not a full release or full retention/backup/provider-deletion proof",
        ],
    }
    if len(canonical(result)) > MAX_BYTES:
        from folio.workbench import fail

        fail("EXPORT_TOO_LARGE", 422)
    return result


def enqueue(workspace, cid, body, req):
    from folio import workbench as w

    w.identity(req)
    w.scope(workspace)
    identifier(cid)
    identifier(req.headers.get("idempotency-key", ""))
    with w.pooled() as db:
        user = authorized(db, req, write=True)
        if body.get("include_originals", False) is not False:
            w.fail("ORIGINAL_EXPORT_DENIED", 403)
        if (
            set(body) - {"case_version", "include_originals", "redaction_profile"}
            or body.get("redaction_profile") != PROFILE
            or not revision(body.get("case_version"))
        ):
            w.fail("REDACTION_INVALID", 422)
        # Existing command journal includes exact body and If-Match, not mutable current case.
        replay = w.command(db, req, user, body)
        if replay:
            return replay
        c = w.load_case(db, workspace, cid)
        if c["version"] != body["case_version"]:
            w.fail("STALE_VERSION", 412)
        if c.get("retention", {}).get("decision_evidence_deleted_at"):
            w.fail("RETENTION_DELETED")
        if (
            req.headers.get("if-match")
            and req.headers["if-match"] != f'"case-v{c["version"]}"'
        ):
            w.fail("STALE_VERSION", 412)
        proposal = (
            w.load_proposal(db, workspace, c["proposal"]["id"])
            if c.get("proposal")
            else None
        )
        payload = snapshot(c, proposal)
        jid = str(uuid4())
        db.execute(
            "INSERT INTO export_jobs(id,workspace,case_id,actor,snapshot,snapshot_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (jid, workspace, cid, user["username"], Jsonb(payload), sha(payload)),
        )
        w.event(
            db,
            cid,
            "DOC-job-changed",
            user["username"],
            {"artifact_job_id": jid, "state": "queued"},
        )
        return w.recorded(db, req, user, body, {"artifact_job_id": jid})


def root():
    value = os.environ.get("FOLIO_EXPORT_ROOT")
    if not value or not Path(value).is_absolute():
        raise RuntimeError("PRIVATE_EXPORT_ROOT_REQUIRED")
    folder = Path(value)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise RuntimeError("PRIVATE_EXPORT_ROOT_SYMLINK_DENIED")
    return folder


def read_bytes(ref, expected, length):
    # Read once into a bounded buffer, hash that exact buffer, serve that same buffer.
    if not re.fullmatch("[a-f0-9-]{36}\\.[a-f0-9-]{36}\\.json", ref or ""):
        raise RuntimeError("ARTIFACT_REFERENCE_INVALID")
    path = root() / ref
    if path.is_symlink():
        raise RuntimeError("ARTIFACT_SYMLINK_DENIED")
    with path.open("rb") as f:
        raw = f.read(MAX_BYTES + 1)
    if (
        len(raw) > MAX_BYTES
        or len(raw) != length
        or hashlib.sha256(raw).hexdigest() != expected
    ):
        raise RuntimeError("ARTIFACT_INTEGRITY_FAILED")
    return raw


def retrieve(workspace, aid, req, download=False):
    from folio import workbench as w

    w.identity(req)
    w.scope(workspace)
    identifier(aid)
    with w.pooled() as db:
        user = authorized(db, req)
        row = db.execute(
            "SELECT state,snapshot_sha256,artifact_ref,artifact_sha256,artifact_bytes,"
            "expires_at<=clock_timestamp(),snapshot FROM export_jobs "
            "WHERE workspace=%s AND id=%s AND actor=%s FOR SHARE",
            (workspace, aid, user["username"]),
        ).fetchone()
        if not row:
            w.fail("NOT_FOUND", 404)
        if row[0] == "tombstoned" or row[5]:
            w.fail("RETENTION_DELETED", 410)
        if req.headers.get("range"):
            w.fail("RANGE_NOT_SUPPORTED", 416)
        headers = {
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        }
        if download:
            if row[0] != "ready":
                w.fail("ARTIFACT_NOT_READY")
            try:
                raw = read_bytes(row[2], row[3], row[4])
            except (OSError, RuntimeError):
                w.fail("ARTIFACT_UNAVAILABLE", 503)
            return Response(
                raw,
                media_type="application/json",
                headers={
                    **headers,
                    "ETag": '"sha256-' + row[3] + '"',
                    "Content-Disposition": f'attachment; filename="folio-evidence-{aid}.json"',
                },
            )
        return Response(
            canonical(
                {
                    "artifact_job_id": aid,
                    "state": row[0],
                    "manifest": row[6],
                    "snapshot_sha256": row[1],
                    "sha256": row[3],
                    "bytes": row[4],
                    "download_url": f"/api/v1/workspaces/{workspace}/artifacts/{aid}?download=true",
                }
            ),
            media_type="application/json",
            headers=headers,
        )
