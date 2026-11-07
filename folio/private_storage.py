"""Private on-host storage helpers for Folio archives.

Windows ACLs are established once per directory per process. New files rely on
inheritance from that protected parent; existing files are reset once when first
opened for archive maintenance. All path components are checked before creation.
"""

import csv
import os
import re
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path

_cache_lock = threading.Lock()
_CACHE_MAX = 512
_secured_directories = OrderedDict()
_secured_files = OrderedDict()
_cached_sid = None


def _absolute_unresolved(path):
    """Make an absolute lexical path without resolving/following reparse points."""
    raw = Path(os.path.abspath(os.fspath(path)))
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(raw))
        if drive.casefold() != "c:" or not tail.startswith(("\\", "/")):
            raise RuntimeError("PRIVATE_ARCHIVE_DRIVE_REFUSED")
    return raw


def _is_reparse(path, metadata):
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _validate_existing_ancestors(path):
    """Reject reparse points in each existing component before touching the leaf."""
    path = _absolute_unresolved(path)
    current = Path(path.anchor)
    components = path.parts[1:] if path.anchor else path.parts
    missing = False
    for component in components:
        current = current / component
        if missing:
            continue
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError as error:
            raise RuntimeError("PRIVATE_ARCHIVE_PATH_UNAVAILABLE") from error
        if _is_reparse(current, metadata):
            raise RuntimeError("PRIVATE_ARCHIVE_REPARSE_POINT")
    return path


def _identity_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _cached(cache, key):
    with _cache_lock:
        found = key in cache
        if found:
            cache.move_to_end(key)
        return found


def _cache(cache, key):
    with _cache_lock:
        cache[key] = True
        cache.move_to_end(key)
        while len(cache) > _CACHE_MAX:
            cache.popitem(last=False)


def _windows_user_sid():
    global _cached_sid
    with _cache_lock:
        if _cached_sid:
            return _cached_sid
        # Use the Windows binary by absolute path: a PATH lookup can find a
        # different whoami (for example GNU coreutils from Git Bash).
        whoami = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe"
        )
        if not os.path.isfile(whoami):
            raise RuntimeError("PRIVATE_ARCHIVE_WHOAMI_UNAVAILABLE")
        result = subprocess.run([whoami, "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError("PRIVATE_ARCHIVE_USER_SID_UNAVAILABLE")
        try:
            fields = next(csv.reader([result.stdout.strip()]))
        except (StopIteration, csv.Error) as error:
            raise RuntimeError("PRIVATE_ARCHIVE_USER_SID_INVALID") from error
        sid = fields[-1].strip() if fields else ""
        if not re.fullmatch(r"S-1-(?:\d+-)+\d+", sid):
            raise RuntimeError("PRIVATE_ARCHIVE_USER_SID_INVALID")
        _cached_sid = sid
        return sid


def _icacls(path, *arguments):
    tool = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe"
    )
    if not os.path.isfile(tool):
        raise RuntimeError("PRIVATE_ARCHIVE_ICACLS_UNAVAILABLE")
    result = subprocess.run([tool, str(path), *arguments], capture_output=True,
                            text=True, timeout=15)
    if result.returncode != 0:
        # Do not echo Windows account, path, ACL or environment diagnostics.
        raise RuntimeError("PRIVATE_ARCHIVE_ACL_UPDATE_FAILED")


def _secure_new_or_recovered_directory(path):
    key = _identity_key(path)
    if _cached(_secured_directories, key):
        return
    sid = _windows_user_sid()
    # Reset inherited grants, remove inheritance, grant only current user and
    # LocalSystem, then verify. This runs once for each directory in this process.
    _icacls(path, "/reset")
    _icacls(path, "/inheritance:r")
    _icacls(path, "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F")
    _icacls(path, "/verify")
    _cache(_secured_directories, key)


def secure_private_directory(path):
    path = _validate_existing_ancestors(path)
    if path == Path(path.anchor):
        raise RuntimeError("PRIVATE_ARCHIVE_ROOT_REFUSED")
    # Create each missing component only after checking its existing ancestors;
    # secure only newly created components and the requested leaf. Existing
    # ancestors are checked for reparse points but never have their ACL changed.
    if os.name == "nt":
        missing = []
        current = path
        while current != Path(current.anchor):
            try:
                current.lstat()
            except FileNotFoundError:
                missing.append(current)
                current = current.parent
                continue
            break
        for directory in reversed(missing):
            _validate_existing_ancestors(directory.parent)
            directory.mkdir()
            metadata = directory.lstat()
            if _is_reparse(directory, metadata) or not directory.is_dir():
                raise RuntimeError("PRIVATE_ARCHIVE_PATH_INVALID")
            _secure_new_or_recovered_directory(directory)
        metadata = path.lstat()
        if _is_reparse(path, metadata) or not path.is_dir():
            raise RuntimeError("PRIVATE_ARCHIVE_PATH_INVALID")
        _secure_new_or_recovered_directory(path)
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
        if _is_reparse(path, metadata) or not path.is_dir():
            raise RuntimeError("PRIVATE_ARCHIVE_PATH_INVALID")
        try:
            path.chmod(0o700)
        except OSError as error:
            raise RuntimeError("PRIVATE_ARCHIVE_MODE_UPDATE_FAILED") from error
    return path


def secure_private_file(path, *, inherited=False):
    path = _validate_existing_ancestors(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError("PRIVATE_ARCHIVE_FILE_UNAVAILABLE") from error
    if _is_reparse(path, metadata) or not path.is_file():
        raise RuntimeError("PRIVATE_ARCHIVE_PATH_INVALID")
    secure_private_directory(path.parent)
    if os.name == "nt":
        key = _identity_key(path)
        if _cached(_secured_files, key) or inherited:
            _cache(_secured_files, key)
            return path
        # Existing recovered files may have explicit grants, so reset them once.
        _icacls(path, "/reset")
        _icacls(path, "/verify")
        _cache(_secured_files, key)
    else:
        try:
            path.chmod(0o600)
        except OSError as error:
            raise RuntimeError("PRIVATE_ARCHIVE_MODE_UPDATE_FAILED") from error
    return path


def secure_private_tree(path):
    """Secure a directory and existing children; later calls use the trusted cache."""
    root = secure_private_directory(path)
    for folder, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in directories:
            item = Path(folder) / name
            _validate_existing_ancestors(item)
            metadata = item.lstat()
            if _is_reparse(item, metadata):
                raise RuntimeError("PRIVATE_ARCHIVE_REPARSE_POINT")
            secure_private_directory(item)
        for name in sorted(files):
            item = Path(folder) / name
            _validate_existing_ancestors(item)
            metadata = item.lstat()
            if _is_reparse(item, metadata):
                raise RuntimeError("PRIVATE_ARCHIVE_REPARSE_POINT")
            secure_private_file(item)
    return root
