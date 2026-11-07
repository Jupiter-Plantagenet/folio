"""Fresh local PostgreSQL databases + actual browser journey; no existing data reset.
Stop API/target/worker first. Existing UI at 5101 may remain running.
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import dev_dsn  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    if not free(8101) or not free(8201):
        raise SystemExit(
            "Stop the Folio API/target/worker first: ports 8101 and 8201 must be free. No unrelated process will be stopped."
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    wb = "folio_browser_wb_" + stamp
    target = "folio_browser_target_" + stamp
    evidence = ROOT / "evidence/documents/flow" / stamp
    evidence.mkdir(parents=True)
    for db, owner in [(wb, "folio_workbench"), (target, "folio_target")]:
        subprocess.run(
            [
                "docker",
                "exec",
                "folio-postgres",
                "psql",
                "-U",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"CREATE DATABASE {db} OWNER {owner}",
            ],
            check=True,
        )
    env = {
        **os.environ,
        "FOLIO_DSN": dev_dsn("folio_workbench", wb),
        "FOLIO_TARGET_DSN": dev_dsn("folio_target", target),
        "FOLIO_TARGET_READ_DSN": dev_dsn("folio_verifier", target),
        "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / ".cache/browsers"),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from folio.target import migrate,seed;from folio.workbench import WORKSPACE,TARGET;migrate();seed(WORKSPACE,TARGET)",
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    processes = []
    streams = []

    def start(args, name, cwd=ROOT):
        out = (evidence / (name + ".log")).open("w")
        streams.append(out)
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=out, stderr=out)
        processes.append(process)

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
                "8201",
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
                "8101",
            ],
            "api",
        )
        if free(5101):
            start(
                [
                    "node",
                    "node_modules/vite/bin/vite.js",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "5101",
                ],
                "web",
                ROOT / "web",
            )
        deadline = time.monotonic() + 30
        for port in [8101, 8201, 5101]:
            while True:
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{port}"
                        + ("/health" if port != 5101 else "/"),
                        timeout=1,
                    )
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Port {port} failed readiness; inspect {evidence}"
                    )
                time.sleep(0.2)
        start([sys.executable, "-m", "folio.worker", "--loop"], "worker")
        result = subprocess.run(
            ["node", "node_modules/@playwright/test/cli.js", "test"],
            cwd=ROOT / "web",
            env=env,
            check=False,
        )
        manifest = {
            "workbench_database": wb,
            "target_database": target,
            "command": "python scripts/browser_proof.py",
            "exit_code": result.returncode,
            "retained_databases": True,
            "egress": "none for document/model operations",
            "profile": "fresh empty local databases, same acquired dependencies; NOT clean machine reproduction",
        }
        (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (ROOT / "evidence/documents/flow/latest-browser-profile.json").write_text(
            json.dumps(manifest, indent=2)
        )
        return result.returncode
    finally:
        for process in reversed(processes):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
