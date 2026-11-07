"""Exercise real nonowner runtime principals and forced workspace policies."""

import os
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest


def test_runtime_role_cannot_bypass_workspace_policy_or_create_objects():
    from folio.database_security import provision_runtime
    from folio.workbench import WORKSPACE, migrate

    migrate()
    database = urlsplit(os.environ["FOLIO_DSN"]).path[1:]
    runtime = provision_runtime(database, "workbench")
    with psycopg.connect(runtime) as db:
        row = db.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert row == (False, False)
        assert (
            db.execute(
                "SELECT tableowner=current_user FROM pg_tables WHERE tablename='cases'"
            ).fetchone()[0]
            is False
        )
        db.execute("SELECT set_config('folio.workspace',%s,true)", (str(uuid4()),))
        assert (
            db.execute(
                "SELECT count(*) FROM cases WHERE workspace<>%s", (WORKSPACE,)
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(runtime) as db:
            db.execute(
                "INSERT INTO cases VALUES(%s,%s,'{}')", (str(uuid4()), str(uuid4()))
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(runtime) as db:
            db.execute("CREATE TABLE should_not_exist(id int)")


def test_target_receipts_and_audit_command_tombstones_are_append_only_for_runtime_roles():
    from folio.database_security import provision_runtime
    from folio.workbench import migrate

    migrate()
    for kind, key, tables in [
        ("target", "FOLIO_TARGET_DSN", ["operations"]),
        ("workbench", "FOLIO_DSN", ["events", "commands"]),
    ]:
        database = urlsplit(os.environ[key]).path[1:]
        dsn = provision_runtime(database, kind)
        with psycopg.connect(dsn) as db:
            for table in tables:
                assert (
                    db.execute(
                        "SELECT has_table_privilege(current_user,%s,'DELETE')", (table,)
                    ).fetchone()[0]
                    is False
                )
                assert (
                    db.execute(
                        "SELECT has_table_privilege(current_user,%s,'UPDATE')", (table,)
                    ).fetchone()[0]
                    is False
                )
