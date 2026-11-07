"""Archive old proof source mirrors privately, preserving every byte and a path receipt.

Dry run by default. No directory links are followed. This replaces deep scanning
of dependency mirrors: future proofs already keep their mirrors private.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def mirrors():
    for folder, dirs, _ in os.walk(ROOT / "evidence", followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d not in {".venv", "node_modules", ".git", ".cache"}
            and not (Path(folder) / d).is_symlink()
        ]
        if "source-mirror" in dirs:
            yield Path(folder) / "source-mirror"
            dirs.remove("source-mirror")


def credential_metadata(mirror):
    entries = []
    local = mirror / ".local"
    if not local.is_dir():
        return entries
    for folder, dirs, names in os.walk(local, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d not in {".venv", "node_modules", ".git"}
            and not (Path(folder) / d).is_symlink()
        ]
        for name in names:
            path = Path(folder) / name
            if (
                path.is_symlink()
                or path.suffix != ".json"
                or path.stat().st_size > 1024 * 1024
            ):
                continue
            raw = path.read_bytes()
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            roles = set()

            def visit(value):
                if isinstance(value, dict):
                    if isinstance(value.get("username"), str):
                        roles.add(value["username"])
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
                elif isinstance(value, str) and value.startswith(
                    ("postgres://", "postgresql://")
                ):
                    user = urlsplit(value).username
                    if user:
                        roles.add(user)

            visit(data)
            entries.append(
                {
                    "path": path.relative_to(mirror).as_posix(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "roles": sorted(roles),
                }
            )
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"at": stamp, "apply": args.apply, "mirrors": []}
    for mirror in mirrors():
        # Already archived locations contain just the receipt.
        if (mirror / "ARCHIVED.json").is_file():
            continue
        rel = mirror.relative_to(ROOT)
        destination = ROOT / ".local/quarantined-evidence-mirrors" / rel
        entry = {
            "original_path": rel.as_posix(),
            "archive_path": destination.relative_to(ROOT).as_posix(),
            "credential_files": credential_metadata(mirror),
            "reason": "Private source mirror with test identities; all original bytes archived, none deleted",
        }
        if args.apply:
            if not mirror.resolve().is_relative_to((ROOT / "evidence").resolve()):
                raise RuntimeError("ARCHIVE_SOURCE_OUTSIDE_EVIDENCE")
            if not destination.resolve().is_relative_to((ROOT / ".local").resolve()):
                raise RuntimeError("ARCHIVE_TARGET_OUTSIDE_PRIVATE_ROOT")
            if destination.exists():
                raise RuntimeError("ARCHIVE_TARGET_EXISTS")
            destination.parent.mkdir(parents=True, exist_ok=True)
            mirror.rename(destination)
            mirror.mkdir()
            (mirror / "ARCHIVED.json").write_text(json.dumps(entry, indent=2))
        report["mirrors"].append(entry)
    out = ROOT / ".local/secdata-20261101" / ("mirror-archive-" + stamp + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "apply": args.apply,
                "mirrors": len(report["mirrors"]),
                "credential_files": sum(
                    len(e["credential_files"]) for e in report["mirrors"]
                ),
                "report": str(out.relative_to(ROOT)),
            }
        )
    )


if __name__ == "__main__":
    main()
