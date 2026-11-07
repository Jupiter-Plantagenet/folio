"""Index current executed evidence without upgrading any original release requirement."""

import hashlib
import importlib.metadata as metadata
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/documents"


def sha(path):
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


def write(path, value):
    path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def select_campaign(folder, selection, report):
    candidates = []
    for manifest in (OUT / folder).glob("*/manifest.json"):
        body = json.loads(manifest.read_text())
        if body.get("selection") == selection and (manifest.parent / report).exists():
            candidates.append((manifest, body))
    if not candidates:
        raise RuntimeError("NO_EXECUTED_FULL_CAMPAIGN:" + folder)
    return sorted(candidates, key=lambda row: row[0].parent.name)[-1]


def main():
    contract = json.loads((ROOT / "contracts/documents-contracts.json").read_text())
    suite_path, suite_manifest = select_campaign("finish", ["tests"], "suite.xml")
    browser_path, browser_manifest = select_campaign(
        "finish-browser", ["all"], "browser-results.json"
    )
    tests = ET.parse(suite_path.parent / "suite.xml").getroot().findall(".//testcase")
    totals = {
        "tests": len(tests),
        "failures": sum(t.find("failure") is not None for t in tests),
        "errors": sum(t.find("error") is not None for t in tests),
        "skipped": sum(t.find("skipped") is not None for t in tests),
    }
    browser = json.loads((browser_path.parent / "browser-results.json").read_text())
    remaining = {
        "DOC-T-FLOW": "Real model/source/tool-to-autonomous-proposal, separate approval/target/verifier browser proof plus other exercised paths; full multi-receipt/missing-information lifecycle and every original interaction/state/role not complete.",
        "DOC-T-INTAKE": "Container PDF/OCR/CSV/XLSX, per-file rejection, cross-route duplicate linking, formulas/missing/target ambiguity subsets exercised; complete layout, supplier/receipt and encrypted-input population not complete.",
        "DOC-T-AUTH": "Private scoped workload identities + signed approved intents, nonowner forced RLS, append-only runtime tombstones and selected negatives exercised; full route/role/two-tenant/account/policy matrix and independent review incomplete.",
        "DOC-T-STALE": "Source/target/revocation/lease/fence and precommit-absence schedules exercised; all grant-generation/authority/expiry/clock-policy races not complete.",
        "DOC-T-ARITH": "Independent rational oracle and exact five-field partial result exercised; multi-receipt allocation/currency/policy population incomplete.",
        "DOC-T-PROVENANCE": "Real PDF/OCR geometry, CSV raw-field bytes, XLSX cells/formulas and versioned human provenance exercised; Unicode original-stream mappings and full geometry-IoU/support population incomplete.",
        "DOC-T-MODEL": "Actual direct/API/nonowner browser local-weight autonomy smokes, not frozen independently adjudicated 150 held-out tasks x3 or declared precision/slice/quality thresholds. Hosted profile remains unauthorized.",
        "DOC-T-COST": "Actual local model token usage and wall timings retained; no full evaluation CPU/RSS/energy/provider billing or representative human burden population.",
        "DOC-T-INJECTION": "Closed typed tool denial and budget/missing-source negatives exist; required 60 malicious/30 benign real-model injection campaign and independent security review not run.",
        "DOC-T-PARSER": "Actual rootless/networkless/read-only bounded parser, OCR and zip/formula negatives exercised; full hostile/breakout/resource-denial population not complete.",
        "DOC-T-PROOF": "Real lost-response OS-kill proof and before-grant/before-mutation/exhaustion regressions pass in selected suite; all supplementary raw-wire/transaction/authority/restore schedules and independent signoff incomplete.",
        "DOC-T-UX": "All14 route IDs/three widths/desktop axe and added live actions exercised; manual keyboard/screen-reader/contrast/cross-browser and six representative human reviewers incomplete.",
        "DOC-T-STATES": "Persisted analysis/model-off, rejected, held/escalated/no-change, target-match/correction/recovery/SSE subsets implemented; full per-role/screen offline/cursor-expiry/draft/state matrix incomplete.",
        "DOC-T-LOAD": "Finite 1000 packet/10000 logical page, 20-session, 600 real-read smoke measured; 60-minute mixed OCR/model/write/enqueue workload, peak RSS and all reference budgets unrun.",
        "DOC-T-RECOVERY": "Real process-tree kill and explicit fenced absence/exhaustion diagnoses exercised; complete outage/heartbeat/fairness/alert and <=60/120s timing population incomplete.",
        "DOC-T-RESTORE": "Actual paired small-workspace AES-GCM disk-read/decrypt/isolated restore, hash/FK/session/tombstone validation; full target newer/older reconciliation, fence rebase, off-host custody and reference RPO/RTO not complete.",
        "DOC-T-MIGRATE": "Actual expand-only checksum-journaled v2/runtime-role switch with preserved records; complete N/N-1 compatibility, interrupted backfill and rollback proof not complete.",
        "DOC-T-REPRO": "Fresh isolated DBs/roles/services and reproducible local commands executed; no independently clean uncached Linux and Windows machine walkthrough.",
        "DOC-T-DELIVERY": "Exact source/dependency/weight/asset evidence indexed; complete SBOM/license/security/redaction review and publication authorization not complete.",
        "DOC-T-RETENTION": "Original-byte legal hold/admin deletion/90-day expiry/tombstones exercised; decision-text purge, backup expiry, durable export and complete deletion/hold population incomplete.",
    }
    evidence = {
        identifier: ["evidence/documents/finish/", "docs/CURRENT-0.2.md"]
        for identifier in remaining
    }
    for identifier, paths in {
        "DOC-T-MODEL": ["evidence/documents/model/local-smoke/"],
        "DOC-T-COST": ["evidence/documents/model/local-smoke/"],
        "DOC-T-PROOF": ["evidence/documents/proof/", "evidence/documents/recovery/"],
        "DOC-T-LOAD": ["evidence/documents/load/"],
        "DOC-T-RESTORE": ["evidence/documents/restore/"],
        "DOC-T-MIGRATE": ["evidence/documents/migrate/"],
        "DOC-T-UX": ["evidence/documents/ux/", "evidence/documents/finish-browser/"],
        "DOC-T-AUTH": [
            "evidence/documents/operations/live-audit.json",
            "docs/REVIEW-DISPOSITION.json",
        ],
    }.items():
        evidence[identifier] += paths
    assert set(remaining) == {t["id"] for t in contract["acceptance_tests"]}
    statuses = [
        {
            **t,
            "implementation_status": "partial"
            if t["id"] != "DOC-T-REPRO"
            else "blocked",
            "evidence_paths": evidence[t["id"]],
            "remaining_gate": remaining[t["id"]],
        }
        for t in contract["acceptance_tests"]
    ]
    result = {
        "schema_version": "folio-acceptance-v2",
        "version": (ROOT / "VERSION").read_text().strip(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_ready": False,
        "normative_contract": "contracts/documents-contracts.json",
        "test_totals": totals,
        "suite_manifest": suite_path.relative_to(ROOT).as_posix(),
        "browser_stats": browser["stats"],
        "browser_manifest": browser_path.relative_to(ROOT).as_posix(),
        "model_evidence_separate": "evidence/documents/model/local-smoke/",
        "acceptance_tests": statuses,
    }
    write(Path("acceptance-status.json"), result)
    routes = json.loads((OUT / "ux/route-accessibility.json").read_text())
    observed = {r["route"] for r in routes}
    assert observed == {s["id"] for s in contract["screens"]}
    write(
        Path("screen-status.json"),
        {
            "version": result["version"],
            "screens": [
                {
                    **s,
                    "status": "partial",
                    "browser_render_and_three_width_overflow_checked": s["id"]
                    in observed,
                    "remaining": "Full original role/state/workflow/manual usability/accessibility gates remain in acceptance-status.json",
                }
                for s in contract["screens"]
            ],
        },
    )
    from folio.target import app as target
    from folio.workbench import app as workbench

    write(Path("contracts/openapi-target.json"), target.openapi())
    write(Path("contracts/openapi-workbench.json"), workbench.openapi())
    write(
        Path("evidence/documents/delivery/python-dependencies-current.json"),
        {
            "review_complete": False,
            "packages": sorted(
                [
                    {
                        "name": d.metadata["Name"],
                        "version": d.version,
                        "license": d.metadata.get("License-Expression")
                        or d.metadata.get("License")
                        or "unresolved",
                    }
                    for d in metadata.distributions()
                ],
                key=lambda p: p["name"].lower(),
            ),
        },
    )
    files = []
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
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append(
                    {
                        "path": path.relative_to(ROOT).as_posix(),
                        "sha256": sha(path),
                        "bytes": path.stat().st_size,
                    }
                )
    for name in [
        "VERSION",
        "README.md",
        "requirements.in",
        "requirements.lock",
        "pytest.ini",
        "web/vite.config.ts",
        "web/playwright.config.ts",
        "web/package.json",
        "web/package-lock.json",
        ".dockerignore",
        ".gitignore",
    ]:
        path = ROOT / name
        files.append({"path": name, "sha256": sha(path), "bytes": path.stat().st_size})
    files.sort(key=lambda item: item["path"])
    tree = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # Only generated local identities are scanned, never unrelated account/provider files.
    secrets = set()
    for path in (ROOT / ".local").rglob("*.json"):
        try:
            body = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(body, dict):
            for key, value in body.items():
                if key in ("password", "private_key") and isinstance(value, str):
                    secrets.add(value.encode())
                if key == "tokens" and isinstance(value, dict):
                    secrets.update(str(v).encode() for v in value.values())
                if (
                    key.startswith("FOLIO_")
                    and isinstance(value, str)
                    and value.startswith("postgresql://")
                    and urlsplit(value).password
                ):
                    secrets.add(urlsplit(value).password.encode())
    leaked = []
    for path in [ROOT / f["path"] for f in files] + [
        p
        for p in OUT.rglob("*")
        if p.is_file() and p.suffix in (".json", ".log", ".md", ".txt")
    ]:
        data = path.read_bytes()
        if any(secret in data for secret in secrets if len(secret) >= 20):
            leaked.append(path.relative_to(ROOT).as_posix())
    assert not leaked, "PROVISIONED_SECRET_IN_PUBLIC_ARTIFACT:" + str(leaked)
    manifest = {
        "version": result["version"],
        "generated_at": result["generated_at"],
        "source_tree_sha256": tree,
        "source_files": files,
        "commit": None,
        "public_remote": None,
        "release_ready": False,
        "full_original_acceptance_gates_passed": 0,
        "normative_acceptance_ids": len(statuses),
        "original_screen_ids": len(observed),
        "suite": totals,
        "suite_manifest": result["suite_manifest"],
        "browser_manifest": result["browser_manifest"],
        "browser_stats": browser["stats"],
        "real_model_smoke": "evidence/documents/model/local-smoke/",
        "private_material_excluded": [
            ".local/",
            "external existing GGUF bytes",
            "private account guide",
            "unrelated provider configuration",
        ],
        "provisioned_secret_scan": {
            "leaks": leaked,
            "scope": "generated local identity strings vs indexed source and textual evidence; not a complete security audit",
        },
        "independent_review": "docs/REVIEW-DISPOSITION.json; parent rerun/signoff pending",
    }
    write(Path("evidence/documents/release-manifest.json"), manifest)
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in [
                    "version",
                    "source_tree_sha256",
                    "normative_acceptance_ids",
                    "original_screen_ids",
                    "suite",
                    "browser_stats",
                    "release_ready",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
