"""Public authored real file regressions for receipt authority R1/R2/R3."""

import csv
import io
from uuid import uuid4

import pytest
from test_receipt_tables import EmptyReservations, table

from folio.allocations import allocate
from folio.formats import parse_document
from folio.provenance import extract_packet


def labelled(ext, field=None, value=None, extra=False):
    rows = [
        ["Receipt", "GR-A"],
        ["Line", "1"],
        ["Received quantity", "20"],
        ["Unit", "each"],
        ["Currency", "USD"],
    ]
    for row in rows:
        if row[0] == field:
            row[1] = value
            if extra:
                row.append("999")
    return file_source(ext, rows)


def file_source(ext, rows):
    if ext == "csv":
        out = io.StringIO(newline="")
        csv.writer(out, quoting=csv.QUOTE_ALL).writerows(rows)
        raw = out.getvalue().encode()
    else:
        from openpyxl import Workbook

        book = Workbook()
        for row in rows:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        book.close()
        raw = out.getvalue()
    return {
        **parse_document(raw, "receipt." + ext),
        "id": str(uuid4()),
        "version": 3,
        "document_role": "receipt",
    }, raw


def allocation_case(support):
    receipt_sources = {}
    for row in support.get("rows", []):
        for evidence in row.get("evidence", []):
            receipt_sources[evidence["source_id"]] = {
                "id": evidence["source_id"],
                "sha256": evidence["source_sha256"],
                "version": evidence.get("source_version", 1),
                "document_role": "receipt",
            }
    return {
        "id": str(uuid4()),
        "workspace": str(uuid4()),
        "supplier": "Supplier A",
        "currency": "USD",
        "unit": "each",
        "target_id": str(uuid4()),
        "source_digest": "a" * 64,
        "version": 1,
        "field_provenance": {
            "currency": [
                {
                    "kind": "human",
                    "value": "USD",
                    "actor": "reviewer",
                    "reason": "Case currency is explicit and source-bound",
                }
            ]
        },
        "sources": [
            {
                "id": "test-invoice",
                "sha256": "b" * 64,
                "version": 1,
                "document_role": "invoice",
            },
            {
                "id": "test-purchase-order",
                "sha256": "c" * 64,
                "version": 1,
                "document_role": "purchase_order",
            },
            *receipt_sources.values(),
        ],
        "receipt_manifest": support,
    }


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "field,base", [("Received quantity", "20"), ("Unit", "each"), ("Currency", "USD")]
)
@pytest.mark.parametrize(
    "suffix,extra", [(" extra", False), ("\n999", False), ("\t999", False), ("", True)]
)
@pytest.mark.parametrize("alias", [False, True])
def test_labelled_complete_financial_cells_deny_prefix_authority(
    ext, field, base, suffix, extra, alias
):
    bad, _ = labelled(ext, field, base + suffix, extra)
    sources = [bad]
    if alias:
        sources.append(table(ext, [["GR-A", "1", "20", "each", "USD"]])[0])
    packet = extract_packet(sources)
    assert packet["receipt_manifest"]["errors"]
    assert "received_quantity" not in packet["values"]
    with pytest.raises(ValueError):
        allocate(EmptyReservations(), allocation_case(packet["receipt_manifest"]), "1")


@pytest.mark.parametrize("field", ["Received quantity", "Unit", "Currency"])
def test_labelled_formula_fields_never_authorize(field):
    source, _ = labelled("xlsx", field, "=20")
    packet = extract_packet([source])
    assert packet["receipt_manifest"]["errors"]
    assert "received_quantity" not in packet["values"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_invalid_unit_cell_cannot_keep_scalar_prefix_provenance(ext):
    source, _ = labelled(ext, "Unit", "each\nbox")
    packet = extract_packet([source])
    assert "unit" not in packet["values"]
    assert "unit" not in packet["field_provenance"]
