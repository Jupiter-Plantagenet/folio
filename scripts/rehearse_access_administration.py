"""Run the access API proof in an owned disposable DB; never change live accounts."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from folio.database_security import admin, provision_runtime  # noqa: E402
from folio.private_storage import secure_private_directory  # noqa: E402


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ').lower()
    name = 'folio_access_scratch_' + stamp
    owner_role = 'folio_access_owner_' + stamp
    owner_password = secrets.token_urlsafe(32)
    out = secure_private_directory(ROOT / '.local/access-rehearsal' / stamp)
    report = {'schema': 'folio.access-real-pg-rehearsal/v1', 'database': name,
              'retained_database_touched': False, 'retained_accounts_changed': False,
              'started_utc': datetime.now(timezone.utc).isoformat()}
    created_db = False
    created_role = False
    runtime_role = None
    runtime_password = None
    status = 1
    try:
        admin(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS').format(
            sql.Identifier(owner_role), sql.Literal(owner_password)).as_string())
        created_role = True
        admin(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name), sql.Identifier(owner_role)).as_string())
        created_db = True
        owner_dsn = f'postgresql://{owner_role}:{owner_password}@127.0.0.1:5541/{name}'
        os.environ['FOLIO_DSN'] = owner_dsn
        os.environ['FOLIO_SCHEMA_MODE'] = 'bootstrap'
        os.environ.pop('FOLIO_MAINTENANCE_MARKER', None)
        from folio import workbench
        workbench.DSN = owner_dsn
        workbench.migrate()
        runtime_dsn = provision_runtime(name, 'workbench')
        info = conninfo_to_dict(runtime_dsn)
        runtime_role, runtime_password = info['user'], info['password']
        env = {**os.environ, 'FOLIO_SCHEMA_MODE': 'runtime', 'FOLIO_DSN': runtime_dsn,
               'FOLIO_ACCESS_SCRATCH_OWNER_DSN': owner_dsn,
               'FOLIO_ACCESS_SCRATCH_RUNTIME_DSN': runtime_dsn}
        pins = {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ('folio', 'scripts') for p in (ROOT / folder).glob('*.py')}
        report['source_sha256'] = pins
        result = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q',
                                 'tests/test_access_administration_pg.py'],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        output = result.stdout + result.stderr
        for password in (owner_password, runtime_password):
            if password:
                output = output.replace(password, '<redacted>')
        with (out / 'pytest-sanitized.log').open('xb') as stream:
            stream.write(output.encode('utf-8'))
        report['pytest_exit_code'] = result.returncode
        report['pytest_log_sha256'] = hashlib.sha256(output.encode()).hexdigest()
        report['source_unchanged'] = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in pins.items())
        status = result.returncode if report['source_unchanged'] else 2
    except Exception as error:
        # Database/libpq errors must never interpolate credentials into output.
        report['error_class'] = type(error).__name__
        report['error_code'] = 'ACCESS_REHEARSAL_SETUP_OR_EXECUTION_FAILED'
    finally:
        from folio.dbpool import close_all
        close_all()
        cleanup = []
        if created_db:
            try:
                assert name.startswith('folio_access_scratch_')
                admin(sql.SQL('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname={} AND pid<>pg_backend_pid()').format(sql.Literal(name)).as_string())
                admin(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)).as_string())
                cleanup.append('owned_database_removed')
            except Exception:
                cleanup.append('owned_database_cleanup_failed')
        for role in (runtime_role, owner_role if created_role else None):
            if role:
                try:
                    admin(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)).as_string())
                    cleanup.append('owned_role_removed')
                except Exception:
                    cleanup.append('owned_role_cleanup_failed')
        report['cleanup'] = cleanup
        report['status'] = 'PASS' if status == 0 and not any('failed' in item for item in cleanup) else 'FAIL'
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        with (out / 'report.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
        print(json.dumps({'status': report['status'], 'report': str((out / 'report.json').relative_to(ROOT)),
                          'pytest_exit_code': report.get('pytest_exit_code'), 'error_class': report.get('error_class'),
                          'cleanup': cleanup}))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
