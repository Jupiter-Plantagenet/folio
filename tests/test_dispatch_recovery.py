"""Round 6 DOC-T-RECOVERY: automatic, never-double-applied dispatch recovery.

A worker that dies before PATCH, or a target outage, used to end `blocked_not_applied`
and need re-approval. Now the next lease (after fencing old attempts and reading the
target primary) revalidates authority and retries the SAME operation key, renewing an
expired grant explicitly (prior JTI + strictly greater fence). Exactly one effect.
"""

import json
import os
from uuid import uuid4

import httpx
import psycopg
import pytest
from authority_fixture import attestation
from fastapi.testclient import TestClient
from test_revocation import approved_case

from folio.identity import service_header

DEAD_TARGET = "http://127.0.0.1:18777"  # nothing listens here


class Killed(BaseException):
    """Simulated process death: escapes every `except Exception` / httpx handler."""


def target_db():
    from folio.target import DSN

    return psycopg.connect(DSN)


def expire_lease(job_id):
    from folio.workbench import connect

    with connect() as db:
        db.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job_id,),
        )


def expire_grant(op):
    with target_db() as db:
        db.execute(
            "UPDATE authority SET expires=clock_timestamp()-interval '1 second' WHERE operation_key=%s",
            (op,),
        )


def state(job_id, p, case):
    from folio.workbench import connect

    with connect() as db:
        job = db.execute(
            "SELECT state,result,fence,tries FROM jobs WHERE id=%s", (job_id,)
        ).fetchone()
        proposal = db.execute("SELECT body FROM proposals WHERE id=%s", (p["id"],)).fetchone()[0]
        c = db.execute("SELECT body FROM cases WHERE id=%s", (case["id"],)).fetchone()[0]
    with target_db() as db:
        ops = db.execute(
            "SELECT count(*) FROM operations WHERE operation_key=%s", (p["operation_key"],)
        ).fetchone()[0]
        payable = db.execute(
            "SELECT body FROM payables WHERE id=%s", (case["target_id"],)
        ).fetchone()[0]
        grant = db.execute(
            "SELECT grant_body FROM authority WHERE operation_key=%s", (p["operation_key"],)
        ).fetchone()
    return dict(
        job=job, proposal=proposal, case=c, ops=ops, payable=payable,
        grant=grant[0] if grant else None,
    )


def die_on(monkeypatch, method, path_suffix):
    """Kill the worker the moment it is about to send `method` to `path_suffix`."""
    original = getattr(httpx.Client, method)

    def wrapped(self, url, *args, **kwargs):
        if str(url).endswith(path_suffix):
            monkeypatch.setattr(httpx.Client, method, original)
            raise Killed(path_suffix)
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, method, wrapped)


def retain(name, value):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "evidence/documents/recovery/round6-unit"
    root.mkdir(parents=True, exist_ok=True)
    (root / (name + ".json")).write_text(json.dumps(value, indent=2, default=str))


# ------------------------------------------------------------------ target contract


def test_target_renews_only_explicitly_after_expiry_with_greater_fence_and_never_twice():
    from folio.target import app, intent_hash, migrate, seed

    migrate()
    client = TestClient(app)
    wid, rid, op = "11111111-1111-4111-8111-111111111111", str(uuid4()), str(uuid4())
    seed(wid, rid)
    p = f"/api/v1/workspaces/{wid}"
    broker = service_header("broker")
    patch = dict(
        recognized_quantity="100", net_amount="4800.00", tax_amount="384.00",
        gross_amount="5184.00", status="partially_matched",
    )
    d = intent_hash(wid, rid, 7, patch)

    def fence(n):
        return client.post(
            p + "/operation-fences", headers=broker,
            json=dict(operation_key=op, intent_digest=d, monotonic_fence=n),
        )

    def grant(n, renew=None):
        body = dict(
            operation_key=op, intent_digest=d, target_id=rid, expected_revision=7,
            target_fence=n, exact_patch=patch, approval=attestation(wid, op, d, rid),
        )
        if renew:
            body["renew_prior_jti"] = renew
        return client.post(p + "/operation-grants", headers=broker, json=body)

    def mutate(jti, n):
        return client.patch(
            p + "/payables/" + rid,
            headers={**service_header("dispatcher"), "Idempotency-Key": op, "If-Match": '"payable-v7"'},
            json=dict(intent_digest=d, grant_jti=jti, fence=n, exact_patch=patch),
        )

    assert fence(41).status_code == 200
    g1 = grant(41).json()
    assert "generation" not in g1  # first-generation shape unchanged
    assert fence(42).status_code == 200
    # Without an explicit renewal request the old generation is replayed (unchanged).
    assert grant(42).json()["jti"] == g1["jti"]
    assert grant(42, renew=str(uuid4())).json()["detail"] == "RENEWAL_PRIOR_JTI_MISMATCH"
    assert grant(42, renew=g1["jti"]).json()["detail"] == "PRIOR_GRANT_UNEXPIRED"
    expire_grant(op)
    g2 = grant(42, renew=g1["jti"])
    assert g2.status_code == 200, g2.text
    g2 = g2.json()
    assert g2["generation"] == 2 and g2["prior_jti"] == g1["jti"] and g2["fence"] == 42
    assert g2["history"] == [
        {"jti": g1["jti"], "fence": 41, "sealed_at": g1["sealed_at"], "generation": 1}
    ]
    # The fenced-out old generation can never write.
    assert mutate(g1["jti"], 41).status_code == 409
    assert mutate(g1["jti"], 42).status_code in (403, 409)
    first = mutate(g2["jti"], 42)
    assert first.status_code == 200 and first.json()["replayed"] is False
    assert mutate(g2["jti"], 42).json()["replayed"] is True
    # Renewal after commit is refused, even with a higher fence and an expired grant.
    assert fence(43).status_code == 200
    expire_grant(op)
    assert grant(43, renew=g2["jti"]).json()["detail"] == "OPERATION_ALREADY_COMMITTED"
    # A renewal must carry a fence strictly greater than the prior grant's.
    with target_db() as db:
        assert db.execute(
            "SELECT count(*) FROM operations WHERE operation_key=%s", (op,)
        ).fetchone()[0] == 1
        assert db.execute("SELECT body->>'revision' FROM payables WHERE id=%s", (rid,)).fetchone()[0] == "8"


def test_renewal_with_unchanged_fence_is_refused():
    from folio.target import app, intent_hash, migrate, seed

    migrate()
    client = TestClient(app)
    wid, rid, op = "11111111-1111-4111-8111-111111111111", str(uuid4()), str(uuid4())
    seed(wid, rid)
    p = f"/api/v1/workspaces/{wid}"
    broker = service_header("broker")
    patch = dict(
        recognized_quantity="100", net_amount="4800.00", tax_amount="384.00",
        gross_amount="5184.00", status="partially_matched",
    )
    d = intent_hash(wid, rid, 7, patch)
    client.post(p + "/operation-fences", headers=broker,
                json=dict(operation_key=op, intent_digest=d, monotonic_fence=41))
    body = dict(operation_key=op, intent_digest=d, target_id=rid, expected_revision=7,
                target_fence=41, exact_patch=patch, approval=attestation(wid, op, d, rid))
    g1 = client.post(p + "/operation-grants", headers=broker, json=body).json()
    expire_grant(op)
    r = client.post(p + "/operation-grants", headers=broker, json={**body, "renew_prior_jti": g1["jti"]})
    assert r.status_code == 409 and r.json()["detail"] == "RENEWAL_FENCE_NOT_GREATER"


# ------------------------------------------------------------------ worker schedules


def test_worker_killed_before_fence_recovers_automatically_with_one_effect(monkeypatch):
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    target = os.environ["FOLIO_TARGET_URL"]
    die_on(monkeypatch, "post", "/operation-fences")
    with pytest.raises(Killed):
        execute(claim(a["job_id"]), target)
    mid = state(a["job_id"], p, case)
    assert mid["proposal"]["gate"] == "seal_pending" and mid["ops"] == 0
    expire_lease(a["job_id"])
    result = execute(claim(a["job_id"]), target)
    after = state(a["job_id"], p, case)
    retain("killed-before-fence", {"result": result, **after})
    assert result["outcome"] == "matched"
    assert after["job"][0] == "completed" and after["case"]["state"] == "verified"
    assert after["ops"] == 1 and after["payable"]["revision"] == 8
    assert after["proposal"]["dispatch_retries"][0]["fence"] == after["job"][2] == 42
    assert after["grant"]["fence"] == 42 and "generation" not in after["grant"]


def test_worker_killed_after_seal_before_patch_defers_then_renews_with_one_effect(monkeypatch):
    from folio.workbench import connect
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    target = os.environ["FOLIO_TARGET_URL"]
    die_on(monkeypatch, "patch", "/payables/" + case["target_id"])
    with pytest.raises(Killed):
        execute(claim(a["job_id"]), target)
    mid = state(a["job_id"], p, case)
    assert mid["proposal"]["gate"] == "sealed" and mid["grant"]["fence"] == 41 and mid["ops"] == 0
    first_jti = mid["grant"]["jti"]
    # Re-lease while the prior grant is still unexpired: no write, rescheduled to expiry.
    expire_lease(a["job_id"])
    deferred = execute(claim(a["job_id"]), target)
    assert deferred["outcome"] == "retry_scheduled"
    assert deferred["reason"] == "PRIOR_GRANT_UNEXPIRED"
    with connect() as db:
        row = db.execute(
            "SELECT state, lease_until > clock_timestamp() + interval '20 seconds' FROM jobs WHERE id=%s",
            (a["job_id"],),
        ).fetchone()
    assert row == ("uncertain", True)
    assert claim(a["job_id"]) is None  # not claimable before the grant can be renewed
    assert state(a["job_id"], p, case)["ops"] == 0
    # Grant expires; the next lease renews it (prior JTI, greater fence) and applies once.
    expire_grant(p["operation_key"])
    expire_lease(a["job_id"])
    result = execute(claim(a["job_id"]), target)
    after = state(a["job_id"], p, case)
    retain("killed-after-seal-before-patch", {"deferred": deferred, "result": result, **after})
    assert result["outcome"] == "matched"
    assert after["job"][0] == "completed" and after["job"][3] == 3
    assert after["ops"] == 1 and after["payable"]["revision"] == 8
    assert after["grant"]["generation"] == 2 and after["grant"]["prior_jti"] == first_jti
    assert after["grant"]["fence"] == after["job"][2] == 43


def test_target_outage_recovers_on_next_lease_with_one_effect():
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    target = os.environ["FOLIO_TARGET_URL"]
    down = execute(claim(a["job_id"]), DEAD_TARGET)
    assert down["outcome"] == "unavailable"
    mid = state(a["job_id"], p, case)
    assert mid["job"][0] == "uncertain" and mid["case"]["state"] == "uncertain" and mid["ops"] == 0
    expire_lease(a["job_id"])
    result = execute(claim(a["job_id"]), target)
    after = state(a["job_id"], p, case)
    retain("target-outage", {"down": down, "result": result, **after})
    assert result["outcome"] == "matched"
    assert after["job"][0] == "completed" and after["ops"] == 1
    assert after["payable"]["revision"] == 8


def test_retry_after_outage_reblocks_when_authority_changed():
    from folio.workbench import connect
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    target = os.environ["FOLIO_TARGET_URL"]
    execute(claim(a["job_id"]), DEAD_TARGET)
    with connect() as db:
        db.execute("UPDATE users SET active=false WHERE username='approver'")
    try:
        expire_lease(a["job_id"])
        result = execute(claim(a["job_id"]), target)
    finally:
        with connect() as db:
            db.execute("UPDATE users SET active=true WHERE username='approver'")
    after = state(a["job_id"], p, case)
    assert result["outcome"] == "blocked_not_applied"
    assert result["reason"] == "STALE_APPROVAL_OR_AUTHORITY"
    assert after["job"][0] == "blocked" and after["ops"] == 0
    assert after["proposal"]["gate"] == "reconciled_absent"


def test_retry_after_outage_refused_by_target_change_blocks_without_effect():
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    target = os.environ["FOLIO_TARGET_URL"]
    execute(claim(a["job_id"]), DEAD_TARGET)
    with target_db() as db:  # an out-of-band target edit while dispatch was uncertain
        db.execute(
            "UPDATE payables SET body=jsonb_set(body,'{revision}','9') WHERE id=%s",
            (case["target_id"],),
        )
    expire_lease(a["job_id"])
    result = execute(claim(a["job_id"]), target)
    after = state(a["job_id"], p, case)
    assert result["outcome"] == "blocked_not_applied"
    assert result["reason"] == "TARGET_REFUSED_412"
    assert after["job"][0] == "blocked" and after["ops"] == 0


# ------------------------------------------------------------------ loop resilience


def test_worker_loop_survives_a_real_database_outage_with_backoff(monkeypatch, tmp_path):
    from folio import workbench as w
    from folio import worker

    beat = tmp_path / "heartbeat.json"
    monkeypatch.setenv("FOLIO_WORKER_HEARTBEAT", str(beat))
    real = w.DSN
    # Port 1 refuses connections: psycopg raises a genuine OperationalError.
    monkeypatch.setattr(w, "DSN", "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=2")
    sleeps = []
    states = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        states.append(json.loads(beat.read_text())["state"])
        if len(sleeps) == 3:
            monkeypatch.setattr(w, "DSN", real)  # the database comes back

    worker.loop(os.environ["FOLIO_TARGET_URL"], iterations=5, sleep=fake_sleep)
    assert states[:3] == ["database_unavailable"] * 3
    assert sleeps[0] < sleeps[1] < sleeps[2]  # jittered exponential backoff
    assert json.loads(beat.read_text())["state"] == "ok"


def test_health_reports_queue_age_verifier_lag_and_alarms():
    from folio import workbench as w
    from folio.workbench import connect

    c, case, p, a, prefix, login = approved_case()
    with connect() as db:
        db.execute(
            "UPDATE proposals SET body=jsonb_set(jsonb_set(body,'{approved_at}',to_jsonb((clock_timestamp()-interval '200 seconds')::text)),'{grant}',jsonb_build_object('sealed_at',(clock_timestamp()-interval '150 seconds')::text)) WHERE id=%s",
            (p["id"],),
        )
    body = TestClient(w.app).get("/health").json()
    assert body["pending_jobs"] >= 1
    assert body["oldest_job_age_s"] >= 199
    assert body["verifier_lag_s"] >= 149
    assert "JOB_AGE_GT_120S" in body["alarms"] and "VERIFIER_LAG_GT_120S" in body["alarms"]
    with connect() as db:  # leave nothing claimable behind for other tests
        db.execute("UPDATE jobs SET state='blocked' WHERE id=%s", (a["job_id"],))


def test_loop_drains_dispatch_before_slow_analysis(monkeypatch):
    """Round-6 load finding: model runs must not starve approval-TTL-bound dispatch."""
    from folio import worker

    jobs = [{"id": f"job-{i}"} for i in range(25)]
    order = []
    monkeypatch.setattr(worker, "expire_originals", lambda: 0)
    monkeypatch.setattr(worker, "claim", lambda: jobs.pop(0) if jobs else None)
    monkeypatch.setattr(worker, "execute", lambda j, target: order.append(j["id"]))
    monkeypatch.setattr(worker, "analyze_one", lambda: order.append("analysis") or {"status": "done"})
    worker.loop("http://unused", iterations=2, sleep=lambda s: None)
    assert order[:20] == [f"job-{i}" for i in range(20)]  # bounded batch first
    assert order[20] == "analysis"
    assert order[21:26] == [f"job-{i}" for i in range(20, 25)] and order[26] == "analysis"
