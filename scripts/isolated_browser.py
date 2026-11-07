"""Fresh nonowner/RLS browser profile at 15101/18101/18201; leaves live apps alone."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from isolated_database import create_profile
from runtime import ROOT, is_free, ready, stop

from folio.database_security import admin, provision_runtime
from folio.identity import provision
from folio.schema_journal import journal_sql


def main():
    for port in [15101, 18101, 18201]:
        if not is_free(port):
            raise SystemExit(f"Proof port {port} occupied; no process stopped")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    names, db_env = create_profile(ROOT, stamp)
    identities = provision(ROOT / ".local/isolated" / stamp)
    private_run = ROOT / ".local/isolated" / stamp
    playwright_output = private_run / "playwright-output"
    playwright_output.mkdir(parents=True, exist_ok=True)
    playwright_report = private_run / "playwright-report.json"
    repro_private_root = os.environ.get("FOLIO_REPRO_PRIVATE_ROOT")
    evidence_root = (
        Path(repro_private_root).resolve()
        if repro_private_root
        else ROOT / "evidence/documents/finish-browser"
    )
    evidence = evidence_root / stamp
    evidence.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        **db_env,
        **identities,
        "PYTHONPATH": str(ROOT),
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "FOLIO_UI_ORIGIN": "http://127.0.0.1:15101",
        "FOLIO_UI_PORT": "15101",
        "FOLIO_API_URL": "http://127.0.0.1:18101",
        "FOLIO_TARGET_URL": "http://127.0.0.1:18201",
        # Per-run disposable Northstar operator credential; never the live one.
        "FOLIO_TARGET_OPERATOR_PASSWORD": __import__("secrets").token_urlsafe(24),
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
            "FOLIO_REPRO_BROWSERS_PATH", str(ROOT / ".cache/browsers")
        ),
        "FOLIO_RECEIPT_TEST_TARGET": "33333333-3333-4333-8333-333333333333",
        "FOLIO_EXPORT_ROOT": str(
            (ROOT / ".local/isolated" / stamp / "artifacts").resolve()
        ),
        "FOLIO_REAL_MODEL_TEST": os.environ.get("FOLIO_REAL_MODEL_TEST", "0"),
        "FOLIO_DIMENSION_FIXTURE_MANIFEST": str(
            (evidence / "dimension-cases.json").resolve()
        ),
        "FOLIO_DIMENSION_API_EVIDENCE": str(
            (evidence / "real-dimension-browser-proof.json").resolve()
        ),
        "FOLIO_DIMENSION_SCREENSHOT_PATH": str(
            (evidence / "reviewer-workflow.png").resolve()
        ),
        "FOLIO_DIMENSION_INPUT_ROOT": str(
            (ROOT / ".local/isolated" / stamp / "dimension-inputs").resolve()
        ),
        "FOLIO_BROWSER_REPORT_PATH": str(playwright_report.resolve()),
        "FOLIO_LIVE_EVIDENCE": str(evidence.resolve()),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from folio.workbench import migrate,WORKSPACE,TARGET;from folio.target import migrate as tm,seed,DSN;import psycopg,os;from psycopg.types.json import Jsonb;migrate();tm();seed(WORKSPACE,TARGET);rid=os.environ['FOLIO_RECEIPT_TEST_TARGET'];r=seed(WORKSPACE,rid);r['invoice_key']='INV-RECEIPTS';db=psycopg.connect(DSN);db.execute('UPDATE payables SET body=%s WHERE workspace=%s AND id=%s',(Jsonb(r),WORKSPACE,rid));db.commit();db.close()",
        ],
        env=env,
        cwd=ROOT,
        check=True,
    )
    if sys.argv[1:] == ["tests/real-dimensions.spec.ts"]:
        subprocess.run(
            [sys.executable, "scripts/seed_dimension_browser.py"],
            env=env,
            cwd=ROOT,
            check=True,
        )
    # Bootstrap and proof runners retain migration ownership; serving roles do not.
    for kind, database in names.items():
        raw = (ROOT / "ops/migrations" / f"002-{kind}.sql").read_bytes()
        admin(
            "BEGIN;" + journal_sql(2, raw) + "COMMIT;",
            database,
        )
    env.update(
        FOLIO_DSN=provision_runtime(names["workbench"], "workbench"),
        FOLIO_TARGET_DSN=provision_runtime(names["target"], "target"),
        FOLIO_TARGET_READ_DSN=provision_runtime(names["target"], "reader"),
        FOLIO_SCHEMA_MODE="runtime",
    )
    # Operator-installed requester-lock capability, as on a hardened install;
    # without it agent-prepared proposals correctly fail closed (503).
    import psycopg

    from folio.requester_lock import install_sql

    with psycopg.connect(env["FOLIO_DSN"]) as db:
        runtime_login = db.execute("SELECT session_user").fetchone()[0]
    statement = install_sql(runtime_login, lambda q: q.as_string(None))
    admin("BEGIN;\n" + statement + "\nCOMMIT;", names["workbench"])
    processes = []
    streams = []

    def start(command, name, cwd=ROOT):
        stream = (evidence / (name + ".log")).open("w")
        streams.append(stream)
        processes.append(
            subprocess.Popen(command, cwd=cwd, env=env, stdout=stream, stderr=stream)
        )

    try:
        start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "folio.target:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18201",
            ],
            "target",
        )
        start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "folio.workbench:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18101",
            ],
            "api",
        )
        start(["node", "node_modules/vite/bin/vite.js"], "web", ROOT / "web")
        for port in [18201, 18101, 15101]:
            ready(port, "/" if port == 15101 else "/health")
        start([sys.executable, "-m", "folio.worker", "--loop"], "worker")
        # Explicit bounded test invocation, not a new scheduled/background export worker.
        subprocess.run(
            [sys.executable, "-m", "folio.exportworker", "--once"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        browser_args = (
            ["--config", "repro-14.config.ts"]
            if os.environ.get("FOLIO_REPRO_OFFLINE") == "1"
            else []
        )
        selected_tests = [] if browser_args else sys.argv[1:]
        result = subprocess.run(
            [
                "node",
                "node_modules/@playwright/test/cli.js",
                "test",
                *browser_args,
                "--output",
                str(playwright_output.resolve()),
                *selected_tests,
            ],
            cwd=ROOT / "web",
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        (private_run / "playwright-stdout.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        print(result.stdout + result.stderr)
        tests = []

        def collect(suite):
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    for attempt in test.get("results", []):
                        tests.append(
                            {
                                "title": spec.get("title", ""),
                                "status": attempt.get("status", "unknown"),
                                "duration_ms": attempt.get("duration", 0),
                            }
                        )
            for child in suite.get("suites", []):
                collect(child)

        report_sha256 = None
        if playwright_report.exists():
            report_bytes = playwright_report.read_bytes()
            report_sha256 = hashlib.sha256(report_bytes).hexdigest()
            raw_report = json.loads(report_bytes)
            for suite in raw_report.get("suites", []):
                collect(suite)
        summary = {
            "selection": sys.argv[1:] or ["all"],
            "exit_code": result.returncode,
            "raw_report_private": str(playwright_report.relative_to(ROOT)),
            "raw_report_sha256": report_sha256,
            "tests": tests,
        }
        (evidence / "playwright.log").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        (evidence / "browser-results.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        manifest = {
            "databases": names,
            "exit_code": result.returncode,
            "ports": [15101, 18101, 18201],
            "runtime_nonowner": True,
            "selection": sys.argv[1:] or ["all"],
            "retained_databases": True,
            "export_worker": "explicit --once before suite; no automatic scheduling",
            "evidence": str(evidence),
        }
        (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if not repro_private_root:
            (ROOT / "evidence/documents/finish-browser/latest.json").write_text(
                json.dumps(manifest, indent=2)
            )
        return result.returncode
    finally:
        for p in reversed(processes):
            stop(p)
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
