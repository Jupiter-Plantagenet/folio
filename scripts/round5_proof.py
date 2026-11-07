"""Round-5 end-to-end proof/provenance run on a DISPOSABLE stack (9101/9201).

Real path: 3 authored fixture PDFs -> /intakes/files (networkless container parser) ->
independent target match -> reviewer decision -> separate approver (Ed25519 attestation) ->
durable worker dispatch -> Northstar PATCH -> independent verifier -> export job -> export
worker -> bundle download. Then collects an evidence pack (DB rows via runtime/read-only
principals, never printed) and runs the external verifier with `python -I` + tamper suite.
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

from round5_stack import API, TARGET, Client, Stack, provision

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = [("invoice.pdf", "invoice"), ("purchase-order.pdf", "purchase_order"), ("receipt.pdf", "receipt")]


def main():
    state = provision()
    out = ROOT / "evidence/documents/proof" / f"round5-{state['stamp']}"
    stack = Stack(state, out / "logs")
    steps = {}
    try:
        stack.start("target")
        stack.start("api")
        assert stack.wait_http(TARGET) and stack.wait_http(API)
        stack.start("worker")
        code = ("import json,uuid;from folio.target import seed;from folio.workbench import WORKSPACE;"
                "rid=str(uuid.uuid4());seed(WORKSPACE,rid);print(rid)")
        rid = subprocess.run([sys.executable, "-c", code], env=stack.env, cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip().splitlines()[-1]
        rev, appr = Client("reviewer"), Client("approver")
        files = [{"name": n, "document_role": r,
                  "base64": base64.b64encode((ROOT / "fixtures/public" / n).read_bytes()).decode()} for n, r in FIXTURES]
        up = rev.post("/intakes/files", {"rights_attestation": "authored-synthetic", "files": files}, timeout=300)
        steps["intake"] = {"status": up.status_code, "accepted": len(up.json().get("accepted", [])),
                           "rejected": up.json().get("rejected")}
        up.raise_for_status()
        cid = up.json()["case_id"]
        case = rev.get(f"/cases/{cid}").json()
        steps["after_intake"] = {"state": case["state"], "missing": case.get("missing_fields"),
                                 "conflicting": case.get("conflicting_fields")}
        m = rev.post(f"/cases/{cid}/target-match", {"selected_target": rid}, if_match=f'"case-v{case["version"]}"')
        steps["target_match"] = m.status_code
        m.raise_for_status()
        case = rev.get(f"/cases/{cid}").json()
        d = rev.post(f"/cases/{cid}/decisions", dict(mode="partial", quantity="100", reason="Receipt GR-208 supports 100",
                                                      evidence_digest=case["source_digest"]),
                     if_match=f'"case-v{case["version"]}"')
        steps["decision"] = {"status": d.status_code, "body": d.json() if d.status_code != 201 else None}
        d.raise_for_status()
        proposal = d.json()["proposal"]
        own = rev.post(f"/proposals/{proposal['id']}/approvals", dict(intent_digest=proposal["digest"], confirmation="approve"),
                       if_match='"proposal-v1"')
        steps["self_approval_denied"] = own.status_code
        a = appr.post(f"/proposals/{proposal['id']}/approvals", dict(intent_digest=proposal["digest"], confirmation="approve"),
                      if_match='"proposal-v1"')
        steps["approval"] = a.status_code
        a.raise_for_status()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            case = rev.get(f"/cases/{cid}").json()
            if case["state"] in ("verified", "uncertain", "needs_review"):
                break
            time.sleep(0.5)
        steps["dispatch_case_state"] = case["state"]
        e = rev.post(f"/cases/{cid}/exports", dict(case_version=case["version"], include_originals=False,
                                                   redaction_profile="audit-v1"))
        steps["export_enqueue"] = e.status_code
        aid = e.json()["artifact_job_id"]
        r = subprocess.run(stack.argv("exportworker"), cwd=ROOT, env=stack.env, capture_output=True, text=True)
        steps["export_worker"] = r.stdout.strip()
        dl = rev.get(f"/artifacts/{aid}", params={"download": "true"})
        steps["download"] = {"status": dl.status_code, "etag": dl.headers.get("etag"), "bytes": len(dl.content)}
        # ---- collect the evidence pack (runtime + read-only principals) ----
        with stack.wb() as db:
            ej = db.execute("SELECT state,snapshot,snapshot_sha256,artifact_sha256,artifact_bytes FROM export_jobs WHERE id=%s",
                            (aid,)).fetchone()
            cbody = db.execute("SELECT body FROM cases WHERE id=%s", (cid,)).fetchone()[0]
            pbody = db.execute("SELECT body FROM proposals WHERE id=%s", (proposal["id"],)).fetchone()[0]
            jobs = db.execute("SELECT id::text,state,fence,tries,result FROM jobs WHERE proposal=%s", (proposal["id"],)).fetchall()
            originals = db.execute("SELECT id::text,sha256,raw FROM originals WHERE case_id=%s", (cid,)).fetchall()
        with stack.target_reader() as db:
            op = db.execute("SELECT body FROM operations WHERE operation_key=%s", (pbody["operation_key"],)).fetchone()[0]
            auth = db.execute("SELECT digest,fence,grant_body FROM authority WHERE operation_key=%s",
                              (pbody["operation_key"],)).fetchone()
            payable = db.execute("SELECT body FROM payables WHERE id=%s", (rid,)).fetchone()[0]
            n_key = db.execute("SELECT count(*) FROM operations WHERE operation_key=%s", (pbody["operation_key"],)).fetchone()[0]
            n_rec = db.execute("SELECT count(*) FROM operations WHERE target_id=%s", (rid,)).fetchone()[0]
        trust = json.loads(Path(state["env"]["FOLIO_TARGET_IDENTITY"]).read_text())
        import hashlib

        pack = {
            "bundle_b64": base64.b64encode(dl.content).decode(),
            "export_job": {"id": aid, "state": ej[0], "snapshot": ej[1], "snapshot_sha256": ej[2],
                           "artifact_sha256": ej[3], "artifact_bytes": ej[4]},
            "case": cbody, "proposal": pbody,
            "jobs": [{"id": j[0], "state": j[1], "fence": j[2], "tries": j[3], "result": j[4]} for j in jobs],
            "originals": [{"id": o[0], "sha256": o[1], "raw_b64": base64.b64encode(o[2]).decode()} for o in originals],
            "target_operation": op, "target_authority": {"digest": auth[0], "fence": auth[1], "grant_body": auth[2]},
            "target_payable": payable, "operations_for_key": n_key, "operations_on_record": n_rec,
            "approval_public_key": trust["approval_public_key"],  # public key only
            "fixture_files": {hashlib.sha256((ROOT / "fixtures/public" / n).read_bytes()).hexdigest(): f"fixtures/public/{n}"
                              for n, _ in FIXTURES},
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "pack.json").write_text(json.dumps(pack, indent=1, default=str))
        (out / "bundle.json").write_bytes(dl.content)
        (out / "steps.json").write_text(json.dumps(steps, indent=2, default=str))
    finally:
        stack.stop_all()
    v = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/round5_external_verify.py"), "verify",
                        str(out / "pack.json"), str(out / "external-verify.json")], capture_output=True, text=True)
    t = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/round5_external_verify.py"), "tamper",
                        str(out / "pack.json"), str(out / "tamper")], capture_output=True, text=True)
    print(json.dumps({"steps": steps, "verify_exit": v.returncode, "verify_stderr": v.stderr[-500:],
                      "tamper_exit": t.returncode, "tamper_stderr": t.stderr[-500:], "out": str(out)}, indent=1, default=str))


if __name__ == "__main__":
    main()
