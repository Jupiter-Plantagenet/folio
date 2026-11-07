"""Round-6 DOC-T-RECOVERY drills on a DISPOSABLE stack (9101/9201), never the live stack.

Re-runs the round-5 schedules (scripts/round5_recovery.py) against the round-6 worker:
same-key automatic recovery after a kill before PATCH or a target outage, DB-outage
survival with backoff, plus the unchanged API / export / exhaustion schedules.
Kill primitive: psutil.Process.kill() (TerminateProcess). The harness plays the supervisor
for component death (restart ~1 s later), but NOT for the DB outage: the worker must
survive on its own.

Register targets (DOC-T-RECOVERY): detected <= 60 s, resolvable single failure
recovered <= 120 s, never a duplicate effect, max 5 retries then explicit escalation.

  python scripts/round6_recovery.py            # all scenarios
Results: evidence/documents/recovery/round6-<stamp>/results.json (no secrets).
"""

import json
import sys
import time
from pathlib import Path

import httpx
from psycopg import sql
from round5_recovery import (
    PROTECTED,
    api_kill,
    exhaustion,
    export_kill,
    target_effects,
    target_outage,
    wait_terminal,
    worker_kill,
)
from round5_stack import (
    API,
    TARGET,
    Client,
    Stack,
    free,
    prepare_and_approve,
    provision,
    seed_case,
)

ROOT = Path(__file__).resolve().parents[1]
DETECT_S, RECOVER_S = 60, 120


def heartbeat(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def db_outage(stack, beat):
    """20 s workbench DB outage while the worker runs and one approved job is in flight."""
    from folio.database_security import admin

    name = stack.state["names"]["workbench"]
    assert name.startswith("folio_finish_wb_r5"), name  # disposable only
    stack.start("worker", key="worker")
    rev, appr = Client("reviewer"), Client("approver")
    seeded = seed_case(stack)
    proposal, a = prepare_and_approve(rev, appr, seeded)
    assert a.status_code == 201, a.text
    t0 = time.monotonic()
    admin(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(name)).as_string())
    admin(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{name}' AND pid<>pg_backend_pid()")
    samples = []
    while time.monotonic() - t0 < 20:
        try:
            r = httpx.get(f"http://127.0.0.1:{API}/health", timeout=3)
            health = (r.status_code, r.json().get("status"), r.json().get("alarms"))
        except (httpx.HTTPError, ValueError) as e:
            health = (type(e).__name__, None, None)
        samples.append({"t": round(time.monotonic() - t0, 1), "worker_alive": stack.procs["worker"].poll() is None,
                        "worker_heartbeat": (heartbeat(beat) or {}).get("state"), "api_health": health})
        time.sleep(2)
    admin(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(name)).as_string())
    t_up = time.monotonic()
    api_ok = worker_ok = None
    while time.monotonic() - t_up < RECOVER_S and (api_ok is None or worker_ok is None):
        try:
            if api_ok is None and httpx.get(f"http://127.0.0.1:{API}/health", timeout=2).status_code == 200:
                api_ok = time.monotonic() - t_up
        except httpx.HTTPError:
            pass
        if worker_ok is None and (heartbeat(beat) or {}).get("state") == "ok":
            worker_ok = time.monotonic() - t_up
        time.sleep(0.25)
    survived = stack.procs["worker"].poll() is None
    outcome = wait_terminal(stack, proposal["id"], None, t_up, timeout=RECOVER_S)
    # A second job approved after the outage proves dispatch fully works again.
    seeded2 = seed_case(stack)
    proposal2, a2 = prepare_and_approve(rev, appr, seeded2)
    after_outage = wait_terminal(stack, proposal2["id"], None, time.monotonic(), timeout=60)
    stack.kill("worker")
    log = (stack.logdir / "worker.log").read_text(errors="replace")
    return {"scenario": "workbench_db_outage_20s_worker_running", "outage_s": round(t_up - t0, 1),
            "during": samples, "worker_survived_without_restart": survived,
            "worker_backoff_lines": sum('"database_unavailable"' in x for x in log.splitlines()),
            "api_healthy_after_db_return_s": api_ok, "worker_heartbeat_ok_after_db_return_s": worker_ok,
            "inflight_job": outcome, "inflight_effects": target_effects(stack, seeded["target"], proposal["operation_key"]),
            "post_outage_job": after_outage,
            "post_outage_effects": target_effects(stack, seeded2["target"], proposal2["operation_key"])}


def verdict(s):
    """Per-scenario checks against the register (None = not applicable)."""
    job = s.get("job") or s.get("inflight_job") or {}
    after = s.get("after") or s.get("inflight_effects") or {}
    out = {
        "no_duplicate_effect": (after.get("operations_for_key", 0) <= 1
                                and after.get("revision", 7) - 7 <= 1) if after else None,
        "protected_unchanged": after.get("protected") == PROTECTED if after.get("protected") else None,
        "detected_le_60s": (job["detected_s"] <= DETECT_S) if job.get("detected_s") is not None else None,
        "resolved_le_120s": (job["resolved_s"] <= RECOVER_S) if job.get("resolved_s") is not None else None,
    }
    if s["scenario"].startswith(("worker_sigkill", "target_outage", "workbench_db_outage")):
        out["recovered_automatically"] = job.get("state") == "completed" and job.get("outcome") == "matched" \
            and after.get("operations_for_key") == 1
    return out


def main():
    for port in (API, TARGET):
        if not free(port):
            raise SystemExit(f"PORT_OCCUPIED:{port}; nothing stopped")
    state = provision()
    out = ROOT / "evidence/documents/recovery" / ("round6-" + state["stamp"])
    stack = Stack(state, out / "logs")
    beat = str(out / "worker.heartbeat.json")
    stack.env = {**stack.env, "FOLIO_WORKER_HEARTBEAT": beat}
    results = {"stamp": state["stamp"], "ports": {"api": API, "target": TARGET},
               "kill_primitive": "psutil.Process.kill() = TerminateProcess (SIGKILL-equivalent on Windows)",
               "targets": {"detect_s": DETECT_S, "recover_s": RECOVER_S}, "scenarios": []}

    def save():
        (out / "results.json").write_text(json.dumps(results, indent=2, default=str))

    def add(s):
        s["verdict"] = verdict(s)
        results["scenarios"].append(s)
        print(json.dumps({"scenario": s["scenario"], "verdict": s["verdict"],
                          "job": s.get("job") or s.get("inflight_job")}, default=str), flush=True)
        save()

    try:
        stack.start("target")
        stack.start("api")
        assert stack.wait_http(TARGET) and stack.wait_http(API)
        rev, appr = Client("reviewer"), Client("approver")
        seeded = seed_case(stack)
        proposal, a = prepare_and_approve(rev, appr, seeded)
        stack.start("worker", key="worker")
        base = wait_terminal(stack, proposal["id"], None, time.monotonic(), 60)
        stack.kill("worker")
        results["baseline"] = {"job": base, "after": target_effects(stack, seeded["target"], proposal["operation_key"])}
        save()
        for mode in ("before_fence", "before_patch", "after_patch"):
            add(worker_kill(stack, rev, appr, mode))
        add(api_kill(stack, rev))
        for mode in ("after_stage", "before_ref"):
            add(export_kill(stack, rev, seeded["case"], mode))
        add(target_outage(stack, rev, appr))
        add(db_outage(stack, beat))
        rev, appr = Client("reviewer"), Client("approver")
        add(exhaustion(stack, rev, appr))
        results["health_at_end"] = httpx.get(f"http://127.0.0.1:{API}/health").json()
        with stack.target_reader() as db:
            results["global_target_check"] = dict(zip(
                ("operations", "distinct_keys", "records_revision_ne_opcount"),
                db.execute(
                    "SELECT count(*), count(DISTINCT operation_key), "
                    "(SELECT count(*) FROM payables WHERE (body->>'revision')::int - 7 <> "
                    " (SELECT count(*) FROM operations o WHERE o.target_id=payables.id)) FROM operations"
                ).fetchone()))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
