"""Round-5: prove whether migration 004 DDL is (re)executed, using a superuser DDL event
trigger installed ONLY in a scratch restore of the round-4 retained snapshot (v2,3,4).

Runs: operator export_migrate --apply (x2), bootstrap migrate() as owner, runtime startup.
Also re-probes N-1 app compatibility on schema N with status codes only (no tokens kept).
Output: evidence/documents/migrate/round5-004-<stamp>/report.json
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from round5_migrate import BACKUP, N1, PY, base_env, restore_snapshot, runtime_role  # noqa: E402

from folio.database_security import admin, provision_runtime  # noqa: E402

TRIGGER = """
CREATE SCHEMA r5audit;
CREATE TABLE r5audit.ddl(id bigserial PRIMARY KEY, run text, tag text, object text, at timestamptz DEFAULT clock_timestamp());
CREATE TABLE r5audit.run(label text);
INSERT INTO r5audit.run VALUES('setup');
CREATE FUNCTION r5audit.log() RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r record; BEGIN
 FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
  INSERT INTO r5audit.ddl(run,tag,object) VALUES((SELECT label FROM r5audit.run LIMIT 1), r.command_tag, r.object_identity);
 END LOOP; END $$;
CREATE EVENT TRIGGER r5audit_ddl ON ddl_command_end EXECUTE FUNCTION r5audit.log();
"""


def label(db, name):
    admin(f"UPDATE r5audit.run SET label='{name}'", db)


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out = ROOT / "evidence/documents/migrate" / f"round5-004-{stamp}"
    out.mkdir(parents=True)
    db, owner_dsn, owner = restore_snapshot("folio_browser_wb_20261026080908.dump", stamp, "m4")
    rt = provision_runtime(db, "workbench")
    admin(TRIGGER, db)
    report = {"scratch_database": db, "snapshot": BACKUP.name, "runs": {}}
    mig = [PY, str(ROOT / "scripts/export_migrate.py"), "--apply", "--runtime-role", runtime_role(rt)]
    runs = [
        ("operator_apply_1", mig, {"FOLIO_MIGRATION_DSN": owner_dsn}),
        ("operator_apply_2", mig, {"FOLIO_MIGRATION_DSN": owner_dsn}),
        ("runtime_startup", [PY, "-c", "from folio.workbench import migrate;migrate()"],
         {"FOLIO_DSN": rt, "FOLIO_SCHEMA_MODE": "runtime"}),
        ("bootstrap_owner_migrate", [PY, "-c", "from folio.workbench import migrate;migrate()"],
         {"FOLIO_DSN": owner_dsn, "FOLIO_SCHEMA_MODE": "bootstrap"}),
    ]
    for name, argv, extra in runs:
        label(db, name)
        r = subprocess.run(argv, env={**base_env(), "PYTHONPATH": str(ROOT), **extra}, cwd=ROOT, capture_output=True, text=True)
        from round5_restore import jq

        ddl = [(x["tag"], x["object"]) for x in jq(db, f"SELECT tag, object FROM r5audit.ddl WHERE run='{name}' ORDER BY id")]
        with psycopg.connect(owner_dsn) as c:
            journal = c.execute("SELECT version, sha256, installed_at::text FROM schema_versions ORDER BY 1").fetchall()
        report["runs"][name] = {
            "exit": r.returncode, "error": (r.stderr.strip().splitlines() or [""])[-1][-160:],
            "ddl_commands": ddl,
            "migration_004_ddl_executed": any(o and "folio_export_lock_actor" in o and t == "CREATE FUNCTION" for t, o in ddl),
            "migration_003_ddl_executed": any(o and "export_jobs" in o and t in ("CREATE TABLE", "CREATE POLICY", "CREATE INDEX") for t, o in ddl),
            "journal": journal,
        }
    admin("DROP EVENT TRIGGER r5audit_ddl", db)

    # N-1 application on schema N (status codes only; tokens are never recorded).
    env = {**base_env(), "FOLIO_DSN": rt, "FOLIO_SCHEMA_MODE": "runtime", "PYTHONPATH": str(N1),
           "FOLIO_EXPORT_ROOT": str((ROOT / ".local/round5" / f"m4{stamp}" / "exports").resolve())}
    log = (out / "appN1-schemaN.log").open("w")
    p = subprocess.Popen([PY, "-B", "-m", "uvicorn", "folio.workbench:app", "--host", "127.0.0.1", "--port", "9106"],
                         cwd=N1, env=env, stdout=log, stderr=subprocess.STDOUT)
    probe = {}
    try:
        for _ in range(60):
            try:
                probe["health"] = httpx.get("http://127.0.0.1:9106/health", timeout=1).status_code
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        c = httpx.Client(base_url="http://127.0.0.1:9106", headers={"Origin": "http://127.0.0.1:5101"})
        lr = c.post("/api/v1/auth/login", json={"username": "reviewer", "password": "folio-reviewer-local"})
        probe["login"] = lr.status_code
        cases = c.get("/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases")
        probe["case_list"] = cases.status_code
        probe["cases_returned"] = len(cases.json().get("items", [])) if cases.status_code == 200 else None
        csrf = lr.json().get("csrf") if lr.status_code == 200 else None
        exp = c.post("/api/v1/workspaces/11111111-1111-4111-8111-111111111111/cases/"
                     + (cases.json()["items"][0]["id"] if probe["cases_returned"] else "x") + "/exports",
                     json={"case_version": 1, "include_originals": False, "redaction_profile": "audit-v1"},
                     headers={"X-CSRF-Token": csrf or "", "Idempotency-Key": "8a1f2a5e-6f3b-4c5e-9d6a-1b2c3d4e5f60"})
        probe["n1_export_enqueue_on_schemaN"] = exp.status_code
    finally:
        psutil.Process(p.pid).kill()
        log.close()
    report["n_minus_1_on_schema_N"] = {"source": str(N1.relative_to(ROOT)), "port": 9106, **probe}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({"report": str(out / "report.json")}))


if __name__ == "__main__":
    main()
