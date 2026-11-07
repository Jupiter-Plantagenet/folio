"""Invalidate archived disposable credentials without touching retained runtime logins."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def query(statement):
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "folio-postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-tA",
        ],
        input=statement,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("ROLE_AUDIT_COMMAND_FAILED")
    return result.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    reports = sorted((ROOT / ".local/secdata-20261101").glob("mirror-archive-*.json"))
    archived = [json.loads(p.read_text()) for p in reports]
    roles = {"folio_workbench", "folio_target", "folio_verifier"}
    for report in archived:
        if report["apply"]:
            for mirror in report["mirrors"]:
                for file in mirror["credential_files"]:
                    roles.update(file["roles"])
    quarantine_manifest = (
        ROOT / "evidence/documents/delivery/round6/quarantine-manifest.json"
    )
    if quarantine_manifest.exists():
        for entry in json.loads(quarantine_manifest.read_text())["entries"]:
            roles.update(entry.get("roles_referenced", []))
    protected = set()
    profiles = json.loads((ROOT / ".local/database-profiles.json").read_text())
    for profile in profiles.values():
        for value in profile.values():
            if isinstance(value, str) and value.startswith(
                ("postgresql://", "postgres://")
            ):
                protected.add(urlsplit(value).username)
    if roles & protected:
        raise RuntimeError("ARCHIVED_ROLE_IS_CURRENT_RETAINED_RUNTIME_ROLE")
    if not all(
        role.startswith(
            (
                "folio_test_",
                "folio_runtime_",
                "folio_wb_",
                "folio_workbench_",
                "folio_target_",
                "folio_reader_",
            )
        )
        or role in {"folio_workbench", "folio_target", "folio_verifier"}
        for role in roles
    ):
        raise RuntimeError("UNRECOGNIZED_ARCHIVED_ROLE_REQUIRES_REVIEW")
    quoted = ",".join(sql.Literal(role).as_string() for role in sorted(roles))
    statement = (
        "SELECT coalesce(json_agg(json_build_object('role',rolname,'login',rolcanlogin,"
        "'password_absent',rolpassword IS NULL)), '[]'::json)::text FROM pg_authid "
        f"WHERE rolname IN ({quoted});"
    )
    before = json.loads(query(statement))
    active = int(
        query(f"SELECT count(*) FROM pg_stat_activity WHERE usename IN ({quoted});")
    )
    if active:
        raise RuntimeError("EXPOSED_ROLE_HAS_ACTIVE_CONNECTIONS_REVIEW_REQUIRED")
    if args.apply:
        changes = "\n".join(
            sql.SQL("ALTER ROLE {} NOLOGIN PASSWORD NULL;")
            .format(sql.Identifier(row["role"]))
            .as_string()
            for row in before
        )
        query("BEGIN;\n" + changes + "\nCOMMIT;")
    after = json.loads(query(statement))
    out = (
        ROOT
        / "evidence/documents/delivery/round6"
        / (
            "credential-retirement-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".json"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "apply": args.apply,
        "before": before,
        "after": after,
        "protected_retained_roles_unchanged": len(protected),
        "active_connections_before": active,
        "invalidated": args.apply
        and all(not r["login"] and r["password_absent"] for r in after),
    }
    out.write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "roles": len(after),
                "invalidated": report["invalidated"],
                "report": str(out.relative_to(ROOT)),
            }
        )
    )


if __name__ == "__main__":
    main()
