"""Finite real-HTTP read capacity smoke, NOT the 60-minute normative load gate."""

import ctypes
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from isolated_database import create_profile
from model_smoke import Memory
from runtime import ROOT, is_free, ready, stop

from folio.database_security import admin, provision_runtime
from folio.identity import provision


def main():
    m = Memory()
    m.length = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    # Report-only unless FOLIO_RESOURCE_GATES=enforce (user direction 2026-11-04).
    if m.available < 3 * 1024**3 and os.environ.get("FOLIO_RESOURCE_GATES") == "enforce":
        raise SystemExit("RESOURCE_ADMISSION_DENIED; no load started")
    if not is_free(18101):
        raise SystemExit("Proof API port occupied")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    evidence = ROOT / "evidence/documents/load" / stamp
    evidence.mkdir(parents=True)
    names, db_env = create_profile(ROOT, stamp)
    env = {
        **os.environ,
        **db_env,
        **provision(ROOT / ".local/isolated" / stamp),
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "PYTHONPATH": str(ROOT),
    }
    # The generated 10-page PDF is one independently authored CC0 fixture reused
    # by 1,000 synthetic packet records. This is capacity data, not task-quality diversity.
    generator = """import io,json,hashlib
from reportlab.pdfgen.canvas import Canvas
from folio.parser import parse_pdf
from folio.workbench import migrate,connect,create_case
buffer=io.BytesIO();canvas=Canvas(buffer)
for i in range(10):
 canvas.drawString(60,750,'Authored synthetic load page '+str(i+1));canvas.showPage()
canvas.save();raw=buffer.getvalue();source=parse_pdf(raw);source['id']='load-authored-source';source['name']='authored-load.pdf'
migrate();ids=[]
with connect() as db:
 for i in range(1000):ids.append(create_case([source],db=db)['id'])
print(json.dumps({'ids':ids,'logical_pages':sum(len(source['pages']) for _ in ids),'fixture_sha256':hashlib.sha256(raw).hexdigest(),'fixture_bytes':len(raw),'rights':'CC0-1.0 independently authored synthetic; shared fixture, not 1000 independent model tasks'}))
"""
    seeded = subprocess.run(
        [sys.executable, "-c", generator],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    dataset = json.loads(seeded.stdout)
    assert len(dataset["ids"]) == 1000 and dataset["logical_pages"] == 10000
    admin(
        "CREATE TABLE schema_versions(version int PRIMARY KEY,sha256 text NOT NULL);INSERT INTO schema_versions VALUES(2,'load-expand-v2');",
        names["workbench"],
    )
    env.update(
        FOLIO_DSN=provision_runtime(names["workbench"], "workbench"),
        FOLIO_SCHEMA_MODE="runtime",
    )
    samples = []
    with (evidence / "api.log").open("w") as log:
        process = subprocess.Popen(
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
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            ready(18101)
            start = time.monotonic()

            def virtual_user(index):
                local = []
                with httpx.Client(
                    base_url="http://127.0.0.1:18101", timeout=20, trust_env=False
                ) as client:
                    login = client.post(
                        "/api/v1/auth/login",
                        json={"username": "viewer", "password": "folio-viewer-local"},
                    )
                    assert login.status_code == 200
                    for j in range(30):
                        identifier = dataset["ids"][
                            (index * 30 + j) % len(dataset["ids"])
                        ]
                        begun = time.perf_counter()
                        result = client.get(
                            "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases/"
                            + identifier
                        )
                        local.append(
                            {
                                "user": index,
                                "request": j,
                                "status": result.status_code,
                                "milliseconds": (time.perf_counter() - begun) * 1000,
                                "bytes": len(result.content),
                            }
                        )
                return local

            with ThreadPoolExecutor(max_workers=20) as pool:
                for batch in pool.map(virtual_user, range(20)):
                    samples.extend(batch)
            elapsed = time.monotonic() - start
        finally:
            stop(process)
    latencies = sorted(s["milliseconds"] for s in samples)

    def percentile(p):
        return latencies[min(len(latencies) - 1, int((len(latencies) - 1) * p))]

    errors = sum(s["status"] >= 500 for s in samples)
    report = {
        "profile": "finite real-HTTP read smoke; not normative 60-minute mixed OCR/model/write load",
        "reference_gate_passed": False,
        "runtime_nonowner": True,
        "databases": names,
        "dataset": {k: v for k, v in dataset.items() if k != "ids"},
        "packets": len(dataset["ids"]),
        "concurrent_sessions": 20,
        "distinct_account_roles": ["viewer"],
        "requests": len(samples),
        "duration_seconds": elapsed,
        "read_ms": {
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
        },
        "5xx_count": errors,
        "initial_host_available_ram_bytes": m.available,
        "excluded": [
            "60-minute soak",
            "5 active model runs",
            "two concurrent OCR parser slots",
            "write/enqueue/OCR/local-model latency budgets",
            "peak application RSS",
            "multi-identity authorization load",
        ],
    }
    assert len(samples) == 600
    (evidence / "raw-requests.json").write_text(json.dumps(samples, indent=2))
    (evidence / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
