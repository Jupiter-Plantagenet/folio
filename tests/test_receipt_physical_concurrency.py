"""Physical PostgreSQL schedules; run ONLY through scripts/isolated_tests.py.

No live profile, fake database/transport, worker execution or production edit.
Hand-authored protected-history scaffolds are inserted once, before contenders.
The short-TTL race changes proposal construction only, never frozen history.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from test_domain_completion import session
from test_receipt_allocations import receipt

from folio import target
from folio import workbench as wb
from folio.receipts import receipt_manifest

OUT = Path(__file__).resolve().parents[1] / "evidence/documents/receipt-concurrency"
PREFIX = f"/api/v1/workspaces/{wb.WORKSPACE}"


@pytest.fixture(autouse=True)
def isolated_only():
    assert conninfo_to_dict(wb.DSN)["dbname"].startswith("folio_finish_wb_"), (
        "ISOLATED_DATABASE_REQUIRED"
    )
    assert conninfo_to_dict(target.DSN)["dbname"].startswith("folio_finish_target_")
    assert os.environ.get("FOLIO_TARGET_URL") == "http://127.0.0.1:18201"
    wb.migrate()


def retain(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (name + ".json")).write_text(
        json.dumps(value, indent=2, default=str), encoding="utf-8"
    )


def case(number, *, xlsx=False, alias=False, supplier="Aster Supply Co.", rid=None):
    """Disposable pre-transaction scaffolding: real parser + seeded target + case."""
    rid = rid or str(uuid4())
    record = target.seed(wb.WORKSPACE, rid)
    if supplier != record["supplier"]:
        record["supplier"] = supplier
        with psycopg.connect(target.DSN) as db:
            db.execute("UPDATE payables SET body=%s WHERE id=%s", (Jsonb(record), rid))
    sources = [receipt("receipt.xlsx" if xlsx else "receipt.csv", number, "100")]
    if alias:
        sources.append(receipt("exact-alias.xlsx", number, "100"))
    with wb.connect() as db:
        c = wb.create_case(sources=sources, target_id=rid, db=db)
        c["supplier"] = supplier
        c["receipt_manifest"] = receipt_manifest(sources)
        wb.save_case(db, c)
    client = TestClient(wb.app)
    auth = session(client)
    return c, client, auth


def headers(auth, version, kind="case"):
    return {**auth, "Idempotency-Key": str(uuid4()), "If-Match": f'"{kind}-v{version}"'}


def prepare(item, quantity="100", version=None):
    c, client, auth = item
    return client.post(
        PREFIX + f"/cases/{c['id']}/decisions",
        headers=headers(auth, version or c["version"]),
        json={
            "mode": "partial",
            "quantity": quantity,
            "reason": "Physical isolated concurrency schedule",
            "evidence_digest": c["source_digest"],
        },
    )


def stored(cids):
    with wb.connect() as db:
        return [
            r[0]
            for r in db.execute(
                "SELECT body FROM proposals WHERE case_id=ANY(%s::uuid[])", (cids,)
            )
        ]


def effects(cids):
    with wb.connect() as db:
        jobs = db.execute(
            "SELECT count(*) FROM jobs WHERE proposal IN (SELECT id FROM proposals WHERE case_id=ANY(%s::uuid[]))",
            (cids,),
        ).fetchone()[0]
    proposals = stored(cids)
    with psycopg.connect(target.DSN) as db:
        ledger = db.execute(
            "SELECT count(*) FROM operations WHERE operation_key=ANY(%s::uuid[])",
            ([p["operation_key"] for p in proposals if "operation_key" in p],),
        ).fetchone()[0]
    return {"jobs": jobs, "ledger_effects": ledger}


def aggregate(cids):
    rows = stored(cids)
    used = {}
    for p in rows:
        protected = p.get("gate") in ("seal_pending", "sealed", "uncertain", "applied")
        active = p["state"] == "approved" or (
            p["state"] == "pending"
            and datetime.fromisoformat(p["expires"]) > datetime.now(timezone.utc)
        )
        if not (protected or active):
            continue
        for row in p.get("allocation_manifest", {}).get("rows", []):
            key = (row["supplier"], row["key"])
            used[key] = used.get(key, Decimal(0)) + Decimal(row["allocated"])
    result = [
        {"supplier": k[0], "identity": k[1], "allocated": str(v), "available": "100"}
        for k, v in used.items()
    ]
    return rows, result


def race(name, functions, *, case_id=None):
    """Controller owns exact real lock. Two HTTP transactions must queue on it.

    Proof is pg_stat_activity + pg_blocking_pids, not thread start or elapsed time.
    Polling has a bounded deadline and no sleeps; release happens only after SQL
    proves TWO distinct server backends are simultaneously waiting at locks.
    """
    barrier = threading.Barrier(len(functions) + 1)
    controller = wb.connect()
    controller.execute("SET LOCAL statement_timeout='12000ms'")
    pid = controller.execute("SELECT pg_backend_pid()").fetchone()[0]
    if case_id:
        controller.execute("SELECT body FROM cases WHERE id=%s FOR UPDATE", (case_id,))
    controller.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (wb.WORKSPACE + ":receipt-allocations-v1",),
    )
    pool = ThreadPoolExecutor(max_workers=len(functions))

    def run(fn):
        barrier.wait(timeout=15)
        return fn()

    futures = [pool.submit(run, fn) for fn in functions]
    proof = []
    try:
        barrier.wait(timeout=15)
        deadline = time.monotonic() + 15
        with psycopg.connect(wb.DSN, autocommit=True) as observer:
            while time.monotonic() < deadline:
                proof = observer.execute(
                    "SELECT pid,wait_event_type,wait_event,pg_blocking_pids(pid),query FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND cardinality(pg_blocking_pids(pid))>0"
                ).fetchall()
                if len(proof) >= len(functions):
                    break
                if any(f.done() for f in futures):
                    break
        retain(
            name + "-barrier",
            {
                "controller_pid": pid,
                "lock": "case-row" if case_id else "receipt-advisory",
                "waiters": proof,
            },
        )
        assert len({r[0] for r in proof}) >= len(functions), (
            "TWO_PHYSICAL_WAITERS_NOT_PROVEN"
        )
    finally:
        controller.rollback()
        controller.close()
        pool.shutdown(wait=True)
    results = [f.result(timeout=15) for f in futures]
    with wb.connect() as released:
        assert released.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
            (wb.WORKSPACE + ":receipt-allocations-v1",),
        ).fetchone()[0], "ALLOCATION_LOCK_NOT_RELEASED"
    retain(
        name + "-http", [{"status": r.status_code, "body": r.json()} for r in results]
    )
    return results


@pytest.mark.parametrize(
    "alias,same_target", [(False, False), (True, False), (False, True)]
)
def test_two_physical_prepares_serialize_without_overreservation(alias, same_target):
    name = (
        "same-target"
        if same_target
        else ("exact-alias-imports" if alias else "different-cases-same-receipt")
    )
    number = "GR-" + uuid4().hex
    a = case(number, alias=alias)
    b = case(number, xlsx=True, rid=a[0]["target_id"] if same_target else None)
    replies = race(name, [lambda: prepare(a, "60"), lambda: prepare(b, "60")])
    assert sorted(r.status_code for r in replies) == [201, 422]
    denied = next(r for r in replies if r.status_code == 422)
    assert (
        "TARGET_ALREADY_RESERVED" if same_target else "QUANTITY_EXCEEDS_UNALLOCATED"
    ) in denied.text
    cids = [a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    retain(
        name + "-final",
        {"proposals": proposals, "aggregate": totals, "effects": effects(cids)},
    )
    assert len(proposals) == 1 and all(
        Decimal(r["allocated"]) <= Decimal(r["available"]) for r in totals
    )
    assert effects(cids) == {"jobs": 0, "ledger_effects": 0}


def test_supplier_isolation_physical_policy():
    number = "GR-" + uuid4().hex
    a, b = case(number), case(number, supplier="Independent Supplier B")
    replies = race("supplier-isolation", [lambda: prepare(a), lambda: prepare(b)])
    assert [r.status_code for r in replies] == [201, 201]
    cids = [a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    retain(
        "supplier-isolation-final",
        {"proposals": proposals, "aggregate": totals, "effects": effects(cids)},
    )
    assert len(totals) == 2 and all(Decimal(r["allocated"]) == 100 for r in totals)
    assert effects(cids) == {"jobs": 0, "ledger_effects": 0}


@pytest.mark.parametrize(
    "state,gate,allowed",
    [
        ("pending", None, True),
        ("approved", "prepared", False),
        ("pending", "uncertain", False),
        ("pending", "applied", False),
        ("pending", "sealed", False),
        ("pending", "seal_pending", False),
    ],
)
def test_expiry_releases_only_unprotected_pending(state, gate, allowed):
    number = "GR-" + uuid4().hex
    prior, a, b = case(number), case(number), case(number, xlsx=True)
    c = prior[0]
    # Explicit prior-transaction scaffold: insert ONCE. No signed/frozen record edit.
    manifest = {
        "policy": "receipt-allocation-v1",
        "source_digest": c["source_digest"],
        "input_case_version": c["version"],
        "rows": [
            {**r, "supplier": c["supplier"], "allocated": "100", "prior_allocated": "0"}
            for r in c["receipt_manifest"]["rows"]
        ],
        "total": "100",
    }
    p = {
        "id": str(uuid4()),
        "workspace": wb.WORKSPACE,
        "case_id": c["id"],
        "target": c["target_id"],
        "state": state,
        "gate": gate,
        "expires": "2000-01-01T00:00:00+00:00",
        "case_version": c["version"] + 1,
        "source_digest": c["source_digest"],
        "patch": {"recognized_quantity": "100"},
        "allocation_manifest": manifest,
    }
    with wb.connect() as db:
        db.execute(
            "INSERT INTO proposals VALUES(%s,%s,%s,%s)",
            (wb.WORKSPACE, p["id"], c["id"], Jsonb(p)),
        )
    name = "expiry-" + state + "-" + str(gate)
    replies = race(name, [lambda: prepare(a), lambda: prepare(b)])
    assert sorted(r.status_code for r in replies) == (
        [201, 422] if allowed else [422, 422]
    )
    cids = [c["id"], a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    retain(
        name + "-final",
        {
            "scaffolding": "hand-authored state inserted before contenders, not lifecycle transition proof",
            "proposals": proposals,
            "aggregate": totals,
            "effects": effects(cids),
        },
    )
    assert next(x for x in proposals if x["id"] == p["id"]) == p
    assert all(Decimal(r["allocated"]) <= 100 for r in totals)
    assert effects(cids) == {"jobs": 0, "ledger_effects": 0}


def test_same_case_correction_supersede_stale_version_and_failed_prepare_rollback():
    number = "GR-" + uuid4().hex
    a, b = case(number), case(number)
    first = prepare(a, "40")
    assert first.status_code == 201, first.text
    old = first.json()["proposal"]
    assert prepare(b, "60").status_code == 201
    a[0].update(a[1].get(PREFIX + f"/cases/{a[0]['id']}").json())
    before = stored([a[0]["id"]])[0]
    denied = prepare(a, "50")
    assert denied.status_code == 422 and "UNALLOCATED" in denied.text
    assert stored([a[0]["id"]])[0] == before, "FAILED_PREPARE_MUST_ROLL_BACK_SUPERSEDE"
    url = PREFIX + f"/cases/{a[0]['id']}"
    corrected = a[1].patch(
        url + "/fields/invoice",
        headers=headers(a[2], a[0]["version"]),
        json={"typed_value": "INV-1048", "reason": "Re-review pre-seal support"},
    )
    assert corrected.status_code == 200, corrected.text
    a[0].update(a[1].get(url).json())
    version = a[0]["version"]
    replies = race(
        "same-case-stale-reprepare",
        [lambda: prepare(a, "40", version), lambda: prepare(a, "40", version)],
        case_id=a[0]["id"],
    )
    assert sorted(r.status_code for r in replies) == [201, 412]
    approver = TestClient(wb.app)
    auth = session(approver, "approver")
    stale = approver.post(
        PREFIX + f"/proposals/{old['id']}/approvals",
        headers=headers(auth, 1, "proposal"),
        json={"intent_digest": old["digest"], "confirmation": "approve"},
    )
    assert stale.status_code == 412
    cids = [a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    retain(
        "correction-rollback-final",
        {
            "proposals": proposals,
            "aggregate": totals,
            "effects": effects(cids),
            "stale_approval_status": stale.status_code,
        },
    )
    historical = next(p for p in proposals if p["id"] == old["id"])
    assert historical["state"] == "superseded"
    assert all(
        historical[k] == old[k] for k in ("digest", "patch", "allocation_manifest")
    )
    assert all(Decimal(r["allocated"]) == 100 for r in totals)
    assert effects(cids) == {"jobs": 0, "ledger_effects": 0}


def test_reject_vs_prepare_releases_without_jobs():
    number = "GR-" + uuid4().hex
    a, b = case(number), case(number)
    response = prepare(a)
    assert response.status_code == 201
    p = response.json()["proposal"]
    approver = TestClient(wb.app)
    auth = session(approver, "approver")

    def reject():
        return approver.post(
            PREFIX + f"/proposals/{p['id']}/approvals",
            headers=headers(auth, 1, "proposal"),
            json={
                "intent_digest": p["digest"],
                "confirmation": "reject",
                "reason": "Release disposable reservation",
            },
        )

    replies = race(
        "reject-vs-prepare", [reject, lambda: prepare(b)], case_id=a[0]["id"]
    )
    assert replies[0].status_code == 201, replies[0].text
    assert replies[1].status_code in (201, 422), replies[1].text
    if replies[1].status_code == 422:
        assert prepare(b).status_code == 201
    cids = [a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    retain(
        "reject-vs-prepare-final",
        {"proposals": proposals, "aggregate": totals, "effects": effects(cids)},
    )
    assert next(x for x in proposals if x["id"] == p["id"])["state"] == "rejected"
    assert all(Decimal(r["allocated"]) == 100 for r in totals)
    assert effects(cids) == {"jobs": 0, "ledger_effects": 0}


def test_concurrent_approval_vs_expiry_cannot_double_consume(monkeypatch):
    """Real HTTP/PG race with real target GET paused AFTER expiry check.

    Short TTL is a construction-time disposable fixture, not frozen-history edit.
    Expected invariant remains RED if production permits double consumption.
    """
    import httpx

    number = "GR-" + uuid4().hex
    a, b = case(number), case(number)
    original_delta = wb.timedelta

    def fixture_delta(*args, **kwargs):
        return (
            timedelta(seconds=2)
            if kwargs == {"minutes": 15}
            else original_delta(*args, **kwargs)
        )

    with monkeypatch.context() as fixture_patch:
        fixture_patch.setattr(wb, "timedelta", fixture_delta)
        response = prepare(a)
    assert response.status_code == 201, response.text
    p = response.json()["proposal"]
    approver = TestClient(wb.app)
    auth = session(approver, "approver")
    fetched, release = threading.Event(), threading.Event()
    original_get = httpx.get

    def approval():
        return approver.post(
            PREFIX + f"/proposals/{p['id']}/approvals",
            headers=headers(auth, 1, "proposal"),
            json={"intent_digest": p["digest"], "confirmation": "approve"},
        )

    # FastAPI runs sync handlers on AnyIO threads; use target id rather than thread name.
    def real_target_barrier(url, **kwargs):
        result = original_get(url, **kwargs)
        if str(url).endswith("/" + a[0]["target_id"]):
            fetched.set()
            assert release.wait(15), "APPROVAL_RELEASE_TIMEOUT"
        return result

    monkeypatch.setattr(httpx, "get", real_target_barrier)
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="approval-expiry"
    ) as pool:
        future = pool.submit(approval)
        try:
            assert fetched.wait(10), "APPROVAL_DID_NOT_REACH_REAL_TARGET_GET"
            with psycopg.connect(wb.DSN, autocommit=True) as observer:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if observer.execute(
                        "SELECT clock_timestamp()>%s::timestamptz", (p["expires"],)
                    ).fetchone()[0]:
                        break
                else:
                    pytest.fail("EXPIRY_NOT_OBSERVED")
                # Approval transaction remains open, holding the first case row.
                active = observer.execute(
                    "SELECT pid,xact_start,query FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction' AND query LIKE 'SELECT body FROM proposals%'"
                ).fetchall()
                assert active, "OPEN_APPROVAL_TRANSACTION_NOT_PROVEN"
            competing = prepare(b)
        finally:
            release.set()
        approved = future.result(timeout=10)
    cids = [a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    retain(
        "approval-vs-expiry-final",
        {
            "fixture": "two-second TTL at original construction; no history mutations; real target GET barrier",
            "open_approval_sessions": active,
            "approval_status": approved.status_code,
            "competing_prepare_status": competing.status_code,
            "proposals": proposals,
            "aggregate": totals,
            "effects": effects(cids),
        },
    )
    assert approved.status_code == 412, approved.text
    assert competing.status_code == 201, competing.text
    assert effects(cids) == {"jobs": 0, "ledger_effects": 0}
    assert stored([a[0]["id"]])[0] == p, "EXPIRED_FAILURE_MUTATED_FROZEN_PROPOSAL"
    assert all(Decimal(r["allocated"]) <= Decimal(r["available"]) for r in totals), (
        "DOUBLE_CONSUMPTION_APPROVAL_CROSSED_PENDING_EXPIRY"
    )


def wait_expiry(p):
    with psycopg.connect(wb.DSN, autocommit=True) as observer:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if observer.execute(
                "SELECT clock_timestamp()>%s::timestamptz", (p["expires"],)
            ).fetchone()[0]:
                return
    pytest.fail("EXPIRY_NOT_OBSERVED")


def short_prepare(monkeypatch, item):
    original = wb.timedelta
    with monkeypatch.context() as patch:
        patch.setattr(
            wb,
            "timedelta",
            lambda *a, **k: (
                timedelta(seconds=2) if k == {"minutes": 15} else original(*a, **k)
            ),
        )
        response = prepare(item)
    assert response.status_code == 201, response.text
    return response.json()["proposal"]


@pytest.mark.parametrize("alias", [False, True])
def test_approval_protection_precedes_expiry_blocks_contender_and_exact_replays(
    monkeypatch, alias
):
    number = "GR-" + uuid4().hex
    a, b = case(number, alias=alias), case(number, xlsx=True)
    approver = TestClient(wb.app)
    auth = session(approver, "approver")
    p = short_prepare(monkeypatch, a)
    request_headers = headers(auth, 1, "proposal")
    body = {"intent_digest": p["digest"], "confirmation": "approve"}
    url = PREFIX + f"/proposals/{p['id']}/approvals"
    protected, release = threading.Event(), threading.Event()
    original_save = wb.save_proposal
    holder = []

    def protected_write(db, proposal):
        if proposal["id"] == p["id"] and proposal["state"] == "approved":
            holder.append(db.execute("SELECT pg_backend_pid()").fetchone()[0])
            protected.set()
            assert release.wait(15), "PROMOTION_RELEASE_TIMEOUT"
        return original_save(db, proposal)

    monkeypatch.setattr(wb, "save_proposal", protected_write)
    proof = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(
            lambda: approver.post(url, headers=request_headers, json=body)
        )
        try:
            assert protected.wait(10), "APPROVAL_NOT_PROTECTED_BEFORE_EXPIRY"
            wait_expiry(p)
            contender = pool.submit(lambda: prepare(b))
            with psycopg.connect(wb.DSN, autocommit=True) as observer:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    proof = observer.execute(
                        "SELECT a.pid,a.wait_event,pg_blocking_pids(a.pid),a.query,l.locktype,l.granted FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid WHERE a.datname=current_database() AND a.wait_event_type='Lock' AND l.locktype='advisory' AND NOT l.granted AND %s=ANY(pg_blocking_pids(a.pid))",
                        (holder[0],),
                    ).fetchall()
                    if proof or contender.done():
                        break
            assert proof and not contender.done(), (
                "RECEIPT_PROMOTION_SQL_BLOCK_NOT_PROVEN"
            )
        finally:
            release.set()
        approved, competing = approval.result(timeout=10), contender.result(timeout=10)
    assert approved.status_code == 201, approved.text
    assert competing.status_code == 422, competing.text
    assert "UNALLOCATED" in competing.text
    replay = approver.post(url, headers=request_headers, json=body)
    assert replay.status_code == 201 and replay.json() == approved.json()
    assert prepare(b).status_code == 422, "APPROVED_EXPIRED_RESERVATION_RELEASED"
    current = stored([a[0]["id"]])[0]
    assert all(
        current[k] == p[k]
        for k in ("digest", "patch", "allocation_manifest", "expires")
    )
    cids = [a[0]["id"], b[0]["id"]]
    proposals, totals = aggregate(cids)
    assert all(Decimal(r["allocated"]) <= 100 for r in totals)
    assert effects(cids) == {"jobs": 1, "ledger_effects": 0}
    retain(
        "promotion-before-expiry-alias-" + str(alias),
        {
            "holder_pid": holder,
            "sql_waiters": proof,
            "approval": approved.json(),
            "competing_status": competing.status_code,
            "expired_replay": replay.json(),
            "proposals": proposals,
            "aggregate": totals,
            "effects": effects(cids),
        },
    )


def test_expired_prepare_exact_replay_cannot_authorize_new_approval(monkeypatch):
    a = case("GR-" + uuid4().hex)
    approver = TestClient(wb.app)
    auth = session(approver, "approver")
    c, client, reviewer = a
    h = headers(reviewer, 1)
    body = {
        "mode": "partial",
        "quantity": "100",
        "reason": "Expired exact replay",
        "evidence_digest": c["source_digest"],
    }
    url = PREFIX + f"/cases/{c['id']}/decisions"
    original = wb.timedelta
    with monkeypatch.context() as patch:
        patch.setattr(
            wb,
            "timedelta",
            lambda *a, **k: (
                timedelta(seconds=2) if k == {"minutes": 15} else original(*a, **k)
            ),
        )
        prepared = client.post(url, headers=h, json=body)
    assert prepared.status_code == 201
    p = prepared.json()["proposal"]
    wait_expiry(p)
    replay = client.post(url, headers=h, json=body)
    assert replay.status_code == 201 and replay.json() == prepared.json()
    denied = approver.post(
        PREFIX + f"/proposals/{p['id']}/approvals",
        headers=headers(auth, 1, "proposal"),
        json={"intent_digest": p["digest"], "confirmation": "approve"},
    )
    assert denied.status_code == 412, denied.text
    assert stored([c["id"]])[0] == p
    assert effects([c["id"]]) == {"jobs": 0, "ledger_effects": 0}
    retain(
        "expired-prepare-exact-replay",
        {
            "proposal": p,
            "approval_status": denied.status_code,
            "effects": effects([c["id"]]),
        },
    )


def test_target_stale_receipt_approval_has_no_job():
    a = case("GR-" + uuid4().hex)
    prepared = prepare(a)
    assert prepared.status_code == 201
    p = prepared.json()["proposal"]
    # Independent target changes, not the proposal or its signed frozen intent.
    with psycopg.connect(target.DSN) as db:
        record = db.execute(
            "SELECT body FROM payables WHERE id=%s", (p["target"],)
        ).fetchone()[0]
        record["revision"] += 1
        db.execute(
            "UPDATE payables SET body=%s WHERE id=%s", (Jsonb(record), p["target"])
        )
    approver = TestClient(wb.app)
    auth = session(approver, "approver")
    denied = approver.post(
        PREFIX + f"/proposals/{p['id']}/approvals",
        headers=headers(auth, 1, "proposal"),
        json={"intent_digest": p["digest"], "confirmation": "approve"},
    )
    assert denied.status_code == 412 and "TARGET_STALE" in denied.text
    assert stored([a[0]["id"]])[0] == p
    assert effects([a[0]["id"]]) == {"jobs": 0, "ledger_effects": 0}
    retain(
        "target-stale-no-job",
        {"status": denied.status_code, "effects": effects([a[0]["id"]])},
    )


def test_frozen_selection_valid_after_other_reservation_released():
    number = "GR-" + uuid4().hex
    a, b = case(number), case(number, alias=True)
    first, second = prepare(a, "40"), prepare(b, "60")
    assert first.status_code == second.status_code == 201
    prior, p = first.json()["proposal"], second.json()["proposal"]
    approver = TestClient(wb.app)
    auth = session(approver, "approver")
    rejected = approver.post(
        PREFIX + f"/proposals/{prior['id']}/approvals",
        headers=headers(auth, 1, "proposal"),
        json={
            "intent_digest": prior["digest"],
            "confirmation": "reject",
            "reason": "Release earlier reservation",
        },
    )
    assert rejected.status_code == 201
    approved = approver.post(
        PREFIX + f"/proposals/{p['id']}/approvals",
        headers=headers(auth, 1, "proposal"),
        json={"intent_digest": p["digest"], "confirmation": "approve"},
    )
    assert approved.status_code == 201, approved.text
    current = stored([b[0]["id"]])[0]
    assert all(current[k] == p[k] for k in ("digest", "allocation_manifest", "patch"))
    assert p["allocation_manifest"]["rows"][0]["prior_allocated"] == "40"
    cids = [a[0]["id"], b[0]["id"]]
    assert effects(cids) == {"jobs": 1, "ledger_effects": 0}
    retain(
        "frozen-selection-after-release",
        {"proposals": stored(cids), "effects": effects(cids)},
    )
