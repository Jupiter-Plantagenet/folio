"""Durable dispatch and bounded local analysis; no browser credentials or model approval authority."""

import argparse
import json
import os
import random
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from folio.dbpool import PoolTimeout
from folio.identity import service_header
from folio.verifier import check
from folio.workbench import (
    event,
    load_case,
    load_proposal,
    pooled,
    save_case,
    save_proposal,
)

# Target grant lifetime (folio/target.py: expires = sealed + 60 s). A renewal is only
# requested after the prior grant has expired, as the blueprint's renewal rule requires.
GRANT_TTL_SECONDS = 60
# Dispatch jobs drained per loop iteration before one analysis run (see loop()).
DISPATCH_BATCH = 20
MAINTENANCE_LOCK_KEY = "folio-paired-backup-admission-v1"


def maintenance_requested():
    from folio.maintenance_gate import marker_present

    return marker_present()


def claim_admission(db):
    """Serialize claims against the supervisor's session-level backup barrier."""
    db.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
               (MAINTENANCE_LOCK_KEY,))
    return not maintenance_requested()


@contextmanager
def connect():
    # Bounded, reused, reset-between-uses connections (round 6): the loop used to open
    # about 3 new TCP connections per second, which contributed to port exhaustion.
    from folio.restore_mode import require_writable

    with pooled() as db:
        require_writable(db)
        yield db


def claim(job_id=None):
    with connect() as db:
        if not claim_admission(db):
            return None
        stranded = db.execute(
            "SELECT j.id,p.case_id FROM jobs j JOIN proposals p ON p.id=j.proposal WHERE j.state IN ('leased','uncertain') AND j.tries>=5 AND j.lease_until<clock_timestamp() ORDER BY j.id LIMIT 20 FOR UPDATE OF j SKIP LOCKED"
        ).fetchall()
        for identifier, cid in stranded:
            diagnosis = {
                "outcome": "unknown",
                "reason": "LAST_ATTEMPT_DIED_OR_UNPUBLISHED",
                "required_action": "investigate_target_outcome_before_retry",
                "write_requests": "unknown; inspect authoritative receipt",
            }
            db.execute(
                "UPDATE jobs SET state='exhausted',result=%s WHERE id=%s",
                (Jsonb(diagnosis), identifier),
            )
            event(
                db,
                str(cid),
                "DOC-recovery-action-required",
                "system",
                {"job": str(identifier), **diagnosis},
            )
        sql = "SELECT id,workspace,proposal,fence FROM jobs WHERE (state='queued' OR (state IN ('leased','uncertain') AND lease_until<clock_timestamp())) AND tries<5"
        args = []
        if job_id:
            sql += " AND id=%s"
            args = [job_id]
        row = db.execute(
            sql + " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1", args
        ).fetchone()
        if not row:
            return None
        attempt = str(uuid4())
        fence = row[3] + 1
        db.execute(
            "UPDATE jobs SET state='leased',attempt=%s,fence=%s,tries=tries+1,lease_until=clock_timestamp()+interval '30 seconds' WHERE id=%s",
            (attempt, fence, row[0]),
        )
        return {
            "id": str(row[0]),
            "workspace": str(row[1]),
            "proposal": str(row[2]),
            "attempt": attempt,
            "fence": fence,
        }


def assert_lease(db, j):
    valid = db.execute(
        "SELECT 1 FROM jobs WHERE id=%s AND attempt=%s AND fence=%s AND lease_until>clock_timestamp() AND state='leased' FOR UPDATE",
        (j["id"], j["attempt"], j["fence"]),
    ).fetchone()
    if not valid:
        raise RuntimeError("FENCE_OR_LEASE_LOST")


def publish(j, result):
    with connect() as db:
        p_ref = db.execute(
            "SELECT case_id FROM proposals WHERE id=%s", (j["proposal"],)
        ).fetchone()
        c = load_case(db, j["workspace"], str(p_ref[0]))
        p = load_proposal(db, j["workspace"], j["proposal"])
        assert_lease(db, j)
        matched = result["outcome"] == "matched"
        tries = db.execute("SELECT tries FROM jobs WHERE id=%s", (j["id"],)).fetchone()[
            0
        ]
        exhausted = not matched and tries >= 5
        if exhausted:
            result = {
                **result,
                "retry_exhausted": True,
                "required_action": "investigate_target_outcome_before_retry",
            }
            event(
                db,
                c["id"],
                "DOC-recovery-action-required",
                "system",
                {
                    "job": j["id"],
                    "reason": "RETRY_EXHAUSTED",
                    "required_action": result["required_action"],
                },
            )
        c["state"] = "verified" if matched else "uncertain"
        c["verification"] = result
        if matched:
            p["state"] = "executed"
            p["gate"] = "applied"
        else:
            p["gate"] = "uncertain"
        save_case(db, c)
        save_proposal(db, p)
        db.execute(
            "UPDATE jobs SET state=%s,result=%s WHERE id=%s",
            (
                "completed" if matched else ("exhausted" if exhausted else "uncertain"),
                Jsonb(result),
                j["id"],
            ),
        )
        event(
            db,
            c["id"],
            "DOC-verification-recorded",
            "system",
            {
                "outcome": result["outcome"],
                "job": j["id"],
                "attempt": j["attempt"],
                "fence": j["fence"],
            },
        )


def block_absent(j, observed, reason="PRIMARY_ABSENCE_AND_PRIOR_GRANT_FENCED"):
    # A primary read under the same operation lock observed no effect AFTER advancing
    # the target fence. Every previous writer/grant is now fenced out; no new write
    # is authorized here. An operator must review and obtain a fresh signed approval.
    with connect() as db:
        ref = db.execute(
            "SELECT case_id FROM proposals WHERE id=%s", (j["proposal"],)
        ).fetchone()
        c = load_case(db, j["workspace"], str(ref[0]))
        p = load_proposal(db, j["workspace"], j["proposal"])
        assert_lease(db, j)
        result = {
            **observed,
            "outcome": "blocked_not_applied",
            "required_action": "review_and_create_new_approved_proposal",
            "reason": reason,
            "write_requests": 0,
        }
        p["state"] = "blocked"
        p["gate"] = "reconciled_absent"
        p["reconciliation"] = result
        c["state"] = "needs_review"
        c["version"] += 1
        c["verification"] = result
        save_proposal(db, p)
        save_case(db, c)
        db.execute(
            "UPDATE jobs SET state='blocked',result=%s WHERE id=%s",
            (Jsonb(result), j["id"]),
        )
        event(
            db,
            c["id"],
            "DOC-recovery-action-required",
            "system",
            {
                "job": j["id"],
                "operation": p["operation_key"],
                "fence": j["fence"],
                "required_action": result["required_action"],
            },
        )
    return result


def authority_problem(db, c, p):
    """None when the current approval/authority is exactly what was approved."""
    approver = db.execute(
        "SELECT active,roles FROM users WHERE username=%s",
        (p.get("approved_by", ""),),
    ).fetchone()
    if (
        p["state"] != "approved"
        or p["prepared_by"] == p.get("approved_by")
        or not approver
        or not approver[0]
        or "approver" not in approver[1]
        or p["case_version"] != c["version"]
        or p["source_digest"] != c["source_digest"]
        or datetime.fromisoformat(p["expires"]) <= datetime.now(timezone.utc)
    ):
        return "STALE_APPROVAL_OR_AUTHORITY"
    return None


def fenced_absence(result, j):
    """Authoritative primary absence observed AFTER this attempt's fence was registered."""
    return (
        result["outcome"] == "not_applied"
        and result["reconciliation"].get("fence") == j["fence"]
    )


def reauthorize_retry(j, observed):
    """Revalidate authority under case->proposal->job locks before a same-key retry.

    Safe because:
    1. this attempt's strictly higher fence is registered at the target, so every older
       in-flight grant or PATCH is rejected there;
    2. the target primary, under the operation lock, showed no receipt for the key;
    3. the retry reuses the SAME operation key, which the target serializes and replays,
       so a second effect is impossible;
    4. approval, approver, case version, source digest and expiry are re-checked here
       exactly as for the first dispatch. Any change blocks instead.
    """
    with connect() as db:
        ref = db.execute(
            "SELECT case_id FROM proposals WHERE id=%s", (j["proposal"],)
        ).fetchone()
        c = load_case(db, j["workspace"], str(ref[0]))
        p = load_proposal(db, j["workspace"], j["proposal"])
        assert_lease(db, j)
        problem = authority_problem(db, c, p)
        if problem:
            return problem
        p["gate"] = "seal_pending"
        p.setdefault("dispatch_retries", []).append(
            {
                "fence": j["fence"],
                "attempt": j["attempt"],
                "at": datetime.now(timezone.utc).isoformat(),
                "observed": "primary_absent_prior_fenced",
                "prior_grant_jti": (observed["reconciliation"].get("grant") or {}).get(
                    "jti"
                ),
            }
        )
        c["state"] = "seal_pending"
        save_proposal(db, p)
        save_case(db, c)
        event(
            db,
            c["id"],
            "DOC-dispatch-changed",
            "system",
            {
                "gate": "seal_pending",
                "fence": j["fence"],
                "retry": "same_operation_key_after_fenced_primary_absence",
            },
        )
    return None


def defer_until(j, observed, not_before, reason):
    """Keep the gate closed and reschedule this job; no effect was requested."""
    with connect() as db:
        assert_lease(db, j)
        result = {
            "outcome": "retry_scheduled",
            "reason": reason,
            "not_before": not_before.isoformat(),
            "reconciliation": observed.get("reconciliation"),
            "write_requests": 0,
        }
        db.execute(
            "UPDATE jobs SET state='uncertain',result=%s,lease_until=greatest(clock_timestamp(),%s) WHERE id=%s",
            (Jsonb(result), not_before, j["id"]),
        )
    return result


def execute(
    j, target=os.getenv("FOLIO_TARGET_URL", "http://127.0.0.1:8201"), recover=False
):
    """Dispatch one leased job.

    ``recover=True`` (operator ``--recover``) is strictly read-only: it reconciles and
    never writes. The automatic path additionally retries the SAME operation key after a
    fenced authoritative absence (worker killed before PATCH, target outage); see
    ``reauthorize_retry``.
    """
    # Claim and durable authorization checks use case->proposal->job lock ordering.
    with connect() as db:
        ref = db.execute(
            "SELECT case_id FROM proposals WHERE id=%s", (j["proposal"],)
        ).fetchone()
        c = load_case(db, j["workspace"], str(ref[0]))
        p = load_proposal(db, j["workspace"], j["proposal"])
        assert_lease(db, j)
        old_gate = p.get("gate", "prepared")
        if old_gate == "prepared" and not recover:
            if authority_problem(db, c, p):
                db.execute(
                    "UPDATE jobs SET state='blocked',result=%s WHERE id=%s",
                    (
                        Jsonb({"reason": "STALE_APPROVAL_OR_AUTHORITY", "effects": 0}),
                        j["id"],
                    ),
                )
                event(
                    db,
                    c["id"],
                    "DOC-job-changed",
                    "system",
                    {"state": "blocked", "reason": "STALE_APPROVAL_OR_AUTHORITY"},
                )
                return {"outcome": "blocked", "effects": 0}
            p["gate"] = "seal_pending"
            c["state"] = "seal_pending"
            save_proposal(db, p)
            save_case(db, c)
            event(
                db,
                c["id"],
                "DOC-dispatch-changed",
                "system",
                {"gate": "seal_pending", "fence": j["fence"]},
            )
    prefix = f"{target}/api/v1/workspaces/{j['workspace']}"
    broker = service_header("broker")
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                prefix + "/operation-fences",
                headers=broker,
                json={
                    "operation_key": p["operation_key"],
                    "intent_digest": p["target_digest"],
                    "monotonic_fence": j["fence"],
                },
            )
            r.raise_for_status()
            # Unknown/previous seal: never blind PATCH on a restart. First reconcile
            # against the target primary under the newly registered fence.
            renew = None
            if recover or old_gate != "prepared":
                result = check(p, target)
                if not fenced_absence(result, j):
                    publish(j, result)
                    print(json.dumps(result), flush=True)
                    return result
                if recover:
                    result = block_absent(j, result)
                    print(json.dumps(result), flush=True)
                    return result
                prior = result["reconciliation"].get("grant")
                if prior and result["reconciliation"].get("grant_unexpired"):
                    sealed = datetime.fromisoformat(prior["sealed_at"])
                    not_before = sealed + timedelta(seconds=GRANT_TTL_SECONDS + 1)
                    result = defer_until(j, result, not_before, "PRIOR_GRANT_UNEXPIRED")
                    print(json.dumps(result), flush=True)
                    return result
                problem = reauthorize_retry(j, result)
                if problem:
                    result = block_absent(j, result, problem)
                    print(json.dumps(result), flush=True)
                    return result
                renew = prior["jti"] if prior else None
            r = client.post(
                prefix + "/operation-grants",
                headers=broker,
                json={
                    "operation_key": p["operation_key"],
                    "intent_digest": p["target_digest"],
                    "target_id": p["target"],
                    "expected_revision": p["target_revision"],
                    "target_fence": j["fence"],
                    "exact_patch": p["patch"],
                    "approval": p.get("approval_attestation"),
                    **({"renew_prior_jti": renew} if renew else {}),
                },
            )
            r.raise_for_status()
            grant = r.json()
            with connect() as db:
                c = load_case(db, j["workspace"], p["case_id"])
                current = load_proposal(db, j["workspace"], p["id"])
                assert_lease(db, j)
                current["gate"] = "sealed"
                current["grant"] = grant
                save_proposal(db, current)
                c["state"] = "sealed"
                save_case(db, c)
            r = client.patch(
                prefix + "/payables/" + p["target"],
                headers={
                    **service_header("dispatcher"),
                    "Idempotency-Key": p["operation_key"],
                    "If-Match": f'"payable-v{p["target_revision"]}"',
                },
                json={
                    "intent_digest": p["target_digest"],
                    "grant_jti": grant["jti"],
                    "fence": j["fence"],
                    "exact_patch": p["patch"],
                },
            )
            r.raise_for_status()
        result = check(p, target)
    except httpx.HTTPStatusError as e:
        if not 400 <= e.response.status_code < 500:
            result = {
                "outcome": "unavailable",
                "reason": "HTTP_" + str(e.response.status_code),
                "write_requests": "unknown; inspect target receipt",
            }
        else:
            # A definitive target refusal (stale fence, changed record, expired grant,
            # invalid intent). Reconcile on the primary instead of retrying blindly.
            try:
                detail = e.response.json().get("detail")
            except ValueError:
                detail = None
            result = check(p, target)
            if fenced_absence(result, j):
                result = block_absent(
                    j,
                    {**result, "target_refusal": detail},
                    "TARGET_REFUSED_" + str(e.response.status_code),
                )
                print(json.dumps(result), flush=True)
                return result
    except httpx.HTTPError as e:
        result = {
            "outcome": "unavailable",
            "reason": type(e).__name__,
            "write_requests": "unknown; inspect target receipt",
        }
    publish(j, result)
    print(json.dumps(result), flush=True)
    return result


def _complete_analysis(db, rid, cid, run, result):
    run.update(
        status=result["status"],
        result=result,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    db.execute(
        "UPDATE analysis_runs SET status=%s,body=%s WHERE id=%s",
        (run["status"], Jsonb(run), rid),
    )
    event(
        db,
        str(cid),
        "DOC-analysis-completed",
        "system",
        {"run_id": str(rid), "status": run["status"], "target_writes": 0},
    )
    return run


def analyze_one(run_id=None):
    from folio.agent import run_bounded
    from folio.analysis_authority import requester_problem

    with connect() as db:
        if not claim_admission(db):
            return None
        args = []
        query = "SELECT id,workspace,case_id,body FROM analysis_runs WHERE (status='queued' OR (status='running' AND lease_until<clock_timestamp())) AND tries<3"
        if run_id:
            query += " AND id=%s"
            args.append(run_id)
        row = db.execute(
            query + " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1", args
        ).fetchone()
        if not row:
            return None
        rid, wid, cid, run = row
        if run.get("prepare_proposal"):
            problem = requester_problem(db, run["requested_by"])
            if problem:
                return _complete_analysis(
                    db,
                    rid,
                    cid,
                    run,
                    {
                        "status": "blocked",
                        "error": problem,
                        "target_writes": 0,
                        "quality_gate_passed": False,
                    },
                )
        attempt = str(uuid4())
        db.execute(
            "UPDATE analysis_runs SET status='running',attempt=%s,lease_until=clock_timestamp()+interval '120 seconds',tries=tries+1 WHERE id=%s",
            (attempt, rid),
        )
    with connect() as db:
        c = load_case(db, str(wid), str(cid))
        run["status"] = "running"
        published = db.execute(
            "UPDATE analysis_runs SET body=%s WHERE id=%s AND attempt=%s "
            "AND status='running' AND lease_until>clock_timestamp() RETURNING id",
            (Jsonb(run), rid, attempt),
        ).fetchone()
        if not published:
            raise RuntimeError("ANALYSIS_LEASE_LOST")

    def lookup():
        response = httpx.get(
            f"{os.getenv('FOLIO_TARGET_URL', 'http://127.0.0.1:8201')}/api/v1/workspaces/{wid}/payables/{c['target_id']}",
            headers=service_header("verifier"),
            timeout=5,
        )
        response.raise_for_status()
        return response.json()

    try:
        result = run_bounded(c, lookup)
    except (httpx.HTTPError, ValueError, RuntimeError) as error:
        result = {
            "status": "blocked",
            "error": type(error).__name__,
            "target_writes": 0,
            "quality_gate_passed": False,
        }
    with connect() as db:
        current = load_case(db, str(wid), str(cid))
        row = db.execute(
            "SELECT attempt,lease_until>clock_timestamp() FROM analysis_runs WHERE id=%s FOR UPDATE",
            (rid,),
        ).fetchone()
        if str(row[0]) != attempt or not row[1]:
            raise RuntimeError("ANALYSIS_LEASE_LOST")
        if (
            current["version"] != run["case_version"]
            or current["source_digest"] != run["source_digest"]
        ):
            result = {
                **result,
                "status": "superseded",
                "error": "SOURCE_CHANGED_DURING_INFERENCE",
            }
        if result["status"] == "recommended" and run.get("prepare_proposal"):
            from fastapi import HTTPException

            from folio.workbench import apply_decision

            problem = requester_problem(db, run["requested_by"])
            if problem:
                result = {
                    **result,
                    "status": "blocked",
                    "error": problem,
                }
            else:
                try:
                    with db.transaction():
                        decision = apply_decision(
                            db,
                            current,
                            {
                                **result["recommendation"],
                                "evidence_digest": run["source_digest"],
                            },
                            run["requested_by"],
                        )
                    result = {
                        **result,
                        "status": "prepared",
                        "prepared_decision": decision,
                        "delegation": "requester-authorized version-bound draft/outcome; independent approval still required for target effects",
                    }
                except HTTPException as error:
                    result = {**result, "status": "blocked", "error": str(error.detail)}
        return _complete_analysis(db, rid, cid, run, result)


def expire_originals():
    from fastapi import HTTPException

    from folio.workbench import delete_original_bytes

    with connect() as db:
        if not claim_admission(db):
            return 0
        rows = db.execute(
            "SELECT DISTINCT o.workspace,o.case_id FROM originals o JOIN cases c ON c.workspace=o.workspace AND c.id=o.case_id WHERE o.raw IS NOT NULL AND o.created_at < clock_timestamp()-interval '90 days' AND coalesce(c.body->'retention'->>'legal_hold','false')!='true' AND c.body->>'state' NOT IN ('seal_pending','sealed','uncertain') ORDER BY o.workspace,o.case_id LIMIT 20"
        ).fetchall()
    deleted = 0
    for workspace, cid in rows:
        with connect() as db:
            row = db.execute(
                "SELECT body FROM cases WHERE workspace=%s AND id=%s FOR UPDATE SKIP LOCKED",
                (workspace, cid),
            ).fetchone()
            if not row or row[0].get("retention", {}).get("legal_hold"):
                continue
            try:
                # A savepoint ensures a refused pending-seal case is left untouched.
                with db.transaction():
                    result = delete_original_bytes(
                        db,
                        row[0],
                        "system",
                        "90-day original retention expired",
                        expired_only=True,
                    )
                    deleted += result["deleted_sources"]
            except HTTPException:
                continue
    return deleted


_last_beat = [0.0, None]


def heartbeat(path, state, failures):
    """Best-effort liveness file read by /health; never fatal (for example disk full).

    Steady "ok" beats are throttled to one write per 5 s; a state change is always written.
    """
    if not path or (
        state == _last_beat[1] == "ok" and time.monotonic() - _last_beat[0] < 5
    ):
        return
    _last_beat[0], _last_beat[1] = time.monotonic(), state
    try:
        with open(path, "w", encoding="utf-8") as out:
            json.dump(
                {
                    "pid": os.getpid(),
                    "at": datetime.now(timezone.utc).isoformat(),
                    "state": state,
                    "consecutive_failures": failures,
                },
                out,
            )
    except OSError:
        pass


def backoff_seconds(failures, cap=30.0):
    """Jittered exponential backoff: about 1, 2, 4, 8, 16, then 30 s."""
    return min(cap, 2.0 ** min(failures - 1, 5)) * random.uniform(0.8, 1.2)


def loop(target, iterations=None, sleep=time.sleep):
    """Worker loop that survives database outages and unexpected errors.

    A DB outage (OperationalError or pool timeout), or any other error in one iteration,
    is logged and retried with jittered backoff. The process does not exit, so a long
    outage can never use up the supervisor's restart budget. Leases and fences keep job
    state safe across a failed iteration; work resumes from durable state.
    """
    beat = os.getenv("FOLIO_WORKER_HEARTBEAT")
    failures = 0
    done = 0
    while iterations is None or done < iterations:
        if maintenance_requested():
            heartbeat(beat, "maintenance_quiesced", 0)
            return
        done += 1
        try:
            expire_originals()
            # Dispatch first (round 6 load finding): each dispatch takes about 1 s and is
            # bound by the 15-min approval TTL, while one model analysis run takes about
            # 6 s. Taking one of each per iteration let approvals queue for 24 min (7
            # expired). Drain a bounded batch of dispatch jobs, then one analysis run.
            dispatched = 0
            while dispatched < DISPATCH_BATCH:
                j = claim()
                if not j:
                    break
                dispatched += 1
                try:
                    execute(j, target)
                except (psycopg.OperationalError, PoolTimeout):
                    raise
                except Exception as e:  # noqa: BLE001 - fenced job state stays safe
                    print(json.dumps({"job": j["id"], "error": str(e)}), flush=True)
                heartbeat(beat, "ok", 0)
            analysed = analyze_one()
            if failures:
                print(
                    json.dumps({"worker": "recovered", "after_failures": failures}),
                    flush=True,
                )
            failures = 0
            heartbeat(beat, "ok", 0)
            if not dispatched and analysed is None:
                sleep(1)
            if maintenance_requested():
                heartbeat(beat, "maintenance_quiesced", 0)
                return
        except Exception as e:  # noqa: BLE001 - log, back off, keep serving
            failures += 1
            delay = backoff_seconds(failures)
            kind = (
                "database_unavailable"
                if isinstance(e, (psycopg.OperationalError, PoolTimeout))
                else "iteration_error"
            )
            print(
                json.dumps(
                    {
                        "worker": kind,
                        "error": type(e).__name__,
                        "consecutive_failures": failures,
                        "backoff_s": round(delay, 2),
                    }
                ),
                flush=True,
            )
            heartbeat(beat, kind, failures)
            sleep(delay)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job")
    parser.add_argument(
        "--target", default=os.getenv("FOLIO_TARGET_URL", "http://127.0.0.1:8201")
    )
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    if args.loop:
        loop(args.target)
    else:
        j = claim(args.job)
        if not j:
            raise SystemExit("NO_CLAIMABLE_JOB")
        execute(j, args.target, args.recover)


if __name__ == "__main__":
    main()
