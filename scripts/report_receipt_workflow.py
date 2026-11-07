"""Read retained isolated proofs, never change records or the live browser profile."""

import base64
import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/documents/receipt-workflow"


def query(database, sql):
    result = subprocess.run(
        [
            "docker",
            "exec",
            "folio-postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-At",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main():
    backend = json.loads((ROOT / "evidence/documents/finish/latest.json").read_text())
    browser = json.loads(
        (ROOT / "evidence/documents/finish-browser/latest.json").read_text()
    )
    assert backend["exit_code"] == browser["exit_code"] == 0
    suite = ElementTree.parse(Path(backend["evidence"]) / "suite.xml").getroot()
    cases = list(suite.iter("testcase"))
    counts = {
        "total": len(cases),
        "skipped": sum(t.find("skipped") is not None for t in cases),
        "failed": sum(
            t.find("failure") is not None or t.find("error") is not None for t in cases
        ),
    }
    counts["passed"] = counts["total"] - counts["skipped"] - counts["failed"]
    rows = query(
        backend["databases"]["workbench"],
        "SELECT coalesce(jsonb_agg(body),'[]'::jsonb) FROM cases WHERE body->>'state'='verified' AND jsonb_array_length(body->'receipt_manifest'->'rows')=2",
    )
    assert len(rows) == 1
    case = rows[0]
    proposals = query(
        backend["databases"]["workbench"],
        f"SELECT jsonb_agg(body) FROM proposals WHERE case_id='{case['id']}' AND body->>'gate'='applied'",
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert (
        sum(Fraction(r["allocated"]) for r in proposal["allocation_manifest"]["rows"])
        == 100
    )
    for row in proposal["allocation_manifest"]["rows"]:
        for evidence in row["evidence"]:
            source = next(
                s for s in case["sources"] if s["id"] == evidence["source_id"]
            )
            page = next(p for p in source["pages"] if p["number"] == evidence["page"])
            assert (
                page["text"][evidence["start"] : evidence["end"]] == evidence["quote"]
            )
            assert source["sha256"] == evidence["source_sha256"]
    operations = query(
        backend["databases"]["target"],
        f"SELECT jsonb_agg(body) FROM operations WHERE operation_key='{proposal['operation_key']}'",
    )
    assert len(operations) == 1
    receipt = operations[0]
    assert receipt["before_revision"] == 7 and receipt["after_revision"] == 8
    assert receipt["after_snapshot"]["gross_amount"] == "5184.00"
    assert proposal["retained_balance"] == "1036.80"
    for field in [
        "invoice_face_amount",
        "supplier",
        "invoice_key",
        "bank_fingerprint",
        "due_date",
    ]:
        assert receipt["before_snapshot"][field] == receipt["after_snapshot"][field]
    files = [
        "folio/allocations.py",
        "folio/receipts.py",
        "folio/provenance.py",
        "folio/workbench.py",
        "web/src/main.tsx",
        "web/tests/receipts.spec.ts",
        "web/tests/northstar.spec.ts",
        "tests/test_receipt_allocations.py",
        "tests/test_receipt_lifecycle.py",
        "tests/test_receipt_identity_conflict.py",
        "tests/test_proof.py",
        "scripts/isolated_browser.py",
        "scripts/report_receipt_workflow.py",
        "docs/ADR-003-RECEIPT-ALLOCATIONS.md",
    ]
    originals = json.loads((ROOT / "contracts/documents-contracts.json").read_text())
    assert len(originals["acceptance_tests"]) == 20
    report = {
        "scope": "ADR-003 finite source-linked receipt and missing-information workflow; not full release",
        "backend": {**backend, "counts": counts},
        "browser": browser,
        "covered_acceptance_subsets": [
            "DOC-T-ARITH",
            "DOC-T-PROVENANCE",
            "DOC-T-INTAKE",
            "DOC-T-FLOW",
            "DOC-T-STALE",
            "DOC-T-AUTH",
            "DOC-T-STATES",
        ],
        "screens_changed": [
            "import",
            "document",
            "reconciliation",
            "decision",
            "changes",
        ],
        "original_acceptance_ids": [t["id"] for t in originals["acceptance_tests"]],
        "full_original_pass_ids": [],
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files
        },
        "live_profile": "Parent-owned supervisor unchanged; coordinated parent restart required to load API changes. No existing account/guide/record resets.",
        "remaining": [
            "Independent review",
            "Receipt identity conflicts stay hold/escalate; selection of conflicting receiving-record versions is not implemented",
            "One labelled receipt line per source page; arbitrary multi-row receipt layouts remain unsupported",
            "Receipt allocation concurrency and full policy/legacy-history populations remain release gates",
            "Real model quality unpassed; backend two real-model skips and browser one skip; no model started",
            "All complete original DOC-T criteria and broader 14-screen state/UX populations remain open",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    inputs = query(
        backend["databases"]["workbench"],
        f"SELECT jsonb_agg(jsonb_build_object('id',id,'sha256',sha256,'base64',encode(raw,'base64'))) FROM originals WHERE case_id='{case['id']}'",
    )
    input_directory = OUT / "inputs"
    input_directory.mkdir(exist_ok=True)
    input_manifest = []
    for original in inputs:
        raw = base64.b64decode(original["base64"])
        assert hashlib.sha256(raw).hexdigest() == original["sha256"]
        source = next(s for s in case["sources"] if s["id"] == original["id"])
        name = original["id"] + "-" + Path(source["name"]).name
        (input_directory / name).write_bytes(raw)
        input_manifest.append(
            {
                "file": name,
                "sha256": original["sha256"],
                "kind": source["kind"],
                "authorship": source["authorship"],
            }
        )
    report["actual_input_manifest"] = input_manifest
    for name, body in [
        ("manifest.json", report),
        ("case-readback.json", case),
        ("proposal-readback.json", proposal),
        ("independent-target-receipt.json", receipt),
    ]:
        (OUT / name).write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(OUT / "manifest.json"),
                "backend_counts": counts,
                "case_id": case["id"],
                "operation_key": proposal["operation_key"],
                "one_effect": len(operations),
                "target_revision": receipt["after_revision"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
