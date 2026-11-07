"""Verify a sealed held-out root and copy selected populations to a work directory.

Never writes inside the sealed root. Verification checks the pinned digest of the
root MANIFEST.sha256 and every file listed in it (sha256sum format).

Usage: python eval/stage_sealed.py <sealed_root> <expected_manifest_sha256> <work_dir> [population ...]
"""

import hashlib
import shutil
import sys
from pathlib import Path


def verify(root, expected):
    manifest = root / "MANIFEST.sha256"
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if digest != expected:
        raise SystemExit(f"SEAL_MANIFEST_DIGEST_MISMATCH {digest}")
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        want, relative = line.split(maxsplit=1)
        relative = relative.lstrip("*")
        path = root / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != want:
            raise SystemExit(f"SEAL_FILE_MISMATCH {relative}")
        checked += 1
    return checked


def main():
    root, expected, work = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    populations = sys.argv[4:]
    checked = verify(root, expected)
    print(f"seal verified: {checked} files")
    for name in populations:
        destination = work / name
        if destination.exists():
            raise SystemExit(f"WORK_DIR_EXISTS {destination}")
        shutil.copytree(root / name, destination)
        print(f"staged {name} -> {destination}")


if __name__ == "__main__":
    main()
