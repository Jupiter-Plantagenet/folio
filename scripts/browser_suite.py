"""Run every Playwright spec, one fresh isolated profile per group.

Several specs import and approve the same authored sample packet, which is
content-linked (a repeat import never manufactures a new case), so specs that
consume it must not share a database. Each group below gets its own
scripts/isolated_browser.py run. Set FOLIO_REAL_MODEL_TEST=1 (with the local
model on 18301) to include the real-model agent journey.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = [
    [
        "tests/completion.spec.ts",
        "tests/journey.spec.ts",
        "tests/northstar.spec.ts",
        "tests/receipts.spec.ts",
    ],
    ["tests/routes.spec.ts", "tests/states.spec.ts"],
    ["tests/real-dimensions.spec.ts"],
]
if os.environ.get("FOLIO_REAL_MODEL_TEST") == "1":
    GROUPS.append(["tests/agent-live.spec.ts"])


def main():
    failed = []
    for group in GROUPS:
        print("== " + " ".join(group), flush=True)
        result = subprocess.run(
            [sys.executable, "-B", "scripts/isolated_browser.py", *group], cwd=ROOT
        )
        if result.returncode:
            failed.append(group)
    print("BROWSER_SUITE " + ("PASS" if not failed else f"FAIL {failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
