"""Create one encrypted, quiesced Folio workbench/target backup pair.

The caller must establish maintenance admission and worker drain before capture.
This helper rechecks DB identity, active transactions and business/worker gates;
it refuses sequential online captures as a coherent pair.
"""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import psycopg
from runtime import ROOT, environment

from folio.backup import open_backup, seal_backup
from folio.private_storage import secure_private_directory, secure_private_file

MAX_PACK_BYTES = 64 * 1024 * 1024
MAX_DUMP_BYTES = 128 * 1024 * 1024
MAINTENANCE_MARKER = ROOT / ".local/maintenance/folio-quiesce.json"
WORKSPACE = "11111111-1111-4111-8111-111111111111"


def resource_preflight():
    """Enforce standing gates before a capture; never returns credentials."""
    import shutil

    import psutil

    free_c = shutil.disk_usage("C:\\").free
    available_ram = psutil.virtual_memory().available
    system_commit = None
    if os.name == "nt":
        command = (
            "$m=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory;"
            "if($null -eq $m -or $m.CommitLimit -le 0){exit 4};"
            "[Console]::Out.WriteLine(([double]$m.CommittedBytes/[double]$m.CommitLimit).ToString('R',[cultureinfo]::InvariantCulture))"
        )
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return {"admitted": False, "reason": "PERFOS_COMMIT_UNAVAILABLE",
                    "free_c_bytes": free_c, "available_ram_bytes": available_ram}
        try:
            system_commit = float(result.stdout.strip())
        except ValueError:
            return {"admitted": False, "reason": "PERFOS_COMMIT_INVALID",
                    "free_c_bytes": free_c, "available_ram_bytes": available_ram}
    return {
        "admitted": free_c >= 50 * 1024**3 and available_ram >= 6 * 1024**3
                    and system_commit is not None and system_commit < 0.85,
        "reason": None if (free_c >= 50 * 1024**3 and available_ram >= 6 * 1024**3
                           and system_commit is not None and system_commit < 0.85)
                  else "RESOURCE_GATE_NOT_MET",
        "free_c_bytes": free_c,
        "available_ram_bytes": available_ram,
        "system_commit_fraction": system_commit,
    }


def _marker_active():
    try:
        info = MAINTENANCE_MARKER.lstat()
    except FileNotFoundError:
        return False
    if MAINTENANCE_MARKER.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuntimeError("BACKUP_MAINTENANCE_MARKER_INVALID")
    return stat.S_ISREG(info.st_mode)


def _identity(db, dsn, expected_prefix):
    username = urlsplit(dsn).username
    database = db.execute("SELECT current_database(), current_user").fetchone()
    if not username or database[1] != username or not database[0].startswith(expected_prefix):
        raise RuntimeError("BACKUP_DATABASE_IDENTITY_REFUSED")
    elevated = db.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
    if not elevated or elevated[0]:
        raise RuntimeError("BACKUP_ELEVATED_RUNTIME_ROLE_REFUSED")
    oid = db.execute("SELECT oid FROM pg_database WHERE datname=current_database()").fetchone()[0]
    active = db.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
        "AND pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle'"
    ).fetchone()[0]
    if active:
        raise RuntimeError("BACKUP_ACTIVE_DB_TRANSACTIONS")
    return {"database": database[0], "role": database[1], "oid": oid}


def _cluster_system_identifier():
    """Bind the captured pair to the exact PostgreSQL cluster without exposing secrets."""
    result = subprocess.run(
        ["docker", "exec", "folio-postgres", "psql", "-X", "-U", "postgres",
         "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-q", "-At", "-c",
         "SELECT system_identifier FROM pg_control_system()"],
        capture_output=True, timeout=30,
    )
    value = result.stdout.decode("ascii", errors="strict").strip()
    if result.returncode != 0 or not value.isdecimal():
        raise RuntimeError("BACKUP_CLUSTER_SYSTEM_IDENTIFIER_UNAVAILABLE")
    return value


def _quiescence(db_wb, db_target):
    if not _marker_active():
        raise RuntimeError("BACKUP_MAINTENANCE_MARKER_REQUIRED")
    in_flight_jobs = db_wb.execute(
        "SELECT count(*) FROM jobs WHERE workspace=%s AND state IN ('leased','uncertain')",
        (WORKSPACE,),
    ).fetchone()[0]
    pending_cases = db_wb.execute(
        "SELECT count(*) FROM cases WHERE workspace=%s AND body->>'state' IN "
        "('seal_pending','sealed','uncertain')", (WORKSPACE,)
    ).fetchone()[0]
    active_analysis = db_wb.execute(
        "SELECT count(*) FROM analysis_runs WHERE workspace=%s AND status='running'",
        (WORKSPACE,),
    ).fetchone()[0]
    active_exports = db_wb.execute(
        "SELECT count(*) FROM export_jobs WHERE workspace=%s AND state='leased'",
        (WORKSPACE,),
    ).fetchone()[0]
    active_target_grants = db_target.execute(
        "SELECT count(*) FROM authority WHERE workspace=%s AND expires>clock_timestamp()",
        (WORKSPACE,),
    ).fetchone()[0]
    if any((in_flight_jobs, pending_cases, active_analysis,
            active_exports, active_target_grants)):
        raise RuntimeError("BACKUP_BUSINESS_WORK_UNRESOLVED")
    queues = {
        "queued_dispatches": db_wb.execute("SELECT count(*) FROM jobs WHERE workspace=%s AND state='queued'", (WORKSPACE,)).fetchone()[0],
        "queued_analysis": db_wb.execute("SELECT count(*) FROM analysis_runs WHERE workspace=%s AND status='queued'", (WORKSPACE,)).fetchone()[0],
        "queued_exports": db_wb.execute("SELECT count(*) FROM export_jobs WHERE workspace=%s AND state='queued'", (WORKSPACE,)).fetchone()[0],
    }
    # These durable jobs remain queued and are preserved in the snapshot.
    return {"active_leases_or_uncertain_jobs": 0, "pending_effect_cases": 0,
            "active_target_grants": 0, "queued_work_preserved": queues}


def _safe_export_tree(export_root):
    from folio.private_storage import _validate_existing_ancestors

    root = Path(os.path.abspath(os.fspath(export_root)))
    if not root.is_absolute():
        raise RuntimeError("BACKUP_EXPORT_ROOT_INVALID")
    if os.name == "nt" and root.drive.casefold() != "c:":
        raise RuntimeError("BACKUP_EXPORT_ROOT_INVALID")
    _validate_existing_ancestors(root)
    metadata = root.lstat()
    if root.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise RuntimeError("BACKUP_EXPORT_ROOT_REPARSE_POINT")
    return root


def _pack_exports(export_root, rows):
    from folio.exports import MAX_BYTES, read_bytes

    inventory = {}
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_STORED) as archive:
        for directory, dirs, files in os.walk(export_root, followlinks=False):
            dirs.sort()
            for name in dirs + files:
                candidate = Path(directory) / name
                metadata = candidate.lstat()
                if candidate.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise RuntimeError("BACKUP_ARTIFACT_REPARSE_POINT")
            for name in sorted(files):
                candidate = Path(directory) / name
                if not stat.S_ISREG(candidate.lstat().st_mode):
                    raise RuntimeError("BACKUP_ARTIFACT_NOT_REGULAR")
                ref = candidate.relative_to(export_root).as_posix()
                parts = PurePosixPath(ref).parts
                if not parts or any(part in ("..", ".") or ":" in part or "\\" in part for part in parts):
                    raise RuntimeError("BACKUP_ARTIFACT_PATH_INVALID")
                raw = candidate.read_bytes()
                if len(raw) > MAX_BYTES or len(inventory) >= 10_000 or packed.tell() + len(raw) > MAX_PACK_BYTES:
                    raise RuntimeError("BACKUP_ARTIFACT_PACK_LIMIT")
                archive.writestr(zipfile.ZipInfo(ref, date_time=(1980, 1, 1, 0, 0, 0)), raw)
                inventory[ref] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    for ref, expected, length in rows:
        read_bytes(ref, expected, length)
        if inventory.get(ref) != {"sha256": expected, "bytes": length}:
            raise RuntimeError("BACKUP_READY_REFERENCE_NOT_CAPTURED")
    return packed.getvalue(), inventory


def capture_quiesced_pair(env, *, stamp=None, pinned_release_evidence=False):
    """Capture encrypted dumps+artifact pack after the supervisor's drain barrier."""
    if not _marker_active():
        raise RuntimeError("BACKUP_MAINTENANCE_MARKER_REQUIRED")
    admission = resource_preflight()
    if not admission["admitted"]:
        raise RuntimeError(admission["reason"])
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        raise ValueError("BACKUP_STAMP_INVALID")
    private = secure_private_directory(ROOT / ".local/backups" / stamp)
    key_dir = secure_private_directory(ROOT / ".local/backup-keys" / stamp)
    export_root = _safe_export_tree(os.getenv("FOLIO_EXPORT_ROOT", ""))
    wb_dsn, target_dsn = env["FOLIO_DSN"], env["FOLIO_TARGET_DSN"]
    with psycopg.connect(wb_dsn) as db_wb, psycopg.connect(target_dsn) as db_target:
        wb_identity = _identity(db_wb, wb_dsn, "folio_browser_wb_")
        target_identity = _identity(db_target, target_dsn, "folio_browser_target_")
        queue_state = _quiescence(db_wb, db_target)
        artifact_rows = db_wb.execute(
            "SELECT artifact_ref,artifact_sha256,artifact_bytes FROM export_jobs "
            "WHERE workspace=%s AND state='ready' ORDER BY artifact_ref",
            (WORKSPACE,)
        ).fetchall()
        artifact_pack, artifact_inventory = _pack_exports(export_root, artifact_rows)

    artifact_key = key_dir / "ready-export-artifacts.key"
    artifact_path = private / "ready-export-artifacts.zip.enc"
    artifact_envelope = seal_backup(artifact_pack, artifact_path, artifact_key,
                                    "ready-export-artifacts:" + stamp)
    secure_private_file(artifact_path, inherited=True)
    secure_private_file(artifact_key, inherited=True)
    if open_backup(artifact_path, artifact_key, "ready-export-artifacts:" + stamp) != artifact_pack:
        raise RuntimeError("BACKUP_ARTIFACT_AUTHENTICATED_READBACK_FAILED")

    dump_records = {}
    for dsn, identity in ((wb_dsn, wb_identity), (target_dsn, target_identity)):
        database = identity["database"]
        command = ["docker", "exec", "folio-postgres", "pg_dump", "-U", "postgres", "-Fc", database]
        result = subprocess.run(command, capture_output=True, timeout=180)
        if result.returncode != 0 or len(result.stdout) > MAX_DUMP_BYTES:
            raise RuntimeError("BACKUP_DATABASE_DUMP_FAILED")
        raw = result.stdout
        filename = database + ".dump.enc"
        keyname = database + ".key"
        encrypted = private / filename
        key_path = key_dir / keyname
        envelope = seal_backup(raw, encrypted, key_path, database)
        secure_private_file(encrypted, inherited=True)
        secure_private_file(key_path, inherited=True)
        restored = open_backup(encrypted, key_path, database)
        if hashlib.sha256(restored).digest() != hashlib.sha256(raw).digest():
            raise RuntimeError("BACKUP_DATABASE_AUTHENTICATED_READBACK_FAILED")
        dump_records[database] = {"encrypted_path": filename, "key_path": keyname,
                                  "dump_sha256": hashlib.sha256(raw).hexdigest(),
                                  "dump_bytes": len(raw), "envelope": envelope,
                                  "authenticated_readback": True}

    manifest = {
        "schema": "folio-paired-backup-v1", "backup_id": stamp,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cluster_system_identifier": _cluster_system_identifier(),
        "pair_consistency": "maintenance marker; zero API active requests; worker claim barrier; zero active/uncertain effect gates; no active database transactions",
        "database_pair": {"workbench": wb_identity, "target": target_identity},
        "databases": dump_records,
        "queues": queue_state["queued_work_preserved"],
        "quiescence": queue_state,
        "exports": {"inventory": artifact_inventory, "file_count": len(artifact_inventory),
                    "encrypted_path": "ready-export-artifacts.zip.enc",
                    "envelope": artifact_envelope, "database_references_verified": True},
        "encrypted": True, "algorithm": "AES-256-GCM",
        "key_custody": "separate private on-host directory; not off-host custody",
        "resource_preflight": admission,
        "pinned_release_evidence": bool(pinned_release_evidence),
        "backup_expiry_days": 30,
        "rpo_rto_reference_gate_passed": False,
    }
    manifest_path = private / "paired-manifest.json"
    with manifest_path.open("xb") as stream:
        stream.write(json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    secure_private_file(manifest_path, inherited=True)
    from scripts.backup_retention import register

    inventory_path = register(private, key_dir, pinned_release_evidence=pinned_release_evidence)
    return {"status": "paired_backup_captured", "backup_id": stamp,
            "private_directory": str(private), "inventory": str(inventory_path),
            "manifest": str(manifest_path), "database_count": len(dump_records),
            "artifact_count": len(artifact_inventory), "resource_preflight": admission,
            "artifact_encrypted_path": str(artifact_path),
            "artifact_key_path": str(artifact_key),
            "artifact_envelope": artifact_envelope,
            "artifact_inventory": artifact_inventory,
            "artifact_rows": artifact_rows,
            "database_files": {
                database: {"encrypted_path": str(private / record["encrypted_path"]),
                           "key_path": str(key_dir / record["key_path"]),
                           "envelope": record["envelope"]}
                for database, record in dump_records.items()
            }}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--stamp")
    parser.add_argument("--pin-release-evidence", action="store_true")
    args = parser.parse_args()
    receipt_path = os.getenv("FOLIO_BACKUP_COMPLETION_RECEIPT")

    def receipt(status, *, error=None, result=None):
        if not receipt_path:
            return
        from folio.private_storage import secure_private_directory, secure_private_file

        target = Path(receipt_path)
        expected_parent = ROOT / ".local/live-round4"
        from folio.private_storage import _validate_existing_ancestors

        target = Path(os.path.abspath(os.fspath(target)))
        if target.parent != Path(os.path.abspath(os.fspath(expected_parent))):
            raise RuntimeError("BACKUP_RECEIPT_PATH_INVALID")
        _validate_existing_ancestors(target.parent)
        secure_private_directory(expected_parent)
        payload = {"schema": "folio-paired-backup-receipt-v1", "status": status,
                   "finished_at": datetime.now(timezone.utc).isoformat(),
                   "backup_id": args.stamp,
                   "database_count": (result or {}).get("database_count", 0),
                   "artifact_count": (result or {}).get("artifact_count", 0),
                   "error": error}
        with target.open("xb") as stream:
            stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        secure_private_file(target, inherited=True)

    try:
        env = environment("browser")
        result = capture_quiesced_pair(env, stamp=args.stamp,
                                       pinned_release_evidence=args.pin_release_evidence)
        receipt("success", result=result)
        print(json.dumps({key: result[key] for key in
                          ("status", "backup_id", "database_count", "artifact_count")}))
    except RuntimeError as error:
        # Error codes are intentionally closed; do not echo database, ACL or OS details.
        code = str(error)
        receipt("failure", error=code if code.isupper() and len(code) < 100 else "PAIRED_BACKUP_FAILED")
        raise SystemExit(code) from None
    except Exception as error:
        receipt("failure", error=type(error).__name__)
        raise SystemExit("PAIRED_BACKUP_FAILED:" + type(error).__name__) from None


if __name__ == "__main__":
    main()
