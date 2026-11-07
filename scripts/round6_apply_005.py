"""Operator apply of migration 005 (expand-only read-path indexes) to ONE workbench DB.

  python scripts/round6_apply_005.py <database> <owner_role> [--dry-run]

Runs as the database owner (SET ROLE from the container superuser session, no password
handled), in ONE transaction: verify the journalled checksums of 2/3/4 against the files,
skip if 005 is already journalled with the right checksum, else journal + execute 005.
Prints the resulting journal and the new indexes. Never drops or rewrites anything.
Back up first (RELEASE.md section 4) and rehearse on a scratch restore of that backup.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from psycopg import sql

from folio.schema_journal import journal_sql

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "ops/migrations"
FILES = {2: "002-workbench.sql", 3: "003-exports.sql", 4: "004-export-actor-lock.sql",
         5: "005-read-path-indexes.sql"}


def psql(database, statement):
    r = subprocess.run(["docker", "exec", "-i", "folio-postgres", "psql", "-U", "postgres", "-d", database,
                        "-v", "ON_ERROR_STOP=1", "-q", "-At"], input=statement.encode(), capture_output=True)
    if r.returncode:
        raise SystemExit("PSQL_FAILED: " + r.stderr.decode(errors="replace")[:400])
    return r.stdout.decode()


def main():
    database, owner = sys.argv[1], sys.argv[2]
    dry = "--dry-run" in sys.argv
    if not database.startswith(("folio_browser_wb_", "folio_restore_", "folio_finish_wb_")):
        raise SystemExit("WORKBENCH_DATABASE_REQUIRED")
    journal = dict(
        line.split("|") for line in psql(database, "SELECT version, sha256 FROM schema_versions ORDER BY 1").split()
    )
    for version in (2, 3, 4):
        want = hashlib.sha256((MIG / FILES[version]).read_bytes()).hexdigest()
        if journal.get(str(version)) != want:
            raise SystemExit(f"MIGRATION_CHECKSUM_MISMATCH_OR_MISSING:{version}")
    raw = (MIG / FILES[5]).read_bytes()
    if "5" in journal:
        if journal["5"] != hashlib.sha256(raw).hexdigest():
            raise SystemExit("MIGRATION_CHECKSUM_MISMATCH:5")
        action = "already_applied_skipped"
    elif dry:
        action = "dry_run_would_apply"
    else:
        psql(database, "BEGIN;\n"
             + sql.SQL("SET LOCAL ROLE {};\n").format(sql.Identifier(owner)).as_string()
             + journal_sql(5, raw) + "\n" + raw.decode("utf-8") + "\nCOMMIT;\n")
        action = "applied"
    after = psql(database, "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_versions;"
                 " SELECT string_agg(indexname, ',' ORDER BY indexname) FROM pg_indexes WHERE schemaname='public'"
                 " AND indexname IN ('cases_queue_listing','proposals_case_id','jobs_proposal','analysis_runs_case');"
                 " SELECT string_agg(DISTINCT pg_get_userbyid(c.relowner), ',') FROM pg_class c"
                 " WHERE c.relname IN ('cases_queue_listing','proposals_case_id','jobs_proposal','analysis_runs_case');").split()
    print(json.dumps({"database": database, "action": action, "journal": after[0] if after else None,
                      "indexes": after[1] if len(after) > 1 else None,
                      "index_owner": after[2] if len(after) > 2 else None}))


if __name__ == "__main__":
    main()
