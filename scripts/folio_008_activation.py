"""Bounded preparation for retained Folio migration 008.

Safety snapshots are explicitly online and unpaired; they are not acceptance
backup evidence.  This helper never performs retained DDL or service control.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from live_supervisor import EXPORT_ROOT  # noqa: E402
from runtime import environment  # noqa: E402

from folio.backup import open_backup, seal_backup  # noqa: E402
from folio.private_storage import secure_private_directory, secure_private_file  # noqa: E402


def resource_gate():
    import psutil

    free = shutil.disk_usage("C:\\").free
    available = psutil.virtual_memory().available
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory)." 
         "PercentCommittedBytesInUse"], capture_output=True, text=True, timeout=15)
    try:
        commit = float(result.stdout.strip()) / 100
    except (ValueError, TypeError):
        commit = 1.0
    return {"free_c_bytes": free, "available_ram_bytes": available,
            "system_commit_fraction": commit,
            "admitted": free >= 50 * 1024**3 and available >= 6 * 1024**3 and commit < .85}


def checked(command):
    result = subprocess.run(command, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("PRIVATE_CAPTURE_COMMAND_FAILED")
    return result.stdout


def safe_export_pack(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("EXPORT_ROOT_UNAVAILABLE")
    files = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("EXPORT_REPARSE_POINT")
        if path.is_file():
            size = path.stat().st_size
            total += size
            if total > 64 * 1024 * 1024:
                raise RuntimeError("EXPORT_PACK_LIMIT")
            files.append(path)
    from io import BytesIO
    stream = BytesIO()
    inventory = []
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            raw = path.read_bytes()
            rel = path.relative_to(root).as_posix()
            archive.writestr(rel, raw)
            inventory.append({"path": rel, "bytes": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest()})
    return stream.getvalue(), inventory


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "safety-backup":
        raise SystemExit("usage: folio_008_activation.py safety-backup")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ").lower()
    backup = secure_private_directory(ROOT / ".local/access-008-safety" / stamp)
    keys = secure_private_directory(ROOT / ".local/backup-keys/access-008" / stamp)
    report = {"schema": "folio.access-008-safety-backup/v1", "backup_id": stamp,
              "paired_consistent": False,
              "scope": "read-only sequential online snapshots; not a coherent pair",
              "database_records": [], "profiles_encrypted": False,
              "ready_exports_encrypted": False, "resource_checks": []}
    try:
        admission = resource_gate()
        report["resource_checks"].append(admission)
        if not admission["admitted"]:
            raise RuntimeError("RESOURCE_GATE_NOT_MET")
        profiles_path = ROOT / ".local/database-profiles.json"
        profiles_raw = profiles_path.read_bytes()
        profile = environment("browser")
        names = [("FOLIO_TARGET_DSN", profile["FOLIO_TARGET_DSN"], "folio_browser_target_"),
                 ("FOLIO_DSN", profile["FOLIO_DSN"], "folio_browser_wb_")]
        for label, dsn, expected in names:
            database = urlsplit(dsn).path.lstrip("/")
            if not database.startswith(expected):
                raise RuntimeError("RETAINED_DATABASE_IDENTITY_REFUSED")
            admission = resource_gate()
            report["resource_checks"].append(admission)
            if not admission["admitted"]:
                raise RuntimeError("RESOURCE_GATE_NOT_MET")
            raw = checked(["docker", "exec", "folio-postgres", "pg_dump", "-U", "postgres", "-Fc", database])
            dest, key = backup / f"{database}.dump.enc", keys / f"{database}.key"
            sealed = seal_backup(raw, dest, key, f"access-008-safety:{stamp}:{database}")
            secure_private_file(dest, inherited=True)
            secure_private_file(key, inherited=True)
            if open_backup(dest, key, f"access-008-safety:{stamp}:{database}") != raw:
                raise RuntimeError("DATABASE_BACKUP_AUTHENTICATION_FAILED")
            report["database_records"].append({"label": label, "database": database,
                                                "paired_consistent": False, **sealed})
        admission = resource_gate()
        report["resource_checks"].append(admission)
        if not admission["admitted"]:
            raise RuntimeError("RESOURCE_GATE_NOT_MET")
        globals_raw = checked(["docker", "exec", "folio-postgres", "pg_dumpall", "-U", "postgres",
                               "--globals-only", "--no-role-passwords"])
        dest, key = backup / "globals-no-passwords.sql.enc", keys / "globals-no-passwords.key"
        globals_pin = seal_backup(globals_raw, dest, key, f"access-008-safety:{stamp}:globals")
        secure_private_file(dest, inherited=True)
        secure_private_file(key, inherited=True)
        if open_backup(dest, key, f"access-008-safety:{stamp}:globals") != globals_raw:
            raise RuntimeError("GLOBALS_BACKUP_AUTHENTICATION_FAILED")
        profile_dest, profile_key = backup / "database-profiles.json.enc", keys / "database-profiles.key"
        profile_pin = seal_backup(profiles_raw, profile_dest, profile_key,
                                  f"access-008-safety:{stamp}:profiles")
        secure_private_file(profile_dest, inherited=True)
        secure_private_file(profile_key, inherited=True)
        if open_backup(profile_dest, profile_key, f"access-008-safety:{stamp}:profiles") != profiles_raw:
            raise RuntimeError("PRIVATE_PROFILE_AUTHENTICATION_FAILED")
        report["globals"] = globals_pin
        report["globals_role_passwords"] = "omitted"
        report["profiles"] = {key: value for key, value in profile_pin.items()
                              if key != "plaintext_sha256"}
        report["profiles_encrypted"] = True
        export_root = Path(EXPORT_ROOT)
        if export_root.is_dir():
            admission = resource_gate()
            report["resource_checks"].append(admission)
            if not admission["admitted"]:
                raise RuntimeError("RESOURCE_GATE_NOT_MET")
            packed, inventory = safe_export_pack(export_root)
            dest, key = backup / "ready-exports.zip.enc", keys / "ready-exports.key"
            export_pin = seal_backup(packed, dest, key, f"access-008-safety:{stamp}:exports")
            secure_private_file(dest, inherited=True)
            secure_private_file(key, inherited=True)
            if open_backup(dest, key, f"access-008-safety:{stamp}:exports") != packed:
                raise RuntimeError("EXPORT_BACKUP_AUTHENTICATION_FAILED")
            report["ready_exports"] = {"files": inventory,
                                       "encrypted_sha256": export_pin["encrypted_sha256"],
                                       "encrypted_bytes": export_pin["encrypted_bytes"]}
            report["ready_exports_encrypted"] = True
        report["status"] = "encrypted_online_safety_copy_complete"
    except Exception as error:
        report["status"] = "failed"
        report["error_code"] = str(error) if str(error).isupper() else "SAFETY_CAPTURE_FAILED"
        report["error_class"] = type(error).__name__
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report_path = backup / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    secure_private_file(report_path, inherited=True)
    print(json.dumps({"status": report["status"], "report": str(report_path.relative_to(ROOT)),
                      "database_count": len(report["database_records"]),
                      "profiles_encrypted": report["profiles_encrypted"],
                      "ready_exports_encrypted": report["ready_exports_encrypted"],
                      "paired_consistent": False}))
    return 0 if report["status"] == "encrypted_online_safety_copy_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
