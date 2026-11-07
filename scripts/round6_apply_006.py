"""Forward-only retained N-1 export lock compatibility; run under the host lock."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from psycopg import sql
from runtime import environment

from folio.schema_journal import journal_sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / 'tools'))

def main():
    import product_lock

    owner_lock = product_lock.read_owner(ROOT.parent / '.cache/product-build.lock') or {}
    if (owner_lock.get('product'), owner_lock.get('purpose')) != ('folio', 'retained-n1-compat-006'):
        raise RuntimeError('EXPECTED_HOST_LOCK_REQUIRED')
    if (owner_lock.get('pid'), owner_lock.get('create_time')) not in {(p.pid,p.create_time()) for p in psutil.Process().parents()}:
        raise RuntimeError('ANCESTOR_HOST_LOCK_REQUIRED')
    # Existing explicit encrypted safety snapshot precedes retained DDL.
    subprocess.run([sys.executable, '-B', '.local/round6_pre_stop_backup.py'], cwd=ROOT, check=True, timeout=120)
    profile = environment('browser')
    endpoint = urlsplit(profile['FOLIO_DSN'])
    database, runtime = endpoint.path.lstrip('/'), endpoint.username
    if endpoint.hostname != '127.0.0.1' or endpoint.port != 5541 or not database.startswith('folio_browser_wb_'):
        raise RuntimeError('RETAINED_BROWSER_IDENTITY_REFUSED')
    def psql(statement):
        result = subprocess.run(['docker','exec','-i','folio-postgres','psql','-U','postgres','-d',database,
                                 '-v','ON_ERROR_STOP=1','-q','-At'], input=statement.encode(), capture_output=True, timeout=60)
        if result.returncode:
            # SQL contains no credentials; retain diagnostics privately, never
            # echo server text or operator configuration into public output.
            (ROOT / '.local/n1-006-last-sql-error.private.log').write_bytes(result.stderr)
            raise RuntimeError('RETAINED_COMPAT_SQL_FAILED')
        return result.stdout.decode().strip()
    owner = psql("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.schema_versions'::regclass")
    if not owner or owner == runtime:
        raise RuntimeError('NONRUNTIME_OWNER_REQUIRED')
    if psql("SELECT pg_has_role((SELECT relowner FROM pg_class WHERE oid='public.schema_versions'::regclass),"
            "(SELECT relowner FROM pg_class WHERE oid='public.users'::regclass),'MEMBER') AND "
            "(SELECT proowner FROM pg_proc WHERE oid='public.folio_export_lock_actor(text)'::regprocedure)="
            "(SELECT relowner FROM pg_class WHERE oid='public.schema_versions'::regclass)") != 't':
        raise RuntimeError('TRUSTED_SCHEMA_OWNER_AUTHORITY_MISMATCH')
    def require_narrow_runtime():
        safe = psql(sql.SQL("""SELECT
          NOT has_table_privilege({r},'public.users','UPDATE')
          AND NOT EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid='public.users'::regclass
            AND a.attnum>0 AND NOT a.attisdropped AND a.attname<>'n1_export_lock_capability'
            AND has_column_privilege({r},a.attrelid,a.attnum,'UPDATE'))
          AND NOT EXISTS(SELECT 1 FROM pg_roles p WHERE pg_has_role({r},p.oid,'MEMBER')
            AND (p.rolsuper OR p.rolcreaterole OR p.rolcreatedb OR p.rolreplication OR p.rolbypassrls))
          AND NOT pg_has_role({r},(SELECT relowner FROM pg_class WHERE oid='public.users'::regclass),'MEMBER')
          AND NOT pg_has_role({r},{o},'MEMBER')
          AND NOT pg_has_role({r},(SELECT datdba FROM pg_database WHERE datname=current_database()),'MEMBER')
        """).format(r=sql.Literal(runtime),o=sql.Literal(owner)).as_string())
        if safe != 't':
            raise RuntimeError('RUNTIME_AUTH_OR_OWNER_AUTHORITY_NOT_NARROW')
    require_narrow_runtime()
    migrations = {2:'002-workbench.sql',3:'003-exports.sql',4:'004-export-actor-lock.sql',
                  5:'005-read-path-indexes.sql',6:'006-n1-export-row-lock-compat.sql'}
    journal = dict(row.split('|') for row in psql('SELECT version,sha256 FROM schema_versions ORDER BY version').splitlines())
    pins = {v:hashlib.sha256((ROOT/'ops/migrations'/name).read_bytes()).hexdigest() for v,name in migrations.items()}
    for version in (2,3,4,5):
        if journal.get(str(version)) != pins[version]:
            raise RuntimeError('APPLIED_PREDECESSOR_CHECKSUM_MISMATCH')
    if '6' in journal and journal['6'] != pins[6]:
        raise RuntimeError('APPLIED_006_CHECKSUM_MISMATCH')
    raw = (ROOT/'ops/migrations'/migrations[6]).read_bytes()
    statement = sql.SQL('BEGIN; SET LOCAL ROLE {}; LOCK TABLE public.schema_versions IN EXCLUSIVE MODE;\n').format(sql.Identifier(owner)).as_string()
    if '6' not in journal:
        statement += journal_sql(6,raw) + '\n' + raw.decode('utf-8') + '\n'
    statement += sql.SQL('GRANT UPDATE(n1_export_lock_capability) ON public.users TO {};').format(sql.Identifier(runtime)).as_string()
    guard_body = raw.decode('utf-8').split('AS $guard$',1)[1].split('$guard$;',1)[0].strip()
    postcondition = sql.SQL("""SELECT has_column_privilege({r},'public.users','n1_export_lock_capability','UPDATE')
      AND EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
        WHERE t.tgrelid='public.users'::regclass AND t.tgname='folio_n1_export_lock_capability_immutable'
          AND NOT t.tgisinternal AND t.tgenabled IN ('O','A')
          AND t.tgtype=19 AND t.tgqual IS NULL
          AND t.tgattr::text=(SELECT a.attnum::text FROM pg_attribute a
            WHERE a.attrelid=t.tgrelid AND a.attname='n1_export_lock_capability' AND NOT a.attisdropped)
          AND p.oid='public.folio_reject_n1_export_lock_capability_update()'::regprocedure
          AND p.prosecdef AND btrim(p.prosrc,E' \\t\\r\\n')=btrim({body},E' \\t\\r\\n')
          AND p.proconfig=ARRAY['search_path=pg_catalog'])""").format(r=sql.Literal(runtime),body=sql.Literal(guard_body)).as_string()
    statement += "\nDO $assert$ BEGIN IF NOT (" + postcondition + ") THEN RAISE EXCEPTION 'COMPAT_POSTCONDITION_FAILED'; END IF; END $assert$; COMMIT;"
    psql(statement)
    require_narrow_runtime()
    verified = psql(postcondition)
    if verified != 't':
        raise RuntimeError('COMPAT_POSTCONDITION_FAILED')
    receipt = {'status':'applied' if '6' not in journal else 'already_applied_grant_verified',
               'migration_sha256':pins[6], 'predecessor_hashes_preserved':True,
               'narrow_grant_and_immutable_trigger_verified':True, 'secret_printed':False}
    path = ROOT/'.local/n1-006-retained-activation-20261102.json'
    path.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps(receipt))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        message = str(exc)
        code = message if re.fullmatch('[A-Z0-9_]+',message) else None
        print(json.dumps({'status':'failed','error_class':type(exc).__name__, 'code':code, 'secret_printed':False}))
        raise SystemExit(2)
