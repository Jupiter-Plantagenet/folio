"""Register and privately archive expired Folio backup inventories.

This tool never deletes backups or keys. A pinned release-evidence inventory is
exempt. The operator must only register a coherent paired backup after review.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from folio.private_storage import (  # noqa: E402
    secure_private_directory,
    secure_private_file,
    secure_private_tree,
)

BACKUP_ROOT = ROOT / ".local/backups"
KEY_ROOT = ROOT / ".local/backup-keys"
STATE_ROOT = ROOT / ".local/backup-retention"
INVENTORY = STATE_ROOT / "inventory"
ARCHIVE = STATE_ROOT / ".expired-private"
RETENTION_DAYS = 30


def _directory(path):
    from folio.private_storage import _validate_existing_ancestors

    path = Path(os.path.abspath(os.fspath(path)))
    if os.name == "nt" and path.drive.casefold() != "c:":
        raise ValueError("BACKUP_RETENTION_DRIVE_REFUSED")
    _validate_existing_ancestors(path)
    meta = path.lstat()
    if path.is_symlink() or getattr(meta, "st_file_attributes", 0) & 0x400:
        raise ValueError("BACKUP_RETENTION_REPARSE_POINT")
    if not stat.S_ISDIR(meta.st_mode):
        raise ValueError("BACKUP_RETENTION_NOT_DIRECTORY")
    return path



def _existing_inventory_directory():
    from folio.private_storage import _validate_existing_ancestors

    _validate_existing_ancestors(INVENTORY)
    try:
        metadata = INVENTORY.lstat()
    except FileNotFoundError:
        return None
    if INVENTORY.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ValueError("BACKUP_RETENTION_REPARSE_POINT")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("BACKUP_RETENTION_NOT_DIRECTORY")
    return INVENTORY


def _inside(path, parent):
    try:
        candidate = Path(os.path.abspath(os.fspath(path)))
        root = Path(os.path.abspath(os.fspath(parent)))
        if os.name == "nt" and (candidate.drive.casefold() != "c:" or root.drive.casefold() != "c:"):
            return False
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _inventory_files(root):
    root = _directory(root)
    result = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in dirs + files:
            item = Path(folder) / name
            meta = item.lstat()
            if item.is_symlink() or getattr(meta, "st_file_attributes", 0) & 0x400:
                raise ValueError("BACKUP_RETENTION_REPARSE_POINT")
        for name in sorted(files):
            item = Path(folder) / name
            if not stat.S_ISREG(item.lstat().st_mode):
                raise ValueError("BACKUP_RETENTION_NOT_REGULAR")
            digest = hashlib.sha256()
            length = 0
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    length += len(chunk)
            result.append({"path": item.relative_to(root).as_posix(), "bytes": length,
                           "sha256": digest.hexdigest()})
    return result


def register(backup_dir, keys_dir, *, pinned_release_evidence=False):
    backup_lexical = Path(os.path.abspath(os.fspath(backup_dir)))
    keys_lexical = Path(os.path.abspath(os.fspath(keys_dir)))
    if not _inside(backup_lexical, BACKUP_ROOT) or not _inside(keys_lexical, KEY_ROOT):
        raise ValueError("BACKUP_RETENTION_ROOT_INVALID")
    backup = _directory(backup_lexical)
    keys = _directory(keys_lexical)
    secure_private_tree(backup)
    secure_private_tree(keys)
    # Start the 30-day clock when this inventory is made. Backdating could make
    # a recently registered backup archive before it has been retained 30 days.
    stamp = datetime.now(timezone.utc).isoformat()
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    backup_relative = str(backup.relative_to(ROOT).as_posix())
    keys_relative = str(keys.relative_to(ROOT).as_posix())
    # Validate the inventory root and its ancestors before any exists/glob/stat.
    inventory_root = _existing_inventory_directory()
    if inventory_root is not None:
        for existing in inventory_root.glob("*.json"):
            _directory(existing.parent)
            from folio.private_storage import _validate_existing_ancestors
            _validate_existing_ancestors(existing)
            if existing.is_symlink() or getattr(existing.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("BACKUP_RETENTION_REPARSE_POINT")
            prior = json.loads(existing.read_text(encoding="utf-8"))
            if (prior.get("backup_path"), prior.get("key_path")) == (backup_relative, keys_relative):
                raise ValueError("BACKUP_RETENTION_ALREADY_REGISTERED")
    identity = str(uuid.uuid4())
    record = {
        "schema": "folio-backup-retention-v1", "id": identity,
        "created_at": dt.astimezone(timezone.utc).isoformat(),
        "retention_days": RETENTION_DAYS,
        "pinned_release_evidence": bool(pinned_release_evidence),
        "backup_path": backup_relative,
        "key_path": keys_relative,
        "backup_files": _inventory_files(backup), "key_files": _inventory_files(keys),
        "state": "active",
    }
    secure_private_directory(STATE_ROOT)
    secure_private_directory(INVENTORY)
    target = INVENTORY / (identity + ".json")
    with target.open("xb") as stream:
        stream.write(json.dumps(record, sort_keys=True, indent=2).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    secure_private_file(target, inherited=True)
    return target


def archive_expired(*, now=None, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("RETENTION_BATCH_LIMIT_INVALID")
    now = now or datetime.now(timezone.utc)
    # Lexical C: validation and lstat prewalk precede all leaf metadata/globbing.
    inventory_root = _existing_inventory_directory()
    if inventory_root is None:
        return {"archived": 0, "due": 0, "pinned_exempt": 0,
                "inventory_count": 0, "active_count": 0}
    archived = due = pinned = 0
    inventory_files = sorted(inventory_root.glob("*.json"))
    from folio.private_storage import _validate_existing_ancestors
    for candidate in inventory_files:
        _validate_existing_ancestors(candidate)
    active_count = 0
    secure_private_directory(STATE_ROOT)
    secure_private_directory(ARCHIVE)
    candidates = inventory_files
    for entry in candidates:
        if due >= limit:
            break
        _validate_existing_ancestors(entry)
        if entry.is_symlink() or getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("BACKUP_RETENTION_REPARSE_POINT")
        record = json.loads(entry.read_text(encoding="utf-8"))
        if record.get("schema") != "folio-backup-retention-v1" or record.get("state") != "active":
            continue
        try:
            if str(uuid.UUID(record.get("id", ""))) != record["id"] or entry.stem != record["id"]:
                raise ValueError("BACKUP_RETENTION_ID_INVALID")
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("BACKUP_RETENTION_ID_INVALID") from error
        active_count += 1
        if record.get("pinned_release_evidence") is True:
            pinned += 1
            continue
        created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        if created > now - timedelta(days=RETENTION_DAYS):
            continue
        due += 1
        backup = Path(os.path.abspath(os.fspath(ROOT / record["backup_path"])))
        keys = Path(os.path.abspath(os.fspath(ROOT / record["key_path"])))
        from folio.private_storage import _validate_existing_ancestors

        _validate_existing_ancestors(backup)
        _validate_existing_ancestors(keys)
        if os.name == "nt" and (backup.drive.casefold() != "c:" or keys.drive.casefold() != "c:"):
            raise ValueError("BACKUP_RETENTION_DRIVE_REFUSED")
        if not _inside(backup, BACKUP_ROOT) or not _inside(keys, KEY_ROOT):
            raise ValueError("BACKUP_RETENTION_ROOT_INVALID")
        destination = ARCHIVE / record["id"]
        destination.mkdir(mode=0o700, exist_ok=True)
        secure_private_directory(destination)
        archive_backup = destination / "backup"
        archive_keys = destination / "keys"
        current_backup = _directory(backup) if backup.exists() else _directory(archive_backup)
        current_keys = _directory(keys) if keys.exists() else _directory(archive_keys)
        if (_inventory_files(current_backup) != record["backup_files"] or
                _inventory_files(current_keys) != record["key_files"]):
            raise ValueError("BACKUP_RETENTION_INVENTORY_MISMATCH")
        # Move whole private inventory trees out of the active discovery root.
        # The operation is reversible and never unlinks bytes. A partial pair
        # remains visible in the inventory and is safely resumed on the next run.
        if ((backup.exists() and archive_backup.exists()) or
                (keys.exists() and archive_keys.exists())):
            raise ValueError("BACKUP_RETENTION_ARCHIVE_COLLISION")
        if backup.exists() and not archive_backup.exists():
            os.replace(backup, archive_backup)
        if keys.exists() and not archive_keys.exists():
            os.replace(keys, archive_keys)
        if _inventory_files(archive_backup) != record["backup_files"] or _inventory_files(archive_keys) != record["key_files"]:
            raise ValueError("BACKUP_RETENTION_ARCHIVE_VERIFY_FAILED")
        secure_private_tree(archive_backup)
        secure_private_tree(archive_keys)
        record["state"] = "archived_private"
        record["archived_at"] = now.astimezone(timezone.utc).isoformat()
        record["archive_path"] = destination.relative_to(ROOT).as_posix()
        record["restore_discovery"] = "operator review required; not implicit"
        temp = entry.with_suffix(".tmp")
        with temp.open("xb") as stream:
            stream.write(json.dumps(record, sort_keys=True, indent=2).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, entry)
        secure_private_file(entry, inherited=True)
        archived += 1
    return {"archived": archived, "due": due, "pinned_exempt": pinned,
            "inventory_count": len(inventory_files), "active_count": active_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("backup_dir")
    reg.add_argument("keys_dir")
    reg.add_argument("--pinned-release-evidence", action="store_true")
    expire = sub.add_parser("archive-expired")
    expire.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.command == "register":
        result = {"inventory": str(register(args.backup_dir, args.keys_dir,
                                            pinned_release_evidence=args.pinned_release_evidence))}
    else:
        result = archive_expired(limit=args.limit)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
