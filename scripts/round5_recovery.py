"""Round-5 timed SIGKILL recovery drills on a DISPOSABLE stack (9101/9201), never retained.

Scenarios: worker killed before fence / after seal before PATCH / after target commit
(response lost); API killed inside the approval transaction; export worker killed after
stage and after rename-before-publish; target outage; workbench DB outage; 5-retry
exhaustion. The harness plays the supervisor: it restarts a killed component ~1 s later.
Results: evidence/documents/recovery/round5-<stamp>/results.json (no secrets).
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
from psycopg import sql
from round5_stack import (
    API,
    TARGET,
    Client,
    Stack,
    load,
    prepare_and_approve,
    provision,
    seed_case,
)

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "blocked", "exhausted"}


def job_row(stack, proposal):
    with stack.wb() as db:
        return db.execute(
            "SELECT id,state,attempt,fence,tries,result FROM jobs WHERE proposal=%s", (proposal,)
        ).fetchall()


def target_effects(stack, rid, operation_key):
    with stack.target_reader() as db:
        body = db.execute("SELECT body FROM payables WHERE id=%s", (rid,)).fetchone()[0]
        ops = db.execute("SELECT count(*) FROM operations WHERE target_id=%s", (rid,)).fetchone()[0]
        mine = db.execute("SELECT count(*) FROM operations WHERE operation_key=%s", (operation_key,)).fetchone()[0]
    protected = {k: body[k] for k in ("supplier", "invoice_key", "invoice_face_amount", "bank_fingerprint", "due_date", "currency")}
    return {"revision": body["revision"], "operations_on_record": ops, "operations_for_key": mine,
            "gross_amount": body["gross_amount"], "status": body["status"], "protected": protected}


PROTECTED = {"supplier": "Aster Supply Co.", "invoice_key": "INV-1048", "invoice_face_amount": "6220.80",
             "bank_fingerprint": "synthetic-bank-unchanged", "due_date": "2026-11-25", "currency": "USD"}


def wait_terminal(stack, proposal, first_attempt, t_kill, timeout=180):
    detected = None
    while time.monotonic() - t_kill < timeout:
        rows = job_row(stack, proposal)
        if rows:
            _, state, attempt, fence, tries, result = rows[0]
            if detected is None and str(attempt) != str(first_attempt):
                detected = time.monotonic() - t_kill
            if state in TERMINAL:
                return {"state": state, "tries": tries, "fence": fence, "detected_s": detected,
                        "resolved_s": time.monotonic() - t_kill, "outcome": (result or {}).get("outcome"),
                        "required_action": (result or {}).get("required_action")}
        time.sleep(0.25)
    rows = job_row(stack, proposal)
    return {"state": rows[0][1] if rows else None, "timeout": True, "detected_s": detected}


def worker_kill(stack, rev, appr, mode):
    seeded = seed_case(stack)
    proposal, a = prepare_and_approve(rev, appr, seeded)
    assert a.status_code == 201, a.text
    job = job_row(stack, proposal["id"])[0]
    key = f"barrier-{mode}"
    stack.start("barrier", ["worker", mode, str(job[0])], key=key)
    assert stack.wait_log(key, '"barrier"', 60), f"barrier {mode} not reached"
    first_attempt = job_row(stack, proposal["id"])[0][2]
    before = target_effects(stack, seeded["target"], proposal["operation_key"])
    t_kill = time.monotonic()
    stack.kill(key)
    time.sleep(1.0)  # supervisor poll interval
    stack.start("worker", key="worker")
    restart_s = time.monotonic() - t_kill
    outcome = wait_terminal(stack, proposal["id"], first_attempt, t_kill)
    stack.kill("worker")
    after = target_effects(stack, seeded["target"], proposal["operation_key"])
    with stack.wb() as db:
        case_state = db.execute("SELECT body->>'state' FROM cases WHERE id=%s", (seeded["case"],)).fetchone()[0]
    return {"scenario": f"worker_sigkill_{mode}", "restart_after_s": restart_s, "at_kill": before,
            "job": outcome, "after": after, "case_state": case_state,
            "duplicate_write": after["operations_for_key"] > 1 or after["revision"] - 7 > 1,
            "protected_unchanged": after["protected"] == PROTECTED,
            "marked_done_without_effect": outcome.get("state") == "completed" and after["operations_for_key"] != 1}


def api_kill(stack, rev):
    """Kill API inside the approval transaction; retry same Idempotency-Key after restart."""
    stack.kill("api")
    stack.start("barrier", ["api", str(API)], key="api-barrier")
    assert stack.wait_http(API)
    appr = Client("approver")
    seeded = seed_case(stack)
    d = rev.post(f"/cases/{seeded['case']}/decisions",
                 dict(mode="partial", quantity="100", reason="Only confirmed receipt", evidence_digest=seeded["digest"]),
                 if_match=f'"case-v{seeded["version"]}"')
    proposal = d.json()["proposal"]
    key = str(uuid4())
    box = {}

    def send():
        try:
            box["r"] = appr.post(f"/proposals/{proposal['id']}/approvals",
                                 dict(intent_digest=proposal["digest"], confirmation="approve"),
                                 if_match='"proposal-v1"', key=key, timeout=120)
        except httpx.HTTPError as e:
            box["error"] = type(e).__name__

    t = threading.Thread(target=send)
    t.start()
    assert stack.wait_log("api-barrier", '"barrier"', 60)
    jobs_inflight = len(job_row(stack, proposal["id"]))  # uncommitted insert is invisible
    t_kill = time.monotonic()
    stack.kill("api-barrier")
    t.join(30)
    detected = None
    while detected is None:
        try:
            httpx.get(f"http://127.0.0.1:{API}/health", timeout=0.5)
        except httpx.HTTPError:
            detected = time.monotonic() - t_kill
        time.sleep(0.1)
    stack.start("api", key="api")
    assert stack.wait_http(API)
    healthy = time.monotonic() - t_kill
    jobs_after_kill = len(job_row(stack, proposal["id"]))
    retry = appr.post(f"/proposals/{proposal['id']}/approvals",
                      dict(intent_digest=proposal["digest"], confirmation="approve"),
                      if_match='"proposal-v1"', key=key)
    replay = appr.post(f"/proposals/{proposal['id']}/approvals",
                       dict(intent_digest=proposal["digest"], confirmation="approve"),
                       if_match='"proposal-v1"', key=key)
    jobs_after_retry = job_row(stack, proposal["id"])
    stack.start("worker", key="worker")
    outcome = wait_terminal(stack, proposal["id"], None, time.monotonic())
    stack.kill("worker")
    after = target_effects(stack, seeded["target"], proposal["operation_key"])
    return {"scenario": "api_sigkill_inside_approval_transaction", "client_error": box.get("error"),
            "client_status": getattr(box.get("r"), "status_code", None),
            "jobs_visible_during_pause": jobs_inflight, "jobs_after_kill": jobs_after_kill,
            "detected_down_s": detected, "healthy_after_restart_s": healthy,
            "retry_same_key_status": retry.status_code, "replay_same_key_status": replay.status_code,
            "replay_identical_body": retry.json() == replay.json(),
            "jobs_after_retry": len(jobs_after_retry), "dispatch": outcome, "after": after,
            "duplicate_write": after["operations_for_key"] > 1 or after["revision"] - 7 > 1}


def export_kill(stack, rev, case_id, mode):
    v = rev.get(f"/cases/{case_id}").json()["version"]
    r = rev.post(f"/cases/{case_id}/exports", dict(case_version=v, include_originals=False, redaction_profile="audit-v1"))
    assert r.status_code == 202, r.text
    aid = r.json()["artifact_job_id"]
    key = f"export-barrier-{mode}"
    stack.start("barrier", ["export", mode], key=key)
    assert stack.wait_log(key, '"barrier"', 60)
    t_kill = time.monotonic()
    stack.kill(key)
    root = Path(stack.env["FOLIO_EXPORT_ROOT"])
    files_at_kill = sorted(p.name.split(".", 1)[1] for p in root.glob(aid + ".*"))
    detected = resolved = None
    attempts = 0
    while time.monotonic() - t_kill < 180:
        time.sleep(3.0)  # supervisor cadence for the --once export worker
        attempts += 1
        subprocess.run(stack.argv("exportworker"), cwd=ROOT, env=stack.env, capture_output=True)
        with stack.wb() as db:
            state, tries, fence = db.execute("SELECT state,tries,fence FROM export_jobs WHERE id=%s", (aid,)).fetchone()
        if detected is None and tries >= 2:
            detected = time.monotonic() - t_kill
        if state in ("ready", "exhausted"):
            resolved = time.monotonic() - t_kill
            break
    meta = rev.get(f"/artifacts/{aid}").json()
    raw = rev.get(f"/artifacts/{aid}", params={"download": "true"})
    import hashlib

    with stack.wb() as db:
        refs = db.execute("SELECT count(*) FROM export_jobs WHERE id=%s AND state='ready'", (aid,)).fetchone()[0]
    return {"scenario": f"exportworker_sigkill_{mode}", "artifact_job_id": aid, "files_at_kill": files_at_kill,
            "state": state, "tries": tries, "fence": fence, "detected_s": detected, "resolved_s": resolved,
            "supervisor_runs": attempts, "download_status": raw.status_code,
            "download_sha256_matches_db": hashlib.sha256(raw.content).hexdigest() == meta.get("sha256"),
            "ready_rows": refs,
            "files_after": sorted(p.name.split(".", 1)[1] for p in root.glob(aid + ".*"))}


def target_outage(stack, rev, appr):
    seeded = seed_case(stack)
    # Decision preparation itself reads the target (it fails 503 while the target is down),
    # so the outage is injected after approval, before the worker dispatches.
    proposal, a = prepare_and_approve(rev, appr, seeded)
    stack.kill("target")
    t_down = time.monotonic()
    stack.start("worker", key="worker")
    time.sleep(20)
    stack.start("target", key="target")
    assert stack.wait_http(TARGET)
    target_back = time.monotonic() - t_down
    outcome = wait_terminal(stack, proposal["id"], None, t_down, timeout=180)
    stack.kill("worker")
    after = target_effects(stack, seeded["target"], proposal["operation_key"])
    return {"scenario": "target_outage_20s_during_dispatch", "target_restored_after_s": target_back,
            "job": outcome, "after": after, "duplicate_write": after["operations_for_key"] > 1}


def db_outage(stack, rev, appr):
    from folio.database_security import admin

    name = stack.state["names"]["workbench"]
    assert name.startswith("folio_finish_wb_r5")
    stack.start("worker", key="worker")
    t0 = time.monotonic()
    admin(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(name)).as_string())
    admin(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{name}' AND pid<>pg_backend_pid()")
    time.sleep(1)
    try:
        during = httpx.get(f"http://127.0.0.1:{API}/health", timeout=5).status_code
    except httpx.HTTPError as e:
        during = type(e).__name__
    time.sleep(19)
    admin(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(name)).as_string())
    t_up = time.monotonic()
    healthy = None
    while time.monotonic() - t_up < 120:
        try:
            if httpx.get(f"http://127.0.0.1:{API}/health", timeout=2).status_code == 200:
                healthy = time.monotonic() - t_up
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    worker_alive = stack.procs["worker"].poll() is None
    if not worker_alive:  # supervisor restart semantics
        stack.start("worker", key="worker")
    rev2, appr2 = Client("reviewer"), Client("approver")
    seeded = seed_case(stack)
    proposal, a = prepare_and_approve(rev2, appr2, seeded)
    outcome = wait_terminal(stack, proposal["id"], None, time.monotonic(), timeout=120)
    stack.kill("worker")
    return {"scenario": "workbench_db_outage_20s", "api_health_during": during,
            "api_healthy_after_db_return_s": healthy, "worker_survived_outage": worker_alive,
            "post_outage_dispatch": outcome, "outage_s": t_up - t0}


def exhaustion(stack, rev, appr):
    """Five consecutive SIGKILLs of the same job, each after its lease is re-acquired."""
    seeded = seed_case(stack)
    proposal, a = prepare_and_approve(rev, appr, seeded)
    jid = str(job_row(stack, proposal["id"])[0][0])
    kills = []
    for i in range(5):
        t = time.monotonic()
        reached = False
        n = 0
        while not reached and time.monotonic() - t < 90:
            key = f"exhaust-{i}-{n}"
            p = stack.start("barrier", ["worker", "after_patch", jid], key=key)
            while p.poll() is None and not stack.wait_log(key, '"barrier"', 0.5):
                pass
            reached = p.poll() is None
            if reached:
                stack.kill(key)
            else:
                n += 1
                time.sleep(1.0)  # lease not yet expired: NO_CLAIMABLE_JOB, retry like a supervisor
        tries = job_row(stack, proposal["id"])[0][4]
        kills.append({"kill": i + 1, "barrier_reached": reached, "claim_retries": n,
                      "killed_after_s": time.monotonic() - t, "tries_after": tries})
    t_last = time.monotonic()
    stack.start("worker", key="worker")
    outcome = wait_terminal(stack, proposal["id"], None, t_last, timeout=120)
    stack.kill("worker")
    with stack.wb() as db:
        events = db.execute(
            "SELECT kind,body FROM events WHERE case_id=%s AND kind='DOC-recovery-action-required'", (seeded["case"],)
        ).fetchall()
        case_state = db.execute("SELECT body->>'state' FROM cases WHERE id=%s", (seeded["case"],)).fetchone()[0]
        proposal_gate = db.execute("SELECT body->>'gate' FROM proposals WHERE id=%s", (proposal["id"],)).fetchone()[0]
    after = target_effects(stack, seeded["target"], proposal["operation_key"])
    return {"scenario": "five_sigkills_then_escalation", "kills": kills, "job": outcome,
            "escalation_events": [e[1] for e in events], "case_state": case_state, "proposal_gate": proposal_gate,
            "after": after,
            # The target DID apply once; the workbench must not claim 'completed/verified' without a check.
            "marked_done_without_verification": outcome.get("state") == "completed"}


def main():
    for port in (API, TARGET):
        if not __import__("round5_stack").free(port):
            raise SystemExit(f"PORT_OCCUPIED:{port}; nothing stopped")
    state = load(sys.argv[1]) if len(sys.argv) > 1 else provision()
    only = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    out = ROOT / "evidence/documents/recovery" / ("round5-" + state["stamp"])
    stack = Stack(state, out / "logs")
    results = {"stamp": state["stamp"], "databases": state["names"], "ports": {"api": API, "target": TARGET},
               "kill_primitive": "psutil.Process.kill() = TerminateProcess (SIGKILL-equivalent on Windows)",
               "scenarios": []}

    def save():
        (out / ("results.json" if not only else f"results-{'-'.join(only)}.json")).write_text(json.dumps(results, indent=2, default=str))

    try:
        stack.start("target")
        stack.start("api")
        assert stack.wait_http(TARGET) and stack.wait_http(API)
        rev, appr = Client("reviewer"), Client("approver")
        # Baseline (no fault) for comparison.
        seeded = seed_case(stack)
        proposal, a = prepare_and_approve(rev, appr, seeded)
        stack.start("worker", key="worker")
        t0 = time.monotonic()
        base = wait_terminal(stack, proposal["id"], None, t0, 60)
        stack.kill("worker")
        results["baseline"] = {"job": base, "after": target_effects(stack, seeded["target"], proposal["operation_key"]),
                               "case": seeded["case"]}
        save()
        if only:
            pass
        for mode in ([] if only else ("before_fence", "before_patch", "after_patch")):
            results["scenarios"].append(worker_kill(stack, rev, appr, mode))
            save()
        if not only:
            results["scenarios"].append(api_kill(stack, rev))
        save()
        for mode in ([] if only else ("after_stage", "before_ref")):
            results["scenarios"].append(export_kill(stack, rev, seeded["case"], mode))
            save()
        if not only or "target_outage" in only:
            results["scenarios"].append(target_outage(stack, rev, appr))
            save()
        if not only or "db_outage" in only:
            results["scenarios"].append(db_outage(stack, rev, appr))
            save()
        rev, appr = Client("reviewer"), Client("approver")
        results["scenarios"].append(exhaustion(stack, rev, appr))
        save()
        results["health"] = httpx.get(f"http://127.0.0.1:{API}/health").json()
        with stack.target_reader() as db:
            results["global_target_check"] = db.execute(
                "SELECT count(*), count(DISTINCT operation_key), "
                "(SELECT count(*) FROM payables WHERE (body->>'revision')::int - 7 <> "
                " (SELECT count(*) FROM operations o WHERE o.target_id=payables.id)) FROM operations"
            ).fetchone()
        with stack.wb() as db:
            results["jobs_by_state"] = db.execute("SELECT state,count(*) FROM jobs GROUP BY state ORDER BY 1").fetchall()
            results["approved_proposals_without_job"] = db.execute(
                "SELECT count(*) FROM proposals p WHERE p.body->>'approved_by' IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.proposal=p.id)").fetchone()[0]
        save()
    finally:
        stack.stop_all()
        save()
    print(json.dumps({"results": str(out / "results.json")}))


if __name__ == "__main__":
    main()
