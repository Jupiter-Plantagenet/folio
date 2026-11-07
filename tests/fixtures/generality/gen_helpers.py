"""Builders for the round-7 generality tests (no database, no network).

Re-exports the round-5 robustness builders (same product parser code) and
adds CSV dialect writers.
"""

import io
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robustness"))

from robust_helpers import (  # noqa: E402,F401
    INVOICE,
    PURCHASE_ORDER,
    ocr_source,
    pdf_bytes,
    receipt_rows,
    source,
    stacked,
    words,
    xlsx_source,
)


def csv_raw(rows, delimiter=","):
    import csv

    text = io.StringIO(newline="")
    csv.writer(text, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    return text.getvalue().encode()


def csv_source(rows, role, delimiter=","):
    return source(csv_raw(rows, delimiter), role, uuid4().hex + ".csv")


def pdf(lines, role):
    return source(stacked(lines), role)


def invoice(*lines):
    return pdf(list(lines), "invoice")


def po(*lines):
    return pdf(list(lines), "purchase_order")


def receipt(*lines):
    return pdf(list(lines), "receipt")


def table_rows(header, rows, xs, top=140, step=22):
    out = [(x, top, h) for x, h in zip(xs, header)]
    for r, row in enumerate(rows, 1):
        out += [(x, top + r * step, str(v)) for x, v in zip(xs, row) if str(v)]
    return out
