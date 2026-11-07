"""Round-5 READ-ONLY external verification of evidence bundles already exported in the
RETAINED workspace. Reads rows through the retained runtime/read-only principals inside
read-only transactions and reads artifact files from the private export root; writes nothing
to the retained databases. Packs (which embed synthetic originals) stay private under
.local/round5/retained-packs/; only verifier reports go to evidence/.
"""

import base64
import json
import subprocess
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from live_supervisor import EXPORT_ROOT  # noqa: E402


def main():
    prof = json.loads((ROOT / ".local/database-profiles.json").read_text())["browser"]
    trust = json.loads((ROOT / ".local/target.json").read_text())
    private = ROOT / ".local/round5/retained-packs"
    private.mkdir(parents=True, exist_ok=True)
    out = ROOT / "evidence/documents/proof/round5-retained-readonly"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    with psycopg.connect(prof["FOLIO_DSN"]) as wb, psycopg.connect(prof["FOLIO_TARGET_READ_DSN"]) as tg:
        wb.execute("SET TRANSACTION READ ONLY")
        tg.execute("SET TRANSACTION READ ONLY")
        rows = wb.execute("SELECT id::text,case_id::text,state,snapshot,snapshot_sha256,artifact_ref,artifact_sha256,artifact_bytes "
                          "FROM export_jobs WHERE state='ready' ORDER BY created_at").fetchall()
        for jid, cid, state, snap, ssha, ref, asha, abytes in rows:
            entry = {"export_job": jid, "case": cid}
            path = Path(EXPORT_ROOT) / ref
            if not path.exists():
                entry["result"] = "artifact file missing"
                results.append(entry)
                continue
            raw = path.read_bytes()
            case = wb.execute("SELECT body FROM cases WHERE id=%s", (cid,)).fetchone()[0]
            pid = (case.get("proposal") or {}).get("id") or (snap.get("proposal") or {}).get("id")
            if not pid:
                entry["result"] = "no proposal in case (not a dispatch case)"
                results.append(entry)
                continue
            prop = wb.execute("SELECT body FROM proposals WHERE id=%s", (pid,)).fetchone()[0]
            jobs = wb.execute("SELECT id::text,state,fence,tries,result FROM jobs WHERE proposal=%s", (pid,)).fetchall()
            originals = wb.execute("SELECT id::text,sha256,raw FROM originals WHERE case_id=%s", (cid,)).fetchall()
            op = tg.execute("SELECT body FROM operations WHERE operation_key=%s", (prop["operation_key"],)).fetchone()
            auth = tg.execute("SELECT digest,fence,grant_body FROM authority WHERE operation_key=%s", (prop["operation_key"],)).fetchone()
            pay = tg.execute("SELECT body FROM payables WHERE id=%s", (prop["target"],)).fetchone()
            nk = tg.execute("SELECT count(*) FROM operations WHERE operation_key=%s", (prop["operation_key"],)).fetchone()[0]
            nr = tg.execute("SELECT count(*) FROM operations WHERE target_id=%s", (prop["target"],)).fetchone()[0]
            if not op:
                entry["result"] = "no target operation for this proposal"
                results.append(entry)
                continue
            pack = {"bundle_b64": base64.b64encode(raw).decode(),
                    "export_job": {"id": jid, "state": state, "snapshot": snap, "snapshot_sha256": ssha,
                                   "artifact_sha256": asha, "artifact_bytes": abytes},
                    "case": case, "proposal": prop,
                    "jobs": [{"id": j[0], "state": j[1], "fence": j[2], "tries": j[3], "result": j[4]} for j in jobs],
                    "originals": [{"id": o[0], "sha256": o[1], "raw_b64": base64.b64encode(o[2] or b"").decode(),
                                   "deleted": o[2] is None} for o in originals],
                    "target_operation": op[0], "target_authority": {"digest": auth[0], "fence": auth[1], "grant_body": auth[2]},
                    "target_payable": pay[0], "operations_for_key": nk, "operations_on_record": nr,
                    "approval_public_key": trust["approval_public_key"]}
            pp = private / f"{jid}.json"
            pp.write_text(json.dumps(pack, default=str))
            rp = out / f"verify-{jid}.json"
            r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/round5_external_verify.py"), "verify", str(pp), str(rp)],
                               capture_output=True, text=True)
            v = json.loads(rp.read_text()) if rp.exists() else {}
            entry.update({"verified": v.get("verified"), "failed_checks": [k for k, x in v.get("checks", {}).items() if not x],
                          "originals_deleted": sum(1 for o in originals if o[2] is None), "exit": r.returncode})
            results.append(entry)
        wb.rollback()
        tg.rollback()
    (out / "summary.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
