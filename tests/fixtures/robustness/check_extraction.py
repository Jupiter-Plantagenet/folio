"""Grade folio.provenance.extract_packet on the robustness fixtures.

Reads the container-parsed sources stored by eval/prepare_cases.py in
set/cases/, re-runs the CURRENT extractor and compares with labels.json.
A field labelled None must be unresolved (missing or conflicting). A field
listed under "ocr_or_unresolved" may also be unresolved (honest hold), but a
wrong value is always a failure.

    python tests/fixtures/robustness/check_extraction.py [--json]
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from folio.provenance import extract_packet  # noqa: E402


def grade(case, expect):
    ex = extract_packet(case["sources"])
    values = dict(ex["values"])
    unresolved = set(ex["missing"]) | set(ex["conflicts"])
    problems, held = [], []
    lenient = set(expect.get("ocr_or_unresolved", []))
    for field, want in expect.items():
        if field in ("ocr_or_unresolved", "supplier_check", "conflict"):
            continue
        got = values.get(field)
        if want is None:
            if field not in unresolved:
                problems.append(f"{field}: expected unresolved, got {got!r}")
        elif field in unresolved or got is None:
            (held if field in lenient else problems).append(f"{field}: unresolved (want {want!r})")
        elif got != want:
            problems.append(f"{field}: got {got!r}, want {want!r}")
    if "supplier_check" in expect and ex["supplier_check"]["status"] != expect["supplier_check"]:
        problems.append(f"supplier_check: {ex['supplier_check']['status']} != {expect['supplier_check']}")
    if "conflict" in expect and expect["conflict"] not in ex["conflicts"]:
        problems.append(f"conflict {expect['conflict']} not reported ({ex['conflicts']})")
    return problems, held, ex


def main():
    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))["tasks"]
    report = {}
    for path in sorted((HERE / "set" / "cases").glob("*.json")):
        case = json.loads(path.read_text())
        problems, held, ex = grade(case, labels[case["task_id"]]["expect"])
        report[case["task_id"]] = {
            "ok": not problems,
            "problems": problems,
            "honest_holds": held,
            "missing": ex["missing"],
            "conflicts": ex["conflicts"],
        }
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        for tid, r in report.items():
            print(("PASS " if r["ok"] else "FAIL ") + tid, r["problems"] or "", r["honest_holds"] or "", "missing=", r["missing"], "conflicts=", r["conflicts"])
    ok = sum(r["ok"] for r in report.values())
    print(json.dumps({"extraction_pass": ok, "of": len(report)}))
    return 0 if ok == len(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
