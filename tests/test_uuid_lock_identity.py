"""FO-02: actual target HTTP + isolated PostgreSQL, no fake lock/row store."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psycopg
import pytest
from authority_fixture import attestation

from folio.identity import WORKSPACE, service_header
from folio.target import DSN, intent_hash, seed


@pytest.fixture
def case():
    # Never run these writes against a retained/live database or service.
    assert "folio_finish_target_" in DSN
    assert os.environ["FOLIO_TARGET_URL"] == "http://127.0.0.1:18201"
    rid, op = str(uuid4()), "abcdefab-" + str(uuid4())[9:]
    seed(WORKSPACE, rid)
    patch = dict(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    digest = intent_hash(WORKSPACE, rid, 7, patch)
    base = os.environ["FOLIO_TARGET_URL"] + f"/api/v1/workspaces/{WORKSPACE}"
    c = dict(rid=rid, op=op, patch=patch, digest=digest, base=base)
    assert request(c, "fence").status_code == 200
    r = request(c, "grant")
    assert r.status_code == 200, r.text
    c["grant"] = r.json()
    return c


def request(
    c,
    route,
    *,
    workspace=WORKSPACE,
    operation=None,
    key=None,
    fence=41,
    body_extra=None,
):
    op = c["op"] if operation is None else operation
    base = c["base"].replace(WORKSPACE, workspace)
    if route == "fence":
        return httpx.post(
            base + "/operation-fences",
            headers=service_header("broker"),
            json=dict(
                operation_key=op, intent_digest=c["digest"], monotonic_fence=fence
            ),
            timeout=15,
        )
    if route == "grant":
        return httpx.post(
            base + "/operation-grants",
            headers=service_header("broker"),
            json=dict(
                operation_key=op,
                intent_digest=c["digest"],
                target_id=c["rid"],
                expected_revision=7,
                target_fence=fence,
                exact_patch=c["patch"],
                approval=attestation(WORKSPACE, c["op"], c["digest"], c["rid"]),
            ),
            timeout=15,
        )
    if route == "read":
        return httpx.get(
            base + "/operations/" + op, headers=service_header("verifier"), timeout=15
        )
    return httpx.patch(
        base + "/payables/" + c["rid"],
        headers={
            **service_header("dispatcher"),
            "Idempotency-Key": op if key is None else key,
            "If-Match": '"payable-v7"',
        },
        json={
            "intent_digest": c["digest"],
            "grant_jti": c["grant"]["jti"],
            "fence": fence,
            "exact_patch": c["patch"],
            **(body_extra or {}),
        },
        timeout=15,
    )


def state(c):
    with psycopg.connect(DSN) as db:
        count = db.execute(
            "SELECT count(*) FROM operations WHERE workspace=%s AND operation_key=%s",
            (WORKSPACE, c["op"]),
        ).fetchone()[0]
        revision = db.execute(
            "SELECT body->>'revision' FROM payables WHERE workspace=%s AND id=%s",
            (WORKSPACE, c["rid"]),
        ).fetchone()[0]
    return count, int(revision)


def wait_blocked(blocker_pid, count=1):
    """Prove a real PostgreSQL backend is blocked, not a timing-only sleep."""
    deadline = time.monotonic() + 8
    with psycopg.connect(DSN, autocommit=True) as db:
        while time.monotonic() < deadline:
            rows = db.execute(
                "SELECT a.pid,CASE WHEN EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid AND NOT l.granted AND l.locktype='advisory') THEN 'pg_advisory' ELSE a.query END FROM pg_stat_activity a WHERE %s=ANY(pg_blocking_pids(a.pid))",
                (blocker_pid,),
            ).fetchall()
            if len(rows) >= count:
                return rows[0]
            time.sleep(0.02)
    pytest.fail(f"No PostgreSQL waiter behind backend {blocker_pid}")


@pytest.mark.parametrize("route", ["fence", "grant", "mutate", "read"])
@pytest.mark.parametrize("spelling", ["upper", "compact", "braced", "malformed"])
def test_operation_aliases_explicitly_rejected_without_effect(case, route, spelling):
    op = case["op"]
    raw = {
        "upper": op.upper(),
        "compact": op.replace("-", ""),
        "braced": "{" + op + "}",
        "malformed": "not-a-uuid",
    }[spelling]
    r = request(case, route, operation=raw)
    assert r.status_code == 422, (raw, r.status_code, r.text)
    assert raw in r.text, r.text
    assert state(case) == (0, 7)


@pytest.mark.parametrize("route", ["fence", "grant", "mutate", "read"])
@pytest.mark.parametrize(
    "raw", [WORKSPACE.replace("-", ""), "{" + WORKSPACE + "}", "bad-workspace"]
)
def test_workspace_aliases_rejected_before_scope_lookup(case, route, raw):
    r = request(case, route, workspace=raw)
    assert r.status_code == 422, r.text
    assert raw in r.text
    assert state(case) == (0, 7)


@pytest.mark.parametrize("route", ["fence", "grant", "mutate", "read"])
def test_all_routes_share_canonical_operation_lock_and_alias_rejects_without_wait(
    case, route
):
    with psycopg.connect(DSN) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (WORKSPACE + case["op"],),
        )
        pending = pool.submit(request, case, route)
        try:
            _, lock_type = wait_blocked(blocker.info.backend_pid)
            assert lock_type == "pg_advisory"
            rejected = request(case, route, operation=case["op"].upper())
            assert rejected.status_code == 422 and case["op"].upper() in rejected.text
            assert not pending.done()
        finally:
            blocker.commit()
        assert pending.result(timeout=12).status_code == (
            404 if route == "read" else 200
        )


def test_uppercase_workspace_identity_cannot_select_a_second_lock():
    from fastapi import HTTPException

    from folio.target import lock_identity

    workspace = "abcdefab-1111-4111-8111-111111111111"
    operation = "abcdefab-2222-4222-8222-222222222222"
    assert lock_identity(workspace, operation) == workspace + operation
    with pytest.raises(HTTPException) as error:
        lock_identity(workspace.upper(), operation)
    assert error.value.status_code == 422
    assert error.value.detail["input"] == workspace.upper()


def test_body_cannot_select_different_operation_than_header(case):
    # MutationCommand deliberately has no second operation identity; extra fields fail closed.
    raw = str(uuid4())
    r = request(case, "mutate", body_extra={"operation_key": raw})
    assert r.status_code == 422 and raw in r.text
    assert state(case) == (0, 7)


def test_stale_41_cannot_write_after_42_authoritative_absence(case):
    assert request(case, "fence", fence=42).status_code == 200
    absent = request(case, "read")
    assert absent.status_code == 404
    assert absent.json()["detail"]["fence"] == 42
    assert absent.json()["detail"]["authoritative"] is True
    for key in (case["op"].upper(), case["op"]):
        r = request(case, "mutate", key=key)
        assert r.status_code == (422 if key != case["op"] else 409), r.text
    assert state(case) == (0, 7)


def test_duplicate_canonical_write_serializes_with_fence_and_read_no_false_absence(
    case,
):
    with psycopg.connect(DSN) as blocker, ThreadPoolExecutor(max_workers=4) as pool:
        blocker.execute(
            "SELECT 1 FROM payables WHERE workspace=%s AND id=%s FOR UPDATE",
            (WORKSPACE, case["rid"]),
        )
        first = pool.submit(request, case, "mutate")
        writer_pid, query = wait_blocked(blocker.info.backend_pid)
        assert "payables" in query
        duplicate = pool.submit(request, case, "mutate")
        fence = pool.submit(request, case, "fence", fence=42)
        read = pool.submit(request, case, "read")
        _, query = wait_blocked(writer_pid, count=3)
        assert "pg_advisory" in query
        assert not first.done() and not read.done()
        blocker.commit()
        results = [f.result(timeout=12) for f in (first, duplicate, fence, read)]
    assert [r.status_code for r in results] == [200, 200, 200, 200], [
        r.text for r in results
    ]
    assert sorted(r.json()["replayed"] for r in results[:2]) == [False, True]
    assert results[3].json()["receipt"]["operation_key"] == case["op"]
    assert state(case) == (1, 8)


def test_fence_lock_barrier_rejects_waiting_stale_writer(case):
    with psycopg.connect(DSN) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (WORKSPACE + case["op"],),
        )
        pending = pool.submit(request, case, "mutate")
        _, query = wait_blocked(blocker.info.backend_pid)
        assert "pg_advisory" in query
        # The barrier stands for a fence transaction paused just before its commit.
        blocker.execute(
            "UPDATE authority SET fence=42 WHERE workspace=%s AND operation_key=%s",
            (WORKSPACE, case["op"]),
        )
        blocker.commit()
        assert pending.result(timeout=12).status_code == 409
    absent = request(case, "read")
    assert absent.status_code == 404 and absent.json()["detail"]["fence"] == 42
    assert state(case) == (0, 7)


@pytest.mark.parametrize("alias", ["upper", "compact", "braced"])
def test_alias_writer_cannot_commit_after_canonical_fence_absence(case, alias):
    raw = {
        "upper": case["op"].upper(),
        "compact": case["op"].replace("-", ""),
        "braced": "{" + case["op"] + "}",
    }[alias]
    with psycopg.connect(DSN) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(
            "SELECT 1 FROM payables WHERE workspace=%s AND id=%s FOR UPDATE",
            (WORKSPACE, case["rid"]),
        )
        pending = pool.submit(request, case, "mutate", key=raw)
        try:
            deadline = time.monotonic() + 8
            with psycopg.connect(DSN, autocommit=True) as observer:
                while not pending.done():
                    rows = observer.execute(
                        "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                        (blocker.info.backend_pid,),
                    ).fetchall()
                    if rows:
                        break
                    assert time.monotonic() < deadline, (
                        "alias neither rejected nor row-blocked"
                    )
                    time.sleep(0.02)
            # Before the fix the alias holds a DIFFERENT operation lock and has read fence 41.
            assert request(case, "fence", fence=42).status_code == 200
            absence = request(case, "read")
            assert (
                absence.status_code == 404 and absence.json()["detail"]["fence"] == 42
            )
        finally:
            blocker.commit()
        result = pending.result(timeout=12)
    assert result.status_code == 422, (
        raw,
        absence.json(),
        result.status_code,
        result.text,
    )
    assert raw in result.text
    assert state(case) == (0, 7)


def test_expired_grant_rechecked_after_payable_row_wait(case):
    with psycopg.connect(DSN) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute(
            "SELECT 1 FROM payables WHERE workspace=%s AND id=%s FOR UPDATE",
            (WORKSPACE, case["rid"]),
        )
        pending = pool.submit(request, case, "mutate")
        _, query = wait_blocked(blocker.info.backend_pid)
        assert "payables" in query
        # Isolated fixture expiry avoids a 60-second wall-clock wait; not a production fence bypass.
        blocker.execute(
            "UPDATE authority SET expires=clock_timestamp()-interval '1 second' WHERE workspace=%s AND operation_key=%s",
            (WORKSPACE, case["op"]),
        )
        blocker.commit()
        r = pending.result(timeout=12)
        assert r.status_code == 403, r.text
    assert state(case) == (0, 7)
