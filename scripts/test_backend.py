"""Own the target server during backend/proof tests; never run a competing worker."""

import subprocess
import sys

from runtime import ROOT, is_free, ready, stop

if not is_free(8201):
    raise SystemExit(
        "Stop the Folio target/worker first. This runner will not stop an existing process."
    )
path = ROOT / "evidence/documents/proof/target-server.log"
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("w") as log:
    p = subprocess.Popen(
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
        cwd=ROOT,
        stdout=log,
        stderr=log,
    )
    try:
        print(ready(8201), flush=True)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-v",
                "--junitxml=evidence/documents/suite.xml",
            ],
            cwd=ROOT,
            check=False,
        )
    finally:
        stop(p)
raise SystemExit(result.returncode)
