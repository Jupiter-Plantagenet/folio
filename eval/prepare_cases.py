"""Turn authored packets into product cases using the REAL product intake path.

Each file goes through folio.sandbox.parse_isolated (networkless container) and
folio.provenance.extract_packet exactly as POST /intakes/files does. Rejected
files are recorded, never dropped silently. No labels are read here.

Usage: python eval/prepare_cases.py <set_dir>   (writes <set_dir>/cases/<task>.json)
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from folio.original_download import UPLOADED_AUTHORSHIP  # noqa: E402
from folio.provenance import FIELDS, extract_packet  # noqa: E402
from folio.sandbox import parse_isolated  # noqa: E402


def parse_with_retry(raw, name):
    for _ in range(60):
        try:
            return parse_isolated(raw, name)
        except ValueError as e:
            if str(e) != "PARSER_CAPACITY_RETRY":
                raise
            time.sleep(0.25)
    raise ValueError("PARSER_CAPACITY_RETRY")


def prepare(set_dir, entry):
    folder = set_dir / "packets" / entry["task_id"]
    out = set_dir / "cases" / (entry["task_id"] + ".json")
    if out.exists():
        return "cached"
    sources, rejected, timings = [], [], []
    for f in entry["files"]:
        raw = (folder / f["name"]).read_bytes()
        started = time.monotonic()
        try:
            parsed = parse_with_retry(raw, f["name"])
            sources.append({**parsed, "id": str(uuid5(NAMESPACE_URL, entry["task_id"] + "/" + f["name"])),
                            "name": f["name"], "document_role": f["document_role"],
                            "version": 1, "authorship": UPLOADED_AUTHORSHIP})
        except ValueError as e:
            rejected.append({"name": f["name"], "reason": str(e)[:250]})
        timings.append({"name": f["name"], "parse_seconds": round(time.monotonic() - started, 3)})
    case = {"task_id": entry["task_id"], "sources": sources, "rejected": rejected, "parse_timings": timings}
    if sources:
        ex = extract_packet(sources)
        case.update({k: ex["values"].get(k, "") for k in FIELDS})
        case.update(receipt_manifest=ex["receipt_manifest"], field_provenance=ex["field_provenance"],
                    missing_fields=ex["missing"], conflicting_fields=ex["conflicts"],
                    invoice_supplier=ex.get("invoice_supplier"), supplier_check=ex.get("supplier_check"))
    case["payable"] = json.loads((folder / "payable.json").read_text())
    case["target_id"] = case["payable"]["id"]
    out.write_text(json.dumps(case))
    return "ok" if not rejected else "partial"


def reextract(set_dir):
    """Re-run ONLY the product extractor on already-parsed sources (no re-parse,
    no input/label change) after an extractor change; records the extractor hash."""
    import hashlib

    digest = hashlib.sha256(
        (Path(__file__).resolve().parents[1] / "folio/provenance.py").read_bytes()
    ).hexdigest()
    for path in sorted((set_dir / "cases").glob("*.json")):
        case = json.loads(path.read_text())
        if case["sources"]:
            ex = extract_packet(case["sources"])
            case.update({k: ex["values"].get(k, "") for k in FIELDS})
            case.update(receipt_manifest=ex["receipt_manifest"], field_provenance=ex["field_provenance"],
                        missing_fields=ex["missing"], conflicting_fields=ex["conflicts"],
                        invoice_supplier=ex.get("invoice_supplier"), supplier_check=ex.get("supplier_check"))
        case["extractor_source_sha256"] = digest
        path.write_text(json.dumps(case))
    print(json.dumps({"reextracted": str(set_dir), "provenance_sha256": digest}))


def main():
    if sys.argv[1] == "--reextract":
        return reextract(Path(sys.argv[2]))
    set_dir = Path(sys.argv[1])
    (set_dir / "cases").mkdir(exist_ok=True)
    manifest = json.loads((set_dir / "manifest.json").read_text())
    started = time.monotonic()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda e: (e["task_id"], prepare(set_dir, e)), manifest["tasks"]))
    summary = {"set": manifest["set"], "tasks": len(results),
               "status_counts": {s: sum(1 for _, r in results if r == s) for s in {r for _, r in results}},
               "wall_seconds": round(time.monotonic() - started, 1)}
    (set_dir / "prepare-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
