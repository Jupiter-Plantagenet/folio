"""Live before-grant and before-mutation process-death schedules, separate proof ports."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from test_revocation import approved_case


@pytest.mark.parametrize("barrier_route", ["operation-fences", "operation-grants"])
def test_precommit_crash_reconciles_absence_into_actionable_fenced_block(barrier_route):
    import psycopg

    from folio.target import DSN
    from folio.workbench import connect

    root = Path(__file__).resolve().parents[1]
    c, case, p, a, prefix, login = approved_case()
    reached = threading.Event()
    release = threading.Event()
    observations = []
    target = os.environ["FOLIO_TARGET_URL"]

    class Proxy(BaseHTTPRequestHandler):
        def forward(self):
            raw = self.rfile.read(int(self.headers.get("content-length", "0")))
            r = httpx.request(
                self.command,
                target + self.path,
                headers={
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ["host", "content-length", "connection"]
                },
                content=raw,
            )
            observations.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "status": r.status_code,
                    "body": r.json(),
                }
            )
            if self.path.endswith("/" + barrier_route) and r.status_code == 200:
                reached.set()
                release.wait(20)
                self.close_connection = True
                return
            self.send_response(r.status_code)
            self.end_headers()
            self.wfile.write(r.content)

        do_POST = forward
        do_PATCH = forward
        do_GET = forward

        def log_message(self, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 18292), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    evidence = root / "evidence/documents/recovery" / barrier_route
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / "worker.log").open("w") as log:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "folio.worker",
                "--job",
                a["job_id"],
                "--target",
                "http://127.0.0.1:18292",
            ],
            cwd=root,
            stdout=log,
            stderr=log,
        )
        try:
            assert reached.wait(15)
            if os.name == "nt":
                killed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(worker.pid)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                (evidence / "kill.txt").write_text(killed.stdout)
            else:
                worker.kill()  # SIGKILL: no cleanup handlers run
                (evidence / "kill.txt").write_text(f"SIGKILL {worker.pid}")
            worker.wait(timeout=5)
        finally:
            release.set()
            proxy.shutdown()
            proxy.server_close()
            if worker.poll() is None and os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(worker.pid)],
                    capture_output=True,
                )
            elif worker.poll() is None:
                worker.kill()
    with connect() as db:
        db.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (a["job_id"],),
        )
    restart = subprocess.run(
        [sys.executable, "-m", "folio.worker", "--job", a["job_id"], "--recover"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    (evidence / "restart.log").write_text(restart.stdout + restart.stderr)
    assert restart.returncode == 0, restart.stderr
    with connect() as db:
        job = db.execute(
            "SELECT state,result,fence FROM jobs WHERE id=%s", (a["job_id"],)
        ).fetchone()
        after = db.execute(
            "SELECT body FROM cases WHERE id=%s", (case["id"],)
        ).fetchone()[0]
        proposal = db.execute(
            "SELECT body FROM proposals WHERE id=%s", (p["id"],)
        ).fetchone()[0]
    with psycopg.connect(DSN) as db:
        count = db.execute(
            "SELECT count(*) FROM operations WHERE operation_key=%s",
            (p["operation_key"],),
        ).fetchone()[0]
        payable = db.execute(
            "SELECT body FROM payables WHERE id=%s", (case["target_id"],)
        ).fetchone()[0]
    (evidence / "result.json").write_text(
        json.dumps(
            {
                "job": job,
                "case": after,
                "proposal": proposal,
                "target_operation_count": count,
                "payable": payable,
                "http": observations,
            },
            indent=2,
        )
    )
    assert job[0] == "blocked", job
    assert job[1]["outcome"] == "blocked_not_applied"
    assert job[1]["required_action"] == "review_and_create_new_approved_proposal"
    assert job[1]["reconciliation"]["authoritative"] is True
    assert job[1]["reconciliation"]["fence"] == job[2]
    assert proposal["gate"] == "reconciled_absent" and after["state"] == "needs_review"
    assert count == 0 and payable["revision"] == 7
    assert not any(x["method"] == "PATCH" for x in observations)


def test_fifth_unavailable_attempt_publishes_exhaustion_and_keeps_gate_closed():
    from folio.workbench import connect
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    with connect() as db:
        db.execute("UPDATE jobs SET tries=4 WHERE id=%s", (a["job_id"],))
    j = claim(a["job_id"])
    execute(j, target="http://127.0.0.1:18777")
    with connect() as db:
        row = db.execute(
            "SELECT state,result FROM jobs WHERE id=%s", (a["job_id"],)
        ).fetchone()
        case_after = db.execute(
            "SELECT body FROM cases WHERE id=%s", (case["id"],)
        ).fetchone()[0]
    assert row[0] == "exhausted", row
    assert row[1]["required_action"] == "investigate_target_outcome_before_retry"
    assert case_after["state"] == "uncertain"
    assert claim(a["job_id"]) is None


def test_last_attempt_process_death_transitions_to_visible_exhaustion_on_next_claim():
    from folio.workbench import connect
    from folio.worker import claim

    c, case, p, a, prefix, login = approved_case()
    with connect() as db:
        db.execute(
            "UPDATE jobs SET tries=5,state='leased',lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (a["job_id"],),
        )
    assert claim(a["job_id"]) is None
    with connect() as db:
        row = db.execute(
            "SELECT state,result FROM jobs WHERE id=%s", (a["job_id"],)
        ).fetchone()
    assert row[0] == "exhausted", row
    assert row[1]["required_action"] == "investigate_target_outcome_before_retry"
