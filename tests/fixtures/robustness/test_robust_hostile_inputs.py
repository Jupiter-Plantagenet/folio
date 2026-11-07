"""Hostile and malformed inputs (DOC-T-PARSER subset): bounded rejection, no evaluation.

Host-level tests call the same parse_document code the container runs; the
docker-marked tests go through folio.sandbox.parse_isolated (networkless,
read-only, blueprint-budgeted container) when a Docker engine and the parser image exist.
"""

import io
import shutil
import subprocess
import time
import zipfile

import pytest
from robust_helpers import csv_source, pdf_bytes, receipt_rows, source, stacked

from folio.formats import parse_document
from folio.provenance import extract_packet
from folio.receipts import receipt_manifest


def xlsx_bomb():
    """Valid-looking workbook with a highly compressible 6 MiB worksheet member."""
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Receipt", "GR-1"])
    good = io.BytesIO()
    book.save(good)
    out = io.BytesIO()
    with (
        zipfile.ZipFile(good) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(
                    b"</sheetData>",
                    b"</sheetData>" + b"<!--" + b"A" * (6 * 1024 * 1024) + b"-->",
                )
            dst.writestr(item, data)
    return out.getvalue()


def test_truncated_pdf_is_rejected_quickly():
    raw = pdf_bytes(receipt_rows())
    started = time.monotonic()
    with pytest.raises(Exception):
        parse_document(raw[: len(raw) // 2], "receipt.pdf")
    assert time.monotonic() - started < 10


def test_not_a_pdf_and_oversize_are_rejected():
    with pytest.raises(ValueError, match="FILE_REJECTED"):
        parse_document(b"<html>%PDF-</html>", "x.pdf")
    with pytest.raises(ValueError, match="FILE_REJECTED"):
        parse_document(b"%PDF-" + b"0" * (20 * 1024 * 1024), "x.pdf")


def test_zip_bomb_xlsx_is_rejected_before_decompression():
    raw = xlsx_bomb()
    assert len(raw) < 200_000
    with pytest.raises(ValueError, match="XLSX_CONTAINER_REJECTED"):
        parse_document(raw, "bomb.xlsx")


def test_huge_csv_line_and_wide_rows_are_bounded():
    with pytest.raises(ValueError, match="CELL_LIMIT"):
        parse_document(b"Receipt," + b"A" * 20_000 + b"\r\n", "big.csv")
    with pytest.raises(ValueError, match="FILE_REJECTED"):
        parse_document(b"A" * (20 * 1024 * 1024 + 1), "huge.csv")
    with pytest.raises(ValueError, match="CSV_LIMIT"):
        parse_document((",".join(["x"] * 51) + "\r\n").encode(), "wide.csv")
    with pytest.raises(ValueError, match="CSV_LIMIT"):
        parse_document(b"a,b\r\n" * 1001, "long.csv")


@pytest.mark.parametrize(
    "raw",
    [
        b"Receipt,GR-1\r\nUnit,\xff\xfe\r\n",
        "Receipt,GR-1\r\n".encode("utf-16-le"),  # no BOM: never guess UTF-16
        b"\xff\xfeR",  # declared UTF-16 with a truncated code unit
        b"Receipt,GR\x00-1\r\n",
    ],
)
def test_undeclared_or_malformed_encoding_or_nul_csv_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_document(raw, "enc.csv")


@pytest.mark.parametrize(
    "encoding,bom", [("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")]
)
def test_bom_declared_utf16_csv_preserves_exact_byte_provenance(encoding, bom):
    raw = bom + 'Receipt,"GR-\u03a9"\r\nUnit,each\r\n'.encode(encoding)
    result = parse_document(raw, "declared.csv")
    assert result["csv"]["encoding"] == encoding and result["csv"]["bom"] is True
    assert result["cells"][1]["value"] == "GR-\u03a9"
    for cell in result["cells"]:
        assert (
            raw[cell["byte_start"] : cell["byte_end"]].decode(encoding) == cell["quote"]
        )


def test_empty_csv_cells_keep_literal_bytes_without_zero_length_projection_words():
    from folio.parser_output import validate

    raw = b"Receipt,\nLine,1\nReceived quantity,2\nUnit,each\nCurrency,USD\n"
    parsed = parse_document(raw, "empty.csv")
    validate(parsed, raw)
    empty = next(c for c in parsed["cells"] if c["row"] == 1 and c["column"] == 2)
    assert empty["value"] == "" and raw[empty["byte_start"] : empty["byte_end"]] == b""
    assert all(w["start"] < w["end"] for p in parsed["pages"] for w in p["words"])
    manifest = receipt_manifest(
        [{**parsed, "id": "empty", "document_role": "receipt", "version": 1}]
    )
    assert manifest["errors"] == ["RECEIPT_IDENTITY_INVALID"] and not manifest["rows"]


@pytest.mark.parametrize(
    "payload",
    [
        "=1+99",
        "+99",
        "-1+100",
        "@SUM(1,99)",
        '=HYPERLINK("http://x.example","100")',
        "=cmd|' /C calc'!A0",
    ],
)
def test_formula_injection_cells_stay_literal_and_never_become_quantities(payload):
    s = csv_source(
        [["Receipt", "GR-1"], ["Received quantity", payload], ["Unit", "each"]],
        "receipt",
    )
    assert any(c["value"] == payload for c in s["cells"])  # literal, not evaluated
    m = receipt_manifest([s])
    assert m["errors"] and not m["rows"]
    inv = csv_source(
        [["Invoice", "INV-1"], ["Quantity", payload], ["Unit price", "USD 4.00"]],
        "invoice",
    )
    ex = extract_packet([inv])
    assert "invoice_quantity" not in ex["values"]


def test_hidden_or_tiny_text_cannot_add_receipt_quantity():
    rows = receipt_rows() + [(60, 400, "Received quantity 999")]
    m = receipt_manifest([source(rows, "receipt")])
    assert m["errors"] and not m["rows"]


def test_ocr_spoofing_inside_words_is_not_repaired_on_digital_text():
    s = source(
        stacked(["Goods receipt GR-1", "Received quantity 1O0", "Unit each"]), "receipt"
    )
    m = receipt_manifest([s])
    assert m["errors"] and not m["rows"]


docker = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(
        ["docker", "image", "inspect", "folio-parser:1"], capture_output=True
    ).returncode,
    reason="Docker parser image unavailable",
)


@docker
@pytest.mark.parametrize(
    "name,raw_fn",
    [
        ("truncated.pdf", lambda: pdf_bytes(receipt_rows())[:900]),
        ("bomb.xlsx", xlsx_bomb),
        ("enc.csv", lambda: b"Receipt,\xff\xfe\r\n"),
        ("big.csv", lambda: b"Receipt," + b"A" * 20_000 + b"\r\n"),
    ],
)
def test_container_parser_rejects_hostile_files(name, raw_fn):
    from folio.sandbox import parse_isolated

    with pytest.raises(ValueError) as error:
        for _ in range(40):
            try:
                parse_isolated(raw_fn(), name)
            except ValueError as e:
                if str(e) == "PARSER_CAPACITY_RETRY":
                    time.sleep(0.25)
                    continue
                raise
    assert str(error.value).startswith(("PARSER_REJECTED", "FILE_REJECTED"))
