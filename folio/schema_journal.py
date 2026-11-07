"""Owner-only migration metadata. Digests cover exact on-disk SQL bytes (no normalization).

Never repair existing journal rows: a checksum mismatch aborts the transaction.
The canonical shape is the deployed hardened-v2 journal, including NOT NULL times.
"""

import hashlib

from psycopg import sql


def check_runtime(db):
    """Read-only startup dependency gate; never install or repair as runtime."""
    if not db.execute(
        "SELECT count(*)=4 FROM public.schema_versions WHERE version IN (2,3,4,8)"
    ).fetchone()[0]:
        raise RuntimeError("SCHEMA_MIGRATION_REQUIRED")
    if not db.execute(
        "SELECT COALESCE(has_function_privilege(current_user, "
        "to_regprocedure('public.folio_export_lock_actor(text)'), 'EXECUTE'), false)"
    ).fetchone()[0]:
        raise RuntimeError("SCHEMA_MIGRATION_REQUIRED")
    if not db.execute(
        "SELECT COALESCE(has_function_privilege(current_user, "
        "to_regprocedure('folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid)'), 'EXECUTE'), false) "
        "AND COALESCE(has_function_privilege(current_user, "
        "to_regprocedure('folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)'), 'EXECUTE'), false)"
    ).fetchone()[0]:
        raise RuntimeError("SCHEMA_MIGRATION_REQUIRED")
    # 006's compatibility contract is stricter than protecting only the two
    # account columns: runtime receives read-only projections and fixed
    # SECURITY DEFINER capabilities, never DML on account/access tables.
    # has_*_privilege(current_user, ...) checks effective privileges, including
    # inherited grants through role membership.
    if db.execute(
        "SELECT EXISTS ("
        "SELECT 1 FROM (VALUES "
        "('public.users'::text),"
        "('public.folio_access_requests'::text),"
        "('public.folio_workspace_access'::text)"
        ") AS protected(rel) WHERE "
        "has_table_privilege(current_user,rel,'INSERT') "
        "OR has_table_privilege(current_user,rel,'UPDATE') "
        "OR has_table_privilege(current_user,rel,'DELETE') "
        "OR has_table_privilege(current_user,rel,'TRUNCATE') "
        "OR has_any_column_privilege(current_user,rel,'INSERT') "
        "OR has_any_column_privilege(current_user,rel,'UPDATE')"
        ")"
    ).fetchone()[0]:
        raise RuntimeError("ACCESS_ADMIN_PRIVILEGE_DRIFT")


def journal_sql(version, raw):
    digest = hashlib.sha256(raw).hexdigest()
    return (
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS schema_versions("
            "version int PRIMARY KEY,sha256 text NOT NULL,"
            "installed_at timestamptz NOT NULL DEFAULT clock_timestamp()); "
            "LOCK TABLE schema_versions IN EXCLUSIVE MODE; "
            "INSERT INTO schema_versions(version,sha256) VALUES({version},{digest}) "
            "ON CONFLICT DO NOTHING; "
            "DO $journal$ BEGIN IF (SELECT sha256 FROM schema_versions WHERE version={version}) "
            "IS DISTINCT FROM {digest} THEN RAISE EXCEPTION 'MIGRATION_CHECKSUM_MISMATCH'; "
            "END IF; END $journal$;"
        )
        .format(version=sql.Literal(version), digest=sql.Literal(digest))
        .as_string()
    )


def install(db, version, path):
    raw = path.read_bytes()
    # Savepoint also rolls metadata/DDL back if a caller catches an exception.
    with db.transaction():
        db.execute(journal_sql(version, raw))
        db.execute(raw.decode("utf-8"))
