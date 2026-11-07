"""Independent ordinary packet examples for general extraction regressions."""

import io
from uuid import uuid4


def _pdf(text):
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    doc = canvas.Canvas(buf, pagesize=(612, 792))
    y = 740
    for line in text.splitlines():
        doc.drawString(54, y, line)
        y -= 24
    doc.save()
    return buf.getvalue()


def _source(raw, name, role):
    from folio.formats import parse_document

    out = parse_document(raw, name)
    return {**out, "id": str(uuid4()), "version": 1, "document_role": role}


def test_independent_pdf_csv_xlsx_packet_preserves_multrow_receipts_and_identity():
    from openpyxl import Workbook

    from folio.provenance import extract_packet

    invoice = _source(
        _pdf(
            "NORTHSTAR OFFICE SYSTEMS\n"
            "INVOICE\n"
            "Invoice Number: INV-2026-0042\n"
            "Supplier: Aster Industrial Inc.\n"
            "PO Number: PO-4401\n"
            "Quantity Invoiced: 120 each\n"
            "Unit Price: USD 48.00\n"
            "Tax Rate: 8%\n"
            "Amount Due: USD 6,220.80"
        ),
        "invoice.pdf",
        "invoice",
    )
    po = _source(
        (
            "Purchase Order Number,Supplier,Ordered Quantity,Unit,Currency\r\n"
            "PO-4401,Aster Industrial Ltd.,120,each,USD\r\n"
        ).encode(),
        "purchase-order.csv",
        "purchase_order",
    )
    book = Workbook()
    sheet = book.active
    sheet.append(["Receipt", "Line", "Received quantity", "Unit", "Currency"])
    sheet.append(["GR-7101", "1", 60, "each", "USD"])
    sheet.append(["GR-7102", "1", 40, "each", "USD"])
    receipt_bytes = io.BytesIO()
    book.save(receipt_bytes)
    receipt = _source(receipt_bytes.getvalue(), "receipts.xlsx", "receipt")

    packet = extract_packet([invoice, po, receipt])

    assert packet["values"]["invoice"] == "INV-2026-0042"
    assert packet["values"]["supplier"] == "Aster Industrial Ltd."
    assert packet["invoice_supplier"]["value"] == "Aster Industrial Inc."
    assert packet["values"]["invoice_quantity"] == "120"
    assert packet["values"]["ordered_quantity"] == "120"
    assert packet["values"]["received_quantity"] == "100"
    assert packet["values"]["unit"] == "each"
    assert packet["values"]["currency"] == "USD"
    assert packet["values"]["unit_price"] == "48.00"
    assert packet["values"]["tax_rate"] == "0.08"
    assert packet["values"]["invoice_face_amount"] == "6220.80"
    assert packet["conflicts"] == []
    assert packet["missing"] == []
    assert packet["supplier_check"]["status"] == "match"
    assert packet["receipt_manifest"]["total"] == "100"
    assert {row["receipt_id"] for row in packet["receipt_manifest"]["rows"]} == {
        "GR-7101",
        "GR-7102",
    }


def test_supplier_name_starting_with_its_label_is_not_truncated():
    from folio.layout import page_lines, read_page

    po = _source(
        (
            "Field,Value\r\n"
            "Supplier,Supplier Technologies\r\n"
            "Ordered Quantity,120\r\n"
            "Unit,each\r\n"
            "Currency,USD\r\n"
        ).encode(),
        "supplier-technologies.csv",
        "purchase_order",
    )
    page = po["pages"][0]
    found, conflicts, _ = read_page(po, page, "purchase_order", page_lines(po, page))
    supplier = [item["value"] for item in found if item["field"] == "supplier"]

    assert supplier == ["Supplier Technologies"]
    assert "supplier" not in conflicts

