"""Explicit one-time local workload provisioning. Never rotates an existing identity."""

import os
import subprocess
from pathlib import Path

from folio.identity import ROOT, provision

if __name__ == "__main__":
    directory = ROOT / ".local"
    paths = provision(directory)
    if os.name == "nt":
        account = os.environ["USERNAME"]
        for value in paths.values():
            subprocess.run(
                [
                    "icacls",
                    str(Path(value)),
                    "/inheritance:r",
                    "/grant:r",
                    account + ":F",
                ],
                check=True,
                capture_output=True,
            )
    print(
        "Provisioned/retained four private product-local identities; no values printed or account passwords changed."
    )
