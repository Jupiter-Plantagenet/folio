"""Paired quiesced local backup and isolated restore, never re-enables restored dispatch.
Requires local API/target ports free; private database dumps stay under .local.
"""

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import psutil
import psycopg
from psycopg import sql
from runtime import ROOT, environment, is_free

from folio.backup import open_backup


def main():
    from live_supervisor import STOPPED

    if not STOPPED.is_file():
        raise SystemExit('OPERATOR_SUPERVISOR_STOP_REQUIRED')
    if not all(is_free(p) for p in [8101, 8201]):
        raise SystemExit(
            "Quiesce Folio supervisor before paired backup; no process stopped"
        )
    for process in psutil.process_iter(['pid','cwd','cmdline']):
        if process.info['pid'] == os.getpid():
            continue
        try:
            process_cwd = os.path.normcase(os.path.normpath(process.info['cwd'] or ''))
            product_root = os.path.normcase(os.path.normpath(os.fspath(ROOT)))
            if process_cwd == product_root and any(
                    arg in ('folio.worker','folio.exportworker') for arg in (process.info['cmdline'] or [])):
                raise SystemExit('QUIESCE_ONESHOT_AND_LOOP_WORKERS_REQUIRED')
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    public = ROOT / "evidence/documents/restore" / stamp
    public.mkdir(parents=True)
    env = environment("browser")
    for key in ('FOLIO_DSN', 'FOLIO_TARGET_DSN'):
        with psycopg.connect(env[key]) as db:
            if db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                          "AND backend_type='client backend' AND pid<>pg_backend_pid()").fetchone()[0]:
                raise SystemExit('QUIESCE_ALL_DATABASE_CLIENTS_REQUIRED')
    # Reuse the supervisor's paired capture protocol. Restore drill additionally
    # requires STOPPED, free service ports, and no other DB clients; create the
    # same private marker so capture cannot proceed as an online sequential dump.
    from live_supervisor import (
        EXPORT_ROOT,
        create_maintenance_marker,
        remove_maintenance_marker,
    )

    from scripts.paired_backup import capture_quiesced_pair

    os.environ['FOLIO_EXPORT_ROOT'] = EXPORT_ROOT
    if (ROOT / ".local/maintenance/folio-quiesce.json").exists():
        raise SystemExit("MAINTENANCE_MARKER_RECOVERY_REQUIRED")
    marker_hash = create_maintenance_marker("restore-drill-" + stamp)
    try:
        capture = capture_quiesced_pair(env, stamp=stamp)
    finally:
        remove_maintenance_marker(marker_hash)
    private = Path(capture["private_directory"])
    artifact_rows = capture["artifact_rows"]
    artifact_inventory = capture["artifact_inventory"]
    artifact_envelope = capture["artifact_envelope"]
    recovered = open_backup(capture["artifact_encrypted_path"],
                            capture["artifact_key_path"],
                            "ready-export-artifacts:" + stamp)
    restored_artifacts = private / 'restored-export-artifacts'
    restored_artifacts.mkdir(mode=0o700)
    import io
    import zipfile

    from folio.exports import MAX_BYTES

    with zipfile.ZipFile(io.BytesIO(recovered)) as archive:
        if set(archive.namelist()) != set(artifact_inventory) or len(archive.infolist()) != len(artifact_inventory):
            raise RuntimeError('RESTORE_ARTIFACT_INVENTORY_MISMATCH')
        for ref, expected in artifact_inventory.items():
            info = archive.getinfo(ref)
            if info.file_size > MAX_BYTES or info.file_size != expected['bytes']:
                raise RuntimeError('RESTORE_ARTIFACT_SIZE_MISMATCH')
            raw = archive.read(ref)
            if hashlib.sha256(raw).hexdigest() != expected['sha256']:
                raise RuntimeError('RESTORE_ARTIFACT_HASH_MISMATCH')
            destination = restored_artifacts.joinpath(*PurePosixPath(ref).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as stream:
                stream.write(raw)
    report = {
        "scope": "quiesced paired local databases; isolated restore and read-only validation, no restored dispatch enabled",
        "databases": [],
        "originals": "stored uploaded bytes are included in the workbench dump; authored public assets remain manifest-pinned",
        "encrypted": True,
        "algorithm": "AES-256-GCM",
        "key_custody": "separate private on-host backup-keys directory; not independent off-host operator custody",
        "backup_expiry_enforced": False,
        "rpo_rto_reference_gate_passed": False,
        "export_artifacts": {'files': artifact_inventory, 'file_count': len(artifact_inventory),
                             'encrypted_envelope': artifact_envelope,
                             'isolated_bytes_restored': True, 'database_references_verified': False},
    }

    def snapshot(dsn):
        with psycopg.connect(dsn) as db:
            tables = [
                x[0]
                for x in db.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
                )
            ]
            result = {}
            for table in tables:
                if table in ["sessions", "target_sessions"]:
                    continue
                rows = db.execute(
                    sql.SQL("SELECT row_to_json(t) FROM {} t").format(
                        sql.Identifier(table)
                    )
                ).fetchall()
                values = sorted(
                    json.dumps(x[0], sort_keys=True, default=str, separators=(",", ":"))
                    for x in rows
                )
                result[table] = {
                    "count": len(values),
                    "sha256": hashlib.sha256("\n".join(values).encode()).hexdigest(),
                }
            return result

    for key, owner in [
        ("FOLIO_TARGET_DSN", "folio_target"),
        ("FOLIO_DSN", "folio_workbench"),
    ]:
        dsn = env[key]
        original = urlsplit(dsn).path[1:]
        restored = (
            "folio_restore_" + ("target_" if owner == "folio_target" else "wb_") + stamp
        )
        before = snapshot(dsn)
        capture_files = capture["database_files"][original]
        envelope = capture_files["envelope"]
        dump = open_backup(capture_files["encrypted_path"],
                           capture_files["key_path"], original)
        restored_dump = dump
        subprocess.run(
            [
                "docker",
                "exec",
                "folio-postgres",
                "psql",
                "-U",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(restored), sql.Identifier(owner)
                ).as_string(),
            ],
            check=True,
            capture_output=True,
        )
        # Mark the new database read-only before pg_restore or any validation
        # connection can expose it. Restore-mode clearing requires a separate
        # signed reconciliation report for the exact workbench/target pair.
        restore_flag = sql.SQL(
            "ALTER DATABASE {} SET folio.restore_mode='read_only'"
        ).format(sql.Identifier(restored)).as_string()
        subprocess.run(
            [
                "docker",
                "exec",
                "folio-postgres",
                "psql",
                "-U",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                restore_flag,
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "folio-postgres",
                "pg_restore",
                "-U",
                "postgres",
                "--exit-on-error",
                "-d",
                restored,
            ],
            input=restored_dump,
            capture_output=True,
            check=True,
        )
        target_dsn = dsn.rsplit("/", 1)[0] + "/" + restored
        after = snapshot(target_dsn)
        if before != after:
            raise RuntimeError("RESTORE_CONTENT_MISMATCH")
        with psycopg.connect(target_dsn) as db:
            from folio.restore_mode import database_read_only

            restore_flagged = database_read_only(db)
            if not restore_flagged:
                raise RuntimeError("RESTORE_MODE_FLAG_MISSING")
            if owner == 'folio_workbench':
                restored_refs = db.execute(
                    "SELECT artifact_ref,artifact_sha256,artifact_bytes FROM export_jobs "
                    "WHERE state='ready' ORDER BY artifact_ref").fetchall()
                if restored_refs != artifact_rows:
                    raise RuntimeError('RESTORED_ARTIFACT_DATABASE_REFS_MISMATCH')
                for ref, expected, length in restored_refs:
                    raw = (restored_artifacts / ref).read_bytes()
                    if len(raw) != length or hashlib.sha256(raw).hexdigest() != expected:
                        raise RuntimeError('RESTORED_ARTIFACT_REFERENCE_INTEGRITY')
                report['export_artifacts']['database_references_verified'] = True
            session = "target_sessions" if owner == "folio_target" else "sessions"
            db.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(session)))
            constraints = db.execute(
                "SELECT count(*) FROM pg_constraint WHERE contype='f' AND NOT convalidated"
            ).fetchone()[0]
            assert constraints == 0
        report["databases"].append(
            {
                "source": original,
                "isolated_restore": restored,
                "dump_sha256": hashlib.sha256(dump).hexdigest(),
                "dump_bytes": len(dump),
                "envelope": envelope,
                "authenticated_disk_readback": True,
                "tables": after,
                "session_reset": True,
                "restore_mode_read_only": restore_flagged,
                "unvalidated_foreign_keys": constraints,
                "target_tombstones_preserved": owner == "folio_target",
            }
        )
    report["elapsed_seconds"] = time.monotonic() - started
    report["status"] = "isolated_content_restore_passed"
    (public / "report.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "status": report["status"],
                "seconds": report["elapsed_seconds"],
                "report": str(public / "report.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
