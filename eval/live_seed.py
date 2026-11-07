"""Seed independently authored packets + matching Northstar payables for the
round-4 live browser journeys (retained workspace, additive only).

Target operator action: inserts NEW payable records (fresh UUIDs) into the
separate Northstar database using the target runtime principal from the private
profile. Never updates or deletes existing records. Files are written under
evidence/documents/flow/round4-live/packets/<scenario>/ (CC0 authored).
"""

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import psycopg  # noqa: E402
from packets import face_total, invoice_doc, po_doc, receipt_doc  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

WORKSPACE = "11111111-1111-4111-8111-111111111111"
OUT = ROOT / "evidence/documents/flow/round4-live/packets"


def scenario(name, stamp, inv, ordq, rec, price, tax_pct, supplier, with_receipt=True):
    from decimal import Decimal

    p = {
        "invoice": f"INV-L4{stamp}-{name}",
        "po": f"PO-L4{stamp}{name[-1]}",
        "supplier": supplier,
        "buyer": "NORTHSTAR STUDIO",
        "item": "Hex bolts M8",
        "inv_qty": str(inv),
        "ord_qty": str(ordq),
        "price": price,
        "tax_pct": tax_pct,
        "tax_rate": str(Decimal(tax_pct) / 100),
        "face": str(face_total(inv, price, Decimal(tax_pct) / 100)),
    }
    folder = OUT / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "invoice.pdf").write_bytes(invoice_doc(p))
    kind, raw = po_doc(p)
    (folder / f"purchase-order.{kind}").write_bytes(raw)
    if with_receipt:
        kind, raw = receipt_doc(p, [{"id": f"GR-L4{stamp}{name[-1]}", "qty": str(rec)}])
        (folder / f"receipt.{kind}").write_bytes(raw)
    record = str(uuid.uuid4())
    body = dict(
        id=record, revision=7, supplier=supplier, invoice_key=p["invoice"],
        invoice_face_amount=p["face"], recognized_quantity="0", net_amount="0.00",
        tax_amount="0.00", gross_amount="0.00", status="unmatched",
        bank_fingerprint="synthetic-bank-unchanged", due_date="2026-12-15", currency="USD",
    )
    meta = {"scenario": name, "packet": p, "payable": body, "with_receipt": with_receipt}
    (folder / "scenario.json").write_text(json.dumps(meta, indent=2))
    return body, meta


def main():
    stamp = sys.argv[1]
    profile = json.loads((ROOT / ".local/database-profiles.json").read_text())["browser"]
    plans = [
        ("happy1", 120, 120, 100, "48.00", 8, "Evergreen Packaging LLC"),
        ("human2", 60, 60, 50, "12.50", 5, "Brightline Parts Ltd"),
        ("hold3", 40, 40, 0, "23.75", 10, "Cobalt Fasteners Inc"),
        ("revoke4", 30, 30, 30, "7.80", 0, "Delmar Office Goods"),
    ]
    seeded = []
    with psycopg.connect(profile["FOLIO_TARGET_DSN"]) as db:
        for name, inv, ordq, rec, price, tax, supplier in plans:
            body, meta = scenario(name, stamp, inv, ordq, rec, price, tax, supplier, with_receipt=(name != "hold3"))
            db.execute("INSERT INTO payables VALUES(%s,%s,%s)", (WORKSPACE, body["id"], Jsonb(body)))
            seeded.append({"scenario": name, "payable_id": body["id"], "invoice_key": body["invoice_key"]})
    (OUT / f"seeded-{stamp}.json").write_text(json.dumps(seeded, indent=2))
    print(json.dumps(seeded, indent=2))


if __name__ == "__main__":
    main()
