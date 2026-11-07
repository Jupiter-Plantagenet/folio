"""Apply every bootstrap migration to brand-new empty databases, twice.

Checks that the checksum journal records each migration file exactly once with the
on-disk bytes, that a second run is a no-op, and that no data changes. Creates and
then removes its own throwaway roles/databases; never touches a live profile.
Output: evidence/documents/migrate/portfolio-<stamp>/report.json (no secrets).
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from isolated_database import create_profile  # noqa: E402
from round5_migrate import fingerprints, journal  # noqa: E402

from folio.database_security import admin  # noqa: E402

# Applied by `folio.workbench.migrate()` at bootstrap. 006 is an operator-only
# compatibility migration for a retired N-1 app and is not part of a fresh install.
BOOTSTRAP = {
    2: "002-workbench.sql",
    3: "003-exports.sql",
    4: "004-export-actor-lock.sql",
    5: "005-read-path-indexes.sql",
    8: "008-access-administration.sql",
}


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out = ROOT / "evidence/documents/migrate" / f"portfolio-{stamp}"
    out.mkdir(parents=True)
    names, owner_env = create_profile(ROOT, f"pmg{stamp}")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("FOLIO_")},
        **owner_env,
        "FOLIO_SCHEMA_MODE": "bootstrap",
        "PYTHONPATH": str(ROOT),
    }
    boot = [
        sys.executable,
        "-c",
        "from folio.workbench import migrate;from folio.target import migrate as tm;migrate();tm()",
    ]
    try:
        first = subprocess.run(boot, env=env, cwd=ROOT, capture_output=True, text=True)
        journal_1 = journal(owner_env["FOLIO_DSN"])
        data_1 = fingerprints(owner_env["FOLIO_DSN"])
        second = subprocess.run(boot, env=env, cwd=ROOT, capture_output=True, text=True)
        journal_2 = journal(owner_env["FOLIO_DSN"])
        data_2 = fingerprints(owner_env["FOLIO_DSN"])
    finally:
        for name in names.values():
            admin(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                .format(sql.Identifier(name))
                .as_string()
            )
        for kind in ["wb", "target", "reader"]:
            admin(
                sql.SQL("DROP ROLE IF EXISTS {}")
                .format(sql.Identifier(f"folio_test_{kind}_pmg{stamp}"))
                .as_string()
            )

    expected = {
        str(v): hashlib.sha256((ROOT / "ops/migrations" / n).read_bytes()).hexdigest()
        for v, n in BOOTSTRAP.items()
    }
    recorded = {v: digest for v, digest, _ in journal_1}
    checks = {
        "first_run_exit_0": first.returncode == 0,
        "second_run_exit_0": second.returncode == 0,
        "journal_has_every_bootstrap_migration": recorded == expected,
        "second_run_left_journal_unchanged": journal_1 == journal_2,
        "second_run_left_data_unchanged": data_1 == data_2,
    }
    report = {
        "at": stamp,
        "migrations_expected": expected,
        "journal_recorded": recorded,
        "checks": checks,
        "passed": all(checks.values()),
        "stderr_tail": [first.stderr[-2000:], second.stderr[-2000:]],
        "cleanup": "throwaway databases and roles dropped",
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"passed": report["passed"], "checks": checks, "report": str(out)}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
