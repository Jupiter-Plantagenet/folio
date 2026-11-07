"""Seed exact synthetic payable targets into a disposable Folio profile."""

import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/fixtures/generality"))
from build_fixtures import lines, pdf  # noqa: E402

from folio import target  # noqa: E402
from folio import workbench as wb  # noqa: E402


def main():
    fixtures = [
        ("EUR", "kg", "48.00", "6220.80"),
        ("JPY", "m", "48", "6221"),
        ("KWD", "box", "48.125", "6237.000"),
    ]
    result = []
    input_root = Path(os.environ["FOLIO_DIMENSION_INPUT_ROOT"])
    input_root.mkdir(parents=True, exist_ok=True)
    for currency, unit, price, face in fixtures:
        target_id = str(uuid4())
        invoice = f"INV-DIM-{currency}-{unit.upper()}"
        purchase_order = f"PO-DIM-{currency}-{unit.upper()}"
        receipt_id = f"GR-{currency}-{unit.upper()}"
        documents = [
            (
                "invoice.pdf",
                "invoice",
                [
                    "Aster Supply Co.",
                    f"Invoice {invoice}",
                    f"Purchase order {purchase_order}",
                    "Quantity 120",
                    f"Unit price {currency} {price}",
                    "Tax rate 8%",
                    f"Invoice face total {currency} {face}",
                ],
            ),
            (
                "purchase-order.pdf",
                "purchase_order",
                [
                    f"Purchase order {purchase_order}",
                    "Supplier Aster Supply Co.",
                    "Ordered quantity 120",
                ],
            ),
            (
                "receipt.pdf",
                "receipt",
                [
                    f"Goods receipt {receipt_id}",
                    f"Purchase order {purchase_order}",
                    "Received quantity 100",
                    f"Unit {unit}",
                    f"Currency {currency}",
                ],
            ),
        ]
        file_manifest = []
        for name, role, text_rows in documents:
            raw = pdf(lines(text_rows))
            path = input_root / f"{currency}-{unit}" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            file_manifest.append(
                {
                    "name": name,
                    "role": role,
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        body = target.seed(wb.WORKSPACE, target_id)
        body.update(
            invoice_key=invoice,
            currency=currency,
            unit=unit,
            invoice_face_amount=face,
        )
        with psycopg.connect(target.DSN) as db:
            db.execute(
                "UPDATE payables SET body=%s WHERE workspace=%s AND id=%s",
                (Jsonb(body), wb.WORKSPACE, target_id),
            )
        result.append(
            {
                "target_id": target_id,
                "invoice": invoice,
                "currency": currency,
                "unit": unit,
                "unit_price": price,
                "face": face,
                "files": file_manifest,
            }
        )
    output = Path(os.environ["FOLIO_DIMENSION_FIXTURE_MANIFEST"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
