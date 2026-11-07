"""Compare `pip freeze` with a (universal, hash-pinned) requirements lock for THIS interpreter.

Usage: python -I scripts/round6_lock_check.py <lock> <freeze.txt>
Lock entries whose environment marker is false here are skipped. Exit 0 only when every
applicable pin is installed at exactly the locked version and nothing extra is installed.
"""

import json
import re
import sys

from pip._vendor.packaging.markers import Marker


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def main(lock_path, freeze_path):
    locked = {}
    for line in open(lock_path, encoding="utf-8").read().splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$", line)
        if m and (not m.group(3) or Marker(m.group(3)).evaluate()):
            locked[norm(m.group(1))] = m.group(2)
    frozen = {}
    for line in open(freeze_path, encoding="utf-8").read().splitlines():
        if "==" in line:
            name, version = line.strip().split("==", 1)
            frozen[norm(name)] = version
    report = {
        "applicable_pins": len(locked),
        "installed": len(frozen),
        "missing": sorted(set(locked) - set(frozen)),
        "extra": sorted(set(frozen) - set(locked)),
        "version_mismatch": sorted(k for k in set(locked) & set(frozen) if locked[k] != frozen[k]),
    }
    report["equal"] = not (report["missing"] or report["extra"] or report["version_mismatch"])
    print(json.dumps(report))
    return 0 if report["equal"] else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
