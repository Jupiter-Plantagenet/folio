"""One-shot archived N-1 export probe on the exact retained browser database.

Never run directly. It requires the Folio shared host lock, a recent encrypted
paired backup with matching PostgreSQL system/database identities, the live
supervisor's selected browser profile, and a private reviewer password in
FOLIO_N1_REVIEWER_PASSWORD. It preserves the single generated session/export job.
"""

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
PB = ROOT.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(PB / "tools")]
import live_supervisor  # noqa: E402
import product_lock  # noqa: E402
import round5_migrate as n1_source  # noqa: E402
import round6_apply_008 as apply008  # noqa: E402

from folio.private_storage import (  # noqa: E402
    secure_private_directory,
    secure_private_file,
)

WORKSPACE = "11111111-1111-4111-8111-111111111111"
EXPECTED_WORKBENCH = "folio_browser_wb_20261026080908"
EXPECTED_TARGET = "folio_browser_target_20261026080908"
EXPECTED_WORKBENCH_OID = 16771
EXPECTED_TARGET_OID = 16772
MAX_BACKUP_AGE_S = 3600
CURRENT_WORKER_FILES = (
    "folio/exportworker.py",
    "folio/exports.py",
    "folio/database.py",
    "folio/database_security.py",
)
N1_RECEIPT = ROOT / ".local/access-rehearsal/retained-apply-20261102t041054638185z/report.json"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_host_lock():
    lock_path = PB / ".cache/product-build.lock"
    owner = product_lock.read_owner(lock_path)
    if (not owner or owner.get("product") != "folio" or
            owner.get("purpose") != "n1-retained-compat-probe"):
        raise RuntimeError("N1_RETAINED_EXPECTED_HOST_LOCK_REQUIRED")
    ancestors = {(p.pid, p.create_time()) for p in psutil.Process().parents()}
    if (owner.get("pid"), owner.get("create_time")) not in ancestors:
        raise RuntimeError("N1_RETAINED_ANCESTOR_HOST_LOCK_REQUIRED")
    return {"product": "folio", "purpose": owner["purpose"], "ancestor_verified": True}


def required_receipt():
    receipt = json.loads(N1_RECEIPT.read_text(encoding="utf-8"))
    if receipt.get("status") != "migration_applied_atomic_assertions_passed":
        raise RuntimeError("N1_RETAINED_008_RECEIPT_INVALID")
    journal_post = receipt.get("journal_post")
    if not isinstance(journal_post, dict) or sorted(map(int, journal_post)) != [2, 3, 4, 5, 6, 8]:
        raise RuntimeError("N1_RETAINED_008_RECEIPT_JOURNAL_INVALID")
    return receipt


def paired_backup(backup_id):
    if not re.fullmatch(r"\d{8}T\d{6}Z", backup_id):
        raise RuntimeError("N1_RETAINED_BACKUP_ID_INVALID")
    root = ROOT / ".local/backups" / backup_id
    manifest_path = root / "paired-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "folio-paired-backup-v1" or manifest.get("backup_id") != backup_id:
        raise RuntimeError("N1_RETAINED_PAIRED_BACKUP_MANIFEST_INVALID")
    captured = datetime.fromisoformat(manifest["captured_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
    if age < -120 or age > MAX_BACKUP_AGE_S:
        raise RuntimeError("N1_RETAINED_BACKUP_NOT_FRESH")
    if (manifest.get("encrypted") is not True or manifest.get("pair_consistency") is None or
            manifest.get("exports", {}).get("database_references_verified") is not True):
        raise RuntimeError("N1_RETAINED_BACKUP_NOT_PAIRED_ENCRYPTED")
    if not str(manifest.get("cluster_system_identifier", "")).isdecimal():
        raise RuntimeError("N1_RETAINED_BACKUP_SYSID_MISSING")
    pair = manifest.get("database_pair", {})
    wb = pair.get("workbench", {})
    target = pair.get("target", {})
    if (wb.get("database"), wb.get("oid")) != (EXPECTED_WORKBENCH, EXPECTED_WORKBENCH_OID):
        raise RuntimeError("N1_RETAINED_BACKUP_WORKBENCH_IDENTITY_MISMATCH")
    if (target.get("database"), target.get("oid")) != (EXPECTED_TARGET, EXPECTED_TARGET_OID):
        raise RuntimeError("N1_RETAINED_BACKUP_TARGET_IDENTITY_MISMATCH")
    databases = manifest.get("databases", {})
    if set(databases) != {EXPECTED_WORKBENCH, EXPECTED_TARGET}:
        raise RuntimeError("N1_RETAINED_BACKUP_DATABASE_SET_MISMATCH")
    for name, record in databases.items():
        dump_file = root / f"{name}.dump.enc"
        key_file = ROOT / ".local/backup-keys" / backup_id / f"{name}.key"
        if (not dump_file.is_file() or dump_file.is_symlink() or
                not key_file.is_file() or key_file.is_symlink() or
                record.get("authenticated_readback") is not True):
            raise RuntimeError("N1_RETAINED_BACKUP_FILE_MISSING")

    from scripts.backup_retention import INVENTORY

    records = []
    for path in INVENTORY.glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("backup_path") == f".local/backups/{backup_id}":
            records.append(item)
    if len(records) != 1 or records[0].get("state") != "active":
        raise RuntimeError("N1_RETAINED_BACKUP_RETENTION_RECORD_INVALID")
    return {"backup_id": backup_id, "age_seconds": int(age),
            "cluster_system_identifier": manifest["cluster_system_identifier"],
            "workbench": wb, "target": target, "manifest_sha256": sha(manifest_path)}


def dsn_identity(dsn, expected_db):
    parts = psycopg.conninfo.conninfo_to_dict(dsn)
    if (parts.get("host") != "127.0.0.1" or int(parts.get("port", "0")) != 5541 or
            parts.get("dbname") != expected_db or not parts.get("user")):
        raise RuntimeError("N1_RETAINED_PROFILE_IDENTITY_REFUSED")
    return parts


def query_profile(dsn, expected_db, expected_oid):
    parsed = dsn_identity(dsn, expected_db)
    with psycopg.connect(dsn, connect_timeout=5) as db:
        row = db.execute("""SELECT current_database(),current_user,
          (SELECT oid FROM pg_database WHERE datname=current_database()),
          r.rolsuper,r.rolbypassrls,r.rolcanlogin,r.rolinherit
          FROM pg_roles r WHERE r.rolname=current_user""").fetchone()
        if not row or row[:3] != (expected_db, parsed["user"], expected_oid):
            raise RuntimeError("N1_RETAINED_DATABASE_OR_ROLE_MISMATCH")
        if row[3] or row[4] or not row[5]:
            raise RuntimeError("N1_RETAINED_RUNTIME_ROLE_ATTRIBUTES_INVALID")
        return {"database": row[0], "runtime_role": row[1], "database_oid": row[2],
                "superuser": row[3], "bypassrls": row[4], "can_login": row[5],
                "inherit": row[6]}


def cluster_sysid():
    result = subprocess.run(
        ["docker", "exec", "folio-postgres", "psql", "-X", "-U", "postgres", "-d", "postgres",
         "-v", "ON_ERROR_STOP=1", "-q", "-At", "-c",
         "SELECT system_identifier FROM pg_control_system()"],
        capture_output=True, timeout=30,
    )
    value = result.stdout.decode("ascii", errors="strict").strip()
    if result.returncode or not value.isdecimal():
        raise RuntimeError("N1_RETAINED_CLUSTER_SYSID_UNAVAILABLE")
    return value


def runtime_fingerprint(dsn, role):
    """Hash role attributes, memberships and effective public-object privileges."""
    with psycopg.connect(dsn, connect_timeout=5) as db:
        attrs = db.execute(sql.SQL("""SELECT rolsuper,rolinherit,rolcreaterole,rolcreatedb,
          rolcanlogin,rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil,rolconfig
          FROM pg_roles WHERE rolname={}""").format(sql.Literal(role))).fetchone()
        memberships = db.execute(sql.SQL("""SELECT granted.rolname,m.admin_option
          FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member
          JOIN pg_roles granted ON granted.oid=m.roleid WHERE member.rolname={}
          ORDER BY granted.rolname""").format(sql.Literal(role))).fetchall()
        relation_rows = db.execute(sql.SQL("""SELECT n.nspname,c.relname,c.relkind,
          has_table_privilege({},c.oid,'SELECT'),has_table_privilege({},c.oid,'INSERT'),
          has_table_privilege({},c.oid,'UPDATE'),has_table_privilege({},c.oid,'DELETE'),
          has_table_privilege({},c.oid,'TRUNCATE'),has_table_privilege({},c.oid,'REFERENCES'),
          has_table_privilege({},c.oid,'TRIGGER')
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname NOT IN ('pg_catalog','information_schema')
            AND c.relkind IN ('r','p','v','m','f') ORDER BY n.nspname,c.relname""").format(
                *[sql.Literal(role)] * 7)).fetchall()
        column_rows = db.execute(sql.SQL("""SELECT n.nspname,c.relname,a.attname,
          has_column_privilege({},c.oid,a.attnum,'SELECT'),
          has_column_privilege({},c.oid,a.attnum,'INSERT'),
          has_column_privilege({},c.oid,a.attnum,'UPDATE'),
          has_column_privilege({},c.oid,a.attnum,'REFERENCES')
          FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname NOT IN ('pg_catalog','information_schema')
            AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m','f')
          ORDER BY n.nspname,c.relname,a.attnum""").format(
                *[sql.Literal(role)] * 4)).fetchall()
        sequence_rows = db.execute(sql.SQL("""SELECT n.nspname,c.relname,
          has_sequence_privilege({},c.oid,'USAGE'),has_sequence_privilege({},c.oid,'SELECT'),
          has_sequence_privilege({},c.oid,'UPDATE') FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S'
          AND n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2""").format(
                *[sql.Literal(role)] * 3)).fetchall()
        function_rows = db.execute(sql.SQL("""SELECT n.nspname,p.oid::regprocedure::text,
          has_function_privilege({},p.oid,'EXECUTE') FROM pg_proc p
          JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2""").format(
                sql.Literal(role))).fetchall()
        schema_rows = db.execute(sql.SQL("""SELECT nspname,has_schema_privilege({},oid,'USAGE'),
          has_schema_privilege({},oid,'CREATE') FROM pg_namespace
          WHERE nspname NOT IN ('pg_catalog','information_schema') ORDER BY nspname""").format(
                sql.Literal(role), sql.Literal(role))).fetchall()
        database_row = db.execute(sql.SQL("""SELECT has_database_privilege({},current_database(),'CONNECT'),
          has_database_privilege({},current_database(),'CREATE'),
          has_database_privilege({},current_database(),'TEMP')""").format(
                sql.Literal(role), sql.Literal(role), sql.Literal(role))).fetchone()
    snapshot = {"attributes": attrs, "memberships": memberships,
                "relations": relation_rows, "columns": column_rows,
                "sequences": sequence_rows, "functions": function_rows,
                "schemas": schema_rows, "database": database_row}
    wire = json.dumps(snapshot, sort_keys=True, default=str, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(wire).hexdigest(),
            "relation_count": len(relation_rows), "column_count": len(column_rows),
            "sequence_count": len(sequence_rows), "function_count": len(function_rows),
            "schema_count": len(schema_rows)}


def journal_readback(dsn):
    expected = {int(k): v for k, v in apply008.EXPECTED.items()}
    expected[8] = apply008.MIGRATION_SHA
    with psycopg.connect(dsn, connect_timeout=5) as db:
        rows = db.execute("SELECT version,sha256,installed_at::text FROM public.schema_versions ORDER BY version").fetchall()
        count = db.execute("SELECT count(*) FROM public.schema_versions").fetchone()[0]
        protected = db.execute("""SELECT
          has_table_privilege(current_user,'public.users','UPDATE'),
          has_column_privilege(current_user,'public.users','active','UPDATE'),
          has_column_privilege(current_user,'public.users','n1_export_lock_capability','UPDATE'),
          has_table_privilege(current_user,'public.folio_access_requests','INSERT,UPDATE,DELETE'),
          has_table_privilege(current_user,'public.folio_workspace_access','INSERT,UPDATE,DELETE')""").fetchone()
        cap_meta = db.execute("""SELECT a.attnotnull,a.atthasdef,t.tgenabled,
          t.tgtype::int,p.prosrc,p.prosecdef
          FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
          JOIN pg_trigger t ON t.tgrelid=c.oid AND t.tgname='folio_n1_export_lock_capability_immutable'
          JOIN pg_proc p ON p.oid=t.tgfoid
          WHERE c.oid='public.users'::regclass AND a.attname='n1_export_lock_capability'
            AND NOT a.attisdropped""").fetchone()
    if len(rows) != 6 or count != 6 or [int(r[0]) for r in rows] != [2, 3, 4, 5, 6, 8]:
        raise RuntimeError("N1_RETAINED_JOURNAL_VERSION_MISMATCH")
    if any(expected[int(version)] != digest for version, digest, _ in rows):
        raise RuntimeError("N1_RETAINED_JOURNAL_CHECKSUM_MISMATCH")
    if protected != (False, False, False, False, False):
        raise RuntimeError("N1_RETAINED_RUNTIME_DML_BOUNDARY_INVALID")
    if (not cap_meta or cap_meta[0:3] != (True, True, "O") or
            "N1_EXPORT_LOCK_CAPABILITY_IMMUTABLE" not in cap_meta[4] or cap_meta[5]):
        raise RuntimeError("N1_RETAINED_006_TRIGGER_METADATA_INVALID")
    receipt = required_receipt()
    receipt_journal = receipt["journal_post"]
    for version, digest, installed in rows:
        item = receipt_journal[str(version)]
        if item.get("sha256") != digest or str(item.get("installed_at")) != installed:
            raise RuntimeError("N1_RETAINED_JOURNAL_RECEIPT_MISMATCH")
    return {"versions": [int(r[0]) for r in rows],
            "journal_sha256": hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest(),
            "current_runtime_users_update": False,
            "current_runtime_capability_update": False,
            "006_immutable_trigger_enabled": cap_meta[2] == "O"}


def postgres_admin(database, statement):
    result = subprocess.run(
        ["docker", "exec", "-i", "folio-postgres", "psql", "-X", "-U", "postgres",
         "-d", database, "-v", "ON_ERROR_STOP=1", "-q", "-At"],
        input=statement.encode("utf-8"), capture_output=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("N1_RETAINED_ADMIN_SQL_FAILED")
    return result.stdout.decode("utf-8", errors="replace").strip()


def current_worker_identity(profile_dsn, timeout=25):
    spec_argv, spec_env, spec_cwd, _ = live_supervisor.specs()["exportworker"]
    if (spec_env.get("FOLIO_DSN") != profile_dsn or
            "folio.exportworker" not in spec_argv or "--once" not in spec_argv or
            Path(spec_cwd).resolve() != ROOT or
            spec_env.get("FOLIO_EXPORT_ROOT") != live_supervisor.base_env().get("FOLIO_EXPORT_ROOT")):
        raise RuntimeError("N1_RETAINED_CURRENT_WORKER_SPEC_MISMATCH")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = live_supervisor.read_state()
        alive, heartbeat_age = live_supervisor.supervisor_health(state)
        comp = state.get("components", {}).get("exportworker", {})
        if (alive and heartbeat_age is not None and heartbeat_age <= live_supervisor.HUNG_S and
                live_supervisor.matches(comp.get("pid"), comp.get("created"))):
            try:
                proc = psutil.Process(comp["pid"])
                env = proc.environ()
                cmdline = proc.cmdline()
                if (env.get("FOLIO_DSN") == profile_dsn and
                        "folio.exportworker" in cmdline and "--once" in cmdline):
                    source_mtime = max((ROOT / rel).stat().st_mtime for rel in CURRENT_WORKER_FILES)
                    if proc.create_time() + 1 < source_mtime:
                        raise RuntimeError("N1_RETAINED_WORKER_SOURCE_PREDATES_CURRENT_FILES")
                    return {"pid": proc.pid, "created": proc.create_time(),
                            "supervisor_heartbeat_age_seconds": heartbeat_age,
                            "argv_module_present": True, "dsn_matches_selected_profile": True,
                            "source_mtime_not_newer_than_process": True}
            except (psutil.Error, KeyError):
                pass
        time.sleep(0.25)
    raise RuntimeError("N1_RETAINED_SUPERVISED_EXPORT_WORKER_UNAVAILABLE")


def current_api_identity(profile_dsn):
    state = live_supervisor.read_state()
    alive, heartbeat_age = live_supervisor.supervisor_health(state)
    comp = state.get("components", {}).get("api", {})
    if (not alive or heartbeat_age is None or heartbeat_age > live_supervisor.HUNG_S or
            not live_supervisor.matches(comp.get("pid"), comp.get("created"))):
        raise RuntimeError("N1_RETAINED_CURRENT_API_PROCESS_UNAVAILABLE")
    proc = psutil.Process(comp["pid"])
    env = proc.environ()
    cmdline = proc.cmdline()
    if (env.get("FOLIO_DSN") != profile_dsn or "folio.workbench:app" not in cmdline or
            "8101" not in cmdline):
        raise RuntimeError("N1_RETAINED_CURRENT_API_PROFILE_MISMATCH")
    return {"pid": proc.pid, "created": proc.create_time(),
            "supervisor_heartbeat_age_seconds": heartbeat_age,
            "dsn_matches_selected_profile": True}


def api_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop_owned_api(process, created):
    if process is None or process.poll() is not None:
        return True
    try:
        proc = psutil.Process(process.pid)
        if abs(proc.create_time() - created) > 0.01:
            return False
        proc.terminate()
        process.wait(timeout=10)
    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError, subprocess.TimeoutExpired):
        try:
            proc = psutil.Process(process.pid)
            if abs(proc.create_time() - created) > 0.01:
                return False
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
    return process.poll() is not None


def create_compat_role(database, role, runtime_role, password):
    # The thirty-minute lease is a backstop if the operator process exits abnormally.
    valid_until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    statement = "\n".join([
        "BEGIN;",
        sql.SQL("CREATE ROLE {} LOGIN INHERIT PASSWORD {} VALID UNTIL {}").format(
            sql.Identifier(role), sql.Literal(password), sql.Literal(valid_until)).as_string(),
        sql.SQL("GRANT {} TO {}").format(sql.Identifier(runtime_role), sql.Identifier(role)).as_string(),
        sql.SQL("GRANT UPDATE (n1_export_lock_capability) ON public.users TO {}").format(
            sql.Identifier(role)).as_string(),
        "COMMIT;",
        sql.SQL("SELECT oid FROM pg_roles WHERE rolname={}").format(sql.Literal(role)).as_string(),
    ])
    oid = postgres_admin(database, statement)
    if not oid.isdecimal():
        raise RuntimeError("N1_RETAINED_COMPAT_ROLE_CREATE_FAILED")
    return int(oid)


def recover_compat_role_identity(database, role, runtime_role):
    raw = postgres_admin(database, sql.SQL("""SELECT r.oid||'|'||r.rolcanlogin||'|'||r.rolsuper||'|'||
      r.rolbypassrls||'|'||has_column_privilege(r.rolname,'public.users',
      'n1_export_lock_capability','UPDATE')||'|'||pg_has_role(r.rolname,{},'MEMBER')
      FROM pg_roles r WHERE r.rolname={}""").format(
        sql.Literal(runtime_role), sql.Literal(role)).as_string())
    if not raw:
        return None
    fields = raw.split("|")
    if (len(fields) != 6 or not fields[0].isdecimal() or
            fields[1:] != ["t", "f", "f", "t", "t"]):
        raise RuntimeError("N1_RETAINED_PARTIAL_ROLE_IDENTITY_REFUSED")
    return int(fields[0])


def cleanup_compat_role(database, role, runtime_role, expected_oid):
    identity = postgres_admin(database, sql.SQL("""SELECT oid||'|'||rolcanlogin||'|'||rolsuper||'|'||rolbypassrls
      FROM pg_roles WHERE rolname={}""").format(sql.Literal(role)).as_string())
    if identity != f"{expected_oid}|t|f|f":
        raise RuntimeError("N1_RETAINED_COMPAT_ROLE_IDENTITY_MISMATCH")
    postgres_admin(database, sql.SQL("ALTER ROLE {} NOLOGIN PASSWORD NULL").format(
        sql.Identifier(role)).as_string())
    postgres_admin(database, sql.SQL("""SELECT pg_terminate_backend(pid)
      FROM pg_stat_activity WHERE usename={} AND pid<>pg_backend_pid()""").format(
        sql.Literal(role)).as_string())
    postgres_admin(database, sql.SQL("REVOKE UPDATE (n1_export_lock_capability) ON public.users FROM {}").format(
        sql.Identifier(role)).as_string())
    postgres_admin(database, sql.SQL("REVOKE {} FROM {}").format(
        sql.Identifier(runtime_role), sql.Identifier(role)).as_string())
    postgres_admin(database, sql.SQL("DROP ROLE {}").format(sql.Identifier(role)).as_string())
    absent = postgres_admin(database, sql.SQL(
        "SELECT NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={})"
    ).format(sql.Literal(role)).as_string())
    if absent != "t":
        raise RuntimeError("N1_RETAINED_COMPAT_ROLE_CLEANUP_FAILED")
    return True


def run_api_workflow(api_env, reviewer_password, case, worker_dsn, export_root, report, out):
    port = api_port()
    report["n1_api_port"] = port
    api_log = out / "n1-retained-api.private.log"
    with api_log.open("xb") as log:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "folio.workbench:app",
             "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
            cwd=n1_source.N1, env=api_env, stdout=log, stderr=subprocess.STDOUT,
        )
    created = psutil.Process(process.pid).create_time()
    report["api_process_identity"] = {"pid": process.pid, "created": created}
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15)
    try:
        healthy = False
        for _ in range(60):
            if process.poll() is not None:
                break
            try:
                if client.get("/health").status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        report["n1_health_200"] = healthy
        if not healthy:
            raise RuntimeError("N1_RETAINED_API_NOT_HEALTHY")
        report["retained_database_write_state"] = "login-in-flight"
        login = client.post("/api/v1/auth/login",
            json={"username": "reviewer", "password": reviewer_password},
            headers={"Origin": "http://127.0.0.1:5101"})
        report["n1_login_status"] = login.status_code
        report["retained_database_write_state"] = "login-response-received"
        if login.status_code != 200:
            raise RuntimeError("N1_RETAINED_LOGIN_FAILED")
        report["retained_database_write_state"] = "session-created"
        csrf = client.get("/api/v1/auth/session").json()["csrf"]
        idempotency_key = str(uuid4())
        report["enqueue_idempotency_key"] = idempotency_key
        report["retained_database_write_state"] = "export-enqueue-in-flight"
        enqueue = client.post(
            f"/api/v1/workspaces/{WORKSPACE}/cases/{case[0]}/exports",
            json={"case_version": int(case[1]), "include_originals": False,
                  "redaction_profile": "audit-v1"},
            headers={"Origin": "http://127.0.0.1:5101", "x-csrf-token": csrf,
                     "idempotency-key": idempotency_key},
        )
        report["n1_enqueue_status"] = enqueue.status_code
        if enqueue.status_code != 202:
            report["retained_database_write_state"] = "enqueue-response-not-accepted"
            raise RuntimeError("N1_RETAINED_ENQUEUE_FAILED")
        report["retained_database_write_state"] = "single-export-job-accepted"
        report["single_export_record_preserved"] = True
        job_id = enqueue.json()["artifact_job_id"]
        report["export_job_id"] = job_id
        deadline = time.monotonic() + 120
        job = None
        with psycopg.connect(worker_dsn, connect_timeout=5) as db:
            while time.monotonic() < deadline:
                job = db.execute("""SELECT state,artifact_ref,artifact_sha256,artifact_bytes
                  FROM public.export_jobs WHERE id=%s""", (job_id,)).fetchone()
                if job and job[0] in ("ready", "failed"):
                    break
                time.sleep(0.5)
        report["supervised_worker_job_state"] = job[0] if job else None
        if not job or job[0] != "ready":
            raise RuntimeError("N1_RETAINED_WORKER_DID_NOT_COMPLETE")
        from folio.exports import read_bytes
        old_export_root = os.environ.get("FOLIO_EXPORT_ROOT")
        os.environ["FOLIO_EXPORT_ROOT"] = export_root
        try:
            artifact_bytes = read_bytes(job[1], job[2], job[3])
        finally:
            if old_export_root is None:
                os.environ.pop("FOLIO_EXPORT_ROOT", None)
            else:
                os.environ["FOLIO_EXPORT_ROOT"] = old_export_root
        download = client.get(f"/api/v1/workspaces/{WORKSPACE}/artifacts/{job_id}?download=true")
        digest = hashlib.sha256(artifact_bytes).hexdigest()
        report["download"] = {
            "status": download.status_code,
            "artifact_sha256_db": job[2],
            "artifact_bytes_db": job[3],
            "artifact_sha256_file": digest,
            "artifact_sha256_download": hashlib.sha256(download.content).hexdigest(),
            "download_matches_file": download.content == artifact_bytes,
            "integrity_matches_db": digest == job[2] and len(artifact_bytes) == job[3],
        }
        if (download.status_code != 200 or download.content != artifact_bytes or
                digest != job[2] or len(artifact_bytes) != job[3]):
            raise RuntimeError("N1_RETAINED_DOWNLOAD_INTEGRITY_FAILED")
        return {"process": process, "created": created}
    except Exception:
        report["api_process_stopped_after_failure"] = stop_owned_api(process, created)
        raise
    finally:
        client.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-id", required=True)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ").lower()
    out = secure_private_directory(ROOT / ".local/n1-retained-probe" / stamp)
    report = {"schema": "folio.n1-retained-compat-probe/v1", "status": "running",
              "retained_database_write_state": "not-started",
              "existing_role_grants_claim": "runtime effective privilege fingerprint is measured before/after",
              "business_record_deletion_claim": "operator source contains no delete path",
              "secret_output_claim": "source-reviewed; raw logs and profiles remain private",
              "backup_id": args.backup_id, "source_sha256": {"operator": sha(__file__)}}
    profile = None
    runtime_role = None
    database = None
    compat_role = "folio_n1c_" + stamp.replace("t", "").replace("z", "")[-20:]
    compat_oid = None
    compat_creation_attempted = False
    api_process = None
    api_created = None
    reviewer_password = os.environ.get("FOLIO_N1_REVIEWER_PASSWORD")
    try:
        report["host_lock"] = require_host_lock()
        if not reviewer_password:
            raise RuntimeError("N1_RETAINED_PRIVATE_REVIEWER_PASSWORD_REQUIRED")
        backup = paired_backup(args.backup_id)
        profile = live_supervisor.base_env()
        wb = query_profile(profile["FOLIO_DSN"], EXPECTED_WORKBENCH, EXPECTED_WORKBENCH_OID)
        target = query_profile(profile["FOLIO_TARGET_DSN"], EXPECTED_TARGET, EXPECTED_TARGET_OID)
        database, runtime_role = wb["database"], wb["runtime_role"]
        if cluster_sysid() != backup["cluster_system_identifier"]:
            raise RuntimeError("N1_RETAINED_CLUSTER_SYSTEM_ID_MISMATCH")
        if (backup["workbench"].get("role") != runtime_role or
                backup["target"].get("role") != target["runtime_role"]):
            raise RuntimeError("N1_RETAINED_BACKUP_RUNTIME_ROLE_MISMATCH")
        report["retained_identity"] = {
            "workbench": wb, "target": target,
            "cluster_system_identifier": backup["cluster_system_identifier"],
            "current_role_from_private_supervisor_profile": True,
        }
        report["backup"] = backup
        report["008_retained_journal"] = journal_readback(profile["FOLIO_DSN"])
        report["active_supervised_api_before"] = current_api_identity(profile["FOLIO_DSN"])
        report["n1_source_before"] = n1_source.verify_n1_source_pins()
        if not report["n1_source_before"].get("verified"):
            raise RuntimeError("N1_RETAINED_ARCHIVED_SOURCE_PIN_FAILED")
        report["current_worker_source_before"] = {
            relative: sha(ROOT / relative) for relative in CURRENT_WORKER_FILES}
        worker = current_worker_identity(profile["FOLIO_DSN"])
        report["supervised_worker_before"] = worker
        runtime_before = runtime_fingerprint(profile["FOLIO_DSN"], runtime_role)
        report["runtime_effective_privilege_fingerprint_before"] = runtime_before

        with psycopg.connect(profile["FOLIO_DSN"], connect_timeout=5) as db:
            case = db.execute("""SELECT id::text,body->>'version' FROM public.cases
              WHERE workspace=%s ORDER BY id LIMIT 1""", (WORKSPACE,)).fetchone()
        if not case or not case[1]:
            raise RuntimeError("N1_RETAINED_CASE_FIXTURE_MISSING")
        report["case_id"] = case[0]

        password = secrets.token_urlsafe(32)
        exists = postgres_admin(database, sql.SQL(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={})"
        ).format(sql.Literal(compat_role)).as_string())
        if exists != "f":
            raise RuntimeError("N1_RETAINED_COMPAT_ROLE_ALREADY_EXISTS")
        compat_creation_attempted = True
        compat_oid = create_compat_role(database, compat_role, runtime_role, password)
        report["compat_role_identity"] = {"name": compat_role, "oid": compat_oid,
                                           "created_and_owned_by_probe": True}
        compat_dsn = psycopg.conninfo.make_conninfo(
            profile["FOLIO_DSN"], user=compat_role, password=password)
        with psycopg.connect(compat_dsn, connect_timeout=5) as db:
            grants = db.execute(sql.SQL("""SELECT
              has_column_privilege({c},'public.users','n1_export_lock_capability','UPDATE'),
              has_column_privilege({c},'public.users','active','UPDATE'),
              has_table_privilege({c},'public.users','UPDATE'),
              has_column_privilege({r},'public.users','n1_export_lock_capability','UPDATE'),
              has_table_privilege({r},'public.users','UPDATE')""").format(
                c=sql.Literal(compat_role), r=sql.Literal(runtime_role))).fetchone()
            report["compat_grant_boundary"] = list(grants)
            if grants != (True, False, False, False, False):
                raise RuntimeError("N1_RETAINED_COMPAT_GRANT_BOUNDARY_FAILED")
            locked = db.execute("""SELECT u.username FROM public.sessions s
              JOIN public.users u USING(username)
              WHERE s.token=(SELECT token FROM public.sessions WHERE username='reviewer'
                ORDER BY expires DESC LIMIT 1) FOR SHARE OF s,u""").fetchone()
            report["legacy_joined_row_lock"] = locked is not None
            if locked is None:
                raise RuntimeError("N1_RETAINED_LEGACY_ROW_LOCK_FAILED")
        try:
            with psycopg.connect(compat_dsn, connect_timeout=5) as db:
                db.execute("UPDATE public.users SET n1_export_lock_capability=true WHERE username='reviewer'")
        except psycopg.errors.InsufficientPrivilege as exc:
            report["immutable_trigger_rejected_dml"] = (
                exc.sqlstate == "42501" and
                "N1_EXPORT_LOCK_CAPABILITY_IMMUTABLE" in str(exc))
        if not report.get("immutable_trigger_rejected_dml"):
            raise RuntimeError("N1_RETAINED_IMMUTABLE_TRIGGER_NOT_ENFORCED")

        # Existing supervisor service is verified but never stopped/reconfigured.
        report["current_api_health"] = httpx.get("http://127.0.0.1:8101/health", timeout=3).status_code
        report["target_health"] = httpx.get("http://127.0.0.1:8201/health", timeout=3).status_code
        if report["current_api_health"] != 200 or report["target_health"] != 200:
            raise RuntimeError("N1_RETAINED_CURRENT_SERVICES_UNHEALTHY")

        export_root = profile.get("FOLIO_EXPORT_ROOT")
        if not export_root:
            raise RuntimeError("N1_RETAINED_EXPORT_ROOT_MISSING")
        api_env = {
            **profile,
            "FOLIO_DSN": compat_dsn,
            "FOLIO_SCHEMA_MODE": "runtime",
            "PYTHONPATH": str(n1_source.N1),
            "FOLIO_EXPORT_ROOT": export_root,
            "FOLIO_TARGET_URL": "http://127.0.0.1:8201",
        }
        api_env.pop("FOLIO_N1_REVIEWER_PASSWORD", None)
        flow = run_api_workflow(api_env, reviewer_password, case,
                                profile["FOLIO_DSN"], export_root, report, out)
        api_process, api_created = flow["process"], flow["created"]
        report["runtime_effective_privilege_fingerprint_after_probe"] = runtime_fingerprint(
            profile["FOLIO_DSN"], runtime_role)
        if report["runtime_effective_privilege_fingerprint_after_probe"] != runtime_before:
            raise RuntimeError("N1_RETAINED_RUNTIME_ROLE_FINGERPRINT_CHANGED")
        report["supervised_worker_after"] = current_worker_identity(profile["FOLIO_DSN"])
        report["active_supervised_api_after"] = current_api_identity(profile["FOLIO_DSN"])
        if (report["active_supervised_api_after"]["pid"] !=
                report["active_supervised_api_before"]["pid"] or
                report["active_supervised_api_after"]["created"] !=
                report["active_supervised_api_before"]["created"]):
            raise RuntimeError("N1_RETAINED_CURRENT_API_PROCESS_CHANGED")
        report["status"] = "PASS"
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error_code"] = str(exc) if str(exc).isupper() else "N1_RETAINED_PROBE_FAILED"
        report["error_class"] = type(exc).__name__
        report["error_message_private"] = str(exc)
    finally:
        if api_process is not None:
            try:
                report["api_process_stopped"] = stop_owned_api(api_process, api_created)
            except Exception as cleanup_exc:
                report["api_process_stopped"] = False
                report["api_process_cleanup_error_class"] = type(cleanup_exc).__name__
            if not report["api_process_stopped"]:
                report["status"] = "FAIL"
        if compat_oid is None and compat_creation_attempted:
            try:
                compat_oid = recover_compat_role_identity(database, compat_role, runtime_role)
            except Exception as cleanup_exc:
                report["status"] = "FAIL"
                report["cleanup_error_code"] = (str(cleanup_exc) if str(cleanup_exc).isupper()
                                                 else "N1_RETAINED_COMPAT_IDENTITY_RECOVERY_FAILED")
                report["cleanup_error_class"] = type(cleanup_exc).__name__
        if compat_oid is not None:
            try:
                report["compat_role_cleanup"] = cleanup_compat_role(
                    database, compat_role, runtime_role, compat_oid)
            except Exception as cleanup_exc:
                report["status"] = "FAIL"
                report["cleanup_error_code"] = (str(cleanup_exc) if str(cleanup_exc).isupper()
                                                 else "N1_RETAINED_COMPAT_CLEANUP_FAILED")
                report["cleanup_error_class"] = type(cleanup_exc).__name__
        if profile and runtime_role:
            try:
                report["runtime_effective_privilege_fingerprint_after_cleanup"] = runtime_fingerprint(
                    profile["FOLIO_DSN"], runtime_role)
                if report.get("runtime_effective_privilege_fingerprint_after_cleanup") != report.get(
                        "runtime_effective_privilege_fingerprint_before"):
                    report["status"] = "FAIL"
                    report["runtime_effective_privilege_fingerprint_unchanged"] = False
                else:
                    report["runtime_effective_privilege_fingerprint_unchanged"] = True
            except Exception as cleanup_exc:
                report["status"] = "FAIL"
                report["runtime_fingerprint_error_class"] = type(cleanup_exc).__name__
        if runtime_role and profile:
            try:
                report["n1_source_after"] = n1_source.verify_n1_source_pins()
                report["n1_source_unchanged"] = report.get("n1_source_before") == report["n1_source_after"]
                if not report["n1_source_unchanged"]:
                    report["status"] = "FAIL"
            except Exception as cleanup_exc:
                report["n1_source_after_error_class"] = type(cleanup_exc).__name__
                report["status"] = "FAIL"
            try:
                report["current_worker_source_after"] = {
                    relative: sha(ROOT / relative) for relative in CURRENT_WORKER_FILES}
                report["current_worker_source_unchanged"] = (
                    report.get("current_worker_source_before") == report["current_worker_source_after"])
                if not report["current_worker_source_unchanged"]:
                    report["status"] = "FAIL"
            except Exception as cleanup_exc:
                report["current_worker_source_after_error_class"] = type(cleanup_exc).__name__
                report["status"] = "FAIL"
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        path = out / "report.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        secure_private_file(path, inherited=True)
        print(json.dumps({"status": report["status"], "report": str(path.relative_to(ROOT)),
                          "retained_database_write_state": report["retained_database_write_state"],
                          "compat_role_cleaned": report.get("compat_role_cleanup") is True,
                          "runtime_effective_privilege_fingerprint_unchanged": report.get(
                              "runtime_effective_privilege_fingerprint_unchanged")}))
    return 0 if report["status"] == "PASS" and report.get("compat_role_cleanup") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
