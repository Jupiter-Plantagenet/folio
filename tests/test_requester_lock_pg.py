"""Requester row-lock capability: real isolated PostgreSQL, nonowner runtime login.

Runs only inside the disposable ``folio_finish_wb_*`` databases created by
``scripts/isolated_tests.py``; never against a retained profile.
"""

import os
import threading
import time

import psycopg
import pytest

from folio.analysis_authority import requester_problem


def _isolated():
    dsn = os.environ.get("FOLIO_DSN", "")
    try:
        return psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", "").startswith(
            "folio_finish_wb_"
        )
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _isolated(), reason="isolated disposable PostgreSQL only"
)


@pytest.fixture(scope="module")
def runtime():
    from folio import workbench as w
    from folio.database_security import provision_runtime

    w.migrate()
    with psycopg.connect(w.DSN) as db:
        database = db.info.dbname
    dsn = provision_runtime(database, "workbench")
    login = psycopg.conninfo.conninfo_to_dict(dsn)["user"]
    return {"owner": w.DSN, "dsn": dsn, "login": login, "database": database}


def wait_blocked(observer, blocked_pid, blocker_pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if observer.execute(
            "SELECT %s=ANY(pg_blocking_pids(%s))", (blocker_pid, blocked_pid)
        ).fetchone()[0]:
            return True
        time.sleep(0.02)
    return False


def test_1_red_runtime_direct_share_lock_is_denied_before_install(runtime):
    """Reproduces the retained defect: 42501 -> fail-closed capability code."""
    with psycopg.connect(runtime["dsn"]) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
            with db.transaction():
                db.execute(
                    "SELECT active FROM users WHERE username='reviewer' FOR SHARE NOWAIT"
                )
        assert caught.value.sqlstate == "42501"
        assert requester_problem(db, "reviewer") == "REQUESTER_LOCK_CAPABILITY_REQUIRED"


def test_2_install_verify_and_runtime_lock_succeeds(runtime):
    from folio.requester_lock import verify
    from scripts.requester_lock_install import apply, runtime_verify

    apply(runtime["database"], runtime["login"])
    report = runtime_verify(runtime["dsn"])
    assert report["verified"] is True
    assert report["probe_unknown_user"] == "REQUESTER_AUTHORITY_REVOKED"
    with psycopg.connect(runtime["dsn"]) as db:
        assert verify(db, runtime["login"])["owner"] == runtime["login"] + "_rq_lock"
        assert requester_problem(db, "reviewer") is None
        assert requester_problem(db, "approver") == "REQUESTER_AUTHORITY_REVOKED"
        assert requester_problem(db, "nobody") == "REQUESTER_AUTHORITY_REVOKED"
        # Runtime still has no users write privilege of any kind.
        for column in ("username", "salt", "password_hash", "roles", "active"):
            assert db.execute(
                "SELECT has_column_privilege(current_user,'public.users',%s,'UPDATE')",
                (column,),
            ).fetchone() == (False,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute("UPDATE users SET active=active WHERE false")


def test_3_reinstall_never_adopts_existing_owner_or_schema(runtime):
    from scripts.requester_lock_install import apply

    with pytest.raises(RuntimeError) as caught:
        apply(runtime["database"], runtime["login"])
    assert "NOT_ADOPTED" in str(caught.value)


def test_4_lock_physically_blocks_role_revocation_until_commit(runtime):
    with (
        psycopg.connect(runtime["owner"], autocommit=True) as observer,
        psycopg.connect(runtime["dsn"]) as guard,
        psycopg.connect(runtime["owner"]) as revoker,
    ):
        # Production callers already hold an open transaction (savepoint path).
        guard.execute("SELECT 1")
        assert requester_problem(guard, "reviewer") is None
        guard_pid = guard.info.backend_pid
        done = {}

        def revoke():
            revoker.execute(
                "UPDATE users SET roles='[\"reviewer\",\"x\"]' WHERE username='reviewer'"
            )
            done["at"] = time.monotonic()
            revoker.rollback()

        t = threading.Thread(target=revoke)
        t.start()
        assert wait_blocked(observer, revoker.info.backend_pid, guard_pid)
        assert "at" not in done
        released = time.monotonic()
        guard.commit()
        t.join(5)
        assert done["at"] >= released


def test_5_concurrent_authority_mutation_reports_busy_not_wait(runtime):
    with (
        psycopg.connect(runtime["owner"]) as mutator,
        psycopg.connect(runtime["dsn"]) as db,
    ):
        mutator.execute(
            "SELECT 1 FROM users WHERE username='reviewer' FOR UPDATE"
        ).fetchone()
        db.execute("SELECT 1")
        started = time.monotonic()
        assert requester_problem(db, "reviewer") == "REQUESTER_LOCK_BUSY"
        assert time.monotonic() - started < 2
        # The outer transaction is still usable after the caught error.
        assert db.execute("SELECT 1").fetchone() == (1,)
        mutator.rollback()


def test_6_deactivated_or_role_removed_requester_is_revoked(runtime):
    with psycopg.connect(runtime["owner"]) as owner:
        owner.execute("UPDATE users SET active=false WHERE username='reviewer'")
        owner.commit()
        try:
            with psycopg.connect(runtime["dsn"]) as db:
                assert requester_problem(db, "reviewer") == "REQUESTER_AUTHORITY_REVOKED"
        finally:
            owner.execute("UPDATE users SET active=true WHERE username='reviewer'")
            owner.commit()


def test_7_other_login_cannot_execute_and_session_binding_holds(runtime):
    from folio.requester_lock import SIGNATURE

    reader = psycopg.conninfo.make_conninfo(
        os.environ["FOLIO_TARGET_READ_DSN"], dbname=runtime["database"]
    )
    with psycopg.connect(reader) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(
                "SELECT * FROM " + SIGNATURE.replace("(text)", "(%s)"), ("reviewer",)
            )
    # Even the database owner (not the bound login) is refused by the body.
    from folio.database_security import admin

    admin(
        "GRANT USAGE ON SCHEMA folio_requester_lock_v1 TO PUBLIC; "
        "GRANT EXECUTE ON FUNCTION folio_requester_lock_v1.locked_requester(text) TO "
        + '"'
        + psycopg.conninfo.conninfo_to_dict(runtime["owner"])["user"]
        + '";',
        runtime["database"],
    )
    try:
        with psycopg.connect(runtime["owner"]) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
                db.execute(
                    "SELECT * FROM " + SIGNATURE.replace("(text)", "(%s)"),
                    ("reviewer",),
                )
            assert "REQUESTER_LOCK_DENIED" in str(caught.value)
    finally:
        admin(
            "REVOKE USAGE ON SCHEMA folio_requester_lock_v1 FROM PUBLIC; "
            "REVOKE EXECUTE ON FUNCTION folio_requester_lock_v1.locked_requester(text) FROM "
            + '"'
            + psycopg.conninfo.conninfo_to_dict(runtime["owner"])["user"]
            + '";',
            runtime["database"],
        )


def test_8_real_api_prepare_analysis_as_runtime_login(runtime, tmp_path):
    """Socket HTTP: least-privileged API accepts a prepare_proposal enqueue."""
    import subprocess
    import sys

    import httpx

    from folio import workbench as w
    from scripts.runtime import is_free, ready, stop

    port = 18293
    assert is_free(port)
    env = {
        **os.environ,
        "FOLIO_DSN": runtime["dsn"],
        "FOLIO_SCHEMA_MODE": "runtime",
    }
    cid = w.create_case()["id"]
    log = (tmp_path / "api.log").open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "folio.workbench:app", "--host",
         "127.0.0.1", "--port", str(port)],
        env=env, stdout=log, stderr=log,
    )
    try:
        ready(port)
        origin = w.ORIGIN
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "reviewer", "password": "folio-reviewer-local"},
                headers={"Origin": origin},
            )
            assert login.status_code == 200, login.text
            csrf = login.json()["csrf"]
            case = client.get(f"/api/v1/workspaces/{w.WORKSPACE}/cases/{cid}")
            assert case.status_code == 200
            version = case.json()["version"]
            import uuid

            r = client.post(
                f"/api/v1/workspaces/{w.WORKSPACE}/cases/{cid}/analysis-runs",
                json={"prepare_proposal": True},
                headers={
                    "Origin": origin,
                    "x-csrf-token": csrf,
                    "Idempotency-Key": str(uuid.uuid4()),
                    "If-Match": f'"case-v{version}"',
                },
            )
            assert r.status_code == 202, r.text
            assert r.json()["status"] == "queued"
    finally:
        stop(server)
        log.close()
