"""Round 6: bounded API connection pool on real PostgreSQL.

Pins: one transaction per lease (commit / rollback), transaction-local tenant context,
DISCARD ALL between leases, no session state or tenant data leaking across reused
connections (owner and non-owner runtime principals under FORCE RLS), bounded size with
an explicit 503, stale-connection replacement, and API reuse under concurrency.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from folio.dbpool import Pool, PoolTimeout

TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture(scope="module")
def owner_dsn():
    from folio.workbench import migrate

    migrate()
    return os.environ["FOLIO_DSN"]


@pytest.fixture(scope="module")
def probe(owner_dsn):
    """A FORCE-RLS table whose policy is keyed ONLY on the transaction-local tenant GUC."""
    table = "pool_probe_" + uuid4().hex[:8]
    with psycopg.connect(owner_dsn) as db:
        db.execute(
            sql.SQL(
                "CREATE TABLE {t}(workspace uuid NOT NULL, secret text NOT NULL);"
                "ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;"
                "ALTER TABLE {t} FORCE ROW LEVEL SECURITY;"
                "CREATE POLICY tenant ON {t} FOR ALL TO PUBLIC"
                " USING (workspace = nullif(current_setting('folio.workspace', true), '')::uuid)"
                " WITH CHECK (workspace = nullif(current_setting('folio.workspace', true), '')::uuid);"
            ).format(t=sql.Identifier(table))
        )
    pool = Pool(owner_dsn, max_size=1, timeout=3)
    for tenant in (TENANT_A, TENANT_B):
        for n in range(3):
            with pool.lease(tenant) as db:
                db.execute(
                    sql.SQL("INSERT INTO {} VALUES(%s,%s)").format(sql.Identifier(table)),
                    (tenant, f"{tenant[:4]}-secret-{n}"),
                )
    pool.close()
    return table


def runtime_dsn_for(owner_dsn, table):
    from folio.database_security import provision_runtime

    runtime = provision_runtime(urlsplit(owner_dsn).path[1:], "workbench")
    role = urlsplit(runtime).username
    with psycopg.connect(owner_dsn) as db:
        db.execute(
            sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(
                sql.Identifier(table), sql.Identifier(role)
            )
        )
    return runtime


def secrets_seen(db, table):
    return sorted(
        r[0]
        for r in db.execute(
            sql.SQL("SELECT secret FROM {}").format(sql.Identifier(table))
        )
    )


def test_lease_is_one_transaction_with_transaction_local_tenant(owner_dsn, probe):
    pool = Pool(owner_dsn, max_size=1, timeout=3)
    with pool.lease(TENANT_A) as db:
        pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
        assert db.execute("SELECT current_setting('folio.workspace')").fetchone()[0] == TENANT_A
        assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        db.execute(
            sql.SQL("INSERT INTO {} VALUES(%s,'committed-row')").format(sql.Identifier(probe)),
            (TENANT_A,),
        )
    with pytest.raises(RuntimeError, match="boom"):
        with pool.lease(TENANT_A) as db:
            db.execute(
                sql.SQL("INSERT INTO {} VALUES(%s,'rolled-back-row')").format(
                    sql.Identifier(probe)
                ),
                (TENANT_A,),
            )
            raise RuntimeError("boom")
    # The idle pooled connection carries no tenant at all between leases.
    idle = pool._idle[-1]
    assert idle.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
    assert idle.execute("SELECT current_setting('folio.workspace', true)").fetchone()[0] in (
        None,
        "",
    )
    idle.rollback()
    with pool.lease(TENANT_A) as db:
        seen = secrets_seen(db, probe)
    assert "committed-row" in seen and "rolled-back-row" not in seen
    with pool.lease(TENANT_A) as db:
        db.execute(sql.SQL("DELETE FROM {} WHERE secret='committed-row'").format(sql.Identifier(probe)))
    pool.close()


def test_reused_connection_carries_no_session_state(owner_dsn):
    pool = Pool(owner_dsn, max_size=1, timeout=3)
    with pool.lease(TENANT_A) as db:
        pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
        # Deliberately pollute every kind of session state a handler could leave behind.
        db.execute("SELECT set_config('folio.workspace', %s, false)", (TENANT_A,))
        db.execute("SET statement_timeout = '1234ms'")
        db.execute("SET search_path = pg_temp, public")
        db.execute("SET application_name = 'polluted'")
        db.execute("CREATE TEMP TABLE leaked(x int)")
        db.execute("SELECT pg_advisory_lock(424242)")
        db.execute("PREPARE leaked_stmt AS SELECT 1")
        db.execute("LISTEN leaked_channel")
        db.execute("DECLARE leaked_cursor CURSOR WITH HOLD FOR SELECT 1")
    with pool.lease(TENANT_B) as db:
        assert db.execute("SELECT pg_backend_pid()").fetchone()[0] == pid, "not reused"
        assert db.execute("SELECT current_setting('folio.workspace')").fetchone()[0] == TENANT_B
        assert db.execute("SHOW statement_timeout").fetchone()[0] == "0"
        assert "pg_temp" not in db.execute("SHOW search_path").fetchone()[0]
        assert db.execute("SELECT current_setting('application_name')").fetchone()[0] == "folio-pool"
        assert db.execute("SELECT to_regclass('pg_temp.leaked')").fetchone()[0] is None
        assert db.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()"
        ).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM pg_listening_channels()").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM pg_cursors").fetchone()[0] == 0
        assert db.execute("SELECT current_user = session_user").fetchone()[0] is True
    # The advisory lock really is released: another session can take it immediately.
    with psycopg.connect(owner_dsn, autocommit=True) as other:
        assert other.execute("SELECT pg_try_advisory_lock(424242)").fetchone()[0] is True
        other.execute("SELECT pg_advisory_unlock(424242)")
    pool.close()


@pytest.mark.parametrize("principal", ["owner", "runtime"])
def test_no_cross_tenant_leak_on_reused_connections(owner_dsn, probe, principal):
    dsn = owner_dsn if principal == "owner" else runtime_dsn_for(owner_dsn, probe)
    if principal == "runtime":
        with psycopg.connect(dsn) as db:
            assert db.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False, False)
    expected = {
        TENANT_A: [f"{TENANT_A[:4]}-secret-{n}" for n in range(3)],
        TENANT_B: [f"{TENANT_B[:4]}-secret-{n}" for n in range(3)],
    }
    # Serial alternation on ONE connection: every lease reuses the previous tenant's backend.
    pool = Pool(dsn, max_size=1, timeout=3)
    pids = set()
    for i in range(20):
        tenant = TENANT_A if i % 2 else TENANT_B
        with pool.lease(tenant) as db:
            pids.add(db.execute("SELECT pg_backend_pid()").fetchone()[0])
            assert secrets_seen(db, probe) == expected[tenant]
    assert len(pids) == 1
    # Writing into the other tenant is refused, and the failed lease leaves no residue.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pool.lease(TENANT_A) as db:
            db.execute(
                sql.SQL("INSERT INTO {} VALUES(%s,'cross-write')").format(sql.Identifier(probe)),
                (TENANT_B,),
            )
    with pool.lease(TENANT_B) as db:
        assert secrets_seen(db, probe) == expected[TENANT_B]
    pool.close()

    # Concurrent alternation: 8 threads x 40 leases over 3 shared connections.
    pool = Pool(dsn, max_size=3, timeout=10)
    errors = []

    def worker(k):
        for i in range(40):
            tenant = TENANT_A if (i + k) % 2 else TENANT_B
            try:
                with pool.lease(tenant) as db:
                    got = db.execute("SELECT current_setting('folio.workspace')").fetchone()[0]
                    seen = secrets_seen(db, probe)
                if got != tenant or seen != expected[tenant]:
                    errors.append((tenant, got, seen))
            except Exception as error:  # noqa: BLE001
                errors.append(repr(error))

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(worker, range(8)))
    assert errors == []
    assert pool.snapshot()["opened"] <= 3
    pool.close()


def test_aborted_transaction_is_rolled_back_and_backend_reused(owner_dsn):
    pool = Pool(owner_dsn, max_size=1, timeout=3)
    with pytest.raises(psycopg.errors.DivisionByZero):
        with pool.lease(TENANT_A) as db:
            pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
            db.execute("SELECT 1/0")
    with pool.lease(TENANT_A) as db:
        assert db.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
        assert db.execute("SELECT 1").fetchone()[0] == 1
    pool.close()


def test_pool_is_bounded_and_times_out(owner_dsn):
    pool = Pool(owner_dsn, max_size=2, timeout=0.5)
    hold = threading.Event()
    held = threading.Barrier(3)

    def holder():
        with pool.lease(TENANT_A):
            held.wait(5)
            hold.wait(5)

    threads = [threading.Thread(target=holder) for _ in range(2)]
    for t in threads:
        t.start()
    held.wait(5)
    started = time.monotonic()
    with pytest.raises(PoolTimeout):
        with pool.lease(TENANT_A):
            pass
    assert 0.4 <= time.monotonic() - started < 3
    assert pool.snapshot()["open"] == 2
    hold.set()
    for t in threads:
        t.join(5)
    with pool.lease(TENANT_A) as db:  # capacity returns after release
        assert db.execute("SELECT 1").fetchone()[0] == 1
    assert pool.snapshot()["opened"] == 2
    pool.close()


def test_stale_connection_after_backend_termination_is_replaced(owner_dsn):
    pool = Pool(owner_dsn, max_size=1, timeout=3)
    with pool.lease(TENANT_A) as db:
        pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(owner_dsn, autocommit=True) as admin:
        assert admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
        for _ in range(50):
            if not admin.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone():
                break
            time.sleep(0.05)
    with pool.lease(TENANT_A) as db:
        assert db.execute("SELECT pg_backend_pid()").fetchone()[0] != pid
    assert pool.snapshot()["discarded"] >= 1
    pool.close()


def test_api_sheds_load_with_503_when_pool_is_exhausted(owner_dsn, monkeypatch):
    from fastapi.testclient import TestClient

    from folio import workbench as w

    # A distinct DSN spelling gets its own (tiny) pool in this process.
    monkeypatch.setattr(w, "DSN", owner_dsn + "?application_name=pool-503-probe")
    monkeypatch.setattr(w, "POOL_MAX", 1)
    monkeypatch.setattr(w, "POOL_TIMEOUT", 0.3)
    client = TestClient(w.app)
    with w.pooled():
        r = client.get("/api/v1/auth/session")
        health = client.get("/health")
    assert r.status_code == 503 and r.json() == {"detail": "DATABASE_BUSY"}
    assert r.headers["retry-after"] == "1"
    assert health.status_code == 503 and health.json()["status"] == "unready"
    assert health.json()["alarms"][0] == "DATABASE_UNAVAILABLE"
    assert client.get("/api/v1/auth/session").status_code == 401  # capacity is back
    assert client.get("/health").status_code == 200
    from folio.dbpool import _pools, _pools_lock

    with _pools_lock:
        probe_pool = _pools.pop(w.DSN)
    probe_pool.close()  # do not leave this test's 1-connection pool open


def test_api_reuses_a_bounded_set_of_connections_under_concurrency(owner_dsn):
    from fastapi.testclient import TestClient

    from folio import workbench as w
    from folio.dbpool import pool_for

    client = TestClient(w.app)
    login = client.post(
        "/api/v1/auth/login", json={"username": "viewer", "password": "folio-viewer-local"}
    )
    assert login.status_code == 200, login.text
    cookie = login.cookies.get("folio_session")
    url = f"/api/v1/workspaces/{w.WORKSPACE}/cases"
    failures = []
    pool = pool_for(w.DSN, w.POOL_MAX, w.POOL_TIMEOUT)
    before = pool.snapshot()["leases"]

    def reader(_):
        local = TestClient(w.app)
        local.cookies.set("folio_session", cookie)
        for _ in range(15):
            r = local.get(url)
            if r.status_code != 200:
                failures.append(r.status_code)

    with ThreadPoolExecutor(20) as ex:
        list(ex.map(reader, range(20)))
    assert failures == []
    snap = pool.snapshot()
    # 20 threads x 15 = 300 requests x 2 leases (identity + handler), <= POOL_MAX connections.
    assert snap["leases"] - before == 600, (before, snap)
    assert snap["open"] <= w.POOL_MAX, snap
    assert snap["opened"] <= w.POOL_MAX + snap["discarded"], snap
    database = urlsplit(owner_dsn).path[1:]
    with psycopg.connect(owner_dsn, autocommit=True) as db:
        backends = db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND application_name='folio-api-pool'",
            (database,),
        ).fetchone()[0]
    assert backends <= w.POOL_MAX
