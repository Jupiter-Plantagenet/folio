"""Real PostgreSQL, HTTP target, response-drop proxy and killed OS worker."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from folio.identity import service_header

ROOT = Path(__file__).resolve().parents[1]
TARGET_URL = os.getenv("FOLIO_TARGET_URL", "http://127.0.0.1:8201")
PROXY_PORT = int(os.getenv("FOLIO_PROOF_PROXY_PORT", "8291"))


def test_commit_response_loss_restart_is_read_only_and_one_effect():
    import psycopg

    from folio.target import DSN as TARGET_DSN
    from folio.target import migrate as target_migrate
    from folio.target import seed
    from folio.workbench import WORKSPACE, app, connect, create_case, migrate

    target_migrate()
    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    from folio.parser import parse_pdf

    sources = [
        {
            "name": path.name,
            "id": str(uuid4()),
            "version": 1,
            "document_role": {
                "invoice.pdf": "invoice",
                "purchase-order.pdf": "purchase_order",
                "receipt.pdf": "receipt",
            }[path.name],
            **parse_pdf(path.read_bytes()),
        }
        for path in sorted((ROOT / "fixtures/public").glob("*.pdf"))
    ]
    case = create_case(sources=sources, target_id=rid)
    # create_case is a low-level business-state fixture. This proof exercises
    # uploaded immutable source authority, so bind its current parser output and
    # field provenance before attempting any receipt-based allocation.
    from folio.provenance import extract_packet
    from folio.workbench import digest, save_case

    extracted = extract_packet(sources)
    case.update(
        extracted["values"],
        field_provenance=extracted["field_provenance"],
        receipt_manifest=extracted["receipt_manifest"],
        missing_fields=extracted["missing"],
        conflicting_fields=extracted["conflicts"],
        invoice_supplier=extracted["invoice_supplier"],
        supplier_check=extracted["supplier_check"],
    )
    case["source_digest"] = digest(
        {"sources": sources, "field_provenance": case["field_provenance"]}
    )
    with connect() as db:
        save_case(db, case)
    with psycopg.connect(TARGET_DSN) as db:
        initial_records = {
            str(r[0]): r[1]
            for r in db.execute(
                "SELECT id,body FROM payables WHERE workspace=%s", (WORKSPACE,)
            )
        }
    client = TestClient(app)
    p = f"/api/v1/workspaces/{WORKSPACE}"

    def login(name):
        s = client.post(
            "/api/v1/auth/login",
            json={"username": name, "password": f"folio-{name}-local"},
        ).json()
        return {
            "Origin": "http://127.0.0.1:5101",
            "X-CSRF-Token": s["csrf"],
            "Idempotency-Key": str(uuid4()),
        }

    h = login("reviewer")
    proposal = client.post(
        p + f"/cases/{case['id']}/decisions",
        headers={**h, "If-Match": '"case-v1"'},
        json=dict(
            mode="partial",
            quantity="100",
            reason="Synthetic receipt confirms 100",
            evidence_digest=case["source_digest"],
        ),
    ).json()["proposal"]
    h = login("approver")
    approval = client.post(
        p + f"/proposals/{proposal['id']}/approvals",
        headers={**h, "If-Match": '"proposal-v1"'},
        json=dict(intent_digest=proposal["digest"], confirmation="approve"),
    ).json()
    job = approval["job_id"]
    op = proposal["operation_key"]
    import pytest

    from folio.worker import claim, execute

    stale = claim(job)
    assert stale["fence"] == 41
    with connect() as db:
        db.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job,),
        )
    # A is paused before seal; DB-clock lease expires. B must obtain fence 42.
    committed = threading.Event()
    release = threading.Event()
    requests = []

    class Proxy(BaseHTTPRequestHandler):
        def forward(self):
            data = self.rfile.read(int(self.headers.get("content-length", "0")))
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "content-length")
            }
            with httpx.Client() as c:
                r = c.request(
                    self.command,
                    TARGET_URL + self.path,
                    headers=headers,
                    content=data,
                )
            requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "status": r.status_code,
                    "body": r.json(),
                }
            )
            if self.command == "PATCH" and r.status_code == 200:
                committed.set()
                release.wait(20)
                self.close_connection = True
                return
            self.send_response(r.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(r.content)

        do_GET = forward
        do_POST = forward
        do_PATCH = forward

        def log_message(self, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    log = ROOT / "evidence/documents/proof/worker-killed.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    def kill_tree(process):
        if os.name == "nt":
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=True,
                capture_output=True,
                text=True,
            )
            (log.parent / "process-kill.txt").write_text(
                killed.stdout, encoding="utf-8"
            )
        else:
            process.kill()
        process.wait(timeout=5)

    with log.open("w") as out:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "folio.worker",
                "--job",
                job,
                "--target",
                f"http://127.0.0.1:{PROXY_PORT}",
            ],
            cwd=ROOT,
            stdout=out,
            stderr=out,
        )
        try:
            assert committed.wait(15), (
                "worker never reached target mutation_committed barrier; see worker-killed.log"
            )
            kill_tree(worker)
        finally:
            release.set()
            proxy.shutdown()
            if worker.poll() is None:
                kill_tree(worker)
    assert worker.returncode != 0
    with pytest.raises(RuntimeError, match="FENCE_OR_LEASE_LOST"):
        execute(stale)
    stale_seal = httpx.post(
        f"{TARGET_URL}/api/v1/workspaces/{WORKSPACE}/operation-grants",
        headers=service_header("broker"),
        json=dict(
            operation_key=op,
            intent_digest=proposal["target_digest"],
            target_id=rid,
            expected_revision=7,
            target_fence=41,
            exact_patch=proposal["patch"],
        ),
    )
    assert stale_seal.status_code == 409
    h = login("reviewer")
    current = client.get(p + f"/cases/{case['id']}").json()
    denied = client.patch(
        p + f"/cases/{case['id']}/fields/invoice",
        headers={**h, "If-Match": f'"case-v{current["version"]}"'},
        json=dict(typed_value="late", reason="Must not pass durable gate"),
    )
    assert denied.status_code == 409
    with connect() as db:
        state = db.execute(
            "SELECT body FROM proposals WHERE id=%s", (proposal["id"],)
        ).fetchone()[0]
        assert state["gate"] == "sealed"
        db.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job,),
        )
    recovery = subprocess.run(
        [sys.executable, "-m", "folio.worker", "--job", job, "--recover"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert recovery.returncode == 0, recovery.stderr + recovery.stdout
    with connect() as db:
        result = db.execute(
            "SELECT state,result,fence FROM jobs WHERE id=%s", (job,)
        ).fetchone()
        assert result[0] == "completed" and result[1]["outcome"] == "matched"
        assert result[2] == 43
        case_after = db.execute(
            "SELECT body FROM cases WHERE id=%s", (case["id"],)
        ).fetchone()[0]
        assert case_after["state"] == "verified"
    with psycopg.connect(TARGET_DSN) as db:
        ops = db.execute(
            "SELECT body FROM operations WHERE workspace=%s AND operation_key=%s",
            (WORKSPACE, op),
        ).fetchall()
        assert (
            len(ops) == 1
            and ops[0][0]["before_revision"] == 7
            and ops[0][0]["after_revision"] == 8
        )
        snap = db.execute(
            "SELECT body FROM payables WHERE workspace=%s AND id=%s", (WORKSPACE, rid)
        ).fetchone()[0]
        assert (
            snap["invoice_face_amount"] == "6220.80"
            and snap["bank_fingerprint"] == "synthetic-bank-unchanged"
        )
        final_records = {
            str(r[0]): r[1]
            for r in db.execute(
                "SELECT id,body FROM payables WHERE workspace=%s", (WORKSPACE,)
            )
        }
        assert {k: v for k, v in initial_records.items() if k != rid} == {
            k: v for k, v in final_records.items() if k != rid
        }
    assert sum(r["method"] == "PATCH" for r in requests) == 1
    assert result[1]["write_requests"] == 0
    assert (
        result[1]["observation"]["read_principal"]
        == __import__("psycopg").conninfo.conninfo_to_dict(
            os.environ["FOLIO_TARGET_READ_DSN"]
        )["user"]
    )
    (log.parent / "before-after-effect-history.json").write_text(
        json.dumps(
            {"before": initial_records, "after": final_records, "receipt": ops[0][0]},
            indent=2,
        )
    )
    for name, value in {
        "raw-http": requests,
        "target-receipts": [o[0] for o in ops],
        "verifier-report": result[1],
        "jobs-attempts-fences": {"job": job, "final_fence": result[2]},
        "schedule": {
            "barrier": "mutation_committed before response flush",
            "worker_exit": worker.returncode,
            "recovery_stdout": recovery.stdout,
            "operation": op,
            "case": case["id"],
        },
    }.items():
        (log.parent / (name + ".json")).write_text(
            json.dumps(value, indent=2), encoding="utf-8"
        )
