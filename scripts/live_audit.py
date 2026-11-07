"""Read back live identity, nonowner/RLS, privacy and preserved user-record evidence."""

import hashlib
import json
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg import sql
from runtime import ROOT, environment


def main():
    env = environment("browser")
    checks = {}
    details = {}
    with httpx.Client(
        base_url="http://127.0.0.1:5101", follow_redirects=False, trust_env=False
    ) as client:
        for name in ["reviewer", "approver", "viewer", "admin", "auditor"]:
            r = client.post(
                "/api/v1/auth/login",
                json={"username": name, "password": f"folio-{name}-local"},
            )
            s = client.get("/api/v1/auth/session")
            checks["existing_" + name + "_account"] = (
                r.status_code == 200
                and s.status_code == 200
                and s.json()["username"] == name
            )
        private = client.get(
            "/@fs/" + str(ROOT / ".local/worker.json").replace("\\", "/")
        )
        checks["private_workload_identity_http_denied"] = private.status_code == 403
    with httpx.Client(trust_env=False) as client:
        redirect = client.get("http://localhost:5101/", follow_redirects=False)
        checks["canonical_localhost_redirect_preserved"] = (
            redirect.status_code == 307
            and redirect.headers["location"] == "http://127.0.0.1:5101/"
        )
        denied = client.get(
            "http://127.0.0.1:8201/api/v1/workspaces/11111111-1111-4111-8111-111111111111/payables/22222222-2222-4222-8222-222222222222",
            headers={"Authorization": "Bearer folio-local-verifier"},
        )
        checks["old_public_target_token_denied"] = denied.status_code == 403
        operator = client.post(
            "http://127.0.0.1:8201/operator/login",
            headers={"Origin": "http://127.0.0.1:8201"},
            json={"username": "operator", "password": __import__("folio.target", fromlist=["operator_password"]).operator_password()},
        )
        checks["existing_target_operator_account"] = (
            operator.status_code == 200
            and client.get("http://127.0.0.1:8201/operator/records").status_code == 200
        )
    backup = json.loads(
        (ROOT / "evidence/documents/restore/20261026100109/report.json").read_text()
    )
    for key, kind in [("FOLIO_DSN", "workbench"), ("FOLIO_TARGET_DSN", "target")]:
        dsn = env[key]
        database = urlsplit(dsn).path[1:]
        prior = next(d for d in backup["databases"] if d["source"] == database)
        with psycopg.connect(dsn) as db:
            role = db.execute(
                "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            details[kind + "_runtime"] = {
                "principal": role[0],
                "superuser": role[1],
                "bypassrls": role[2],
            }
            checks[kind + "_nonowner_nonbypass_runtime"] = (
                not role[1]
                and not role[2]
                and not db.execute(
                    "SELECT tableowner=current_user FROM pg_tables WHERE schemaname='public' AND tablename=%s",
                    ("cases" if kind == "workbench" else "payables",),
                ).fetchone()[0]
            )
            checks[kind + "_workspace_rls_forced"] = all(
                row[0] and row[1]
                for row in db.execute(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='public' AND relname=ANY(%s)",
                    (
                        [
                            "cases",
                            "proposals",
                            "jobs",
                            "commands",
                            "events",
                            "analysis_runs",
                            "originals",
                        ]
                        if kind == "workbench"
                        else ["payables", "operations", "authority"],
                    ),
                )
            )
            for table in (
                ["cases", "proposals", "jobs", "commands", "events"]
                if kind == "workbench"
                else ["payables", "authority", "operations"]
            ):
                rows = db.execute(
                    sql.SQL("SELECT row_to_json(t) FROM {} t").format(
                        sql.Identifier(table)
                    )
                ).fetchall()
                values = sorted(
                    json.dumps(
                        row[0], sort_keys=True, default=str, separators=(",", ":")
                    )
                    for row in rows
                )
                current = {
                    "count": len(values),
                    "sha256": hashlib.sha256("\n".join(values).encode()).hexdigest(),
                }
                checks[kind + "_" + table + "_preserved_from_quiesced_backup"] = (
                    current == prior["tables"][table]
                )
    result = {
        "checks": checks,
        "details": details,
        "all_checked_passed": all(checks.values()),
        "security_certification": False,
        "live_process": json.loads((ROOT / ".local/live-process.json").read_text()),
    }
    path = ROOT / "evidence/documents/operations/live-audit.json"
    path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    assert result["all_checked_passed"]


if __name__ == "__main__":
    main()
