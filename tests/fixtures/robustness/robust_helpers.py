"""Shared builders for the round-5 robustness tests (no database, no network)."""

import hashlib
import io
from uuid import uuid4


def pdf_bytes(rows, size=11):
    """rows: (x, y_from_top, text); deterministic digital PDF."""
    from reportlab.pdfgen.canvas import Canvas

    out = io.BytesIO()
    c = Canvas(out, pagesize=(612, 792), invariant=1)
    c.setFont("Helvetica", size)
    for x, y, text in rows:
        c.drawString(x, 792 - y, text)
    c.save()
    return out.getvalue()


def stacked(texts, x=60, top=60, step=26):
    return [(x, top + i * step, t) for i, t in enumerate(texts)]


def source(raw_or_rows, role, name=None):
    """Parse with the product PDF/CSV/XLSX parser code and attach intake fields."""
    from folio.formats import parse_document

    if isinstance(raw_or_rows, list):
        raw, name = pdf_bytes(raw_or_rows), name or "doc.pdf"
    else:
        raw = raw_or_rows
    parsed = parse_document(raw, name)
    return {
        **parsed,
        "id": str(uuid4()),
        "name": name,
        "document_role": role,
        "version": 1,
    }


def csv_source(rows, role):
    import csv

    text = io.StringIO(newline="")
    csv.writer(text).writerows(rows)
    return source(text.getvalue().encode(), role, uuid4().hex + ".csv")


def xlsx_source(rows, role, formulas=()):
    from openpyxl import Workbook

    book = Workbook()
    for row in rows:
        book.active.append(row)
    for cell, formula in formulas:
        book.active[cell] = formula
    out = io.BytesIO()
    book.save(out)
    return source(out.getvalue(), role, uuid4().hex + ".xlsx")


def ocr_source(lines, role, confidence=95.0, overrides=None, kind="ocr-or-mixed-pdf"):
    """Synthetic OCR page in the parser's exact output shape.

    lines: list of rows; each row is a list of (x, text) words on one line.
    overrides: {word_text: confidence}.
    """
    overrides = overrides or {}
    text, words = "", []
    for n, row in enumerate(lines):
        top = 50 + n * 28
        for x, value in row:
            if text:
                text += " "
            start = len(text)
            text += value
            width = 6.5 * len(value)
            words.append(
                {
                    "quote": value,
                    "start": start,
                    "end": len(text),
                    "boxes": [[x, top, x + width, top + 12]],
                    "geometry_kind": "ocr-word-box",
                    "confidence": overrides.get(value, confidence),
                    "requires_review": True,
                }
            )
    return {
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "kind": kind,
        "id": str(uuid4()),
        "name": "scan.pdf",
        "document_role": role,
        "version": 1,
        "pages": [
            {
                "number": 1,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "width": 612,
                "height": 792,
                "words": words,
                "text_kind": "ocr-word-order-v1",
                "ocr_engine": "tesseract-eng",
                "render_dpi": 144,
            }
        ],
    }


def words(*items, x=60, gap=8):
    """Place words left to right on one OCR line: ("Received", "quantity", "75")."""
    out, pos = [], x
    for item in items:
        out.append((pos, item))
        pos += 6.5 * len(item) + gap
    return out


INVOICE = stacked(
    [
        "ASTER SUPPLY CO.",
        "Invoice INV-7001",
        "Purchase order PO-7001",
        "Quantity 120",
        "Unit price USD 48.00",
        "Tax rate 8%",
        "Invoice face total USD 6220.80",
    ]
)
PURCHASE_ORDER = stacked(
    [
        "NORTHSTAR STUDIO",
        "Purchase order PO-7001",
        "Supplier Aster Supply Co.",
        "Ordered quantity 120",
        "Unit price USD 48.00",
    ]
)


def receipt_rows(*extra, quantity="100", rid="GR-7001"):
    return stacked(
        [
            "NORTHSTAR STUDIO",
            f"Goods receipt {rid}",
            "Purchase order PO-7001",
            f"Received quantity {quantity}",
            "Unit each",
            *extra,
        ]
    )
