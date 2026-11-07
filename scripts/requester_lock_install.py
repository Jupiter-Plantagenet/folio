"""Operator-only install of the requester row-lock capability (folio.requester_lock).

Runs the fixed SQL in ONE transaction as the container's trusted operator via
``docker exec`` (never through HTTP, never with runtime credentials), then
verifies the result as the declared runtime login itself.

  python scripts/requester_lock_install.py --database DB --login ROLE --apply
  python scripts/requester_lock_install.py --runtime-dsn-env FOLIO_DSN --verify

DSNs are read from the environment only and are never printed.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from folio.database_security import admin  # noqa: E402
from folio.requester_lock import install_sql, verify  # noqa: E402


def apply(database, login):
    if not database.startswith("folio_"):
        raise SystemExit("PRODUCT_DATABASE_REQUIRED")
    statement = install_sql(login, lambda q: q.as_string(None))
    admin("BEGIN;\n" + statement + "\nCOMMIT;", database)


def runtime_verify(dsn):
    with psycopg.connect(dsn) as db:
        login = db.execute("SELECT session_user").fetchone()[0]
        report = verify(db, login)
        from folio.analysis_authority import requester_problem

        # Actual lock attempt as the runtime principal, then roll back.
        report["probe_unknown_user"] = requester_problem(db, "\x01no-such-user")
        db.rollback()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database")
    parser.add_argument("--login")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--runtime-dsn-env", default="FOLIO_DSN")
    args = parser.parse_args()
    if args.apply:
        if not (args.database and args.login):
            parser.error("--apply needs --database and --login")
        apply(args.database, args.login)
        print(json.dumps({"applied": True, "database": args.database}))
    if args.verify:
        dsn = os.environ.get(args.runtime_dsn_env)
        if not dsn:
            parser.error(args.runtime_dsn_env + " must be supplied in the environment")
        print(json.dumps(runtime_verify(dsn), indent=2))


if __name__ == "__main__":
    main()
