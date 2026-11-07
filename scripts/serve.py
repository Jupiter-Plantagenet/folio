"""Loopback-only local supervisor. Stop with Ctrl+C before running isolated proof suites."""

import argparse
import subprocess
import sys
import time

from runtime import ROOT, environment, is_free, ready, stop

parser = argparse.ArgumentParser()
parser.add_argument("--profile", choices=["main", "browser"], default="main")
args = parser.parse_args()
for port in [8101, 8201]:
    if not is_free(port):
        raise SystemExit(f"Port {port} occupied. No existing process will be stopped.")
env = environment(args.profile)
procs = []
logs = []
base = ROOT / "evidence/documents/operations"
base.mkdir(parents=True, exist_ok=True)


def start(command, name, cwd=ROOT):
    log = (base / (name + "-live.log")).open("w")
    logs.append(log)
    p = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log)
    procs.append(p)


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
    print(ready(8201), flush=True)
    print(ready(8101), flush=True)
    start([sys.executable, "-m", "folio.worker", "--loop"], "worker")
    if is_free(5101):
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
    ready(5101, "/")
    print(
        "Folio http://127.0.0.1:5101 | Northstar http://127.0.0.1:8201 | profile "
        + args.profile,
        flush=True,
    )
    while all(p.poll() is None for p in procs):
        time.sleep(1)
    raise RuntimeError("A supervised process stopped. Inspect operations logs.")
except KeyboardInterrupt:
    pass
finally:
    for p in reversed(procs):
        stop(p)
    for log in logs:
        log.close()
