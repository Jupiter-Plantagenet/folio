"""Restore the encrypted Folio 008 safety copy into an owned scratch container.

Never connects to a retained database for writes. The only ownership change and
migration install in this program target the newly created scratch clone.
"""
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from runtime import environment  # noqa: E402

from folio.backup import open_backup  # noqa: E402
from folio.private_storage import secure_private_directory, secure_private_file  # noqa: E402

sys.path.insert(0, str(ROOT.parent / "tools"))
import round5_migrate as n1_compat  # noqa: E402
from round6_apply_008 import MIGRATION_SHA, build_apply_transaction  # noqa: E402

IMAGE = "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
PORT = 15548
EXPECTED_002_006 = {
    "2": "128edf371b46411bbfe3800e6b5088c51e88cfc284d398fa6540933711736801",
    "3": "93dd4063c5d6538f79648a3ca50efdbc3fa7196442a5b3cc80e35c8e50cf1d63",
    "4": "59ed4f421f4332cafb0325f260baac6ffa1faf07f3e17f4c2a5f843f18ac51ad",
    "5": "9ac766dae0467db4823bf663516c912c92bcd5ec294ada8dcd227e2625b74461",
    "6": "ee0bcc7ca7dba28ffeb31f60efd46ddb1145ce4286f126a88e09a2f0b1a7af08",
}


def gate():
    import shutil

    import psutil
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory).PercentCommittedBytesInUse"],
        capture_output=True, text=True, timeout=15)
    try:
        commit = float(result.stdout.strip()) / 100
    except (ValueError, TypeError):
        commit = 1.0
    return {"free_c_bytes": shutil.disk_usage("C:\\").free,
            "available_ram_bytes": psutil.virtual_memory().available,
            "system_commit_fraction": commit}


def admitted(report):
    check = gate()
    check["admitted"] = (check["free_c_bytes"] >= 50 * 1024**3 and
                         check["available_ram_bytes"] >= 6 * 1024**3 and
                         check["system_commit_fraction"] < .85)
    report["resource_checks"].append(check)
    if not check["admitted"]:
        raise RuntimeError("RESOURCE_GATE_NOT_MET")


def run(args, *, data=None, timeout=180):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("SCRATCH_COMMAND_FAILED")
    return result.stdout


def psql(container, statement, database="postgres"):
    return run(["docker", "exec", "-i", container, "psql", "-U", "postgres",
                "-d", database, "-v", "ON_ERROR_STOP=1", "-q", "-At"],
               data=statement.encode(), timeout=120).decode().strip()


def psql_injected_failure(container, statement, database, error_path):
    result = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "postgres",
        "-d", database, "-v", "ON_ERROR_STOP=1", "-q", "-At"],
        input=statement.encode(), capture_output=True, timeout=120)
    error_path.write_bytes(result.stderr)
    secure_private_file(error_path, inherited=True)
    if result.returncode == 0 or b"INJECTED_POSTCONDITION_FAILURE" not in result.stderr:
        raise RuntimeError("INJECTED_ATOMIC_ROLLBACK_DID_NOT_FIRE")
    return hashlib.sha256(statement.encode()).hexdigest()


def access_objects_absent(db):
    return db.execute("""SELECT to_regclass('public.folio_access_requests') IS NULL
      AND to_regclass('public.folio_workspace_access') IS NULL
      AND NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='folio_access_admin_v1')""").fetchone()[0]


def journal(db):
    rows = db.execute("SELECT version,sha256,installed_at::text FROM public.schema_versions ORDER BY version").fetchall()
    return {str(version): {"sha256": digest, "installed_at": installed}
            for version, digest, installed in rows}


def journal_table_acl(db):
    return db.execute("""SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'grantee',COALESCE(gr.rolname,'PUBLIC'),'privilege',a.privilege_type,
      'grantable',a.is_grantable) ORDER BY COALESCE(gr.rolname,'PUBLIC'),
      a.privilege_type,a.is_grantable),'[]'::jsonb)
      FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
      LEFT JOIN pg_roles gr ON gr.oid=a.grantee
      WHERE c.oid='public.schema_versions'::regclass AND a.grantee<>c.relowner""").fetchone()[0]


def all_counts(db):
    names = db.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    return {name: db.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(name))).fetchone()[0]
            for (name,) in names}


def n1_metadata(db, runtime_role):
    row = db.execute(sql.SQL("""SELECT
      a.attnotnull,a.atthasdef,
      t.tgenabled,t.tgtype::int,t.tgattr::text,
      p.prosrc,p.prosecdef,p.proconfig,
      COALESCE(bool_or(pa.grantee=0 AND pa.privilege_type='EXECUTE'),false),
      pg_get_userbyid(c.relowner),pg_get_userbyid(p.proowner)
      FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
      JOIN pg_trigger t ON t.tgrelid=c.oid AND t.tgname='folio_n1_export_lock_capability_immutable'
      JOIN pg_proc p ON p.oid=t.tgfoid
      LEFT JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) pa ON true
      WHERE a.attrelid='public.users'::regclass AND a.attname='n1_export_lock_capability'
        AND NOT a.attisdropped GROUP BY a.attnotnull,a.atthasdef,t.tgenabled,t.tgtype,t.tgattr,
          p.prosrc,p.prosecdef,p.proconfig,c.relowner,p.proowner""")).fetchone()
    return row[:5] + (hashlib.sha256(row[5].encode()).hexdigest(),) + row[6:]


def n1_runtime_column_grant(db, runtime_role):
    return db.execute(sql.SQL("SELECT has_column_privilege({},'public.users','n1_export_lock_capability','UPDATE')").format(
        sql.Literal(runtime_role))).fetchone()[0]


def run_post008_n1_export_probe(runtime_role, out, private_out, stamp):
    """Exercise archived N-1 through a disposable, column-scoped login on the clone."""
    compat_role = "folio_n1c_" + re.sub(r"[^a-z0-9]", "", stamp.lower())[-18:]
    export_root = secure_private_directory(private_out / "n1-post008-exports")
    report = {
        "compat_role": compat_role,
        "runtime_role_acl_comparison": "NOT_MEASURED",
        "runtime_role_privileges_measured": "only users capability/active/table UPDATE boundary",
    }
    api_process = None
    api_log_handle = None
    role_created = False
    try:
        report["n1_source_before"] = n1_compat.verify_n1_source_pins()
        if not report["n1_source_before"].get("verified"):
            raise RuntimeError("N1_SOURCE_PIN_CHECK_FAILED")

        dsn = psycopg.conninfo.make_conninfo(
            host="127.0.0.1", port=PORT, dbname="folio_008_workbench", user="postgres"
        )
        with psycopg.connect(dsn, autocommit=True) as admin_db:
            admin_db.execute(sql.SQL("CREATE ROLE {} LOGIN INHERIT").format(
                sql.Identifier(compat_role)))
            role_created = True
            admin_db.execute(sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(runtime_role), sql.Identifier(compat_role)))
            admin_db.execute(sql.SQL(
                "GRANT UPDATE (n1_export_lock_capability) ON public.users TO {}"
            ).format(sql.Identifier(compat_role)))

        compat_dsn = psycopg.conninfo.make_conninfo(dsn, user=compat_role)
        with psycopg.connect(compat_dsn) as db:
            boundary = db.execute(sql.SQL("""SELECT
              has_column_privilege({compat},'public.users','n1_export_lock_capability','UPDATE'),
              has_column_privilege({compat},'public.users','active','UPDATE'),
              has_table_privilege({compat},'public.users','UPDATE'),
              has_column_privilege({runtime},'public.users','n1_export_lock_capability','UPDATE'),
              has_table_privilege({runtime},'public.users','UPDATE')""").format(
                compat=sql.Literal(compat_role), runtime=sql.Literal(runtime_role))).fetchone()
            report["grant_boundary"] = {
                "compat_capability_column_update": boundary[0],
                "compat_active_column_update": boundary[1],
                "compat_table_update": boundary[2],
                "current_runtime_capability_column_update": boundary[3],
                "current_runtime_table_update": boundary[4],
            }
            if boundary != (True, False, False, False, False):
                raise RuntimeError("N1_COMPAT_PRIVILEGE_BOUNDARY_FAILED")
            lock_row = db.execute(sql.SQL("""SELECT u.username,s.token
              FROM public.sessions s JOIN public.users u USING(username)
              WHERE s.token=(SELECT token FROM public.sessions WHERE username='reviewer'
                ORDER BY expires DESC LIMIT 1) FOR SHARE OF s,u""")).fetchone()
            report["legacy_joined_row_lock"] = lock_row is not None
            if lock_row is None:
                raise RuntimeError("N1_COMPAT_ROW_LOCK_FAILED")

        try:
            with psycopg.connect(compat_dsn) as db:
                db.execute("UPDATE public.users SET n1_export_lock_capability=true WHERE username='reviewer'")
        except psycopg.errors.InsufficientPrivilege as exc:
            report["immutable_trigger_rejected_dml"] = (
                exc.sqlstate == "42501"
                and
                "N1_EXPORT_LOCK_CAPABILITY_IMMUTABLE" in str(exc)
            )
        if not report.get("immutable_trigger_rejected_dml"):
            raise RuntimeError("N1_COMPAT_IMMUTABLE_TRIGGER_NOT_ENFORCED")

        api_port = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            api_port = sock.getsockname()[1]
        api_env = {
            **n1_compat.base_env(),
            "FOLIO_DSN": compat_dsn,
            "FOLIO_SCHEMA_MODE": "runtime",
            "PYTHONPATH": str(n1_compat.N1),
            "FOLIO_EXPORT_ROOT": str(export_root.resolve()),
            "FOLIO_TARGET_URL": "http://127.0.0.1:9291",
        }
        api_log = private_out / "n1-post008-api.log"
        api_log_handle = api_log.open("w", encoding="utf-8")
        api_process = subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "folio.workbench:app",
             "--host", "127.0.0.1", "--port", str(api_port)],
            cwd=n1_compat.N1, env=api_env, stdout=api_log_handle, stderr=subprocess.STDOUT,
        )
        client = httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=15)
        try:
            healthy = False
            for _ in range(60):
                if api_process.poll() is not None:
                    break
                try:
                    if client.get("/health").status_code == 200:
                        healthy = True
                        break
                except httpx.HTTPError:
                    time.sleep(0.5)
            report["n1_health_200"] = healthy
            if not healthy:
                raise RuntimeError("N1_COMPAT_API_NOT_HEALTHY")
            login = client.post("/api/v1/auth/login",
                json={"username": "reviewer", "password": "folio-reviewer-local"},
                headers={"Origin": "http://127.0.0.1:5101"})
            report["n1_login_status"] = login.status_code
            if login.status_code != 200:
                raise RuntimeError("N1_COMPAT_LOGIN_FAILED")
            csrf = client.get("/api/v1/auth/session").json()["csrf"]
            with psycopg.connect(dsn) as db:
                case = db.execute("""SELECT id::text,body->>'version' FROM public.cases
                  WHERE workspace='11111111-1111-4111-8111-111111111111'
                  ORDER BY id LIMIT 1""").fetchone()
            if not case or not case[1]:
                raise RuntimeError("N1_COMPAT_CASE_FIXTURE_MISSING")
            enqueue = client.post(
                "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/"
                f"cases/{case[0]}/exports",
                json={"case_version": int(case[1]), "include_originals": False,
                      "redaction_profile": "audit-v1"},
                headers={"Origin": "http://127.0.0.1:5101", "x-csrf-token": csrf,
                         "idempotency-key": str(uuid4())},
            )
            report["n1_export_enqueue_status"] = enqueue.status_code
            if enqueue.status_code != 202:
                raise RuntimeError("N1_EXPORT_ENQUEUE_COMPATIBILITY_FAILED")
            job_id = enqueue.json()["artifact_job_id"]
            report["n1_export_job_id"] = job_id
            runtime_dsn = psycopg.conninfo.make_conninfo(dsn, user=runtime_role)
            worker_env = {**api_env, "FOLIO_DSN": runtime_dsn, "PYTHONPATH": str(ROOT)}
            worker = subprocess.run(
                [sys.executable, "-B", "-m", "folio.exportworker", "--once", "--job", job_id],
                cwd=ROOT, env=worker_env, capture_output=True, text=True, timeout=90,
            )
            (private_out / "n1-post008-worker.log").write_text(
                worker.stdout + worker.stderr, encoding="utf-8")
            report["worker_exit"] = worker.returncode
            if worker.returncode:
                raise RuntimeError("N1_EXPORT_WORKER_FAILED")
            with psycopg.connect(dsn) as db:
                artifact = db.execute("""SELECT state,artifact_ref,artifact_sha256,artifact_bytes
                  FROM public.export_jobs WHERE id=%s""", (job_id,)).fetchone()
            if not artifact or artifact[0] != "ready":
                raise RuntimeError("N1_EXPORT_ARTIFACT_NOT_READY")
            artifact_path = export_root / artifact[1]
            body = artifact_path.read_bytes()
            downloaded = client.get(
                f"/api/v1/workspaces/11111111-1111-4111-8111-111111111111/artifacts/{job_id}?download=true")
            digest = hashlib.sha256(body).hexdigest()
            report["download"] = {
                "status": downloaded.status_code,
                "artifact_sha256_db": artifact[2],
                "artifact_bytes_db": artifact[3],
                "artifact_sha256_file": digest,
                "artifact_sha256_download": hashlib.sha256(downloaded.content).hexdigest(),
                "download_matches_file": downloaded.content == body,
                "integrity_matches_db": digest == artifact[2] and len(body) == artifact[3],
            }
            if (downloaded.status_code != 200 or downloaded.content != body or
                    digest != artifact[2] or len(body) != artifact[3]):
                raise RuntimeError("N1_EXPORT_DOWNLOAD_OR_INTEGRITY_FAILED")
        finally:
            client.close()
        report["n1_source_after"] = n1_compat.verify_n1_source_pins()
        report["n1_source_unchanged"] = report["n1_source_before"] == report["n1_source_after"]
        if not report["n1_source_unchanged"]:
            raise RuntimeError("N1_SOURCE_CHANGED_DURING_DRILL")
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error_code"] = str(exc) if str(exc).isupper() else "N1_POST008_PROBE_FAILED"
        report["error_class"] = type(exc).__name__
        report["error_message_private"] = str(exc)
        raise
    finally:
        if api_process is not None:
            if api_process.poll() is None:
                api_process.terminate()
                try:
                    api_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    api_process.kill()
                    api_process.wait(timeout=10)
        if api_log_handle is not None:
            api_log_handle.close()
        if role_created:
            with psycopg.connect(dsn, autocommit=True) as admin_db:
                identity = admin_db.execute(sql.SQL("""SELECT rolcanlogin,rolsuper,rolbypassrls
                  FROM pg_roles WHERE rolname={}""").format(sql.Literal(compat_role))).fetchone()
                if identity != (True, False, False):
                    raise RuntimeError("OWNED_COMPAT_ROLE_IDENTITY_MISMATCH")
                admin_db.execute(sql.SQL("REVOKE UPDATE (n1_export_lock_capability) ON public.users FROM {}").format(
                    sql.Identifier(compat_role)))
                admin_db.execute(sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(runtime_role), sql.Identifier(compat_role)))
                admin_db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(compat_role)))
                report["compat_role_cleanup"] = admin_db.execute(sql.SQL(
                    "SELECT NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={})"
                ).format(sql.Literal(compat_role))).fetchone()[0]
        report_path = out / "n1-post008-probe.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        secure_private_file(report_path, inherited=True)
    return report


def legacy_definer_contract(db):
    rows = db.execute("""SELECT p.oid::regprocedure::text,p.proname,
      pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig,p.prosrc,
      COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'grantee',COALESCE(gr.rolname,'PUBLIC'),'grantor',gt.rolname,
        'privilege',a.privilege_type,'grantable',a.is_grantable)
        ORDER BY COALESCE(gr.rolname,'PUBLIC'),gt.rolname,a.privilege_type,a.is_grantable)
        FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        LEFT JOIN pg_roles gr ON gr.oid=a.grantee
        LEFT JOIN pg_roles gt ON gt.oid=a.grantor),'[]'::jsonb)
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='public' AND p.prosecdef ORDER BY p.oid::regprocedure::text""").fetchall()
    return {name: {"function": name, "proname": proname, "owner": owner,
                   "security_definer": secdef, "config": config,
                   "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                   "acl": acl}
            for name, proname, owner, secdef, config, body, acl in rows}


def source_owner_prerequisites(container, dbname, owner):
    query = sql.SQL("""SELECT r.rolsuper,r.rolbypassrls,r.rolcanlogin,
      has_schema_privilege({o},'public','USAGE'),has_schema_privilege({o},'public','CREATE'),
      has_table_privilege({o},'public.users','SELECT,INSERT,UPDATE,DELETE'),
      has_table_privilege({o},'public.sessions','SELECT,INSERT,UPDATE,DELETE'),
      has_table_privilege({o},'public.events','INSERT'),
      has_table_privilege({o},'public.commands','SELECT,INSERT,UPDATE'),
      has_table_privilege({o},'public.schema_versions','SELECT,INSERT,UPDATE'),
      NOT EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles elevated ON elevated.oid=m.roleid
        WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname={o}) AND
          (elevated.rolsuper OR elevated.rolbypassrls OR elevated.rolcreaterole OR elevated.rolcreatedb OR elevated.rolreplication))
      FROM pg_roles r WHERE r.rolname={o}""").format(o=sql.Literal(owner)).as_string()
    row = psql(container, query, dbname).split("|")
    ready = (len(row) == 11 and row[0:2] == ["f", "f"] and
             row[3:9] == ["t", "t", "t", "t", "t", "t"] and row[9:] == ["f", "t"])
    return {"fields": ["superuser", "bypassrls", "canlogin", "public_usage", "public_create",
                        "users_dml", "sessions_dml", "events_insert", "commands_dml", "journal_dml",
                        "elevated_role_membership"],
            "values": row, "ready_except_journal_ownership": ready}


def source_database_acl(container, dbname):
    query = """SELECT COALESCE(r.rolname,'PUBLIC'),a.privilege_type,a.is_grantable
      FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a
      LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE d.datname='""" + dbname + "' ORDER BY 1,2"
    rows = psql(container, query).splitlines()
    entries = [line.split("|") for line in rows if line]
    sql_lines = [sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier("folio_008_" + ("target" if "target" in dbname else "workbench"))).as_string()]
    dest = "folio_008_" + ("target" if "target" in dbname else "workbench")
    for role, privilege, grantable in entries:
        if role == "PUBLIC":
            grantee = sql.SQL("PUBLIC")
        else:
            grantee = sql.Identifier(role)
        phrase = {"CONNECT": "CONNECT", "CREATE": "CREATE",
                  "TEMP": "TEMPORARY", "TEMPORARY": "TEMPORARY"}.get(privilege)
        if phrase:
            line = sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                sql.SQL(phrase), sql.Identifier(dest), grantee,
                sql.SQL(" WITH GRANT OPTION") if grantable == "t" else sql.SQL("")).as_string()
            sql_lines.append(line)
    return entries, ";\n".join(sql_lines) + ";"


def strip_postgres_role_statements(globals_raw):
    """Keep global roles/ACLs but avoid changing scratch's bootstrap postgres."""
    text = globals_raw.decode("utf-8")
    out = []
    for line in text.splitlines(keepends=True):
        if re.match(r'^\s*(?:CREATE|ALTER) ROLE\s+(?:"postgres"|postgres)(?:\s|;)', line, re.I):
            continue
        out.append(line)
    return "".join(out).encode("utf-8")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: folio_008_scratch_rehearsal.py BACKUP_ID")
    backup_id = sys.argv[1]
    if not re.fullmatch(r"\d{8}t\d{6}\d{6}z", backup_id):
        raise SystemExit("BACKUP_ID_INVALID")
    backup = ROOT / ".local/access-008-safety" / backup_id
    keys = ROOT / ".local/backup-keys/access-008" / backup_id
    capture = json.loads((backup / "report.json").read_text(encoding="utf-8"))
    if capture.get("status") != "encrypted_online_safety_copy_complete" or capture.get("paired_consistent") is not False:
        raise SystemExit("SAFETY_BACKUP_NOT_AUTHENTICATED_OR_SCOPE_INVALID")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ").lower()
    scratch = "folio-008-scratch-" + stamp
    volume = "folio-008-volume-" + stamp
    out = secure_private_directory(ROOT / ".local/access-rehearsal" / ("activation-" + stamp))
    report = {"schema": "folio.access-008-scratch-rehearsal/v1", "backup_id": backup_id,
              "scratch_container": scratch, "scratch_volume": volume,
              "loopback_port": PORT, "retained_write_connection": False,
              "retained_databases_changed": False, "retained_accounts_changed": False,
              "source_unpaired_safety_copy": True, "resource_checks": [],
              "status": "running"}
    report["source_sha256"] = {
        "scratch_helper": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "migration_008": hashlib.sha256((ROOT / "ops/migrations/008-access-administration.sql").read_bytes()).hexdigest(),
        "apply_helper": hashlib.sha256((ROOT / "scripts/round6_apply_008.py").read_bytes()).hexdigest(),
    }
    created = False
    volume_created = False
    try:
        report["phase"] = "preflight"
        profile = environment("browser")
        src_wb = urlsplit(profile["FOLIO_DSN"]).path.lstrip("/")
        src_target = urlsplit(profile["FOLIO_TARGET_DSN"]).path.lstrip("/")
        runtime_role = urlsplit(profile["FOLIO_DSN"]).username
        if src_wb != "folio_browser_wb_20261026080908" or src_target != "folio_browser_target_20261026080908":
            raise RuntimeError("RETAINED_DATABASE_IDENTITY_REFUSED")
        # Read-only catalog mapping. No credential or profile value enters evidence.
        owners = {}
        for label, name in (("workbench", src_wb), ("target", src_target)):
            query = ("SELECT pg_get_userbyid(datdba), (SELECT rolsuper FROM pg_roles WHERE oid=datdba), "
                     "(SELECT rolbypassrls FROM pg_roles WHERE oid=datdba), "
                     "has_database_privilege(pg_get_userbyid(datdba),current_database(),'CONNECT') "
                     f"FROM pg_database WHERE datname='{name}'")
            row = run(["docker", "exec", "folio-postgres", "psql", "-U", "postgres", "-d", "postgres",
                       "-v", "ON_ERROR_STOP=1", "-q", "-At", "-F", "|", "-c", query]).decode().strip().split("|")
            if len(row) != 4 or row[1:3] != ["f", "f"]:
                raise RuntimeError("DATABASE_OWNER_NOT_NARROW")
            owners[label] = row[0]
        report["database_owners"] = owners
        report["runtime_role"] = runtime_role
        report["owner_preflight_scope"] = "evaluated against restored pre-008 scratch clone"
        image_id = run(["docker", "inspect", "folio-postgres", "--format", "{{.Image}}"]
                       ).decode().strip()
        if image_id != IMAGE:
            raise RuntimeError("POSTGRES_IMAGE_PIN_MISMATCH")
        admitted(report)
        run(["docker", "volume", "create", volume])
        volume_created = True
        run(["docker", "run", "-d", "--name", scratch, "--memory=1g", "--cpus=2",
             "-p", f"127.0.0.1:{PORT}:5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
             "--mount", f"source={volume},target=/var/lib/postgresql/data", IMAGE])
        created = True
        for _ in range(40):
            probe = subprocess.run(["docker", "exec", scratch, "pg_isready", "-U", "postgres"],
                                   capture_output=True, timeout=5)
            if probe.returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("SCRATCH_POSTGRES_NOT_READY")
        global_raw = open_backup(backup / "globals-no-passwords.sql.enc",
                                 keys / "globals-no-passwords.key",
                                 f"access-008-safety:{backup_id}:globals")
        globals_scratch = strip_postgres_role_statements(global_raw)
        report["phase"] = "restore_globals"
        admitted(report)
        psql(scratch, globals_scratch.decode("utf-8"))
        report["globals_restored"] = True
        dump_by_label = {row["label"]: row for row in capture["database_records"]}
        before_counts = {}
        before_journal = {}
        before_journal_acl = None
        for label, source_db in (("target", src_target), ("workbench", src_wb)):
            record = dump_by_label["FOLIO_TARGET_DSN" if label == "target" else "FOLIO_DSN"]
            dump = open_backup(backup / f"{source_db}.dump.enc", keys / f"{source_db}.key",
                               f"access-008-safety:{backup_id}:{source_db}")
            if hashlib.sha256(dump).hexdigest() != record["plaintext_sha256"]:
                raise RuntimeError("BACKUP_DUMP_HASH_MISMATCH")
            clone = "folio_008_" + label
            report["phase"] = "restore_" + label
            owner = owners[label]
            psql(scratch, sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(clone),
                 sql.Identifier(owner)).as_string())
            acl_rows, acl_sql = source_database_acl("folio-postgres", source_db)
            psql(scratch, acl_sql)
            report.setdefault("database_acls", {})[label] = acl_rows
            admitted(report)
            result = subprocess.run(["docker", "exec", "-i", scratch, "pg_restore", "-U", "postgres",
                                     "--exit-on-error", "-d", clone], input=dump,
                                    capture_output=True, timeout=180)
            if result.returncode:
                raise RuntimeError("SCRATCH_PG_RESTORE_FAILED")
            with psycopg.connect(host="127.0.0.1", port=PORT, dbname=clone, user="postgres") as db:
                before_counts[label] = all_counts(db)
                if label == "workbench":
                    before_journal = journal(db)
                    before_journal_acl = journal_table_acl(db)
                    n1_before = n1_metadata(db, runtime_role)
                    n1_column_grant_before = n1_runtime_column_grant(db, runtime_role)
                    legacy_definers_before = legacy_definer_contract(db)
                    report["006_metadata_before"] = list(n1_before)
                    if {k: v["sha256"] for k, v in before_journal.items()} != EXPECTED_002_006:
                        raise RuntimeError("PREDECESSOR_JOURNAL_MISMATCH")
                    if "8" in before_journal:
                        raise RuntimeError("MIGRATION_008_ALREADY_PRESENT_IN_BACKUP")
        owner_preflight = source_owner_prerequisites(
            scratch, "folio_008_workbench", owners["workbench"])
        report["workbench_owner_prerequisites"] = owner_preflight
        if not owner_preflight["ready_except_journal_ownership"]:
            raise RuntimeError("SCRATCH_DATABASE_OWNER_PREREQUISITES_FAILED")
        report["pre_migration_journal"] = before_journal
        report["pre_migration_journal_acl"] = before_journal_acl
        report["restored_table_counts"] = before_counts
        admitted(report)
        report["phase"] = "injected_atomic_rollback"
        raw = (ROOT / "ops/migrations/008-access-administration.sql").read_bytes()
        if hashlib.sha256(raw).hexdigest() != MIGRATION_SHA:
            raise RuntimeError("MIGRATION_008_SOURCE_PIN_MISMATCH")
        # Exercise a failure after every postcondition, but before COMMIT, using
        # the exact transaction builder that will perform the positive rehearsal.
        negative_payload = build_apply_transaction(
            "folio_008_workbench", owners["workbench"], runtime_role, raw, inject_failure=True)
        rollback_error = out / "injected-rollback.private.stderr.log"
        report["negative_transaction_payload_sha256"] = psql_injected_failure(
            scratch, negative_payload, "folio_008_workbench", rollback_error)
        with psycopg.connect(host="127.0.0.1", port=PORT, dbname="folio_008_workbench", user="postgres") as db:
            rollback_journal = journal(db)
            rollback_acl = journal_table_acl(db)
            rollback_counts = all_counts(db)
            rollback_owner = db.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.schema_versions'::regclass").fetchone()[0]
            if (rollback_owner != "postgres" or rollback_journal != before_journal or
                    rollback_acl != before_journal_acl or rollback_counts != before_counts["workbench"] or
                    not access_objects_absent(db)):
                raise RuntimeError("ATOMIC_ROLLBACK_STATE_CHANGED")
        report["injected_failure_rollback"] = {
            "fired_after_all_in_transaction_postconditions": True,
            "journal_owner_unchanged": rollback_owner == "postgres",
            "journal_rows_timestamps_unchanged": rollback_journal == before_journal,
            "journal_acl_unchanged": rollback_acl == before_journal_acl,
            "all_preexisting_public_rowcounts_unchanged": rollback_counts == before_counts["workbench"],
            "008_objects_absent": True,
        }
        report["phase"] = "install_008_atomic_positive"
        admitted(report)
        positive_payload = build_apply_transaction(
            "folio_008_workbench", owners["workbench"], runtime_role, raw)
        report["positive_transaction_payload_sha256"] = hashlib.sha256(positive_payload.encode()).hexdigest()
        transaction_output = psql(scratch, positive_payload, "folio_008_workbench")
        receipt = json.loads(transaction_output.splitlines()[-1])
        if receipt.get("status") != "ATOMIC_ASSERTIONS_PASSED" or receipt.get("journal_8_sha256") != MIGRATION_SHA:
            raise RuntimeError("ATOMIC_SQL_RECEIPT_INVALID")
        report["journal_owner_transfer"] = {"table": "public.schema_versions",
             "from": "postgres", "to": owners["workbench"], "scope": "scratch clone only"}
        report["all_critical_postconditions_inside_transaction"] = True
        with psycopg.connect(host="127.0.0.1", port=PORT, dbname="folio_008_workbench", user="postgres") as db:
            after_journal = journal(db)
            after_journal_acl = journal_table_acl(db)
            old_unchanged = all(after_journal.get(k) == v for k, v in before_journal.items())
            if (not old_unchanged or before_journal_acl != after_journal_acl or
                    set(after_journal) != set(before_journal) | {"8"}):
                raise RuntimeError("JOURNAL_PRESERVATION_FAILED")
            role = runtime_role
            privileged = db.execute(sql.SQL("""SELECT
              NOT has_table_privilege({r},'public.users','INSERT,UPDATE,DELETE,TRUNCATE')
              AND NOT has_table_privilege({r},'public.folio_access_requests','INSERT,UPDATE,DELETE,TRUNCATE')
              AND NOT has_table_privilege({r},'public.folio_workspace_access','INSERT,UPDATE,DELETE,TRUNCATE')
              AND NOT EXISTS(SELECT 1 FROM (VALUES ('public.users'),('public.folio_access_requests'),
                ('public.folio_workspace_access')) AS t(rel) WHERE
                has_any_column_privilege({r},rel,'INSERT') OR has_any_column_privilege({r},rel,'UPDATE'))
              AND has_schema_privilege({r},'folio_access_admin_v1','USAGE')
              AND has_function_privilege({r},'folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid)','EXECUTE')
              AND has_function_privilege({r},'folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)','EXECUTE')""").format(r=sql.Literal(role))).fetchone()[0]
            functions = db.execute("SELECT p.proname,pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='folio_access_admin_v1' ORDER BY p.proname").fetchall()
            pub_exec = db.execute("SELECT COALESCE(bool_or(a.grantee=0 AND a.privilege_type='EXECUTE'),false) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace LEFT JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a ON true WHERE n.nspname='folio_access_admin_v1'").fetchone()[0]
            schema_owner = db.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='folio_access_admin_v1'").fetchone()[0]
            definer_ok = len(functions) == 3 and all(
                owner == owners["workbench"] and secdef and
                set(config or []) == {"search_path=pg_catalog, pg_temp", "lock_timeout=5s"}
                for _, owner, secdef, config in functions)
            if not privileged or pub_exec or schema_owner != owners["workbench"] or not definer_ok:
                raise RuntimeError("RUNTIME_GRANTS_OR_FUNCTION_OWNER_FAILED")
            report["phase"] = "runtime_function_probe"
            with db.transaction():
                db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                probe = db.execute("SELECT folio_access_admin_v1.authorize_mutation('invalid-probe','invalid-probe',"
                                    "'11111111-1111-4111-8111-111111111111','create',NULL)").fetchone()[0]
                db.execute("RESET ROLE")
            execute_probe = isinstance(probe, dict) and probe.get("error") == "SESSION_REQUIRED"
            if not execute_probe:
                raise RuntimeError("RUNTIME_SECURITY_DEFINER_EXECUTION_FAILED")
            post_counts = all_counts(db)
            if any(post_counts.get(k) != (v + 1 if k == "schema_versions" else v)
                   for k, v in before_counts["workbench"].items()):
                raise RuntimeError("WORKBENCH_TABLE_COUNTS_CHANGED")
            n1_after = n1_metadata(db, runtime_role)
            n1_column_grant_after = n1_runtime_column_grant(db, runtime_role)
            legacy_definers_after = legacy_definer_contract(db)
            report["006_metadata_after"] = list(n1_after)
            if n1_after != n1_before:
                raise RuntimeError("N1_006_METADATA_OR_ACL_CHANGED")
        with psycopg.connect(host="127.0.0.1", port=PORT, dbname="folio_008_target", user="postgres") as db:
            if all_counts(db) != before_counts["target"]:
                raise RuntimeError("TARGET_TABLE_COUNTS_CHANGED")
        report["post_migration_journal"] = after_journal
        report["post_migration_journal_acl"] = after_journal_acl
        report["journal_acl_grants_unchanged"] = before_journal_acl == after_journal_acl
        report["002_006_journal_rows_and_timestamps_unchanged"] = old_unchanged
        report["006_n1_requester_lock_metadata_unchanged"] = n1_before == n1_after
        report["006_n1_runtime_inert_column_update_before"] = n1_column_grant_before
        report["006_n1_runtime_inert_column_update_after"] = n1_column_grant_after
        report["legacy_003_006_definer_contract_unchanged"] = legacy_definers_before == legacy_definers_after
        report["legacy_003_006_definers_before"] = legacy_definers_before
        report["legacy_003_006_definers_after"] = legacy_definers_after
        if legacy_definers_before != legacy_definers_after:
            raise RuntimeError("LEGACY_DEFINER_CONTRACT_CHANGED")
        report["workbench_existing_table_counts_unchanged"] = True
        report["workbench_post_migration_table_counts"] = post_counts
        report["runtime_no_effective_dml_and_secdef_execute"] = bool(privileged)
        report["runtime_schema_usage_and_secdef_call"] = bool(execute_probe)
        report["security_definer_owners"] = [{"function": name, "owner": owner,
             "security_definer": secdef, "search_path": config} for name, owner, secdef, config in functions]
        report["public_execute_revoked"] = not bool(pub_exec)
        restored_db_acls = {}
        for label, source_db in (("target", src_target), ("workbench", src_wb)):
            source_acl = report["database_acls"][label]
            restored_acl, _ = source_database_acl(scratch, "folio_008_" + label)
            restored_db_acls[label] = restored_acl
            report.setdefault("database_acls_observed", {})[label] = restored_acl
            if source_acl != restored_acl:
                raise RuntimeError("DATABASE_ACL_RESTORE_MISMATCH")
        report["database_acls_after_restore"] = restored_db_acls
        report["database_acls_preserved"] = True
        report["phase"] = "post008_n1_compatibility"
        report["n1_post008_compatibility"] = run_post008_n1_export_probe(
            runtime_role, out, out, stamp)
        report["006_metadata_review"] = "must be checked against frozen activation probe before live; clone retains restored bytes"
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["error_code"] = str(error) if str(error).isupper() else "SCRATCH_REHEARSAL_FAILED"
        report["error_class"] = type(error).__name__
        report["error_message_private"] = str(error)
    finally:
        cleanup = []
        if created:
            rm = subprocess.run(["docker", "rm", "-f", scratch], capture_output=True, timeout=30)
            cleanup.append("owned_container_removed" if rm.returncode == 0 else "owned_container_cleanup_failed")
        if volume_created:
            rm = subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)
            cleanup.append("owned_volume_removed" if rm.returncode == 0 else "owned_volume_cleanup_failed")
        report["cleanup"] = cleanup
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report_path = out / "report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        secure_private_file(report_path, inherited=True)
        print(json.dumps({"status": report["status"], "report": str(report_path.relative_to(ROOT)),
                          "retained_write_connection": False, "cleanup": cleanup,
                          "error_code": report.get("error_code")}))
    return 0 if report["status"] == "PASS" and all(x.endswith("removed") for x in cleanup) else 1


if __name__ == "__main__":
    raise SystemExit(main())
