"""Operator-only database principal provisioning via the project-owned container.
No source-defined runtime password; nonowner roles, no BYPASSRLS/schema-create.
This module is never routed through an HTTP endpoint.
"""

import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = "11111111-1111-4111-8111-111111111111"


def admin(statement, database="postgres"):
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            # Disposable repro stacks name their own container; default is the project one.
            os.environ.get("FOLIO_POSTGRES_CONTAINER", "folio-postgres"),
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-q",
        ],
        input=statement.encode(),
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            "DATABASE_PROVISIONING_FAILED:"
            + result.stderr.decode(errors="replace")[:500]
        )


def export_grants(role):
    """Same least-scoped v3 permissions for operator apply and isolated provisioning."""
    return (
        sql.SQL(
            "GRANT USAGE ON SCHEMA public TO {role}; "
            "GRANT SELECT,INSERT ON export_jobs TO {role}; "
            "GRANT UPDATE(state,attempt,fence,tries,lease_until,artifact_ref,"
            "artifact_sha256,artifact_bytes,deleted_at) ON export_jobs TO {role}; "
            "GRANT UPDATE(expires) ON sessions TO {role}; "
            "GRANT SELECT ON schema_versions TO {role};"
        )
        .format(role=sql.Identifier(role))
        .as_string()
    )


def actor_lock_grants(role):
    return (
        sql.SQL("GRANT EXECUTE ON FUNCTION public.folio_export_lock_actor(text) TO {}")
        .format(sql.Identifier(role))
        .as_string()
    )


def access_administration_grants(role):
    """Read-only access projections plus the fixed, session-bound access capability."""
    return sql.SQL(
        "DO $access$ BEGIN "
        "IF to_regclass('public.folio_workspace_access') IS NOT NULL THEN "
        "ALTER TABLE public.folio_workspace_access ENABLE ROW LEVEL SECURITY; "
        "ALTER TABLE public.folio_workspace_access FORCE ROW LEVEL SECURITY; "
        "IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public' AND tablename='folio_workspace_access' AND policyname='folio_workspace') THEN "
        "CREATE POLICY folio_workspace ON public.folio_workspace_access FOR ALL TO PUBLIC USING (workspace='11111111-1111-4111-8111-111111111111'::uuid) WITH CHECK (workspace='11111111-1111-4111-8111-111111111111'::uuid); END IF; "
        "EXECUTE 'REVOKE ALL ON public.folio_workspace_access FROM '||quote_ident({role}); "
        "EXECUTE 'GRANT SELECT ON public.folio_workspace_access TO '||quote_ident({role}); "
        "END IF; "
        "IF to_regclass('public.folio_access_requests') IS NOT NULL THEN "
        "ALTER TABLE public.folio_access_requests ENABLE ROW LEVEL SECURITY; "
        "ALTER TABLE public.folio_access_requests FORCE ROW LEVEL SECURITY; "
        "IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public' AND tablename='folio_access_requests' AND policyname='folio_workspace') THEN "
        "CREATE POLICY folio_workspace ON public.folio_access_requests FOR ALL TO PUBLIC USING (workspace='11111111-1111-4111-8111-111111111111'::uuid) WITH CHECK (workspace='11111111-1111-4111-8111-111111111111'::uuid); END IF; "
        "EXECUTE 'REVOKE ALL ON public.folio_access_requests FROM '||quote_ident({role}); "
        "EXECUTE 'GRANT SELECT ON public.folio_access_requests TO '||quote_ident({role}); "
        "END IF; "
        "IF to_regclass('public.users') IS NOT NULL THEN "
        "EXECUTE 'REVOKE ALL ON public.users FROM '||quote_ident({role}); "
        "EXECUTE 'GRANT SELECT ON public.users TO '||quote_ident({role}); "
        "END IF; "
        "IF to_regclass('public.folio_workspace_access') IS NOT NULL THEN "
        "IF to_regprocedure('folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid)') IS NULL "
        "OR to_regprocedure('folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)') IS NULL THEN "
        "RAISE EXCEPTION 'ACCESS_ADMIN_FUNCTION_REQUIRED'; END IF; "
        "EXECUTE 'GRANT USAGE ON SCHEMA folio_access_admin_v1 TO '||quote_ident({role}); "
        "EXECUTE 'GRANT EXECUTE ON FUNCTION folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid) TO '||quote_ident({role}); "
        "EXECUTE 'GRANT EXECUTE ON FUNCTION folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text) TO '||quote_ident({role}); "
        "END IF; END $access$;"
    ).format(role=sql.Literal(role)).as_string()


def provision_runtime(database, kind):
    if kind not in ("workbench", "target", "reader") or not database.startswith(
        "folio_"
    ):
        raise ValueError("PRODUCT_DATABASE_REQUIRED")
    folder = ROOT / ".local/database-identities"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = hashlib.sha256(database.encode()).hexdigest()[:12]
    path = folder / f"{kind}-{suffix}.json"
    if path.exists():
        identity = json.loads(path.read_text())
    else:
        identity = {
            "username": f"folio_{kind}_{suffix}",
            "password": secrets.token_urlsafe(40),
            "database": database,
        }
        with path.open("x") as f:
            json.dump(identity, f)
        path.chmod(0o600)
        admin(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;"
            )
            .format(
                sql.Identifier(identity["username"]), sql.Literal(identity["password"])
            )
            .as_string()
        )
    role = sql.Identifier(identity["username"])
    tables = (
        [
            "cases",
            "proposals",
            "jobs",
            "commands",
            "events",
            "analysis_runs",
            "originals",
        ]
        if kind == "workbench"
        else ["payables", "authority", "operations"]
    )
    commands = [
        sql.SQL(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA public TO {};"
        ).format(role)
    ]
    for table in tables:
        ident = sql.Identifier(table)
        commands.append(
            sql.SQL(
                "ALTER TABLE {} ENABLE ROW LEVEL SECURITY; ALTER TABLE {} FORCE ROW LEVEL SECURITY; DROP POLICY IF EXISTS folio_workspace ON {}; CREATE POLICY folio_workspace ON {} FOR ALL TO PUBLIC USING (workspace = {}::uuid) WITH CHECK (workspace = {}::uuid);"
            ).format(
                ident,
                ident,
                ident,
                ident,
                sql.Literal(WORKSPACE),
                sql.Literal(WORKSPACE),
            )
        )
    commands.append(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}; GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {};"
        ).format(role, role)
    )
    if kind == "reader":
        commands.append(
            sql.SQL("GRANT SELECT ON payables,authority,operations TO {};").format(role)
        )
    elif kind == "target":
        commands.append(
            sql.SQL(
                "GRANT SELECT,INSERT,UPDATE ON payables,authority TO {}; GRANT SELECT,INSERT ON operations TO {}; GRANT SELECT,INSERT,DELETE ON target_sessions TO {};"
            ).format(role, role, role)
        )
    else:
        commands.append(
            sql.SQL(
                "GRANT SELECT,INSERT,UPDATE ON cases,proposals,jobs,analysis_runs,originals TO {}; GRANT SELECT,INSERT ON events,commands TO {}; GRANT SELECT,INSERT,DELETE ON sessions TO {}; GRANT SELECT ON users TO {};"
            ).format(role, role, role, role)
        )
    # Initial test fixtures do not yet have the deployment journal.
    if kind == "workbench":
        commands.append(
            sql.SQL(
                "DO $exports$ BEGIN IF to_regclass('public.export_jobs') IS NOT NULL THEN "
                + export_grants(identity["username"])
                + " IF to_regprocedure('public.folio_export_lock_actor(text)') IS NOT NULL THEN "
                + actor_lock_grants(identity["username"])
                + "; END IF;"
                + " END IF; END $exports$;"
            )
        )
        commands.append(sql.SQL(access_administration_grants(identity["username"])))
    if kind != "reader":
        commands.append(
            sql.SQL(
                "DO $j$ BEGIN IF to_regclass('public.schema_versions') IS NOT NULL THEN EXECUTE 'GRANT SELECT ON schema_versions TO '||quote_ident({}); END IF; END $j$;"
            ).format(sql.Literal(identity["username"]))
        )
    admin(
        "BEGIN;\n" + "\n".join(x.as_string() for x in commands) + "\nCOMMIT;", database
    )
    return f"postgresql://{identity['username']}:{identity['password']}@127.0.0.1:5541/{database}"
