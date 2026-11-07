"""Quiesced versioned expand migration + nonowner runtime switch, no sample resets.
Run restore_drill.py first. Runtime cannot perform these DDL operations.
"""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit

from runtime import ROOT, environment, is_free

from folio.database_security import admin, provision_runtime
from folio.schema_journal import journal_sql


def main():
    if not all(is_free(port) for port in [8101, 8201]):
        raise SystemExit("Quiesce only Folio supervisor; no process stopped here")
    profile_file = ROOT / ".local/database-profiles.json"
    profiles = json.loads(profile_file.read_text()) if profile_file.exists() else {}
    report = []
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=["main", "browser"],
        default=["main", "browser"],
    )
    args = parser.parse_args()
    for profile in args.profiles:
        env = environment(profile)
        databases = {
            kind: urlsplit(env.get(key, default)).path[1:]
            for kind, key, default in [
                ("workbench", "FOLIO_DSN", "postgresql://localhost/folio_workbench"),
                ("target", "FOLIO_TARGET_DSN", "postgresql://localhost/folio_target"),
            ]
        }
        next_env = {}
        for kind, database in databases.items():
            if not database.startswith("folio_"):
                raise ValueError("PROJECT_DATABASE_REQUIRED")
            raw = (ROOT / "ops/migrations" / f"002-{kind}.sql").read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            journal = journal_sql(2, raw)
            admin("BEGIN;" + raw.decode() + journal + "COMMIT;", database)
            next_env["FOLIO_DSN" if kind == "workbench" else "FOLIO_TARGET_DSN"] = (
                provision_runtime(database, kind)
            )
            if kind == "target":
                next_env["FOLIO_TARGET_READ_DSN"] = provision_runtime(
                    database, "reader"
                )
            report.append(
                {
                    "profile": profile,
                    "database": database,
                    "migration": 2,
                    "migration_sha256": sha,
                    "runtime_nonowner": True,
                    "workspace_rls_forced": True,
                }
            )
        profiles[profile] = next_env
    profile_file.write_text(json.dumps(profiles, indent=2))
    profile_file.chmod(0o600)
    admin(
        "ALTER ROLE folio_workbench NOLOGIN; ALTER ROLE folio_target NOLOGIN; ALTER ROLE folio_verifier NOLOGIN;"
    )
    if os.name == "nt":
        # Apply inheritable flags to directories only; file ACEs must apply to self.
        # icacls (OI)(CI) on a file can leave it with no usable access entry.
        for directory, _, filenames in os.walk(ROOT / ".local"):
            subprocess.run(
                [
                    "icacls",
                    directory,
                    "/inheritance:r",
                    "/grant:r",
                    os.environ["USERNAME"] + ":(OI)(CI)F",
                ],
                check=True,
                capture_output=True,
            )
            for filename in filenames:
                subprocess.run(
                    [
                        "icacls",
                        str(__import__("pathlib").Path(directory) / filename),
                        "/inheritance:r",
                        "/grant:r",
                        os.environ["USERNAME"] + ":F",
                    ],
                    check=True,
                    capture_output=True,
                )
    evidence = ROOT / "evidence/documents/migrate"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "runtime-switch.json").write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "profiles": report,
                "legacy_source_credentials_login_disabled": True,
                "account_passwords_rotated": False,
                "compatibility": "expand-only tables; N-1 app can use the expanded schema with runtime mode, complete interrupted-backfill/app-rollback matrix untested",
            },
            indent=2,
        )
    )
    print(
        "Applied schema v2, forced workspace policies, nonowner runtimes; disabled only legacy source-defined DB logins. App accounts and records retained."
    )


if __name__ == "__main__":
    main()
