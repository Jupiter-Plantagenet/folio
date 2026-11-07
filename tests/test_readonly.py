from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from folio.identity import service_header


def test_verifier_target_read_runs_as_readonly_database_principal():
    from folio.target import app, migrate, seed
    from folio.workbench import WORKSPACE

    migrate()
    c = TestClient(app)
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    r = c.get(
        f"/api/v1/workspaces/{WORKSPACE}/payables/{rid}",
        headers=service_header("verifier"),
    )
    assert r.status_code == 200
    with psycopg.connect(__import__("os").environ["FOLIO_TARGET_READ_DSN"]) as db:
        assert (
            db.execute(
                "SELECT has_table_privilege(current_user,'payables','UPDATE')"
            ).fetchone()[0]
            is False
        )
    # Introspect actual independent target API read connection, not only the existence of a spare account.
    from folio.target import read_connection

    with read_connection() as db:
        assert (
            db.execute("SELECT current_user").fetchone()[0]
            == __import__("psycopg").conninfo.conninfo_to_dict(
                __import__("os").environ["FOLIO_TARGET_READ_DSN"]
            )["user"]
        )
        assert (
            db.execute(
                "SELECT has_table_privilege(current_user,'operations','INSERT')"
            ).fetchone()[0]
            is False
        )
