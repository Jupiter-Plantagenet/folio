"""Read uploaded original bytes without bypassing their case/digest tombstones."""

import hashlib
import hmac
import re
from urllib.parse import quote
from uuid import UUID

MAX_ORIGINAL_BYTES = 20 * 1024 * 1024
# Server-written authorship of user-uploaded sources; such a source must have a stored row.
UPLOADED_AUTHORSHIP = "user-attested independently authored synthetic input"
AVAILABILITY_SCHEMA = "folio-original-availability-v1"
# Blueprint actor table: viewers never export originals; auditor original export needs a
# separately scoped evidence permission that does not exist yet, so it is denied too.
ORIGINAL_EXPORT_ROLES = ("reviewer", "approver")
AVAILABILITY_STATES = {
    None: "available",
    "ORIGINAL_TOO_LARGE": "too_large",
    "ORIGINAL_DELETED": "deleted",
    "ORIGINAL_METADATA_MISMATCH": "metadata_mismatch",
    "ORIGINAL_BYTES_MISSING": "bytes_missing",
    "ORIGINAL_DIGEST_MISMATCH": "digest_mismatch",
}


class OriginalUnavailable(ValueError):
    def __init__(self, code, status):
        self.code = code
        self.status = status
        super().__init__(code)


def original_problem(
    source_id, sources, stored_sha, deleted_at, stored_length, has_bytes, actual_sha
):
    """Return (code, status) denying one stored original, or None when it may be served.

    Download and advisory availability share these rules; each caller supplies its own
    freshly computed byte digest, so availability can never relax a download check.
    """
    if stored_length is not None and stored_length > MAX_ORIGINAL_BYTES:
        return "ORIGINAL_TOO_LARGE", 413
    if deleted_at is not None:
        return "ORIGINAL_DELETED", 410
    if type(sources) is not list:
        return "ORIGINAL_METADATA_MISMATCH", 503
    matching = [
        source
        for source in sources
        if type(source) is dict and source.get("id") == source_id
    ]
    if len(matching) != 1:
        return "ORIGINAL_METADATA_MISMATCH", 503
    source = matching[0]
    if source.get("original_deleted"):
        return "ORIGINAL_DELETED", 410
    # Absent bytes without a deletion tombstone are a storage fault, not a retention deletion.
    if not has_bytes:
        return "ORIGINAL_BYTES_MISSING", 503
    if (
        type(stored_sha) is not str
        or re.fullmatch(r"[0-9a-f]{64}", stored_sha) is None
        or source.get("sha256") != stored_sha
        or type(actual_sha) is not str
        or not hmac.compare_digest(actual_sha, stored_sha)
    ):
        return "ORIGINAL_DIGEST_MISMATCH", 503
    return None


def may_export_originals(roles):
    return type(roles) is list and any(role in roles for role in ORIGINAL_EXPORT_ROLES)


def read_original(db, workspace, case_id, source_id):
    """Caller authenticates the request and authorizes workspace before this read."""
    for identifier in (workspace, case_id, source_id):
        try:
            if type(identifier) is not str or str(UUID(identifier)) != identifier:
                raise ValueError
        except (ValueError, AttributeError):
            raise OriginalUnavailable("NOT_FOUND", 404) from None
    row = db.execute(
        "SELECT CASE WHEN octet_length(o.raw)<=20971520 THEN o.raw END, "
        "o.sha256,o.deleted_at,c.body->'sources',octet_length(o.raw) "
        "FROM originals o JOIN cases c ON c.workspace=o.workspace AND c.id=o.case_id "
        "WHERE o.workspace=%s AND o.case_id=%s AND o.id=%s",
        (workspace, case_id, source_id),
    ).fetchone()
    if row is None:
        raise OriginalUnavailable("NOT_FOUND", 404)
    raw, stored_sha, deleted_at, sources, stored_length = row
    # Re-hash the exact bytes being served; advisory availability is never trusted here.
    actual_sha = (
        hashlib.sha256(bytes(raw)).hexdigest()
        if isinstance(raw, (bytes, bytearray, memoryview))
        else None
    )
    problem = original_problem(
        source_id,
        sources,
        stored_sha,
        deleted_at,
        stored_length,
        raw is not None,
        actual_sha,
    )
    if problem is not None:
        raise OriginalUnavailable(*problem)
    raw = bytes(raw)
    source = next(
        source
        for source in sources
        if type(source) is dict and source.get("id") == source_id
    )
    filename = source.get("name")
    if type(filename) is not str:
        filename = "original"
    filename = re.split(r"[/\\]", filename)[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename).strip(" .")[:120] or "original"
    try:
        encoded_name = quote(filename, safe="")
    except UnicodeError:
        encoded_name = "original"
    headers = {
        "Content-Disposition": f"attachment; filename=\"source-{source_id}\"; filename*=UTF-8''{encoded_name}",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    return raw, headers


def original_availability(db, workspace, case_id, case, roles):
    """Advisory per-source availability of stored originals for one authorized case.

    The caller has authenticated, scoped the workspace and loaded this case exactly as
    the download route requires. Case detail is polled, so this reads metadata only:
    recorded versus stored digest, byte presence and length, never the bytes or their
    hash. Byte corruption is caught by the download route, which re-reads and re-hashes
    before releasing anything. The result is response-only: never persisted or signed.
    """
    stored = {}
    for row in db.execute(
        "SELECT o.id,o.sha256,o.deleted_at,octet_length(o.raw),"
        "o.created_at < clock_timestamp()-interval '90 days' "
        "FROM originals o WHERE o.workspace=%s AND o.case_id=%s",
        (workspace, case_id),
    ):
        stored[str(row[0])] = row[1:]
    sources = case.get("sources")
    entries = []
    for source in sources if type(sources) is list else []:
        if type(source) is not dict:
            continue
        source_id = source.get("id")
        row = stored.get(source_id) if type(source_id) is str else None
        if row is None:
            # No stored object: sample fixtures never stored bytes; an upload should have.
            if source.get("original_deleted"):
                state = "deleted"
            elif source.get("authorship") == UPLOADED_AUTHORSHIP:
                state = "missing"
            else:
                state = "not_stored"
            code, bytes_stored, expired = "NOT_FOUND", False, None
        else:
            stored_sha, deleted_at, length, expired = row
            # Metadata-only: the stored digest stands in for the byte hash here.
            problem = original_problem(
                source_id,
                sources,
                stored_sha,
                deleted_at,
                length,
                length is not None,
                stored_sha,
            )
            code = problem[0] if problem else None
            state = AVAILABILITY_STATES[code]
            bytes_stored, expired = length is not None, bool(expired)
        entries.append(
            {
                "source_id": source_id,
                "source_version": source.get("version"),
                "state": state,
                "code": code,
                "downloadable": state == "available",
                "bytes_stored": bytes_stored,
                "retention_expired": expired,
            }
        )
    permitted = may_export_originals(roles)
    if not permitted:
        for entry in entries:
            entry["downloadable"] = False
    retention = case.get("retention")
    return {
        "schema_version": AVAILABILITY_SCHEMA,
        "advisory": True,
        "download_permitted": permitted,
        "legal_hold": type(retention) is dict and retention.get("legal_hold") is True,
        "sources": entries,
    }
