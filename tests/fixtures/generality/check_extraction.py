"""Grade folio.provenance.extract_packet on the round-7 generality fixtures.

Reads container-parsed sources stored by eval/prepare_cases.py in
set/cases/ (or, with --host, parses the packet files in-process with
folio.formats.parse_document, which is the code the container runs), re-runs
the CURRENT extractor and compares with labels.json.

A field labelled None must be unresolved (missing or conflicting). A field
listed under "ocr_or_unresolved" may also be unresolved (an honest refusal),
but a wrong value is always a failure. Also prints per-field supported
precision: correct values / all values produced, over labelled fields.

    python tests/fixtures/generality/check_extraction.py [--host] [--json] [--set DIR --labels FILE]
"""

import json
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from folio.provenance import extract_packet  # noqa: E402

SKIP = ("ocr_or_unresolved", "supplier_check", "conflict")


def grade(sources, expect):
    ex = extract_packet(sources)
    values = dict(ex["values"])
    unresolved = set(ex["missing"]) | set(ex["conflicts"])
    problems, held, tally = [], [], {}
    lenient = set(expect.get("ocr_or_unresolved", []))
    for field, want in expect.items():
        if field in SKIP:
            continue
        got = values.get(field)
        t = tally.setdefault(field, {"correct": 0, "wrong": 0, "unresolved": 0, "should_refuse_ok": 0, "should_refuse_bad": 0})
        if want is None:
            if field not in unresolved:
                problems.append(f"{field}: expected unresolved, got {got!r}")
                t["should_refuse_bad"] += 1
            else:
                t["should_refuse_ok"] += 1
        elif field in unresolved or got is None:
            t["unresolved"] += 1
            (held if field in lenient else problems).append(f"{field}: unresolved (want {want!r})")
        elif got != want:
            t["wrong"] += 1
            problems.append(f"{field}: got {got!r}, want {want!r}")
        else:
            t["correct"] += 1
    if "supplier_check" in expect and ex["supplier_check"]["status"] != expect["supplier_check"]:
        problems.append(f"supplier_check: {ex['supplier_check']['status']} != {expect['supplier_check']}")
    if "conflict" in expect and expect["conflict"] not in ex["conflicts"]:
        problems.append(f"conflict {expect['conflict']} not reported ({ex['conflicts']})")
    return problems, held, ex, tally


def host_sources(set_dir, entry):
    from folio.formats import parse_document

    folder = set_dir / "packets" / entry["task_id"]
    sources, rejected = [], []
    for f in entry["files"]:
        try:
            parsed = parse_document((folder / f["name"]).read_bytes(), f["name"])
        except Exception as error:  # noqa: BLE001 - recorded, like intake
            rejected.append(f["name"] + ":" + str(error)[:80])
            continue
        sources.append({**parsed, "id": str(uuid5(NAMESPACE_URL, entry["task_id"] + "/" + f["name"])),
                        "name": f["name"], "document_role": f["document_role"], "version": 1})
    return sources, rejected


def main():
    args = sys.argv[1:]
    set_dir = Path(args[args.index("--set") + 1]) if "--set" in args else HERE / "set"
    labels_path = Path(args[args.index("--labels") + 1]) if "--labels" in args else HERE / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["tasks"]
    report, totals = {}, {}
    manifest = json.loads((set_dir / "manifest.json").read_text())
    for entry in manifest["tasks"]:
        tid = entry["task_id"]
        if "--host" in args:
            sources, rejected = host_sources(set_dir, entry)
        else:
            path = set_dir / "cases" / (tid + ".json")
            if not path.exists():
                continue
            case = json.loads(path.read_text())
            sources, rejected = case["sources"], case.get("rejected", [])
        if not sources:
            report[tid] = {"ok": False, "problems": ["no sources"], "rejected": rejected}
            continue
        problems, held, ex, tally = grade(sources, labels[tid]["expect"])
        for field, t in tally.items():
            agg = totals.setdefault(field, {k: 0 for k in t})
            for k, v in t.items():
                agg[k] += v
        report[tid] = {"ok": not problems, "problems": problems, "honest_holds": held, "rejected": rejected,
                       "missing": ex["missing"], "conflicts": ex["conflicts"]}
    precision = {}
    for field, t in sorted(totals.items()):
        produced = t["correct"] + t["wrong"] + t["should_refuse_bad"]
        precision[field] = {**t, "precision": round(t["correct"] / produced, 4) if produced else None}
    all_c = sum(t["correct"] for t in totals.values())
    all_p = sum(t["correct"] + t["wrong"] + t["should_refuse_bad"] for t in totals.values())
    out = {"extraction_pass": sum(r["ok"] for r in report.values()), "of": len(report),
           "supported_field_precision": {"correct": all_c, "produced": all_p,
                                          "rate": round(all_c / all_p, 4) if all_p else None},
           "per_field": precision}
    if "--json" in args:
        print(json.dumps({"report": report, **out}, indent=2))
    else:
        for tid, r in report.items():
            print(("PASS " if r["ok"] else "FAIL ") + tid, r["problems"] or "", r.get("honest_holds") or "",
                  "rej=" + str(r["rejected"]) if r["rejected"] else "", "conflicts=", r.get("conflicts"))
        print(json.dumps(out))
    return 0 if out["extraction_pass"] == out["of"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
