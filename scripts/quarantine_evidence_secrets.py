"""Move credential files out of evidence/ into the private, gitignored quarantine.

Each moved file is replaced in place by `<name>.REDACTED.json` recording the
original path, SHA256, size, reason and quarantine location, so the evidence
chain stays auditable. Nothing is deleted. Values are never printed.

  python -I scripts/quarantine_evidence_secrets.py            # dry run
  python -I scripts/quarantine_evidence_secrets.py --apply
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
QUARANTINE = ROOT / ".local/quarantined-evidence-secrets"
MANIFEST = EVIDENCE / "documents/delivery/round6/quarantine-manifest.json"
sys.path.insert(0, str(ROOT / "scripts"))
from secret_scan import scan_file  # noqa: E402

# Files under an evidence `.local/` tree that hold credentials by construction.
CREDENTIAL_NAMES = {"databases.json", "approver.json", "worker.json", "verifier.json"}
REASONS = {
    "databases.json": "disposable-role DSNs with passwords written by a test run whose ROOT was an evidence mirror",
    "approver.json": "disposable Ed25519 approval private key",
    "worker.json": "disposable broker/dispatcher service tokens",
    "verifier.json": "disposable verifier service token",
    "database-identities": "disposable PostgreSQL role username+password",
}
SOURCE_SUFFIXES = {".py", ".sh", ".ts", ".tsx", ".js", ".md", ".sql"}


def roles_in(path):
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    roles = []
    # Logs can expose DSNs too; do not require a top-level JSON object.
    for match in re.finditer(r"postgres(?:ql)?://[^\s\"'<>]+", raw):
        try:
            user = urlsplit(match.group()).username
            if user and re.fullmatch(r"folio_[A-Za-z0-9_]+", user):
                roles.append(user)
        except ValueError:
            pass
    try:
        data = json.loads(raw)
    except ValueError:
        data = None

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("username"), str):
                roles.append(value["username"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    return sorted(set(roles))


def io_path(path):
    """Private archival paths can exceed Windows MAX_PATH after adding a prefix."""
    return Path("\\\\?\\" + str(path.absolute())) if os.name == "nt" else path


def candidates():
    for f in sorted(EVIDENCE.rglob("*")):
        if not f.is_file() or f.name.endswith(".REDACTED.json"):
            continue
        parts = f.relative_to(EVIDENCE).parts
        if ".local" in parts:
            if f.name in CREDENTIAL_NAMES:
                yield f, REASONS[f.name]
                continue
            if "database-identities" in parts:
                yield f, REASONS["database-identities"]
                continue
        # Any other non-source evidence file carrying a secret pattern.
        if f.suffix.lower() not in SOURCE_SUFFIXES:
            hits = scan_file(f)
            if hits:
                kinds = sorted({h["pattern"] for h in hits})
                yield f, "secret pattern(s) in evidence output: " + ",".join(kinds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for f, reason in candidates():
        raw = f.read_bytes()
        rel = f.relative_to(ROOT).as_posix()
        dest = QUARANTINE / f.relative_to(ROOT)
        entry = {
            "original_path": rel,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "reason": reason,
            "quarantined_to": dest.relative_to(ROOT).as_posix(),
            "roles_referenced": roles_in(f),
            "moved_at": stamp,
            "action": "moved (not deleted); value never printed",
        }
        entries.append(entry)
        if a.apply:
            if not dest.resolve().is_relative_to(QUARANTINE.resolve()):
                raise SystemExit("QUARANTINE_PATH_OUTSIDE_PRIVATE_ROOT")
            io_path(dest.parent).mkdir(parents=True, exist_ok=True)
            if io_path(dest).exists():
                raise SystemExit("QUARANTINE_TARGET_EXISTS " + entry["quarantined_to"])
            shutil.copy2(f, io_path(dest))
            if (
                hashlib.sha256(io_path(dest).read_bytes()).hexdigest()
                != entry["sha256"]
            ):
                raise SystemExit("QUARANTINE_COPY_MISMATCH " + rel)
            f.unlink()
            note = f.with_name(f.name + ".REDACTED.json")
            note.write_text(
                json.dumps(
                    {"redaction": "round6-credential-quarantine", **entry}, indent=2
                )
            )
    previous = (
        json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {"entries": []}
    )
    if a.apply and entries:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(
            json.dumps(
                {
                    "quarantine_root": QUARANTINE.relative_to(ROOT).as_posix()
                    + " (gitignored, private)",
                    "entries": previous["entries"] + entries,
                },
                indent=2,
            )
        )
    roles = sorted({r for e in entries for r in e["roles_referenced"]})
    inventory = (
        ROOT
        / ".local/secdata-20261101"
        / (
            "quarantine-inventory-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".json"
        )
    )
    inventory.parent.mkdir(parents=True, exist_ok=True)
    with inventory.open("x", encoding="utf-8") as stream:
        json.dump({"apply": a.apply, "entries": entries}, stream, indent=2)
    print(
        json.dumps(
            {
                "apply": a.apply,
                "files": len(entries),
                "roles_referenced": len(roles),
                "by_reason": {
                    r: sum(1 for e in entries if e["reason"] == r)
                    for r in {e["reason"] for e in entries}
                },
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
