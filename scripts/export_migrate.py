"""Operator-only additive export migration. No retained profile discovery.
Requires FOLIO_MIGRATION_DSN explicitly; run only after independent review/approval.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from folio.database_security import actor_lock_grants, export_grants


def apply(db, role):
    # Metadata, actual DDL and grants must commit or roll back together.
    with db.transaction():
        if db.execute("SELECT current_user=%s", (role,)).fetchone()[0]:
            raise RuntimeError("TRUSTED_NONRUNTIME_MIGRATION_OWNER_REQUIRED")
        # Validate every existing predecessor, never rewrite applied history.
        db.execute("LOCK TABLE public.schema_versions IN EXCLUSIVE MODE")
        import hashlib
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "ops/migrations"
        for version, name in (
            (2, "002-workbench.sql"),
            (3, "003-exports.sql"),
            (4, "004-export-actor-lock.sql"),
            (5, "005-read-path-indexes.sql"),
            (6, "006-n1-export-row-lock-compat.sql"),
        ):
            row = db.execute(
                "SELECT sha256 FROM public.schema_versions WHERE version=%s", (version,)
            ).fetchone()
            if row and row[0] != hashlib.sha256((root / name).read_bytes()).hexdigest():
                raise RuntimeError("MIGRATION_CHECKSUM_MISMATCH")
        # Never re-execute an already journaled migration (e.g. committed 004). Versions
        # whose checksum was verified above are skipped; only absent versions install.
        from folio.schema_journal import install as install_version

        for version, name in (
            (3, "003-exports.sql"),
            (4, "004-export-actor-lock.sql"),
            (5, "005-read-path-indexes.sql"),  # round 6, expand-only indexes
            (6, "006-n1-export-row-lock-compat.sql"),  # bounded N-1 lock compatibility
        ):
            if not db.execute(
                "SELECT 1 FROM public.schema_versions WHERE version=%s", (version,)
            ).fetchone():
                install_version(db, version, root / name)
        if not db.execute(
            "SELECT p.proowner=current_user::regrole AND NOT pg_has_role(%s,p.proowner,'MEMBER') FROM pg_proc p WHERE p.oid='public.folio_export_lock_actor(text)'::regprocedure",
            (role,),
        ).fetchone()[0]:
            raise RuntimeError("TRUSTED_NONRUNTIME_MIGRATION_OWNER_REQUIRED")
        db.execute(export_grants(role))
        db.execute(actor_lock_grants(role))
        # The immutable N-1 app locks users directly with SELECT ... FOR SHARE.
        # Grant only the dedicated inert column needed for PostgreSQL's lock ACL
        # check; migration 006 rejects every attempted DML update of that column.
        if db.execute(
            "SELECT 1 FROM public.schema_versions WHERE version=6"
        ).fetchone():
            db.execute(
                sql.SQL(
                    "GRANT UPDATE(n1_export_lock_capability) ON public.users TO {}"
                ).format(sql.Identifier(role))
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--runtime-role", required=True)
    args = parser.parse_args()
    dsn = os.environ.get("FOLIO_MIGRATION_DSN")
    if not dsn:
        parser.error("FOLIO_MIGRATION_DSN must be explicitly supplied")
    # Retained profiles must use the backed, lock-guarded operator path. This
    # generic CLI remains available for explicit disposable/fresh provisioning.
    profiles = Path(__file__).resolve().parents[1] / '.local/database-profiles.json'
    if profiles.exists():
        selected = urlsplit(dsn)
        for profile in json.loads(profiles.read_text(encoding='utf-8')).values():
            for key in ('FOLIO_DSN', 'FOLIO_TARGET_DSN', 'FOLIO_TARGET_READ_DSN'):
                retained = urlsplit(profile.get(key, ''))
                if (selected.hostname,selected.port,selected.path) == (retained.hostname,retained.port,retained.path):
                    parser.error('RETAINED_PROFILE_REQUIRES_ROUND6_APPLY_006_OPERATOR')
    with psycopg.connect(dsn) as db:
        if profiles.exists():
            # Connection identity closes hostname, URL-escape and keyword-DSN
            # aliases. Conservatively reserve retained DB names on any endpoint.
            from psycopg.conninfo import conninfo_to_dict

            retained_names = {conninfo_to_dict(value)['dbname']
                              for profile in json.loads(profiles.read_text(encoding='utf-8')).values()
                              for key,value in profile.items() if key in
                              ('FOLIO_DSN','FOLIO_TARGET_DSN','FOLIO_TARGET_READ_DSN')}
            if db.execute('SELECT current_database()').fetchone()[0] in retained_names:
                parser.error('RETAINED_DATABASE_IDENTITY_REQUIRES_ROUND6_APPLY_006_OPERATOR')
        apply(db, args.runtime_role)
        assert db.execute(
            "SELECT version FROM schema_versions WHERE version=4"
        ).fetchone() == (4,)
        assert db.execute(
            "SELECT version FROM schema_versions WHERE version=6"
        ).fetchone() == (6,)
    print("Export schema versions 4 and 6 and bounded row-lock capabilities verified")


if __name__ == "__main__":
    main()
