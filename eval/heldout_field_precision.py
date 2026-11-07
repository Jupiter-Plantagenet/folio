"""Supported-field precision of the current extractor on a held-out set.

Re-runs folio.provenance.extract_packet on container-parsed sources in
<set_dir>/cases/ and compares each produced value with the evaluator-only field
truth (heldout public schema truth keys). Precision = correct / produced, where a
field left missing or conflicting is an honest refusal, not an error. Decimals
compare numerically; supplier names and unit codes use the product's own
normalization (so EA/each or HOUR/h are not counted as errors).

Usage: python eval/heldout_field_precision.py <set_dir> <labels-full.json> [--json OUT]
"""

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from folio.layout import normalize_supplier  # noqa: E402
from folio.provenance import extract_packet  # noqa: E402
from folio.units import canonical_unit  # noqa: E402

# truth key -> (product field, kind)
FIELD_MAP = {
    "invoice_number": ("invoice", "text"),
    "invoiced_quantity": ("invoice_quantity", "decimal"),
    "ordered_quantity": ("ordered_quantity", "decimal"),
    "received_accepted": ("received_quantity", "decimal"),
    "unit_price": ("unit_price", "decimal"),
    "tax_rate_percent": ("tax_rate", "percent"),
    "currency": ("currency", "upper"),
    "unit": ("unit", "unit"),
    "supplier_legal": ("supplier", "supplier"),
    "invoice_issuer": ("invoice_supplier", "supplier"),
    # Not a label truth key: graded against the packet's payable.json face amount,
    # which the set author writes equal to the invoice total.
    "invoice_face_amount": ("invoice_face_amount", "decimal"),
}


def same(kind, got, want):
    try:
        if kind == "decimal":
            return Decimal(str(got)) == Decimal(str(want))
        if kind == "percent":
            return Decimal(str(got)) == Decimal(str(want)) / 100
    except InvalidOperation:
        return False
    if kind == "supplier":
        return normalize_supplier(str(got)) == normalize_supplier(str(want))
    if kind == "upper":
        return str(got).strip().upper() == str(want).strip().upper()
    if kind == "unit":
        # Truth may use unit codes (EA, HOUR); compare canonical units.
        canon = canonical_unit(str(want)) or canonical_unit(str(want).lower())
        return str(got).strip().lower() == (canon or str(want).strip().lower())
    return str(got).strip() == str(want).strip()


def main():
    set_dir, labels_path = Path(sys.argv[1]), Path(sys.argv[2])
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["tasks"]
    totals = {f: {"correct": 0, "wrong": 0, "refused": 0} for f, _ in FIELD_MAP.values()}
    tasks = 0
    for path in sorted((set_dir / "cases").glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        truth = dict(labels[case["task_id"]])
        payable = set_dir / "packets" / case["task_id"] / "payable.json"
        if payable.is_file():
            face = json.loads(payable.read_text(encoding="utf-8")).get("invoice_face_amount")
            truth.setdefault("invoice_face_amount", face)
        if not case.get("sources"):
            continue
        tasks += 1
        extracted = extract_packet(case["sources"])
        values = dict(extracted["values"])
        issuer = extracted.get("invoice_supplier")
        values["invoice_supplier"] = issuer.get("value") if isinstance(issuer, dict) else issuer
        unresolved = set(extracted["missing"]) | set(extracted["conflicts"])
        for key, (field, kind) in FIELD_MAP.items():
            # Truth keys sit on the task (v5) or inside its meta (v6).
            want = truth.get(key, (truth.get("meta") or {}).get(key))
            if want in (None, ""):
                continue
            got = values.get(field)
            tally = totals[field]
            if field in unresolved or got in (None, ""):
                tally["refused"] += 1
            elif same(kind, got, want):
                tally["correct"] += 1
            else:
                tally["wrong"] += 1
    per_field = {}
    for field, t in totals.items():
        produced = t["correct"] + t["wrong"]
        per_field[field] = {**t, "precision": round(t["correct"] / produced, 4) if produced else None}
    correct = sum(t["correct"] for t in totals.values())
    produced = sum(t["correct"] + t["wrong"] for t in totals.values())
    refused = sum(t["refused"] for t in totals.values())
    out = {
        "set": str(set_dir),
        "tasks": tasks,
        "supported_field_precision": {
            "correct": correct,
            "produced": produced,
            "rate": round(correct / produced, 4) if produced else None,
        },
        "refused_fields": refused,
        "coverage": round(produced / (produced + refused), 4) if produced + refused else None,
        "per_field": per_field,
    }
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
