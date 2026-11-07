"""Product-enforced read-only restore mode (DOC-T-RESTORE).

A restored (or otherwise suspect) database pair stays read-only until an
independent reconciliation is signed. Two switches, either one is enough:

* process flag: ``FOLIO_READ_ONLY=1`` (or ``restore``) in the service environment;
* database flag: ``ALTER DATABASE <db> SET folio.restore_mode = 'read_only'``,
  set by the restore drill as the owner/superuser and cleared only by
  ``scripts/restore_mode.py clear`` with a separately signed reconciliation
  report bound to both exact database identities. The clear command requires
  the process-level hold and zero open clients on both databases.

The database flag is read from ``pg_db_role_setting`` (not ``current_setting``)
so pooled connections opened before the flag was set still see it. While read
only, every state-changing ``/api/v1/`` request (and every target mutation) is
refused with 503 ``READ_ONLY_RESTORE_MODE`` before any handler runs. Login and
logout stay available so reviewers can read and reconcile. If the flag cannot
be read the write is refused (fail closed) with ``RESTORE_MODE_UNVERIFIABLE``.
"""

import json
import os
import threading
import time

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SETTING = "folio.restore_mode"
_QUERY = (
    "SELECT coalesce(bool_or(c = 'folio.restore_mode=read_only'), false) "
    "FROM pg_db_role_setting s CROSS JOIN LATERAL unnest(s.setconfig) c "
    "WHERE s.setrole = 0 AND s.setdatabase = "
    "(SELECT oid FROM pg_database WHERE datname = current_database())"
)
TTL_SECONDS = 2.0


def env_read_only():
    return os.getenv("FOLIO_READ_ONLY", "").strip().lower() in (
        "1",
        "true",
        "restore",
        "read_only",
    )


def database_read_only(db):
    return bool(db.execute(_QUERY).fetchone()[0])


class _Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.at = 0.0

    def get(self, probe):
        with self.lock:
            if self.value is not None and time.monotonic() - self.at < TTL_SECONDS:
                return self.value
        value = probe()
        with self.lock:
            self.value, self.at = value, time.monotonic()
        return value


def mode(probe, cache=None):
    """'read_only_env', 'read_only_database', 'read_write' or 'unverifiable'."""
    if env_read_only():
        return "read_only_env"
    try:
        flagged = cache.get(probe) if cache else probe()
    except Exception:
        return "unverifiable"
    return "read_only_database" if flagged else "read_write"


def require_writable(db):
    """Uncached worker admission, including claims, publication and retention.

    The restore flag is set before a restored pair is exposed to workers. This
    also checks already-open pooled connections against database metadata.
    """
    state = mode(lambda: database_read_only(db))
    if state != "read_write":
        code = (
            "RESTORE_MODE_UNVERIFIABLE"
            if state == "unverifiable"
            else "READ_ONLY_RESTORE_MODE"
        )
        raise RuntimeError(code)


class RestoreModeGuard:
    """Pure ASGI middleware (keeps SSE streaming untouched)."""

    def __init__(self, app, probe, prefix="/api/v1/", exempt=()):
        self.app = app
        self.probe = probe
        self.prefix = prefix
        self.exempt = set(exempt)
        self.cache = _Cache()

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] in WRITE_METHODS
            and scope["path"].startswith(self.prefix)
            and scope["path"] not in self.exempt
        ):
            state = mode(self.probe, self.cache)
            if state != "read_write":
                code = (
                    "RESTORE_MODE_UNVERIFIABLE"
                    if state == "unverifiable"
                    else "READ_ONLY_RESTORE_MODE"
                )
                body = json.dumps({"detail": code, "mode": state}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff"),
                            (b"retry-after", b"300"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
