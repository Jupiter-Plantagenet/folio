"""Workbook XML numeric oracles authored as literals, not Python floats."""

import io
import zipfile
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from test_receipt_authority_round2 import labelled
from test_receipt_tables import table

from folio.formats import parse_document
from folio.persisted_receipts import manifest
from folio.provenance import extract_packet


def numeric_xml(literal, layout="table", formula=False):
    source, raw = (
        table("xlsx", [["GR-A", "1", "20", "each", "USD"]])
        if layout == "table"
        else labelled("xlsx")
    )
    coordinate = "C2" if layout == "table" else "B3"
    out = io.BytesIO()
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, "w") as dst:
        for member in src.infolist():
            data = src.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                tree = ET.fromstring(data)
                cell = tree.find(f"s:sheetData/s:row/s:c[@r='{coordinate}']", ns)
                cell.clear()
                cell.set("r", coordinate)
                cell.set("t", "n")
                if formula:
                    ET.SubElement(cell, "{" + ns["s"] + "}f").text = "10+10"
                ET.SubElement(cell, "{" + ns["s"] + "}v").text = literal
                data = ET.tostring(tree)
            dst.writestr(member, data)
    return {
        **parse_document(out.getvalue(), "literal.xlsx"),
        "id": source["id"],
        "version": 3,
        "document_role": "receipt",
    }


@pytest.mark.parametrize("layout", ["table", "labelled"])
@pytest.mark.parametrize(
    "literal",
    [
        "999999999999.123456",
        "999999999999.999999",
        "20",
        "20.5",
        "0.000001",
        "100.000000",
    ],
)
def test_xlsx_xml_numeric_exact_fixed_point_authority(layout, literal):
    source = numeric_xml(literal, layout)
    packet = extract_packet([source])
    assert not packet["receipt_manifest"]["errors"]
    assert Decimal(packet["receipt_manifest"]["total"]) == Decimal(literal)
    row = packet["receipt_manifest"]["rows"][0]
    assert row["evidence"][0]["quote"] == literal
    manifest(packet["receipt_manifest"])
    from test_receipt_authority_round2 import allocation_case
    from test_receipt_tables import EmptyReservations

    from folio.allocations import allocate

    selected = allocate(
        EmptyReservations(), allocation_case(packet["receipt_manifest"]), literal
    )
    assert Decimal(selected["total"]) == Decimal(literal)
    manifest(selected, supplier="Supplier A")


@pytest.mark.parametrize("layout", ["table", "labelled"])
@pytest.mark.parametrize(
    "literal",
    ["20.0000000000000001", "999999999999.1234567", "1000000000000", "2e1", "0", "-1"],
)
def test_xlsx_unsupported_raw_numeric_not_rounded_into_scope(layout, literal):
    packet = extract_packet([numeric_xml(literal, layout)])
    assert packet["receipt_manifest"]["errors"]
    assert "received_quantity" not in packet["values"]


@pytest.mark.parametrize("layout", ["table", "labelled"])
def test_xlsx_cached_numeric_formula_keeps_raw_cache_but_never_authorizes(layout):
    source = numeric_xml("20.0000000000000001", layout, formula=True)
    packet = extract_packet([source])
    assert packet["receipt_manifest"]["errors"]
    assert "received_quantity" not in packet["values"]
    cell = next(c for c in source["cells"] if c.get("formula"))
    assert cell["numeric_lexical"] == "20.0000000000000001"
    assert cell["raw_type"] == "n"
