"""RV-02: entire logical fields through real CSV/XLSX parsers, never prefixes."""

import base64
import copy
import csv
import io
from uuid import uuid4

import pytest
from test_receipt_allocations import packet_files, parsed
from test_receipt_lifecycle import hdr, setup

from folio.receipts import receipt_manifest


def pdf_source(identity_lines):
    from reportlab.pdfgen import canvas

    raw = io.BytesIO()
    pdf = canvas.Canvas(raw)
    y = 750
    for text in identity_lines + ["Received quantity 100", "Unit each", "Currency USD"]:
        pdf.drawString(72, y, text)
        y -= 25
    pdf.save()
    return parsed("receipt.pdf", raw.getvalue(), "receipt")


@pytest.mark.parametrize(
    "lines",
    [
        ["Receipt GR_A /B", "Line 1"],
        ["Receipt GR_A", "/B", "Line 1"],
        ["Receipt GR_A", "Line 1 /2"],
        ["Receipt GR_A", "Line 1", "/2"],
        ["Receipt", "GR_A", "Line 1"],
    ],
)
def test_pdf_identity_multivalue_and_continuation_fail_closed(lines):
    m = receipt_manifest([pdf_source(lines)])
    assert m["errors"] and not m["rows"]


def test_pdf_supported_identity_lines_keep_genuine_workflow():
    m = receipt_manifest([pdf_source(["Goods receipt GR_A", "Line L_1"])])
    assert not m["errors"] and m["rows"][0]["key"] == "GR_A:L_1"
    assert m["total"] == "100"


BAD_IDS = [
    "GR_A\n/B",
    "GR_A\n/C",
    "GR_A /B",
    "GR_A\t/B",
    "",
    " ",
    "A" * 129,
    "A" * 10000,
]
BAD_LINES = ["1\n/2", "1 /2", "1\t/2", "", " ", "1" * 129]


def field_file(
    ext, rid="GR_A", line="1", extra=False, continuation=False, quantity="2"
):
    rows = [
        ["Receipt", rid],
        ["Line", line],
        ["Received quantity", quantity],
        ["Unit", "each"],
        ["Currency", "USD"],
    ]
    if extra:
        rows[0].append("/B")
    if continuation:
        rows.insert(1, ["/B"])
    out = io.BytesIO()
    if ext == "csv":
        text = io.StringIO(newline="")
        csv.writer(text).writerows(rows)
        raw = text.getvalue().encode()
    else:
        from openpyxl import Workbook

        book = Workbook()
        for row in rows:
            book.active.append(row)
        book.save(out)
        book.close()
        raw = out.getvalue()
    return {
        "name": uuid4().hex + "." + ext,
        "document_role": "receipt",
        "base64": base64.b64encode(raw).decode(),
    }


def source(file):
    return parsed(file["name"], base64.b64decode(file["base64"]), "receipt")


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "kwargs",
    [{"rid": v} for v in BAD_IDS]
    + [{"line": v} for v in BAD_LINES]
    + [{"extra": True}, {"continuation": True}],
)
def test_entire_authoritative_identity_field_rejects(ext, kwargs):
    s = source(field_file(ext, **kwargs))
    before = copy.deepcopy(s)
    m = receipt_manifest([s])
    assert m["errors"], m
    assert m["rows"] == [] and m["total"] == "0", m
    assert s == before


def test_distinct_reviewer_cells_never_create_alias():
    m = receipt_manifest(
        [
            source(field_file("csv", rid="GR_A\n/B")),
            source(field_file("csv", rid="GR_A\n/C")),
        ]
    )
    assert m["errors"] and not m["rows"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_exact_valid_identity_boundary_alias_and_conflict(ext):
    token = "Ab_9-" + "x" * 123
    a = source(field_file(ext, rid=token, line="L_1"))
    b = source(field_file("xlsx", rid=token, line="L_1"))
    m = receipt_manifest([a, b])
    assert not m["errors"] and m["rows"][0]["key"] == token + ":L_1"
    assert m["total"] == "2" and len(m["rows"][0]["evidence"]) == 2
    c = source(field_file(ext, rid=token, line="L_1", quantity="3"))
    assert receipt_manifest([a, c])["errors"] == ["RECEIPT_IDENTITY_CONFLICT"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_missing_authoritative_cells_cannot_fall_back_to_projection(ext):
    s = source(field_file(ext))
    del s["cells"]
    m = receipt_manifest([s])
    assert m["errors"] and not m["rows"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"rid": "GR_A\n/B"},
        {"rid": "GR_A\n/C"},
        {"line": "1\n/2"},
        {"rid": "GR_A /B"},
        {"line": "1\t/2"},
        {"extra": True},
        {"continuation": True},
        {"rid": ""},
        {"rid": "A" * 129},
    ],
)
def test_direct_api_bad_fields_never_prepare_or_dispatch(ext, kwargs):
    import psycopg

    from folio.target import DSN
    from folio.workbench import connect

    client, auth, prefix, url = setup(packet_files() + [field_file(ext, **kwargs)])
    c = client.get(url).json()
    assert c["receipt_manifest"]["errors"] and not c["receipt_manifest"]["rows"]
    with psycopg.connect(DSN) as db:
        before = db.execute("SELECT count(*) FROM operations").fetchone()[0]
    for mode in ["partial", "no_change"]:
        response = client.post(
            url + "/decisions",
            headers=hdr(auth, c),
            json={
                "mode": mode,
                "quantity": "2",
                "reason": "Whole field required",
                "evidence_digest": c["source_digest"],
            },
        )
        assert response.status_code == 422, response.text
    assert client.get(url).json()["proposal"] is None
    with connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM proposals WHERE case_id=%s", (c["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE proposal IN (SELECT id FROM proposals WHERE case_id=%s)",
                (c["id"],),
            ).fetchone()[0]
            == 0
        )
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == before


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_absent_receipt_label_never_manufactures_identity(ext):
    s = source(field_file(ext))
    s["cells"] = [c for c in s["cells"] if c["row"] != 1]
    m = receipt_manifest([s])
    assert m["errors"] == ["RECEIPT_IDENTITY_REQUIRED"] and not m["rows"]


@pytest.mark.parametrize("policy", ["missing_geometry", "ocr"])
def test_pdf_missing_supported_boundaries_cannot_use_word_order_prefix(policy):
    s = pdf_source(["Receipt GR_A", "Line 1"])
    if policy == "missing_geometry":
        for w in s["pages"][0]["words"]:
            w["boxes"] = []
    else:
        s["kind"] = "ocr-or-mixed-pdf"
        s["pages"][0]["ocr_engine"] = "tesseract-eng"
    m = receipt_manifest([s])
    assert m["errors"] == ["RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED"] and not m["rows"]
