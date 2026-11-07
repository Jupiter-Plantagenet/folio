"""Read-only runtime-role metadata probe; no credentials or business rows emitted."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from runtime import ROOT, environment


def main():
    report = {'schema': 'folio.backup-runtime-visibility/v1',
              'at_utc': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'no_backup_or_quiescence_claim': True, 'profiles': {}}
    try:
        env = environment('browser')
        for kind, key in [('workbench', 'FOLIO_DSN'), ('target', 'FOLIO_TARGET_DSN')]:
            with psycopg.connect(env[key]) as db:
                db.execute('SET TRANSACTION READ ONLY')
                row = db.execute("""SELECT current_database(),
                    (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
                    (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user),
                    count(*) FILTER(WHERE pid<>pg_backend_pid()),
                    count(*) FILTER(WHERE pid<>pg_backend_pid() AND state IS NULL),
                    count(*) FILTER(WHERE pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle')
                    FROM pg_stat_activity WHERE datname=current_database()""").fetchone()
                report['profiles'][kind] = dict(zip(
                    ('database', 'superuser', 'bypassrls', 'other_connections',
                     'other_state_hidden', 'nonidle_or_hidden_connections'), row))
        report['status'] = 'observed'
    except Exception as error:
        report['status'] = 'failed'
        report['error_class'] = type(error).__name__
    report['probe_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out = ROOT / 'docs/round6-logs' / ('backup-runtime-visibility-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    with out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({'status': report['status'], 'report': str(out.relative_to(ROOT)),
                      'profiles': report['profiles']}))
    return 0 if report['status'] == 'observed' else 1


if __name__ == '__main__':
    sys.exit(main())
