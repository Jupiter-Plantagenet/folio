"""Actual provisioned nonowner authority composition, isolated PostgreSQL/socket only."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.skipif(
    os.getenv("FOLIO_EXPORT_PROOF") != "1", reason="isolated export proof only"
)


def test_actual_runtime_unknown_artifact_and_auth_write_denials(tmp_path):
    from folio import workbench as w
    from folio.database_security import provision_runtime
    from scripts.export_migrate import apply

    w.migrate()
    owner_dsn = w.DSN
    with psycopg.connect(owner_dsn) as db:
        database = db.info.dbname
    assert database.startswith("folio_finish_wb_export")
    runtime = provision_runtime(database, "workbench")
    role = psycopg.conninfo.conninfo_to_dict(runtime)["user"]
    # Retained authority is users SELECT only, even before corrected provisioning.
    with psycopg.connect(owner_dsn) as db:
        apply(db, role)
    denials = {}
    with psycopg.connect(runtime) as db:
        for col in ("username", "salt", "password_hash", "roles", "active"):
            assert db.execute(
                "SELECT has_column_privilege(current_user,'public.users',%s,'UPDATE')",
                (col,),
            ).fetchone() == (False,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
                with db.transaction():
                    db.execute(
                        sql.SQL("UPDATE public.users SET {}={} WHERE false").format(
                            sql.Identifier(col), sql.Identifier(col)
                        )
                    )
            denials[col] = caught.value.sqlstate
        for command in (
            "INSERT INTO public.users SELECT * FROM public.users WHERE false",
            "DELETE FROM public.users WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(command)
        token = hashlib.sha256(b"unknown").hexdigest()
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
            with db.transaction():
                db.execute(
                    "SELECT u.username FROM public.sessions s JOIN public.users u USING(username) WHERE s.token=%s FOR SHARE OF s,u",
                    (token,),
                )
        assert caught.value.sqlstate == "42501"
    from scripts.runtime import is_free, ready, stop

    assert is_free(18291)
    env = {**os.environ, "FOLIO_DSN": runtime, "FOLIO_SCHEMA_MODE": "runtime"}
    logpath = Path(os.environ["FOLIO_EXPORT_EVIDENCE"]) / "least-runtime-api.log"
    with logpath.open("w") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "folio.workbench:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18291",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            ready(18291)
            with httpx.Client(base_url="http://127.0.0.1:18291") as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "auditor", "password": "folio-auditor-local"},
                )
                assert login.status_code == 200
                result = client.get(
                    f"/api/v1/workspaces/{w.WORKSPACE}/artifacts/{uuid4()}"
                )
                (
                    Path(os.environ["FOLIO_EXPORT_EVIDENCE"])
                    / "least-runtime-result.json"
                ).write_text(
                    json.dumps(
                        {
                            "status": result.status_code,
                            "auth_write_denials": denials,
                            "direct_join_lock_sqlstate": "42501",
                        }
                    )
                )
                assert result.status_code == 404, result.text
        finally:
            stop(server)


@pytest.fixture
def actor_runtime():
    from folio import workbench as w
    from folio.database_security import provision_runtime

    w.migrate()
    with psycopg.connect(w.DSN) as db:
        database = db.info.dbname
    assert database.startswith("folio_finish_wb_export")
    runtime = provision_runtime(database, "workbench")
    cookie = uuid4().hex
    token = hashlib.sha256(cookie.encode()).hexdigest()
    from starlette.requests import Request

    req = Request(
        {"type": "http", "headers": [(b"cookie", ("folio_session=" + cookie).encode())]}
    )
    with psycopg.connect(w.DSN) as db:
        db.execute(
            "INSERT INTO public.sessions VALUES(%s,'auditor','csrf',clock_timestamp()+interval '1 hour')",
            (token,),
        )
    yield w.DSN, runtime, token, req


def wait_blocked(observer, blocked, blocker):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if observer.execute(
            "SELECT %s=ANY(pg_blocking_pids(%s))", (blocker, blocked)
        ).fetchone()[0]:
            return
        time.sleep(0.02)
    raise AssertionError("physical database blocking edge absent")


def test_helper_security_catalog_and_ungranted_principal_denial(actor_runtime):
    owner, runtime, token, req = actor_runtime
    with psycopg.connect(runtime) as db:
        role = db.info.user
        row = db.execute(
            "SELECT prosecdef,provolatile,proconfig,pg_get_userbyid(proowner),pg_get_function_result(oid) FROM pg_proc WHERE oid='public.folio_export_lock_actor(text)'::regprocedure"
        ).fetchone()
        assert row[0:2] == (True, "v")
        assert row[2] == ["search_path=pg_catalog, pg_temp", "lock_timeout=5s"]
        assert row[3] != role
        assert row[4] == "TABLE(username text, roles jsonb, csrf text)"
        assert db.execute(
            "SELECT count(*) FROM pg_proc p,LATERAL aclexplode(p.proacl) a WHERE p.oid='public.folio_export_lock_actor(text)'::regprocedure AND a.grantee=0"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE')"
        ).fetchone() == (False,)
        for col in ("username", "salt", "password_hash", "roles", "active"):
            assert db.execute(
                "SELECT has_column_privilege(current_user,'public.users',%s,'UPDATE')",
                (col,),
            ).fetchone() == (False,)
    stranger = psycopg.conninfo.make_conninfo(
        os.environ["FOLIO_TARGET_READ_DSN"],
        dbname=psycopg.conninfo.conninfo_to_dict(runtime)["dbname"],
    )
    with psycopg.connect(stranger) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM public.folio_export_lock_actor(%s)", (token,))


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE public.users SET roles='[]' WHERE username='auditor'",
        "UPDATE public.users SET active=false WHERE username='auditor'",
        "DELETE FROM public.sessions WHERE token=%s",
        "UPDATE public.sessions SET expires=clock_timestamp()-interval '1 second' WHERE token=%s",
    ],
)
def test_authorized_transaction_physically_blocks_revokers(actor_runtime, mutation):
    from concurrent.futures import ThreadPoolExecutor

    from folio.exports import authorized

    owner, runtime, token, req = actor_runtime
    with (
        psycopg.connect(owner, autocommit=True) as observer,
        psycopg.connect(runtime) as guard,
        psycopg.connect(owner) as revoke,
    ):
        assert authorized(guard, req)["username"] == "auditor"

        def change():
            revoke.execute(mutation, (token,) if "%s" in mutation else None)
            revoke.commit()

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(change)
            try:
                wait_blocked(observer, revoke.info.backend_pid, guard.info.backend_pid)
                assert not future.done()
            finally:
                guard.commit()
            future.result(timeout=5)
    # Owner teardown only, not a runtime mutation capability.
    with psycopg.connect(owner) as db:
        db.execute(
            "UPDATE public.users SET roles='[\"auditor\"]',active=true WHERE username='auditor'"
        )


@pytest.mark.parametrize(
    "mode,expected", [("roles", 403), ("active", 401), ("expiry", 401)]
)
def test_lock_wait_rechecks_committed_revocation_and_wall_expiry(
    actor_runtime, mode, expected
):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import HTTPException

    from folio.exports import authorized

    owner, runtime, token, req = actor_runtime
    with (
        psycopg.connect(owner, autocommit=True) as observer,
        psycopg.connect(owner) as blocker,
        psycopg.connect(runtime) as auth,
    ):
        if mode == "expiry":
            observer.execute(
                "UPDATE public.sessions SET expires=clock_timestamp()+interval '1 second' WHERE token=%s",
                (token,),
            )
        blocker.execute(
            "SELECT username FROM public.users WHERE username='auditor' FOR UPDATE"
        )

        def read():
            try:
                authorized(auth, req)
                return 200
            except HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(read)
            try:
                wait_blocked(observer, auth.info.backend_pid, blocker.info.backend_pid)
                if mode == "roles":
                    blocker.execute(
                        "UPDATE public.users SET roles='[]' WHERE username='auditor'"
                    )
                elif mode == "active":
                    blocker.execute(
                        "UPDATE public.users SET active=false WHERE username='auditor'"
                    )
                else:
                    while not observer.execute(
                        "SELECT expires<=clock_timestamp() FROM public.sessions WHERE token=%s",
                        (token,),
                    ).fetchone()[0]:
                        time.sleep(0.02)
            finally:
                blocker.commit()
            assert future.result(timeout=5) == expected
    with psycopg.connect(owner) as db:
        db.execute(
            "UPDATE public.users SET roles='[\"auditor\"]',active=true WHERE username='auditor'"
        )
