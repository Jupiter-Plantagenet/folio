"""Offline regex secret scanner for the maintained Folio source and evidence.

Findings never contain matched values. Directory walking is C:-anchored, checks
the complete path ancestry before enumeration, and refuses reparse points.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parent.parent
ALLOWLIST = ROOT / "ops/secret-scan-allowlist.json"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

PATTERNS = {
    "dsn_with_password": re.compile(
        r"postgres(?:ql)?://[^:/\s\"'@{}]+:(?!\{|<|\*{3})[^@\s\"'{}]{6,}@"
    ),
    "pem_private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "json_private_key": re.compile(r"\"private_key\"\s*:\s*\"[A-Za-z0-9+/=_-]{32,}\""),
    "json_password": re.compile(r"\"password\"\s*:\s*\"[^\"<]{12,}\""),
    "json_service_token": re.compile(
        r"\"(?:broker|dispatcher|verifier|token|secret|api_key)\"\s*:\s*\"[A-Za-z0-9_-]{32,}\""
    ),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "openai_like_key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}\b"),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "slack_token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{20,}\b"),
}
TEXT_SUFFIXES = {
    ".py", ".sh", ".ps1", ".ts", ".tsx", ".js", ".mjs", ".json", ".jsonl", ".md",
    ".txt", ".log", ".sql", ".toml", ".ini", ".yml", ".yaml", ".csv", ".env",
    ".html", ".css", ".in", ".lock", ".cfg", ".xml", ".sha256", "",
}
CERTIFIABLE_SUFFIXES = {
    ".cfg", ".css", ".csv", ".env", ".html", ".in", ".ini", ".js", ".json",
    ".jsonl", ".lock", ".log", ".md", ".mjs", ".py", ".ps1", ".sh", ".sql",
    ".sha256", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
SKIP_DIRS = {
    ".venv", "node_modules", "__pycache__", ".git", ".ruff_cache",
    ".pytest_cache", "evaluation-sealed", ".local", ".cache", "dist",
}
MAX_BYTES = 8 * 1024 * 1024
MAX_FINDINGS_PER_FILE = 4096


class UnsafeScanPath(RuntimeError):
    """The requested scan crosses a non-C: path or a reparse point."""


class UnsupportedScanContent(RuntimeError):
    """A file cannot be certified by the UTF-8 regex scanner."""


def _is_reparse(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise UnsafeScanPath("SCAN_PATH_UNREADABLE") from exc
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    )


def validate_c_ancestry(path):
    """Lstat every existing component from C:\\ through the requested path."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute.drive.upper() != "C:":
        raise UnsafeScanPath("SCAN_PATH_MUST_BE_ON_C")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if ":" in part:
            raise UnsafeScanPath("SCAN_PATH_ALTERNATE_STREAM")
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise UnsafeScanPath("SCAN_PATH_COMPONENT_MISSING") from exc
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & REPARSE_POINT
        ):
            raise UnsafeScanPath("SCAN_PATH_REPARSE_POINT")
    return absolute


def make_safe_directory(path):
    """Create missing directory components only after lstat-validating ancestry."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute.drive.upper() != "C:":
        raise UnsafeScanPath("SCAN_PATH_MUST_BE_ON_C")
    current = Path(absolute.anchor)
    root_info = os.lstat(current)
    if stat.S_ISLNK(root_info.st_mode) or bool(
        getattr(root_info, "st_file_attributes", 0) & REPARSE_POINT
    ) or not stat.S_ISDIR(root_info.st_mode):
        raise UnsafeScanPath("SCAN_C_ROOT_UNSAFE")
    for part in absolute.parts[1:]:
        if ":" in part:
            raise UnsafeScanPath("SCAN_PATH_ALTERNATE_STREAM")
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
                info = os.lstat(current)
            except FileExistsError:
                info = os.lstat(current)
            except OSError as exc:
                raise UnsafeScanPath("SCAN_DIRECTORY_CREATE_FAILED") from exc
        except OSError as exc:
            raise UnsafeScanPath("SCAN_PATH_UNREADABLE") from exc
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & REPARSE_POINT
        ):
            raise UnsafeScanPath("SCAN_PATH_REPARSE_POINT")
        if current != absolute and not stat.S_ISDIR(info.st_mode):
            raise UnsafeScanPath("SCAN_ANCESTOR_NOT_DIRECTORY")
    if not stat.S_ISDIR(os.lstat(absolute).st_mode):
        raise UnsafeScanPath("SCAN_PATH_NOT_DIRECTORY")
    return absolute


def copy_file_exclusive(source, destination):
    """Copy a checked regular file without following existing destination links."""
    source = validate_c_ancestry(source)
    destination = Path(os.path.abspath(os.fspath(destination)))
    if destination.drive.upper() != "C:" or ":" in destination.name:
        raise UnsafeScanPath("SCAN_DESTINATION_UNSAFE")
    validate_c_ancestry(destination.parent)
    before = os.lstat(source)
    if not stat.S_ISREG(before.st_mode) or bool(
        getattr(before, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise UnsafeScanPath("SCAN_SOURCE_NOT_REGULAR")
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    )
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise UnsafeScanPath("SCAN_SOURCE_OPEN_FAILED") from exc
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_ino and opened.st_ino and before.st_ino != opened.st_ino
        ):
            raise UnsafeScanPath("SCAN_SOURCE_CHANGED_DURING_OPEN")
        try:
            destination_fd = os.open(destination, destination_flags, 0o600)
        except OSError as exc:
            raise UnsafeScanPath("SCAN_DESTINATION_CREATE_FAILED") from exc
        digest = hashlib.sha256()
        with os.fdopen(source_fd, "rb") as source_stream:
            source_fd = -1
            with os.fdopen(destination_fd, "wb") as destination_stream:
                while chunk := source_stream.read(1024 * 1024):
                    destination_stream.write(chunk)
                    digest.update(chunk)
        return digest.hexdigest()
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def write_bytes_exclusive(destination, data):
    """Create a new regular file without following an existing target link."""
    destination = Path(os.path.abspath(os.fspath(destination)))
    if destination.drive.upper() != "C:" or ":" in destination.name:
        raise UnsafeScanPath("SCAN_DESTINATION_UNSAFE")
    validate_c_ancestry(destination.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise UnsafeScanPath("SCAN_DESTINATION_CREATE_FAILED") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except OSError as exc:
        raise UnsafeScanPath("SCAN_DESTINATION_WRITE_FAILED") from exc


def read_regular_file(path, *, max_bytes=MAX_BYTES):
    """Read a regular file through a no-follow descriptor where supported."""
    path = validate_c_ancestry(path)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or bool(
        getattr(before, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise UnsafeScanPath("SCAN_SOURCE_NOT_REGULAR")
    if max_bytes is not None and before.st_size > max_bytes:
        raise UnsafeScanPath("SCAN_FILE_EXCEEDS_LIMIT")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UnsafeScanPath("SCAN_FILE_OPEN_FAILED") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_ino and opened.st_ino and before.st_ino != opened.st_ino
        ):
            raise UnsafeScanPath("SCAN_FILE_CHANGED_DURING_OPEN")
        chunks = []
        total = 0
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise UnsafeScanPath("SCAN_FILE_EXCEEDS_LIMIT")
                chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if fd >= 0:
            os.close(fd)


def regular_file_size(path):
    """Return size from lstat only after rejecting links and non-files."""
    path = validate_c_ancestry(path)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise UnsafeScanPath("SCAN_SOURCE_NOT_REGULAR")
    return info.st_size


def iter_files(paths, *, text_only=True):
    """Enumerate without following symlinks/junctions; refuse unsafe trees."""
    for raw in paths:
        root = validate_c_ancestry(raw)
        if root.is_file():
            if not text_only or root.suffix.lower() in TEXT_SUFFIXES:
                yield root
            continue
        if not root.is_dir():
            raise UnsafeScanPath("SCAN_PATH_NOT_FILE_OR_DIRECTORY")
        stack = [root]
        while stack:
            folder = stack.pop()
            validate_c_ancestry(folder)
            try:
                with os.scandir(folder) as entries:
                    rows = sorted(entries, key=lambda entry: entry.name.lower())
            except OSError as exc:
                raise UnsafeScanPath("SCAN_DIRECTORY_UNREADABLE") from exc
            children = []
            for entry in rows:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS:
                        continue
                    if _is_reparse(entry.path):
                        raise UnsafeScanPath("SCAN_PATH_REPARSE_POINT")
                    children.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if _is_reparse(entry.path):
                        raise UnsafeScanPath("SCAN_PATH_REPARSE_POINT")
                    path = Path(entry.path)
                    if not text_only or path.suffix.lower() in TEXT_SUFFIXES:
                        yield path
                elif _is_reparse(entry.path):
                    raise UnsafeScanPath("SCAN_PATH_REPARSE_POINT")
            stack.extend(reversed(children))


def plausible_token(text):
    """Skip identifier-like values such as ``independent-rational-nochange-v1``."""
    value = text.rsplit(":", 1)[1].strip().strip('"')
    return any(c.isdigit() for c in value) and value.count("-") < 3


def scan_file(path):
    return scan_bytes(path, read_regular_file(path))


def scan_bytes(path, raw):
    """Scan these exact bounded bytes as an allowlisted strict UTF-8 text format."""
    if Path(path).suffix.lower() not in CERTIFIABLE_SUFFIXES:
        raise UnsupportedScanContent("OUTPUT_FORMAT_UNSUPPORTED")
    if b"\x00" in raw:
        raise UnsupportedScanContent("OUTPUT_CONTAINS_NUL")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise UnsupportedScanContent("OUTPUT_NOT_STRICT_UTF8") from exc
    if any(ord(char) < 0x20 and char not in "\t\n\r\f" for char in text):
        raise UnsupportedScanContent("OUTPUT_HAS_BINARY_CONTROL_BYTES")
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, rx in PATTERNS.items():
            for match in rx.finditer(line):
                if name == "json_service_token" and not plausible_token(match.group(0)):
                    continue
                found.append(
                    {
                        "path": rel(path),
                        "line": lineno,
                        "pattern": name,
                        "match_sha256": hashlib.sha256(match.group(0).encode()).hexdigest(),
                    }
                )
                if len(found) >= MAX_FINDINGS_PER_FILE:
                    raise UnsupportedScanContent("OUTPUT_MATCH_COUNT_LIMIT")
    return found


def rel(path):
    try:
        return Path(os.path.abspath(path)).relative_to(ROOT).as_posix()
    except ValueError:
        return Path(os.path.abspath(path)).as_posix()


def scan(paths):
    return [finding for file_path in iter_files(paths) for finding in scan_file(file_path)]


def allowed(findings):
    try:
        validate_c_ancestry(ALLOWLIST)
    except UnsafeScanPath as exc:
        if "COMPONENT_MISSING" not in str(exc):
            raise
        rules = []
    else:
        rules = json.loads(read_regular_file(ALLOWLIST).decode("utf-8"))
    keys = {(rule["path"], rule["pattern"], rule["match_sha256"]) for rule in rules}
    return [finding for finding in findings if (finding["path"], finding["pattern"], finding["match_sha256"]) not in keys]


def source_paths():
    sys.path.insert(0, str(ROOT / "scripts"))
    from round5_inventory import source_files

    return [ROOT / path for path in source_files()]


def main():
    validate_c_ancestry(ROOT)
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--source", action="store_true")
    ap.add_argument("--evidence", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()
    paths = list(args.paths)
    if args.source:
        paths += source_paths()
    if args.evidence:
        paths += [ROOT / "evidence"] + sorted((ROOT / "docs").glob("round*-logs"))
    if not paths:
        ap.error("nothing to scan")
    # Materialize once so an enumeration failure cannot become a partial clean result.
    files = list(iter_files(paths))
    findings = [finding for path in files for finding in scan_file(path)]
    open_findings = allowed(findings)
    report = {
        "scanner": "folio scripts/secret_scan.py (regex, offline)",
        "patterns": sorted(PATTERNS),
        "files_scanned": len(files),
        "findings": len(findings),
        "unallowlisted": open_findings,
    }
    if args.json:
        output = Path(os.path.abspath(args.json))
        make_safe_directory(output.parent)
        write_bytes_exclusive(output, json.dumps(report, indent=2).encode("utf-8"))
    for finding in open_findings:
        print(f"{finding['path']}:{finding['line']}: {finding['pattern']} (sha256 {finding['match_sha256'][:12]})")
    print(f"SECRET_SCAN files={len(files)} findings={len(findings)} unallowlisted={len(open_findings)}")
    return 1 if open_findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UnsafeScanPath as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
