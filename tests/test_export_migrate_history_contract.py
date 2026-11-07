"""Fast contract regression for the N-1 migration operator path."""

import hashlib
from contextlib import contextmanager
from pathlib import Path

from scripts import export_migrate

ROOT = Path(__file__).resolve().parents[1]


def _sql_text(query):
    if hasattr(query, "string"):
        return query.string
    if hasattr(query, "_obj"):
        return "".join(_sql_text(part) for part in query._obj)
    if hasattr(query, "seq"):
        return "".join(_sql_text(part) for part in query.seq)
    return str(query)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _AlreadyMigratedDatabase:
    """Minimal SQL double representing verified, already-applied v2-v6."""

    def __init__(self):
        self.calls = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, params=None):
        self.calls.append((query, params))
        statement = str(query)
        if "SELECT current_user=%s" in statement:
            return _Result((False,))
        if "SELECT sha256 FROM public.schema_versions" in statement:
            version = params[0]
            migration = {
                2: "002-workbench.sql",
                3: "003-exports.sql",
                4: "004-export-actor-lock.sql",
                5: "005-read-path-indexes.sql",
                6: "006-n1-export-row-lock-compat.sql",
            }[version]
            digest = hashlib.sha256(
                (ROOT / "ops/migrations" / migration).read_bytes()
            ).hexdigest()
            return _Result((digest,))
        if "SELECT 1 FROM public.schema_versions WHERE version=%s" in statement:
            return _Result((1,))
        if "SELECT 1 FROM public.schema_versions WHERE version=6" in statement:
            return _Result((1,))
        if "SELECT p.proowner=current_user::regrole" in statement:
            return _Result((True,))
        return _Result(None)


def test_operator_skips_verified_history_and_only_reasserts_n1_lock_grant(
    monkeypatch,
):
    db = _AlreadyMigratedDatabase()
    installed = []
    monkeypatch.setattr("folio.schema_journal.install", lambda *args: installed.append(args))

    export_migrate.apply(db, "folio_runtime")

    assert installed == []
    statements = [_sql_text(query) for query, _ in db.calls]
    assert sum("SELECT sha256 FROM public.schema_versions" in s for s in statements) == 5
    assert sum("SELECT 1 FROM public.schema_versions WHERE version=%s" in s for s in statements) == 4
    assert any("n1_export_lock_capability" in s for s in statements)
    assert not any(
        token in s.upper()
        for s in statements
        for token in ("UPDATE SCHEMA_VERSIONS", "DELETE FROM SCHEMA_VERSIONS")
    )
