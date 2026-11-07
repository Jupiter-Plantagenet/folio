"""Generate an evidence index from actual retained reports, not guessed pass flags."""

import hashlib
import importlib.metadata as metadata
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/documents"


def write(path, data):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    for p in ROOT.glob("evidence-*.log"):
        dest = OUT / "tdd" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    contract = json.loads((ROOT / "contracts/documents-contracts.json").read_text())
    blocked = {
        "DOC-T-MODEL": "No application-authorized local model runtime/weights/provider is configured; no real model runs or held-out population.",
        "DOC-T-COST": "No real model request, usage or human-burden study; external spending authorization zero.",
        "DOC-T-INJECTION": "No real agent path or 60 malicious / 30 benign injection campaign.",
        "DOC-T-PARSER": "Hostile-upload sandbox/OCR/CSV/XLSX/resource-denial campaign not implemented; only manifest-pinned authored PDFs enabled.",
        "DOC-T-LOAD": "Reference 60-minute workload, resource envelope and SLO measurements not run.",
        "DOC-T-RESTORE": "No paired encrypted backup restoration exercise.",
        "DOC-T-MIGRATE": "Only idempotent initial schema bootstrap; no N/N-1 or interrupted backfill migration exercise.",
        "DOC-T-REPRO": "Fresh databases exercised, not a clean independent Linux/Windows machine or model profile.",
        "DOC-T-DELIVERY": "Dependency inventory is not full rights/security review; no publication authorization.",
        "DOC-T-RETENTION": "Retention/deletion/legal-hold/backup-expiry enforcement not implemented; synthetic data only.",
    }
    partial = {
        "DOC-T-FLOW": (
            "flow/",
            "Real authored digital PDF -> human decision -> separate approval -> durable worker -> independent target/verifier browser path. Other outcomes/workflows and real model remain incomplete.",
        ),
        "DOC-T-INTAKE": (
            "flow/",
            "Three real manifest-pinned digital PDFs and duplicate-link path. No general-format/OCR/supplier ambiguity intake.",
        ),
        "DOC-T-AUTH": (
            "suite.xml",
            "Real local sessions, role separation, CSRF/origin/workspace/export negatives. No complete every-route/two-tenant/RLS/rate-limit suite.",
        ),
        "DOC-T-STALE": (
            "proof/",
            "Source supersession, stale target approval denial, revocation gate, stale lease/fence and post-seal edit denial exercised. Full authority/lookup/policy/grant-expiry race matrix remains open.",
        ),
        "DOC-T-ARITH": (
            "tdd/evidence-green-arithmetic.log",
            "Eight exact fractional quantities checked against independent rational/integer rounding oracle, including 4800.00/384.00/5184.00/1036.80. Multiple receipts/allocation/currency policies not complete.",
        ),
        "DOC-T-PROVENANCE": (
            "tdd/evidence-green-parser.log",
            "Actual digital PDF quote offsets, text digests and character boxes. Text explicitly parser word-order with inserted spaces; no complete OCR/normalization/CSV/XLSX provenance gate.",
        ),
        "DOC-T-PROOF": (
            "proof/",
            "Primary A41 expired/B42 commit-response-loss/OS process-tree death/recovery43 read-only schedule passes with one receipt/revision and unchanged unrelated records. Full supplementary schedule, raw wire/transaction-ID capture and independent reviewer signoff remain incomplete.",
        ),
        "DOC-T-UX": (
            "ux/",
            "Fourteen routes at 1440/1024/390 without global overflow; axe desktop route checks and real screenshots. No six-person usability/NVDA/VoiceOver/cross-browser/manual WCAG completion.",
        ),
        "DOC-T-STATES": (
            "flow/",
            "Actual error/denial/pending/uncertain/verified and polling/draft preservation. Not every screen/state; no SSE/offline/replay-expiration/refresh-safe draft gate.",
        ),
        "DOC-T-RECOVERY": (
            "proof/",
            "Real worker process-tree kill and conservative read-only reconciliation. Full checkpoint/outage/disk/retry/heartbeat/alert timing population not run.",
        ),
    }
    statuses = []
    for test in contract["acceptance_tests"]:
        identifier = test["id"]
        folder, note = partial.get(
            identifier, ("", blocked.get(identifier, "Unverified"))
        )
        statuses.append(
            {
                **test,
                "implementation_status": "blocked"
                if identifier in blocked
                else "partial",
                "evidence_paths": ["evidence/documents/" + folder] if folder else [],
                "remaining_gate": note,
            }
        )
    suite = ET.parse(OUT / "suite.xml").getroot()
    tests = suite.findall(".//testcase")
    totals = {
        "tests": len(tests),
        "failures": sum(t.find("failure") is not None for t in tests),
        "errors": sum(t.find("error") is not None for t in tests),
        "skipped": sum(t.find("skipped") is not None for t in tests),
    }
    browser = json.loads((OUT / "flow/browser-results.json").read_text())
    receipt = json.loads((OUT / "proof/verifier-report.json").read_text())
    proof_pass = any(
        t.attrib.get("name")
        == "test_commit_response_loss_restart_is_read_only_and_one_effect"
        and t.find("failure") is None
        and t.find("error") is None
        for t in tests
    )
    write(
        "acceptance-status.json",
        {
            "schema_version": "folio-acceptance-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "release_ready": False,
            "normative_contract": "contracts/documents-contracts.json",
            "test_totals": totals,
            "browser_stats": browser["stats"],
            "critical_primary_schedule": {
                "id": "DOC-P-LOST-RESPONSE",
                "status": "passed"
                if proof_pass and receipt["outcome"] == "matched"
                else "failed",
                "scope": "primary schedule plus listed focused negatives, NOT full DOC-T-PROOF release gate",
                "evidence": "evidence/documents/proof/",
            },
            "acceptance_tests": statuses,
        },
    )
    # Every original screen ID is retained, with deliberately non-complete release status.
    axe = json.loads((OUT / "ux/route-accessibility.json").read_text())
    exercised = {r["route"] for r in axe}
    write(
        "screen-status.json",
        {
            "screens": [
                {
                    "id": s["id"],
                    "status": "partial",
                    "browser_render_and_three_width_overflow_checked": s["id"]
                    in exercised,
                    "evidence": "evidence/documents/ux/route-accessibility.json",
                    "required_acceptance_ids": s["acceptance_ids"],
                    "remaining": "Full per-role workflows and all declared states remain governed by acceptance-status.json and docs/IMPLEMENTATION-BOUNDARIES.md",
                }
                for s in contract["screens"]
            ]
        },
    )
    from folio.target import app as target
    from folio.workbench import app as wb

    write("contracts/openapi-workbench.json", wb.openapi())
    write("contracts/openapi-target.json", target.openapi())
    packages = []
    for dist in metadata.distributions():
        packages.append(
            {
                "name": dist.metadata["Name"],
                "version": dist.version,
                "license": dist.metadata.get("License-Expression")
                or dist.metadata.get("License")
                or "unresolved",
            }
        )
    write(
        "evidence/documents/delivery/python-dependencies.json",
        {
            "review_complete": False,
            "packages": sorted(packages, key=lambda p: p["name"].lower()),
        },
    )
    sources = []
    for folder in [
        "folio",
        "scripts",
        "tests",
        "web/src",
        "web/tests",
        "contracts",
        "ops",
        "docs",
    ]:
        for p in (ROOT / folder).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                sources.append(
                    {
                        "path": p.relative_to(ROOT).as_posix(),
                        "sha256": sha(p),
                        "bytes": p.stat().st_size,
                    }
                )
    for name in [
        "requirements.lock",
        "web/package-lock.json",
        "README.md",
        "ruff.toml",
    ]:
        p = ROOT / name
        if p.exists():
            sources.append({"path": name, "sha256": sha(p), "bytes": p.stat().st_size})

    def version(args):
        return subprocess.run(
            args, capture_output=True, text=True, check=True
        ).stdout.strip()

    pg = version(
        [
            "docker",
            "exec",
            "folio-postgres",
            "psql",
            "-U",
            "postgres",
            "-Atc",
            "SELECT version()",
        ]
    )
    write(
        "evidence/documents/environment/runtime.json",
        {
            "python": sys.version,
            "platform": platform.platform(),
            "node": version(["node", "--version"]),
            "postgresql": pg,
            "docker_image_digest": "postgres@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
            "free_disk_bytes_at_report": shutil.disk_usage(ROOT).free,
            "model_gate": {
                "status": "blocked",
                "project_model_weights_present": (ROOT / "models").exists(),
                "new_external_spend_authorized": 0,
                "hosted_egress_authorized": False,
                "fake_model_execution": False,
            },
        },
    )
    write(
        "evidence/documents/release-manifest.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "commit": None,
            "public_remote": None,
            "release_ready": False,
            "authorship": "Folio implementation plus supplied original design/specifications; synthetic fixture rights manifest retained",
            "suite": totals,
            "browser_stats": browser["stats"],
            "source_files": sources,
            "limitations": "No model/security/production/full release claim. See acceptance-status.json.",
        },
    )
    assert len(statuses) == len(contract["acceptance_tests"]) and len(exercised) == len(
        contract["screens"]
    )
    print(
        json.dumps(
            {
                "acceptance_ids_preserved": len(statuses),
                "screens_preserved": len(exercised),
                "suite": totals,
                "browser_stats": browser["stats"],
                "release_ready": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
