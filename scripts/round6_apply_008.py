"""Guarded retained workbench migration 008 installer (root review required).

This file is prepared for a future explicitly coordinated locked invocation.
It is not run as part of scratch preparation. It writes only migration/ACL
metadata and the access workspace seed; it never rewrites existing journal rows.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PB = ROOT.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(PB / "tools")]
from runtime import environment  # noqa: E402

from folio.database_security import access_administration_grants  # noqa: E402
from folio.private_storage import secure_private_directory, secure_private_file  # noqa: E402
from folio.schema_journal import journal_sql  # noqa: E402

MIGRATION = ROOT / "ops/migrations/008-access-administration.sql"
MIGRATION_SHA = "2aa9e579fb9851713667987b10d7bd0ae3b18c21537e225aa1016b8cfd877808"
FAILURE_LOG = None
EXPECTED = {
    "2": "128edf371b46411bbfe3800e6b5088c51e88cfc284d398fa6540933711736801",
    "3": "93dd4063c5d6538f79648a3ca50efdbc3fa7196442a5b3cc80e35c8e50cf1d63",
    "4": "59ed4f421f4332cafb0325f260baac6ffa1faf07f3e17f4c2a5f843f18ac51ad",
    "5": "9ac766dae0467db4823bf663516c912c92bcd5ec294ada8dcd227e2625b74461",
    "6": "ee0bcc7ca7dba28ffeb31f60efd46ddb1145ce4286f126a88e09a2f0b1a7af08",
}


def gated_resource():
    import shutil

    import psutil
    result = subprocess.run(["powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory).PercentCommittedBytesInUse"],
        capture_output=True, text=True, timeout=15)
    try:
        commit = float(result.stdout.strip()) / 100
    except (ValueError, TypeError):
        commit = 1.0
    return (shutil.disk_usage("C:\\").free >= 50 * 1024**3 and
            psutil.virtual_memory().available >= 6 * 1024**3 and commit < .85)


def psql(database, statement):
    result = subprocess.run(["docker", "exec", "-i", "folio-postgres", "psql", "-U", "postgres",
        "-d", database, "-v", "ON_ERROR_STOP=1", "-q", "-At"], input=statement.encode(),
        capture_output=True, timeout=120)
    if result.returncode:
        path = FAILURE_LOG or ROOT / ".local/access-008-unscoped-error.private.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.stderr)
        secure_private_file(path, inherited=True)
        raise RuntimeError("RETAINED_008_SQL_FAILED")
    return result.stdout.decode().strip()


def journal_fingerprint(rows):
    exact = {k: rows[k] for k in sorted(EXPECTED)}
    raw = json.dumps(exact, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def exit_code_for_status(status):
    """Successful atomic apply reports exit 0; all other outcomes fail closed."""
    return 0 if status == "migration_applied_atomic_assertions_passed" else 1


def build_apply_transaction(database, owner, runtime, raw, *, inject_failure=False):
    """Return one atomic migration payload; every acceptance assertion precedes COMMIT."""
    for value in (database, owner, runtime):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,63}", value or ""):
            raise RuntimeError("POSTGRES_IDENTIFIER_INVALID")
    def ident(value):
        return '"' + value.replace('"', '""') + '"'

    def lit(value):
        return "'" + value.replace("'", "''") + "'"
    expected_rows = ",".join(f"({version},{lit(digest)})" for version, digest in EXPECTED.items())
    pre = f"""DO $folio008_pre$ DECLARE dbowner text; BEGIN
      IF current_database() IS DISTINCT FROM {lit(database)} THEN RAISE EXCEPTION 'DB_IDENTITY_MISMATCH'; END IF;
      SELECT pg_get_userbyid(datdba) INTO dbowner FROM pg_database WHERE datname=current_database();
      IF dbowner IS DISTINCT FROM {lit(owner)} OR {lit(owner)}={lit(runtime)} THEN RAISE EXCEPTION 'DB_OWNER_MISMATCH'; END IF;
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={lit(owner)} AND NOT rolsuper AND NOT rolbypassrls) OR
         EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles e ON e.oid=m.roleid
           WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname={lit(owner)}) AND
             (e.rolsuper OR e.rolbypassrls OR e.rolcreaterole OR e.rolcreatedb OR e.rolreplication))
        THEN RAISE EXCEPTION 'DB_OWNER_ELEVATED'; END IF;
      IF NOT has_schema_privilege({lit(owner)},'public','USAGE') OR NOT has_schema_privilege({lit(owner)},'public','CREATE') OR
         NOT has_table_privilege({lit(owner)},'public.users','SELECT,INSERT,UPDATE,DELETE') OR
         NOT has_table_privilege({lit(owner)},'public.sessions','SELECT,INSERT,UPDATE,DELETE') OR
         NOT has_table_privilege({lit(owner)},'public.events','INSERT') OR
         NOT has_table_privilege({lit(owner)},'public.commands','SELECT,INSERT,UPDATE')
        THEN RAISE EXCEPTION 'DB_OWNER_REQUIRED_PRIVILEGES_MISSING'; END IF;
      IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.schema_versions'::regclass) IS DISTINCT FROM 'postgres' OR
         has_table_privilege({lit(owner)},'public.schema_versions','INSERT')
        THEN RAISE EXCEPTION 'JOURNAL_OWNER_PRECONDITION_MISMATCH'; END IF;
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={lit(runtime)}) OR
         NOT has_table_privilege({lit(runtime)},'public.schema_versions','SELECT') OR
         has_table_privilege({lit(runtime)},'public.schema_versions','INSERT,UPDATE,DELETE,TRUNCATE')
        THEN RAISE EXCEPTION 'RUNTIME_JOURNAL_ACL_PRECONDITION_MISMATCH'; END IF;
      IF EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
         LEFT JOIN pg_roles g ON g.oid=a.grantee WHERE c.oid='public.schema_versions'::regclass AND a.grantee<>c.relowner
           AND NOT (g.rolname={lit(runtime)} AND a.privilege_type='SELECT' AND NOT a.is_grantable))
        THEN RAISE EXCEPTION 'JOURNAL_ACL_PRECONDITION_MISMATCH'; END IF;
      IF to_regclass('public.folio_workspace_access') IS NOT NULL OR
         to_regclass('public.folio_access_requests') IS NOT NULL OR
         EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='folio_access_admin_v1')
        THEN RAISE EXCEPTION 'ACCESS_008_OBJECT_PREEXISTS'; END IF;
      IF (SELECT count(*) FROM pg_temp._folio008_journal_before)<>5 OR
         (SELECT count(*) FROM public.schema_versions)<>5 OR
         EXISTS(SELECT 1 FROM public.schema_versions WHERE version NOT BETWEEN 2 AND 6) OR
         EXISTS(SELECT 1 FROM pg_temp._folio008_journal_before j
           LEFT JOIN (VALUES {expected_rows}) e(version,sha256) USING(version)
           WHERE j.sha256 IS DISTINCT FROM e.sha256) OR
         EXISTS(SELECT 1 FROM (VALUES {expected_rows}) e(version,sha256)
           LEFT JOIN pg_temp._folio008_journal_before j USING(version) WHERE j.version IS NULL)
        THEN RAISE EXCEPTION 'PREDECESSOR_JOURNAL_MISMATCH'; END IF;
      IF NOT has_column_privilege({lit(runtime)},'public.users','n1_export_lock_capability','UPDATE') OR
         has_table_privilege({lit(runtime)},'public.users','INSERT,UPDATE,DELETE,TRUNCATE') OR
         has_any_column_privilege({lit(runtime)},'public.users','INSERT') OR
         EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid='public.users'::regclass
           AND a.attnum>0 AND NOT a.attisdropped AND a.attname<>'n1_export_lock_capability'
           AND has_column_privilege({lit(runtime)},a.attrelid,a.attnum,'UPDATE')) OR
         EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles e ON e.oid=m.roleid
           WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname={lit(runtime)}) AND
             (e.rolsuper OR e.rolbypassrls OR e.rolcreaterole OR e.rolcreatedb OR e.rolreplication)) OR
         EXISTS(SELECT 1 FROM pg_roles WHERE rolname={lit(runtime)} AND (rolsuper OR rolbypassrls))
        THEN RAISE EXCEPTION 'RUNTIME_USER_ACL_PRECONDITION_MISMATCH'; END IF;
    END $folio008_pre$;"""
    post = f"""DO $folio008_post$ DECLARE rec record; actual_count bigint; BEGIN
      IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.schema_versions'::regclass) IS DISTINCT FROM {lit(owner)} OR
         NOT has_table_privilege({lit(runtime)},'public.schema_versions','SELECT') OR
         has_table_privilege({lit(runtime)},'public.schema_versions','INSERT,UPDATE,DELETE,TRUNCATE')
        THEN RAISE EXCEPTION 'JOURNAL_OWNER_OR_RUNTIME_SELECT_FAILED'; END IF;
      IF EXISTS(SELECT version,sha256,installed_at FROM pg_temp._folio008_journal_before
          EXCEPT SELECT version,sha256,installed_at FROM public.schema_versions WHERE version BETWEEN 2 AND 6) OR
         EXISTS(SELECT version,sha256,installed_at FROM public.schema_versions WHERE version BETWEEN 2 AND 6
          EXCEPT SELECT version,sha256,installed_at FROM pg_temp._folio008_journal_before) OR
         (SELECT count(*) FROM public.schema_versions WHERE version=8)<>1 OR
         (SELECT sha256 FROM public.schema_versions WHERE version=8) IS DISTINCT FROM {lit(MIGRATION_SHA)}
        THEN RAISE EXCEPTION 'JOURNAL_PRESERVATION_OR_008_HASH_FAILED'; END IF;
      IF (SELECT acl FROM pg_temp._folio008_acl_before) IS DISTINCT FROM
         (SELECT COALESCE(jsonb_agg(jsonb_build_object('grantee',COALESCE(g.rolname,'PUBLIC'),
           'privilege',a.privilege_type,'grantable',a.is_grantable) ORDER BY COALESCE(g.rolname,'PUBLIC'),
           a.privilege_type,a.is_grantable),'[]'::jsonb)
          FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
          LEFT JOIN pg_roles g ON g.oid=a.grantee WHERE c.oid='public.schema_versions'::regclass AND a.grantee<>c.relowner)
        THEN RAISE EXCEPTION 'JOURNAL_ACL_PRESERVATION_FAILED'; END IF;
      FOR rec IN SELECT table_name,row_count FROM pg_temp._folio008_counts_before LOOP
        EXECUTE format('SELECT count(*) FROM public.%I',rec.table_name) INTO actual_count;
        IF actual_count IS DISTINCT FROM rec.row_count + (CASE WHEN rec.table_name='schema_versions' THEN 1 ELSE 0 END)
          THEN RAISE EXCEPTION 'PREEXISTING_TABLE_COUNT_CHANGED'; END IF;
        INSERT INTO pg_temp._folio008_counts_after VALUES(rec.table_name,actual_count);
      END LOOP;
      IF (SELECT count(*) FROM public.folio_access_requests)<>0 OR
         (SELECT count(*) FROM public.folio_workspace_access)<>1
        THEN RAISE EXCEPTION 'ACCESS_008_SEED_COUNT_INVALID'; END IF;
      IF EXISTS(SELECT 1 FROM pg_temp._folio008_legacy_before b FULL JOIN
          (SELECT p.oid,p.proowner,p.prosecdef,p.proconfig,p.prosrc,p.proacl FROM pg_proc p
           JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef) a USING(oid)
          WHERE b.oid IS NULL OR a.oid IS NULL OR
            (b.proowner,b.prosecdef,b.proconfig,b.prosrc,b.proacl) IS DISTINCT FROM
            (a.proowner,a.prosecdef,a.proconfig,a.prosrc,a.proacl))
        THEN RAISE EXCEPTION 'LEGACY_DEFINER_CONTRACT_CHANGED'; END IF;
      IF EXISTS(SELECT 1 FROM pg_temp._folio008_006_before b CROSS JOIN LATERAL
          (SELECT a.attnotnull,a.atthasdef,pg_get_expr(d.adbin,d.adrelid) AS default_expr,
            t.tgenabled,t.tgtype::int,t.tgattr::text,p.proowner,p.prosecdef,p.proconfig,p.prosrc,p.proacl
           FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
           JOIN pg_trigger t ON t.tgrelid=c.oid AND t.tgname='folio_n1_export_lock_capability_immutable'
           JOIN pg_proc p ON p.oid=t.tgfoid LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
           WHERE a.attrelid='public.users'::regclass AND a.attname='n1_export_lock_capability' AND NOT a.attisdropped) a
          WHERE (b.attnotnull,b.atthasdef,b.default_expr,b.tgenabled,b.tgtype,b.tgattr,b.proowner,b.prosecdef,
                 b.proconfig,b.prosrc,b.proacl) IS DISTINCT FROM
                (a.attnotnull,a.atthasdef,a.default_expr,a.tgenabled,a.tgtype,a.tgattr,a.proowner,a.prosecdef,
                 a.proconfig,a.prosrc,a.proacl))
        THEN RAISE EXCEPTION 'N1_006_METADATA_CHANGED'; END IF;
      IF (SELECT nspowner FROM pg_namespace WHERE nspname='folio_access_admin_v1') IS DISTINCT FROM
           (SELECT oid FROM pg_roles WHERE rolname={lit(owner)}) OR
         (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname='folio_access_admin_v1')<>3 OR
         EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname='folio_access_admin_v1' AND
             (p.proowner IS DISTINCT FROM (SELECT oid FROM pg_roles WHERE rolname={lit(owner)}) OR NOT p.prosecdef OR
              p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp','lock_timeout=5s']::text[] OR
              EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) x
                WHERE x.grantee=0 AND x.privilege_type='EXECUTE')))
        THEN RAISE EXCEPTION 'ACCESS_008_FUNCTION_OWNER_ACL_FAILED'; END IF;
      IF NOT has_schema_privilege({lit(runtime)},'folio_access_admin_v1','USAGE') OR
         NOT has_table_privilege({lit(runtime)},'public.users','SELECT') OR
         NOT has_table_privilege({lit(runtime)},'public.folio_access_requests','SELECT') OR
         NOT has_table_privilege({lit(runtime)},'public.folio_workspace_access','SELECT') OR
         NOT has_function_privilege({lit(runtime)},'folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid)','EXECUTE') OR
         NOT has_function_privilege({lit(runtime)},'folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)','EXECUTE') OR
         has_function_privilege({lit(runtime)},'folio_access_admin_v1.authenticate(text,text)','EXECUTE')
        THEN RAISE EXCEPTION 'RUNTIME_ACCESS_CAPABILITY_FAILED'; END IF;
      IF EXISTS(SELECT 1 FROM (VALUES('public.users'),('public.folio_access_requests'),('public.folio_workspace_access')) t(rel)
        WHERE has_table_privilege({lit(runtime)},rel,'INSERT') OR has_table_privilege({lit(runtime)},rel,'UPDATE') OR
          has_table_privilege({lit(runtime)},rel,'DELETE') OR has_table_privilege({lit(runtime)},rel,'TRUNCATE') OR
          has_any_column_privilege({lit(runtime)},rel,'INSERT') OR has_any_column_privilege({lit(runtime)},rel,'UPDATE'))
        THEN RAISE EXCEPTION 'RUNTIME_PROTECTED_DML_GRANTED'; END IF;
      IF NOT EXISTS(SELECT 1 FROM pg_class WHERE oid='public.folio_access_requests'::regclass AND relrowsecurity AND relforcerowsecurity) OR
         NOT EXISTS(SELECT 1 FROM pg_class WHERE oid='public.folio_workspace_access'::regclass AND relrowsecurity AND relforcerowsecurity) OR
         (SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename IN ('folio_access_requests','folio_workspace_access'))<>2 OR
         (SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename IN ('folio_access_requests','folio_workspace_access')
           AND policyname='folio_workspace' AND cmd='ALL' AND roles=ARRAY['public']::name[] AND
             qual LIKE '%11111111-1111-4111-8111-111111111111%' AND
             with_check LIKE '%11111111-1111-4111-8111-111111111111%')<>2
        THEN RAISE EXCEPTION 'ACCESS_008_RLS_POSTCONDITION_FAILED'; END IF;
    END $folio008_post$;"""
    acl_snapshot = """CREATE TEMP TABLE _folio008_acl_before ON COMMIT DROP AS
      SELECT COALESCE(jsonb_agg(jsonb_build_object('grantee',COALESCE(g.rolname,'PUBLIC'),
        'privilege',a.privilege_type,'grantable',a.is_grantable) ORDER BY COALESCE(g.rolname,'PUBLIC'),
        a.privilege_type,a.is_grantable),'[]'::jsonb) AS acl
      FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
      LEFT JOIN pg_roles g ON g.oid=a.grantee WHERE c.oid='public.schema_versions'::regclass AND a.grantee<>c.relowner;"""
    snapshots = """CREATE TEMP TABLE _folio008_journal_before ON COMMIT DROP AS
        SELECT version,sha256,installed_at FROM public.schema_versions WHERE version BETWEEN 2 AND 6;
      CREATE TEMP TABLE _folio008_legacy_before ON COMMIT DROP AS
        SELECT p.oid,p.proowner,p.prosecdef,p.proconfig,p.prosrc,p.proacl FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef;
      CREATE TEMP TABLE _folio008_006_before ON COMMIT DROP AS
        SELECT a.attnotnull,a.atthasdef,pg_get_expr(d.adbin,d.adrelid) AS default_expr,
          t.tgenabled,t.tgtype::int,t.tgattr::text,p.proowner,p.prosecdef,p.proconfig,p.prosrc,p.proacl
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_trigger t ON t.tgrelid=c.oid AND t.tgname='folio_n1_export_lock_capability_immutable'
        JOIN pg_proc p ON p.oid=t.tgfoid LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='public.users'::regclass AND a.attname='n1_export_lock_capability' AND NOT a.attisdropped;
      CREATE TEMP TABLE _folio008_counts_before(table_name text PRIMARY KEY,row_count bigint NOT NULL) ON COMMIT DROP;
      CREATE TEMP TABLE _folio008_counts_after(table_name text PRIMARY KEY,row_count bigint NOT NULL) ON COMMIT DROP;
      DO $folio008_count$ DECLARE r record; c bigint; BEGIN
        FOR r IN SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' LOOP
          EXECUTE format('SELECT count(*) FROM public.%I',r.tablename) INTO c;
          INSERT INTO pg_temp._folio008_counts_before VALUES(r.tablename,c);
        END LOOP;
      END $folio008_count$;
      GRANT SELECT ON _folio008_journal_before,_folio008_legacy_before,_folio008_006_before,
        _folio008_counts_before,_folio008_acl_before TO OWNER_ROLE_TOKEN;
      GRANT SELECT,INSERT ON _folio008_counts_after TO OWNER_ROLE_TOKEN;"""
    snapshots = snapshots.replace("OWNER_ROLE_TOKEN", ident(owner))
    payload = ["BEGIN ISOLATION LEVEL REPEATABLE READ;",
        "LOCK TABLE public.schema_versions IN ACCESS EXCLUSIVE MODE;",
        acl_snapshot, snapshots, pre,
        f"ALTER TABLE public.schema_versions OWNER TO {ident(owner)};",
        f"SET LOCAL ROLE {ident(owner)};",
        f"GRANT SELECT ON public.schema_versions TO {ident(runtime)};",
        "LOCK TABLE public.schema_versions IN EXCLUSIVE MODE;",
        journal_sql(8, raw), raw.decode("utf-8"), access_administration_grants(runtime),
        # Migration DDL and function grants run as the mapped nonlogin DB owner.
        # Catalog and complete public-table count assertions use the original
        # operator session, still inside this same transaction.
        "RESET ROLE;", post]
    if inject_failure:
        payload.append("DO $folio008_injected$ BEGIN RAISE EXCEPTION 'INJECTED_POSTCONDITION_FAILURE'; END $folio008_injected$;")
    payload.append("SELECT jsonb_build_object('status','ATOMIC_ASSERTIONS_PASSED',"
        "'owner',pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='public.schema_versions'::regclass)),"
        "'journal_8_sha256',(SELECT sha256 FROM public.schema_versions WHERE version=8),"
        "'acl_before',(SELECT acl FROM pg_temp._folio008_acl_before),"
        "'acl_after',(SELECT COALESCE(jsonb_agg(jsonb_build_object('grantee',COALESCE(g.rolname,'PUBLIC'),"
        "'privilege',a.privilege_type,'grantable',a.is_grantable) ORDER BY COALESCE(g.rolname,'PUBLIC'),"
        "a.privilege_type,a.is_grantable),'[]'::jsonb) FROM pg_class c CROSS JOIN LATERAL "
        "aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a LEFT JOIN pg_roles g ON g.oid=a.grantee "
        "WHERE c.oid='public.schema_versions'::regclass AND a.grantee<>c.relowner),"
        "'journal_before',(SELECT COALESCE(jsonb_object_agg(version::text,jsonb_build_object('sha256',sha256,'installed_at',installed_at::text)),'{}'::jsonb) FROM pg_temp._folio008_journal_before),"
        "'journal_after',(SELECT COALESCE(jsonb_object_agg(version::text,jsonb_build_object('sha256',sha256,'installed_at',installed_at::text)),'{}'::jsonb) FROM public.schema_versions WHERE version BETWEEN 2 AND 8),"
        "'counts_before',(SELECT COALESCE(jsonb_object_agg(table_name,row_count),'{}'::jsonb) FROM pg_temp._folio008_counts_before),"
        "'counts_after',(SELECT COALESCE(jsonb_object_agg(table_name,row_count),'{}'::jsonb) FROM pg_temp._folio008_counts_after))::text;")
    payload.append("COMMIT;")
    return "\n".join(payload)


def main():
    global FAILURE_LOG
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ").lower()
    report_dir = secure_private_directory(ROOT / ".local/access-rehearsal" / ("retained-apply-" + stamp))
    error_dir = secure_private_directory(ROOT / ".local/access-008-errors")
    FAILURE_LOG = error_dir / (stamp + ".psql-stderr.private.log")
    report = {"schema": "folio.access-008-retained-apply/v1", "status": "failed",
              "retained_database_changed": False, "raw_secrets_printed": False,
              "started_utc": datetime.now(timezone.utc).isoformat()}
    try:
        raw = MIGRATION.read_bytes()
        if hashlib.sha256(raw).hexdigest() != MIGRATION_SHA:
            raise RuntimeError("MIGRATION_008_SOURCE_PIN_MISMATCH")
        report["source_sha256"] = {"migration_008": MIGRATION_SHA,
            "apply_helper": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        reviewed = os.environ.get("FOLIO_008_REVIEWED_SOURCE_SHA")
        if reviewed != MIGRATION_SHA:
            raise RuntimeError("ROOT_REVIEW_SOURCE_PIN_REQUIRED")
        # Host lock tool lives beside the repo (portfolio-build/tools); import on use
        # so this module stays importable from a standalone checkout.
        import product_lock

        lock = product_lock.read_owner(PB / ".cache/product-build.lock") or {}
        if (lock.get("product"), lock.get("purpose")) != ("folio", "retained-access-008"):
            raise RuntimeError("EXPECTED_HOST_LOCK_REQUIRED")
        import psutil
        ancestors = {(p.pid, p.create_time()) for p in psutil.Process().parents()}
        if (lock.get("pid"), lock.get("create_time")) not in ancestors:
            raise RuntimeError("ANCESTOR_HOST_LOCK_REQUIRED")
        if not gated_resource():
            raise RuntimeError("RESOURCE_GATE_NOT_MET")
        # Fresh authenticated online snapshot; deliberately not labelled paired.
        backup_result = subprocess.run([sys.executable, "-B", "scripts/folio_008_activation.py",
            "safety-backup"], cwd=ROOT, capture_output=True, text=True, timeout=180)
        if backup_result.returncode:
            raise RuntimeError("FRESH_ENCRYPTED_SAFETY_BACKUP_FAILED")
        backup_stdout = json.loads(backup_result.stdout)
        backup_report = json.loads((ROOT / backup_stdout["report"]).read_text(encoding="utf-8"))
        if backup_report.get("status") != "encrypted_online_safety_copy_complete":
            raise RuntimeError("FRESH_ENCRYPTED_SAFETY_BACKUP_INVALID")
        report["backup_id"] = backup_report["backup_id"]
        report["backup_paired_consistent"] = False
        profile = environment("browser")
        endpoint = urlsplit(profile["FOLIO_DSN"])
        database, runtime = endpoint.path.lstrip("/"), endpoint.username
        if endpoint.hostname != "127.0.0.1" or endpoint.port != 5541 or database != "folio_browser_wb_20261026080908":
            raise RuntimeError("RETAINED_BROWSER_IDENTITY_REFUSED")
        owner = psql(database,
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()")
        report["database_owner"] = owner
        report["runtime_role"] = runtime
        report["schema_versions_owner_before"] = "postgres"
        if not gated_resource():
            raise RuntimeError("RESOURCE_GATE_NOT_MET")
        statement = build_apply_transaction(database, owner, runtime, raw)
        transaction_output = psql(database, statement)
        report["retained_database_changed"] = True
        receipt = json.loads(transaction_output.splitlines()[-1])
        if receipt.get("status") != "ATOMIC_ASSERTIONS_PASSED" or receipt.get("journal_8_sha256") != MIGRATION_SHA:
            raise RuntimeError("ATOMIC_SQL_RECEIPT_INVALID")
        report["schema_versions_owner_after"] = receipt["owner"]
        report["journal_pre"] = receipt["journal_before"]
        report["journal_post"] = receipt["journal_after"]
        report["schema_versions_acl_before"] = receipt["acl_before"]
        report["schema_versions_acl_after"] = receipt["acl_after"]
        report["schema_versions_nonowner_acl_preserved"] = receipt["acl_before"] == receipt["acl_after"]
        report["journal_002_006_fingerprint_pre"] = journal_fingerprint({
            k: {"sha256": v["sha256"], "installed_at": v["installed_at"]}
            for k, v in receipt["journal_before"].items() if k in EXPECTED})
        report["journal_002_006_fingerprint_after"] = journal_fingerprint({
            k: {"sha256": v["sha256"], "installed_at": v["installed_at"]}
            for k, v in receipt["journal_after"].items() if k in EXPECTED})
        report["schema_journal_select_preserved"] = True
        report["public_table_counts_before"] = receipt["counts_before"]
        report["public_table_counts_after"] = receipt["counts_after"]
        report["all_critical_postconditions_inside_transaction"] = True
        report["status"] = "migration_applied_atomic_assertions_passed"
    except Exception as error:
        report["error_class"] = type(error).__name__
        report["error_code"] = str(error) if re.fullmatch(r"[A-Z0-9_]+", str(error)) else "RETAINED_008_APPLY_FAILED"
    finally:
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        path = report_dir / "report.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        secure_private_file(path, inherited=True)
        print(json.dumps({"status": report["status"], "report": str(path.relative_to(ROOT)),
                          "retained_database_changed": report["retained_database_changed"],
                          "error_code": report.get("error_code")}))
    return exit_code_for_status(report["status"])


if __name__ == "__main__":
    raise SystemExit(main())
