"""folio-layout-v2 generality: label variants, header-row tables, receipt lists.

Authored in-memory documents only (reportlab / csv / openpyxl through the
product parser); no database.
"""

import csv
import io
from uuid import uuid4

import pytest

from folio.provenance import extract_packet, validate_citation


def _source(raw, name, role):
    from folio.formats import parse_document

    return {**parse_document(raw, name), "id": str(uuid4()), "name": name, "document_role": role, "version": 1}


def pdf(lines, role):
    from reportlab.pdfgen.canvas import Canvas

    out = io.BytesIO()
    c = Canvas(out, pagesize=(612, 792), invariant=1)
    c.setFont("Helvetica", 11)
    for i, text in enumerate(lines):
        c.drawString(60, 792 - (60 + i * 24), text)
    c.save()
    return _source(out.getvalue(), "doc.pdf", role)


def table(rows, role, ext="csv", formulas=()):
    if ext == "csv":
        text = io.StringIO(newline="")
        csv.writer(text).writerows(rows)
        return _source(text.getvalue().encode(), uuid4().hex + ".csv", role)
    from openpyxl import Workbook

    book = Workbook()
    for row in rows:
        book.active.append(row)
    for cell, formula in formulas:
        book.active[cell] = formula
    out = io.BytesIO()
    book.save(out)
    return _source(out.getvalue(), uuid4().hex + ".xlsx", role)


PO = [
    ["Supplier", "Kestrel Pumps Ltd."],
    ["Purchase order", "PO-8801"],
    ["Description", "Ordered quantity", "Unit", "Unit price", "Currency"],
    ["Impeller kit", "40", "box", "12.50", "USD"],
]


def packet(*sources):
    ex = extract_packet(list(sources))
    for field, entries in ex["field_provenance"].items():
        for entry in entries:
            if "start" in entry.get("source", {}):
                validate_citation(list(sources), entry["source"])  # exact quoted spans
    return ex


def unresolved(ex, field):
    return field in ex["missing"] or field in ex["conflicts"]


@pytest.mark.parametrize(
    "label",
    ["Invoice No.", "Invoice #", "Invoice number", "Invoice reference", "Bill number", "Reference", "Ref"],
)
def test_invoice_number_label_variants(label):
    invoice = pdf(["Kestrel Pumps Ltd.", f"{label}: INV-20417", "Purchase order: PO-8801"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"]["invoice"] == "INV-20417"
    assert ex["field_provenance"]["invoice"][0]["source"]["quote"] == "INV-20417"


def test_generic_reference_names_the_invoice_only_when_invoice_prefixed():
    invoice = pdf(["Kestrel Pumps Ltd.", "Reference: PO-8801", "Your ref: 55-A"], "invoice")
    assert "invoice" not in packet(invoice, table(PO, "purchase_order"))["values"]


def test_two_invoice_numbers_conflict():
    invoice = pdf(["Invoice number: INV-20417", "Reference: INV-20418"], "invoice")
    assert "invoice" in packet(invoice, table(PO, "purchase_order"))["conflicts"]


def test_combined_currency_unit_declaration():
    invoice = pdf(
        ["Kestrel Pumps Ltd.", "Invoice No: INV-1", "Currency / unit: USD / BOX",
         "Quantity | 40 | BOX", "Unit price | 12.50 | USD", "Sales tax 8% | 40.00", "Invoice total | 540.00"],
        "invoice",
    )
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"]["currency"] == "USD" and ex["values"]["unit"] == "box"
    assert ex["values"]["invoice_quantity"] == "40" and ex["values"]["unit_price"] == "12.50"


def test_combined_declaration_naming_another_dimension_conflicts():
    eur = pdf(["Invoice No: INV-1", "Currency / unit: EUR / BOX", "Quantity | 40 | BOX"], "invoice")
    assert "currency" in packet(eur, table(PO, "purchase_order"))["conflicts"]
    kg = pdf(["Invoice No: INV-1", "Currency / unit: USD / KG", "Quantity | 40 | KG"], "invoice")
    assert "unit" in packet(kg, table(PO, "purchase_order"))["conflicts"]
    bad = pdf(["Invoice No: INV-1", "Currency / unit: ZZQ / BOX"], "invoice")
    assert "currency" in packet(bad, table(PO, "purchase_order"))["conflicts"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_header_row_table_in_cells(ext):
    invoice = table(
        [["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"],
         ["Item / service", "Quantity", "BOX", "Unit price", "USD", "Tax %"],
         ["Impeller kit", "40", "BOX", "12.50", "USD", "8"],
         ["Amount due", "540.00", "USD"]],
        "invoice", ext,
    )
    ex = packet(invoice, table(PO, "purchase_order"))
    v = ex["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"], v["unit"], v["currency"]) == ("40", "12.50", "0.08", "box", "USD")
    assert v["ordered_quantity"] == "40" and v["supplier"] == "Kestrel Pumps Ltd."
    assert ex["invoice_arithmetic"]["status"] == "consistent"
    assert ex["field_provenance"]["tax_rate"][0]["derivation"] == "percent / 100"


def test_header_row_table_with_printed_rules():
    invoice = pdf(
        ["Kestrel Pumps Ltd.", "Invoice No: INV-77",
         "Item / service | Quantity | BOX | Unit price | USD | Tax %",
         "Impeller kit | 40 | BOX | 12.50 | USD | 8",
         "Amount due | 540.00 | USD"],
        "invoice",
    )
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"]["invoice_quantity"] == "40"
    assert ex["values"]["unit_price"] == "12.50" and ex["values"]["tax_rate"] == "0.08"
    quote = ex["field_provenance"]["invoice_quantity"][0]["source"]["quote"]
    assert quote == "40"


def test_heading_merged_with_its_code_expands_only_when_counts_then_agree():
    invoice = pdf(
        ["Invoice No: INV-77", "Item | Quantity BOX | Unit price USD | Tax %",
         "Impeller kit | 40 | BOX | 12.50 | USD | 8", "Invoice total | USD 540.00"],
        "invoice",
    )
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"]["invoice_quantity"] == "40" and ex["values"]["unit_price"] == "12.50"


def test_misaligned_item_row_is_a_conflict_not_a_guess():
    invoice = pdf(
        ["Invoice No: INV-77", "Item | Quantity | BOX | Unit price | USD | Tax %",
         "Impeller kit | 40 | BOX | 12.50 | USD 8"],
        "invoice",
    )
    ex = packet(invoice, table(PO, "purchase_order"))
    for field in ("invoice_quantity", "unit_price", "tax_rate"):
        assert field in ex["conflicts"], field


def test_two_item_rows_without_binding_identity_conflict():
    invoice = table(
        [["Invoice number", "INV-77"], ["Item", "Quantity", "Unit price"],
         ["Impeller kit", "40", "12.50"], ["Seal ring", "10", "3.00"]],
        "invoice",
    )
    po = table([["Supplier", "Kestrel Pumps Ltd."], ["Ordered quantity", "40"]], "purchase_order")
    ex = packet(invoice, po)
    assert "invoice_quantity" in ex["conflicts"] and "unit_price" in ex["conflicts"]


def test_key_value_rows_with_qualifier_cells():
    invoice = table(
        [["Invoice number", "INV-77"], ["Quantity", "40", "BOX"], ["Rate", "12.50", "USD"],
         ["Tax rate", "8", "percent"], ["Tax", "40.00", "USD"], ["Invoice total", "540.00", "USD"]],
        "invoice",
    )
    v = packet(invoice, table(PO, "purchase_order"))["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"], v["invoice_face_amount"]) == ("40", "12.50", "0.08", "540.00")


def test_bare_tax_heading_needs_the_invoice_arithmetic():
    rows = [["Invoice number", "INV-77"], ["Line", "Qty", "Rate", "Tax"], ["Impeller kit", "40", "12.50", "8"]]
    ok = table(rows + [["Total payable", "540.00", "USD"]], "invoice")
    ex = packet(ok, table(PO, "purchase_order"))
    assert ex["values"]["tax_rate"] == "0.08"
    assert "confirmed" in ex["field_provenance"]["tax_rate"][0]["derivation"]
    # The column held a tax amount (8.00 of 500.00), not 8 %: the rate stays missing.
    amount = table(rows + [["Total payable", "508.00", "USD"]], "invoice")
    ex = packet(amount, table(PO, "purchase_order"))
    assert "tax_rate" in ex["missing"]


def RECEIPT(rows, ext="csv", formulas=()):
    head = [["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"], ["Currency / unit", "USD / BOX"],
            ["Receipt", "Line", "Accepted quantity", "BOX", "Received date"]]
    return table(head + rows, "receipt", ext, formulas)


INVOICE = [["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"], ["Purchase order", "PO-8801"],
           ["Quantity", "40", "BOX"], ["Unit price", "12.50", "USD"]]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_receipt_list_table_sums_unique_receipt_lines(ext):
    receipt = RECEIPT([["GR-1", "1", "30", "BOX", "2026-03-01"], ["GR-2", "1", "8", "BOX", "2026-03-04"]], ext)
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), receipt)
    m = ex["receipt_manifest"]
    assert m["errors"] == [] and m["total"] == "38" and ex["values"]["received_quantity"] == "38"
    assert sorted(r["key"] for r in m["rows"]) == ["GR-1:1", "GR-2:1"]
    assert ex["values"]["unit"] == "box"


def test_receipt_list_rows_repeated_in_two_files_are_not_double_counted():
    rows = [["GR-1", "1", "30", "BOX", "2026-03-01"]]
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), RECEIPT(rows), RECEIPT(rows, "xlsx"))
    assert ex["receipt_manifest"]["total"] == "30" and ex["values"]["received_quantity"] == "30"


def test_receipt_list_same_key_two_quantities_conflict():
    a = RECEIPT([["GR-1", "1", "30", "BOX", "2026-03-01"]])
    b = RECEIPT([["GR-1", "1", "31", "BOX", "2026-03-01"]])
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), a, b)
    assert "RECEIPT_IDENTITY_CONFLICT" in ex["receipt_manifest"]["errors"]
    assert "received_quantity" in ex["conflicts"]


@pytest.mark.parametrize(
    "rows",
    [
        [["GR-1", "1", "30", "KG", "2026-03-01"]],  # a unit other than its heading
        [["GR-1", "1", "30", "BOX", "2026-03-01"], ["GR-2", "1", "abc", "BOX", "2026-03-01"]],
        [["GR-1 X", "1", "30", "BOX", "2026-03-01"]],
    ],
)
def test_receipt_list_bad_cells_fail_closed(rows):
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), RECEIPT(rows))
    assert ex["receipt_manifest"]["errors"] and not ex["receipt_manifest"]["rows"]
    assert "received_quantity" in ex["conflicts"]


def test_receipt_list_formula_quantity_is_never_evidence():
    receipt = RECEIPT([["GR-1", "1", "30", "BOX", "2026-03-01"]], "xlsx", formulas=[("C5", "=10+20")])
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), receipt)
    assert ex["receipt_manifest"]["errors"] and "received_quantity" in ex["conflicts"]


def test_printed_receipt_list_with_rules():
    receipt = pdf(
        ["Kestrel Pumps Ltd.", "Purchase order: PO-8801", "Currency / unit: USD / BOX",
         "Invoice reference: INV-77",
         "Receipt | Line | Accepted quantity | BOX | Received date",
         "GR-1 | 1 | 30 | BOX | 2026-03-01", "GR-2 | 1 | 8 | BOX | 2026-03-04"],
        "receipt",
    )
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), receipt)
    assert ex["receipt_manifest"]["errors"] == [] and ex["values"]["received_quantity"] == "38"


def test_receipt_supplier_label_fills_and_checks_the_supplier():
    po = table([["Purchase order", "PO-8801"], ["Ordered quantity", "40"]], "purchase_order")
    receipt = RECEIPT([["GR-1", "1", "30", "BOX", "2026-03-01"]])
    assert packet(table(INVOICE, "invoice"), po, receipt)["values"]["supplier"] == "Kestrel Pumps Ltd."
    other = table([["Supplier", "Osprey Valves GmbH"], ["Purchase order", "PO-8801"], ["Ordered quantity", "40"]], "purchase_order")
    assert "supplier" in packet(table(INVOICE, "invoice"), other, receipt)["conflicts"]


def test_attachment_issuer_cannot_vouch_for_an_invoice_from_another_company():
    attachment = table([["Supplier", "Kestrel Pumps Ltd."], ["Note", "see invoice"]], "invoice")
    invoice = pdf(["Harrow Trading LLC", "Billing Desk | Service Ticket", "Invoice No: INV-77"], "invoice")
    ex = packet(attachment, invoice, table(PO, "purchase_order"))
    assert ex["invoice_supplier"] is None and "supplier" in ex["conflicts"]


def test_letterhead_with_department_line_names_the_legal_entity():
    invoice = pdf(["Harrow Trading LLC", "Billing Desk | Service Ticket", "Invoice No: INV-77"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["invoice_supplier"]["value"] == "Harrow Trading LLC"
    assert "supplier" in ex["conflicts"]  # the PO names Kestrel Pumps Ltd.


def test_split_heading_code_must_stand_over_that_code():
    # "Quantity EA" split in two only to make the counts agree: 12.50 would land
    # under "EA" and the line amount under "Unit price". Not aligned: conflict.
    invoice = pdf(["Kestrel Pumps Ltd.", "Invoice No: INV-77", "Item | Quantity EA | Unit price",
                   "Impeller kit | 40 | 12.50 | 500.00"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"].get("unit_price") != "500.00"
    assert unresolved(ex, "unit_price") and unresolved(ex, "invoice_quantity")


@pytest.mark.parametrize(
    "rows",
    [
        [["GR-1", "1", "Impeller kit", "30", "BOX"], ["GR-2", "1", "", "8", "BOX"]],
        [["GR-1", "1", "Impeller kit", "30", "BOX"], ["GR-2", "1", "Impeller kit blue", "8", "BOX"]],
    ],
)
def test_receipt_list_rows_of_unproven_items_are_not_dropped(rows):
    head = [["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"], ["Currency / unit", "USD / BOX"],
            ["Receipt", "Line", "Description", "Accepted quantity", "BOX"]]
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), table(head + rows, "receipt"))
    assert ex["receipt_manifest"]["errors"] and "received_quantity" in ex["conflicts"]
    assert ex["values"].get("received_quantity") != "30"


def test_printed_receipt_list_with_misspelled_item_fails_closed():
    receipt = pdf(["Kestrel Pumps Ltd.", "Purchase order: PO-8801", "Currency / unit: USD / BOX",
                   "Receipt | Line | Description | Accepted quantity | BOX",
                   "GR-1 | 1 | Impeller kit | 30 | BOX", "GR-2 | 1 | Impeller kt | 8 | BOX"], "receipt")
    ex = packet(table(INVOICE, "invoice"), table(PO, "purchase_order"), receipt)
    assert "received_quantity" in ex["conflicts"]


@pytest.mark.parametrize("heading", ["Qty (M)", "Qty M", "Qty (Kg)", "Qty (T)"])
def test_heading_unit_symbols_keep_their_case(heading):
    po = table([["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
                ["Description", "Ordered quantity", "Unit price", "Currency"], ["Impeller kit", "40", "12.50", "USD"]],
               "purchase_order")
    invoice = table([["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"],
                     ["Item", heading, "Unit price"], ["Impeller kit", "40", "12.50"]], "invoice")
    ex = packet(invoice, po)
    assert ex["values"].get("unit") not in ("m", "kg", "t")


def test_unknown_code_in_a_currency_column_conflicts():
    invoice = table([["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"],
                     ["Item", "Qty", "Unit price", "Currency"], ["Impeller kit", "40", "12.50", "XYZ"]], "invoice")
    assert "currency" in packet(invoice, table(PO, "purchase_order"))["conflicts"]


# ---- round 2: receipt layouts, RV-01 refinement, issuer/buyer, misaligned rows


def received(*receipts, invoice=None, po=None):
    ex = packet(invoice or table(INVOICE, "invoice"), po or table(PO, "purchase_order"), *receipts)
    return ex, ex["receipt_manifest"]


def test_inspection_record_key_value_form():
    receipt = pdf(["INBOUND INSPECTION RECORD", "Record no. | GRN-9733", "Inspected | 09 July 2026",
                   "PO reference | PO-8801", "Vendor | Kestrel Pumps Ltd.", "Vendor DN | LS-43346",
                   "Material | Impeller kit", "Code | IK-40", "Unit | BOX", "Units counted | 40",
                   "Units held / rejected | 4", "Units passed QC | 36", "Inspector: J. Okafor"], "receipt")
    ex, m = received(receipt)
    assert m["errors"] == [] and ex["values"]["received_quantity"] == "36"
    assert m["rows"][0]["receipt_id"] == "GRN-9733"


def test_delivery_receipt_prefers_receipt_number_and_reads_count_unit_grouping():
    receipt = pdf(["DELIVERY RECEIPT", "Receipt no. | WE-22471", "Date | 2026-07-20", "Order no. | PO-8801",
                   "Delivery note | DEL/7663", "Impeller kit (IK-40)", "Received | 5,350 BOX",
                   "Damaged / returned | 0 BOX", "Qty accepted | 5,350 BOX"], "receipt")
    ex, m = received(receipt)
    assert m["errors"] == [] and m["total"] == "5350"
    assert m["rows"][0]["receipt_id"] == "WE-22471"
    assert m["rows"][0]["evidence"][0]["normalization"]["decimal"] == "."


def test_measured_unit_keeps_ambiguous_grouping_unread():
    receipt = pdf(["Receipt no. | WE-1", "Purchase order | PO-8801", "Received | 5,350 kg"], "receipt")
    ex, m = received(receipt)
    assert m["errors"] and "received_quantity" in ex["conflicts"]


def test_delivery_note_number_is_the_identity_only_without_a_receipt_number():
    ok = pdf(["Delivery note | DN-7663", "Purchase order | PO-8801", "Received | 30 BOX"], "receipt")
    assert received(ok)[1]["rows"][0]["receipt_id"] == "DN-7663"
    bad = pdf(["Delivery note | DEL/7663", "Purchase order | PO-8801", "Received | 30 BOX"], "receipt")
    ex, m = received(bad)
    assert m["errors"] and not m["rows"] and "received_quantity" not in ex["values"]


def test_snake_case_receipt_table():
    receipt = table([["grn_no", "grn_date", "po_no", "supplier", "item_code", "description", "qty_received",
                      "qty_rejected", "qty_accepted", "uom", "received_by"],
                     ["GRN-99", "2026-09-26", "PO-8801", "Kestrel Pumps Ltd.", "IK-40", "Impeller kit", "40", "2",
                      "38", "BOX", "E. Fairlie"]], "receipt")
    ex, m = received(receipt)
    assert m["errors"] == [] and ex["values"]["received_quantity"] == "38"


def test_receipt_title_beside_its_labelled_number():
    receipt = pdf(["GOODS RECEIVED NOTE | GRN No. | GR-592", "Date received | 16/09/2026",
                   "Main stores | PO No. | PO-8801", "Supplier: Kestrel Pumps Ltd.",
                   "Item | Description | Ordered | Delivered | Rejected | Accepted UoM",
                   "IK-40 | Impeller kit | 40 | 40 | 0 | 40 BOX"], "receipt")
    ex, m = received(receipt)
    assert m["errors"] == [] and ex["values"]["received_quantity"] == "40"


def test_receipt_for_another_purchase_order_is_refused():
    receipt = pdf(["Receipt no. | WE-1", "Purchase order | PO-9999", "Received | 30 BOX"], "receipt")
    ex, m = received(receipt)
    assert "RECEIPT_PURCHASE_ORDER_MISMATCH" in m["errors"] or not m["rows"]
    assert "received_quantity" in ex["conflicts"]


def test_receipt_table_line_column_is_local_but_po_line_column_binds():
    po = table(PO[:2] + [["Line", "2"], ["Ordered quantity", "40"]], "purchase_order")
    head = [["Purchase order", "PO-8801"], ["Currency / unit", "USD / BOX"]]
    local = table(head + [["Receipt", "Line", "Accepted quantity", "BOX"], ["GR-1", "1", "30", "BOX"]], "receipt")
    assert received(local, po=po)[1]["errors"] == []
    bound = table(head + [["Receipt", "PO line", "Accepted quantity", "BOX"], ["GR-1", "1", "30", "BOX"]], "receipt")
    assert "RECEIPT_PO_LINE_BINDING_MISMATCH" in received(bound, po=po)[1]["errors"]


def test_two_receipt_files_without_lines_need_the_po_line_item():
    po = table([["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
                ["Item code", "Description", "Ordered quantity", "Unit"], ["IK-40", "Impeller kit", "40", "box"]],
               "purchase_order")

    def grn(number, code, qty):
        return table([["Receipt no.", number], ["Purchase order", "PO-8801"],
                      ["Item", "Description", "Received", "UoM"], [code, "Impeller kit", qty, "BOX"]], "receipt")

    ex, m = received(grn("GR-1", "IK-40", "30"), grn("GR-2", "IK-40", "8"), po=po)
    assert m["errors"] == [] and ex["values"]["received_quantity"] == "38"
    ex, m = received(grn("GR-1", "IK-40", "30"), grn("GR-2", "SR-10", "8"), po=po)
    assert "received_quantity" in ex["conflicts"]


def test_heading_beside_a_party_heading_is_not_the_issuer():
    invoice = pdf(["COMMERCIAL INVOICE", "Seller | Document data", "Kestrel Pumps Ltd. | Doc. ref | INV-77",
                   "14 Wharf Road | Issued on | 12-Sep-2026"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["invoice_supplier"]["value"] == "Kestrel Pumps Ltd."
    assert "supplier" not in ex["conflicts"]


def test_buyer_named_under_a_ship_to_heading_is_never_the_issuer():
    po = table([["Harrow Trading Ltd.", "PURCHASE ORDER"], ["PO Number", "PO-8801"], ["VENDOR", "SHIP TO"],
                ["Kestrel Pumps Ltd.", "Harrow Trading Ltd."], ["Ordered quantity", "40"]], "purchase_order")
    invoice = pdf(["CUSTOMER", "Invoice # | INV-77 | Harrow Trading Ltd.", "Purchase order | PO-8801"], "invoice")
    ex = packet(invoice, po)
    assert ex["invoice_supplier"] is None and ex["supplier_check"]["status"] == "not_determined"
    assert "supplier" not in ex["conflicts"]


def test_trade_name_short_form_is_not_determined():
    invoice = pdf(["Kestrel", "Maison Corvelle SAS | Invoice number | INV-77"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["invoice_supplier"] is None and "supplier" not in ex["conflicts"]


def test_shifted_printed_row_is_a_conflict_not_a_line_total():
    invoice = pdf(["Kestrel Pumps Ltd.", "Invoice No: INV-77", "Qty Unit | Description | Rate | Amount",
                   "50 BOX | Impeller kit | 13.29 | 664.50 | x"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"].get("unit_price") != "664.50" and unresolved(ex, "unit_price")


def test_logo_short_name_is_never_reported_as_the_issuer():
    invoice = pdf(["Kestrel", "Kestrel Pumps Ltd. 14 Wharf Road 3526 KM Utrecht", "Invoice number | INV-77"], "invoice")
    po = table([["Purchase order", "PO-8801"], ["Ordered quantity", "40"]], "purchase_order")
    ex = packet(invoice, po)
    assert ex["invoice_supplier"] is None and "supplier" not in ex["conflicts"]


def test_letterhead_region_ends_at_a_label_cell():
    invoice = pdf(["CUSTOMER", "Invoice # | INV-77 | Harrow Trading Ltd.", "Toronto ON", "Canada"], "invoice")
    ex = packet(invoice, table([["Purchase order", "PO-8801"], ["Ordered quantity", "40"]], "purchase_order"))
    assert ex["invoice_supplier"] is None


# ---- round 3: reviewer defects


@pytest.mark.parametrize("line_heading", ["Line", "#"])
def test_one_receipt_with_distinct_local_lines_is_not_summed(line_heading):
    receipt = table([["Receipt", line_heading, "Accepted quantity", "Unit"],
                     ["GR-1", "1", "32", "BOX"], ["GR-1", "2", "8", "BOX"]], "receipt")
    ex, m = received(receipt)
    assert "RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED" in m["errors"] and "received_quantity" in ex["conflicts"]


def test_local_lines_tied_to_the_po_item_are_summed():
    po = table([["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
                ["Item code", "Description", "Ordered quantity", "Unit"], ["IK-40", "Impeller kit", "40", "box"]],
               "purchase_order")
    receipt = table([["Receipt", "Line", "Item", "Description", "Accepted quantity", "Unit"],
                     ["GR-1", "1", "IK-40", "Impeller kit", "32", "BOX"],
                     ["GR-1", "2", "IK-40", "Impeller kit", "8", "BOX"]], "receipt")
    ex, m = received(receipt, po=po)
    assert m["errors"] == [] and ex["values"]["received_quantity"] == "40"


def test_grn_and_the_delivery_note_it_books_are_one_delivery():
    grn = pdf(["Receipt no. | GR-1", "Delivery note | DN-1", "Purchase order | PO-8801", "Line | 1", "Received | 30 BOX"],
              "receipt")
    dn = pdf(["Delivery note | DN-1", "Purchase order | PO-8801", "Line | 1", "Received | 30 BOX"], "receipt")
    ex, m = received(grn, dn)
    assert m["errors"] == [] and m["total"] == "30" and ex["values"]["received_quantity"] == "30"
    other = pdf(["Delivery note | DN-1", "Purchase order | PO-8801", "Line | 1", "Received | 31 BOX"], "receipt")
    ex, m = received(grn, other)
    assert "RECEIPT_IDENTITY_CONFLICT" in m["errors"] and "received_quantity" in ex["conflicts"]


@pytest.mark.parametrize(
    "rows",
    [["Purchase order | 9999 rev2"], ["Order no. | SO 9999"], ["PO number | 4500 9999"], ["PO number | PO 9999"],
     ["Purchase order | see attached"]],
)
def test_receipt_purchase_order_reference_must_key_to_this_po(rows):
    receipt = pdf(["Receipt no. | WE-1"] + rows + ["Received | 40 BOX"], "receipt")
    ex, m = received(receipt)
    assert not m["rows"] and "received_quantity" not in ex["values"]


def test_multi_word_po_reference_that_keys_to_this_po_is_accepted():
    receipt = pdf(["Receipt no. | WE-1", "PO number | PO 8801", "Received | 40 BOX"], "receipt")
    assert received(receipt)[0]["values"]["received_quantity"] == "40"


def test_count_unit_grouping_needs_the_packets_decimal_point():
    eu_invoice = table([["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"], ["Purchase order", "PO-8801"],
                        ["Quantity", "1,5", "BOX"], ["Unit price", "12,50", "EUR"]], "invoice")
    receipt = pdf(["Receipt no. | WE-5", "Purchase order | PO-8801", "Received | 1,500 BOX"], "receipt")
    ex, m = received(receipt, invoice=eu_invoice)
    assert not m["rows"] and "received_quantity" not in ex["values"]
    dozen = pdf(["Receipt no. | WE-5", "Purchase order | PO-8801", "Received | 1,500 dozen"], "receipt")
    ex, m = received(dozen)
    assert not m["rows"] and "received_quantity" not in ex["values"]


# ---- round 3: invoice totals, cards, prose, wide tables


def test_total_label_with_currency_code_then_amount_cell():
    invoice = pdf(["Kestrel Pumps Ltd.", "Invoice No: INV-77", "Qty | Description | Rate | Line total",
                   "40 | Impeller kit | 12.50 | 500.00", "Subtotal | USD 500.00", "Sales tax (8%) | USD 40.00",
                   "Amount due (USD) | USD 540.00"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    v = ex["values"]
    assert (v["invoice_face_amount"], v["tax_rate"], v["invoice_quantity"]) == ("540.00", "0.08", "40")
    assert ex["invoice_arithmetic"]["status"] == "consistent"


def test_exported_record_card_with_snake_case_keys():
    invoice = table([["field", "value"], ["issuer", "Kestrel Pumps Ltd."], ["invoice_number", "INV-77"],
                     ["units_billed", "40"], ["measure", "box"], ["rate_per_unit", "12.50"], ["levy_percent", "8"],
                     ["sum_payable", "540.00"], ["currency", "USD"]], "invoice")
    v = packet(invoice, table(PO, "purchase_order"))["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"], v["invoice_face_amount"]) == ("40", "12.50", "0.08", "540.00")


def test_wide_table_with_merged_headings_picks_the_one_fitting_layout():
    invoice = pdf(["Kestrel Pumps Ltd. | Invoice", "Statement no. | INV-77",
                   "Pos Order ref | Article no. | Description | Qty UoM | List price Disc % | Net price | Net amount | Tax",
                   "10 | PO-8801 | IK-40 | Impeller kit | 40 box | 12.50 | 0.00 | 12.50 | 500.00 | 8%",
                   "Amount payable USD | 540.00"], "invoice")
    v = packet(invoice, table(PO, "purchase_order"))["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"]) == ("40", "12.50", "0.08")


def test_prose_letter_invoice():
    invoice = pdf(["Kestrel Pumps Ltd.", "Please find below our invoice ref. INV-77, raised against your order PO-8801.",
                   "We supplied 40 box of Impeller kit (our code IK-40)",
                   "at $12.50 per box, giving a goods value of $500.00.",
                   "Sales tax charged at 8% amounts to $40.00.", "The amount now due is $540.00."], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    v = ex["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"], v["invoice_face_amount"]) == ("40", "12.50", "0.08", "540.00")


def test_quantity_times_priced_amount():
    invoice = pdf(["Kestrel Pumps Ltd.", "Invoice No: INV-77", "40 box × $12.50 | $500.00",
                   "Sales tax 8% | $40.00", "Total due | $540.00"], "invoice")
    v = packet(invoice, table(PO, "purchase_order"))["values"]
    assert (v["invoice_quantity"], v["unit_price"]) == ("40", "12.50")


@pytest.mark.parametrize("face, ok", [("540.00", True), ("5400.00", False)])
def test_quantity_per_heading_needs_the_arithmetic(face, ok):
    invoice = pdf(["Kestrel Pumps Ltd.", "Invoice No: INV-77", "Code | Item | Quantity per | Price | Ext.",
                   "IK-40 | Impeller kit | 40 box | 12.50 | 500.00", "Sales tax 8% | 40.00",
                   f"Please pay this amount (USD) | {face}"], "invoice")
    v = packet(invoice, table(PO, "purchase_order"))["values"]
    assert (v.get("invoice_quantity") == "40") is ok


def test_vendor_identifier_is_not_the_vendor_name():
    po = table([["Purchase order", "PO-8801"], ["Vendor ID", "V938588"], ["Approved vendor", "Kestrel Pumps Ltd."],
                ["Ordered quantity", "40"]], "purchase_order")
    ex = packet(table(INVOICE, "invoice"), po)
    assert ex["values"]["supplier"] == "Kestrel Pumps Ltd." and "supplier" not in ex["conflicts"]


# ---- round 4: reviewer regressions and the arithmetic guard

HEAD4 = ["Kestrel Pumps Ltd.", "Invoice No: INV-77", "Purchase order: PO-8801"]


@pytest.mark.parametrize(
    "rows",
    [
        ["Code | Description | Qty | List price Unit price | Amount", "IK-40 | Impeller kit | 40 | 12.50 | 500.00 | 8%"],
        ["Description | Batch Qty | Unit price | Amount", "Impeller kit | 40 | 12.50 | 500.00 | 1"],
        ["Description | Qty | Disc % Unit price | Amount", "Impeller kit | 40 | 15.00 | 540.00 | x5"],
        ["Description | Lot Qty | Price | Amount", "Impeller kit | 40 | 12.50 | 500.00 | 99"],
        ["Description | Qty | Unit price Disc % | Amount", "Impeller kit | 40 | 15.00 | 10 | 540.00"],
    ],
)
def test_shifted_rows_under_merged_headings_never_give_values(rows):
    v = packet(pdf(HEAD4 + rows, "invoice"), table(PO, "purchase_order"))["values"]
    assert v.get("unit_price") in (None, "12.50") and v.get("invoice_quantity") in (None, "40")
    assert v.get("unit_price") != "15.00"


@pytest.mark.parametrize(
    "tail",
    [
        ["Sales tax 8% | 40.00", "Deposit received | 200.00", "Amount now due (USD) | USD 340.00"],
        ["Sales tax 8% | 40.00", "Paid on account | 200.00", "Sum payable | USD 340.00"],
        ["Sales tax 8% | 40.00", "Credit note CN-4 | -100.00", "Please pay this amount | USD 440.00"],
        ["Sales tax 8% | 40.00", "The amount now due is $340.00 after your deposit of $200.00."],
    ],
)
def test_balance_after_deposit_or_credit_is_not_the_face(tail):
    invoice = pdf(HEAD4 + ["Qty | Description | Rate | Amount", "40 | Impeller kit | 12.50 | 500.00"] + tail, "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"].get("invoice_face_amount") is None


def test_balance_line_beside_an_invoice_total_keeps_the_total():
    invoice = pdf(HEAD4 + ["Qty | Description | Rate | Amount", "40 | Impeller kit | 12.50 | 500.00",
                           "Sales tax 8% | 40.00", "Invoice total | 540.00", "Deposit received | 200.00",
                           "Balance due | 340.00"], "invoice")
    assert packet(invoice, table(PO, "purchase_order"))["values"]["invoice_face_amount"] == "540.00"


def test_prose_price_beside_a_second_price_is_refused():
    invoice = pdf(["Kestrel Pumps Ltd.", "Invoice ref. INV-77 for order PO-8801.", "We supplied 40 box of Impeller kit",
                   "list price at $15.00 per box, discounted to $12.50.", "The amount now due is $540.00.",
                   "Sales tax charged at 8%."], "invoice")
    assert packet(invoice, table(PO, "purchase_order"))["values"].get("unit_price") != "15.00"


def test_arithmetic_guard_refuses_every_reading_when_the_total_does_not_add_up():
    rows = ["Qty | Description | Rate | Amount", "40 | Impeller kit | 12.50 | 500.00", "Sales tax 8% | 40.00"]
    ex = packet(pdf(HEAD4 + rows + ["Invoice total | 560.00"], "invoice"), table(PO, "purchase_order"))
    for field in ("invoice_quantity", "unit_price", "tax_rate", "invoice_face_amount"):
        assert field in ex["conflicts"], field
    assert ex["invoice_arithmetic"]["status"] == "inconsistent_refused"
    # A printed tax amount must be the rate applied to the goods.
    bad_tax = ["Qty | Description | Rate | Amount", "40 | Impeller kit | 12.50 | 500.00", "Sales tax 8% | 45.00",
               "Invoice total | 540.00"]
    ex = packet(pdf(HEAD4 + bad_tax, "invoice"), table(PO, "purchase_order"))
    assert "invoice_face_amount" in ex["conflicts"] and "unit_price" in ex["conflicts"]
    # Printed freight is an exact, known convention.
    freight = rows + ["Freight | 20.00", "Invoice total | 560.00"]
    ex = packet(pdf(HEAD4 + freight, "invoice"), table(PO, "purchase_order"))
    assert ex["invoice_arithmetic"]["status"] == "consistent" and ex["values"]["invoice_face_amount"] == "560.00"


def test_po_currency_in_a_label_conflicts_with_the_invoice():
    po = pdf(["Harrow Trading Ltd. | PURCHASE ORDER", "PO Number | PO-8801", "Supplier | Kestrel Pumps Ltd.",
              "Ordered quantity | 40", "Order total (CAD, excl. tax) | 500.00"], "purchase_order")
    ex = packet(table(INVOICE, "invoice"), po)
    assert "currency" in ex["conflicts"]


def test_purchase_order_conflict_refuses_the_received_quantity():
    receipt = pdf(["Receipt no. | WE-1", "PO number | PO-88 01", "Received | 40 BOX"], "receipt")
    ex, m = received(receipt)
    assert "received_quantity" not in ex["values"]


# ---- round 5: guard gaps and reviewer paths


@pytest.mark.parametrize(
    "invoice_rows",
    [
        [["Invoice number", "INV-77"], ["Line", "Qty", "Rate", "Tax"], ["Impeller kit", "40", "12.50", "8"],
         ["Total payable", "548.00", "USD"]],
    ],
)
def test_dropping_an_unconfirmed_reading_never_disables_the_guard(invoice_rows):
    ex = packet(table(invoice_rows, "invoice"), table(PO, "purchase_order"))
    for field in ("invoice_quantity", "unit_price", "invoice_face_amount"):
        assert field in ex["conflicts"] and field not in ex["values"], field


def test_quantity_per_heading_with_a_wrong_total_refuses_everything():
    invoice = pdf(HEAD4 + ["Code | Item | Quantity per | Price | Ext.", "IK-40 | Impeller kit | 40 box | 12.50 | 500.00",
                           "Sales tax 8% | 40.00", "Please pay this amount (USD) | 548.00"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert not {"invoice_quantity", "unit_price", "tax_rate", "invoice_face_amount"} & set(ex["values"])


# Round 6 (confirm-or-refuse): without a tax rate no money field is reported.
@pytest.mark.parametrize("tax, face_ok", [("40.00", False), ("45.00", False)])
def test_unknown_rate_face_needs_the_printed_tax_amount(tax, face_ok):
    invoice = pdf(HEAD4 + ["Quantity: 40", "Unit price: USD 12.50", f"Tax: USD {tax}", "Invoice total: USD 540.00"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert (ex["values"].get("invoice_face_amount") == "540.00") is face_ok


@pytest.mark.parametrize(
    "note",
    ["Bank details (EUR account) | IBAN DE89370400440532013000", "Remit to (GBP account) | Sort code 20-00-00",
     "Payments from abroad (CHF) incur fees", "Bank charges (EUR) borne by payer", "Equivalent (CHF) | 590.00"],
)
def test_bank_and_conversion_notes_declare_no_currency(note):
    po = table([["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
                ["Description", "Ordered quantity", "Unit", "Unit price"], ["Impeller kit", "40", "box", "12.50"]],
               "purchase_order")
    body = ["Qty | Description | Unit price | Amount", "40 | Impeller kit | 12.50 | 500.00", "Tax 8%", "Invoice total | 540.00"]
    ex = packet(pdf(HEAD4 + body + [note], "invoice"), po)
    assert ex["values"].get("currency") is None


def test_delivery_note_beside_an_uncited_grn_is_refused():
    grn = pdf(["Receipt no. | GR-1", "Purchase order | PO-8801", "Line | 1", "Received | 30 BOX"], "receipt")
    dn = pdf(["Delivery note | DN-1", "Purchase order | PO-8801", "Line | 1", "Delivered | 30 BOX"], "receipt")
    ex, m = received(grn, dn)
    assert "received_quantity" not in ex["values"] and "RECEIPT_IDENTITY_CONFLICT" in m["errors"]


PO_ITEM = [["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
           ["Item code", "Description", "Ordered quantity", "Unit"], ["IK-40", "Impeller kit", "40", "box"]]


def test_receipt_for_another_item_is_refused():
    kv = pdf(["Receipt no. | GR-1", "Purchase order | PO-8801", "Code | SR-10", "Material | Seal ring",
              "Received | 25 BOX"], "receipt")
    tbl = table([["Receipt no.", "GR-1"], ["Purchase order", "PO-8801"], ["Item", "Description", "Received", "UoM"],
                 ["SR-10", "Seal ring", "25", "BOX"]], "receipt")
    for receipt in (kv, tbl):
        ex, _ = received(receipt, po=table(PO_ITEM, "purchase_order"))
        assert "received_quantity" not in ex["values"]


def test_receipt_line_must_be_the_invoice_bound_po_line():
    po2 = table([["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
                 ["Line", "Item code", "Description", "Ordered quantity", "Unit", "Unit price", "Currency"],
                 ["1", "IK-40", "Impeller kit", "40", "box", "12.50", "USD"],
                 ["2", "SR-10", "Seal ring", "40", "box", "3.00", "USD"]], "purchase_order")
    inv = table([["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"], ["Purchase order", "PO-8801"],
                 ["Item code", "Description", "Quantity", "Unit", "Unit price", "Currency"],
                 ["IK-40", "Impeller kit", "40", "BOX", "12.50", "USD"]], "invoice")
    for label in ("Line", "PO line"):
        rec = pdf(["Receipt no. | GR-1", "Purchase order | PO-8801", f"{label} | 2", "Received | 25 BOX"], "receipt")
        ex, m = received(rec, invoice=inv, po=po2)
        assert "RECEIPT_PO_LINE_BINDING_MISMATCH" in m["errors"] and "received_quantity" not in ex["values"]


@pytest.mark.parametrize(
    "line, field",
    [("Minimum order quantity: 10", "invoice_quantity"), ("Units: 12 per box", "invoice_quantity"),
     ("Withholding tax 10%", "tax_rate"), ("Early payment discount 2%", "tax_rate")],
)
def test_non_invoiced_quantities_and_percentages_are_not_fields(line, field):
    invoice = pdf(HEAD4 + [line, "Invoice total: USD 540.00"], "invoice")
    assert field not in packet(invoice, table(PO, "purchase_order"))["values"]


@pytest.mark.parametrize(
    "rows",
    [
        [["Receipt no.", "GR-1"], ["Purchase order", "PO-8801"], ["Item", "Description", "Received", "Failed QC", "UoM"],
         ["IK-40", "Impeller kit", "40", "5", "BOX"]],
        [["Receipt", "Line", "Received quantity", "Unit", "Movement"], ["GR-1", "1", "40", "BOX", "Goods receipt"],
         ["GR-2", "1", "5", "BOX", "Return delivery"]],
    ],
)
def test_rejections_and_returns_are_never_added(rows):
    ex, _ = received(table(rows, "receipt"))
    assert ex["values"].get("received_quantity") not in ("40", "45")


def test_received_in_another_unit_than_ordered_is_refused():
    po = table([["Supplier", "Kestrel Pumps Ltd."], ["Purchase order", "PO-8801"],
                ["Description", "Ordered quantity", "Unit", "Unit price", "Currency"], ["Impeller kit", "10", "dozen", "150.00", "USD"]],
               "purchase_order")
    inv = table([["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"], ["Purchase order", "PO-8801"],
                 ["Quantity", "10", "dozen"], ["Unit price", "150.00", "USD"]], "invoice")
    rec = pdf(["Receipt no. | GR-1", "Purchase order | PO-8801", "Received | 120 EA"], "receipt")
    ex, _ = received(rec, invoice=inv, po=po)
    assert "received_quantity" not in ex["values"] and "received_quantity" in ex["conflicts"]


def test_freight_billed_separately_is_not_on_the_invoice():
    invoice = pdf(HEAD4 + ["Quantity: 40", "Unit price: USD 12.50", "Shipping: USD 40.00 (billed separately)",
                           "Invoice total: USD 540.00"], "invoice")
    ex = packet(invoice, table(PO, "purchase_order"))
    assert ex["values"].get("invoice_face_amount") is None


def test_document_title_beside_a_short_number_is_no_invoice_number():
    invoice = pdf(["Brackenridge Fastener Works Ltd", "TAX INVOICE | 7 | Unit 7, Calder Trading Estate",
                   "Purchase order: PO-8801"], "invoice")
    assert "invoice" not in packet(invoice, table(PO, "purchase_order"))["values"]
    # Round 6: a bare "INVOICE" title never gives the number; an explicit label does.
    title = pdf(["Brackenridge Fastener Works Ltd", "INVOICE | 554841", "Purchase order: PO-8801"], "invoice")
    assert "invoice" not in packet(title, table(PO, "purchase_order"))["values"]
    ok = pdf(["Brackenridge Fastener Works Ltd", "Invoice No. | 554841", "Purchase order: PO-8801"], "invoice")
    assert packet(ok, table(PO, "purchase_order"))["values"]["invoice"] == "554841"


# ---- round 6: confirm-or-refuse money fields, percent cells, invoice numbers, credit notes, PO keys

FULL = ["Qty | Description | Rate | Amount", "40 | Impeller kit | 12.50 | 500.00", "Sales tax 8% | 40.00",
        "Invoice total | USD 540.00"]


def test_money_fields_are_reported_only_together_and_consistent():
    ex = packet(pdf(HEAD4 + FULL, "invoice"), table(PO, "purchase_order"))
    v = ex["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"], v["invoice_face_amount"]) == ("40", "12.50", "0.08", "540.00")


@pytest.mark.parametrize(
    "lines",
    [
        ["Labour rate: USD 45.00", "Sales tax 8%", "Invoice total: USD 540.00"],
        ["Hourly rate: USD 45.00", "Invoice total: USD 540.00"],
        ["Surcharge at USD 0.40 per kg", "Sales tax 8%", "Invoice total: USD 540.00"],
        ["Pallet charge: 2 pallet at USD 15.00 per pallet", "Invoice total: USD 570.00"],
        ["Quantity: 45", "Invoice total: USD 500.00"],
    ],
)
def test_partial_money_readings_are_never_reported(lines):
    v = packet(pdf(HEAD4 + lines, "invoice"), table(PO, "purchase_order"))["values"]
    assert not {"invoice_quantity", "unit_price", "tax_rate", "invoice_face_amount"} & set(v)


def test_combined_taxes_are_never_one_component_rate():
    lines = ["Quantity: 40", "Unit price: USD 12.50", "GST 5%", "PST 7%"]
    v = packet(pdf(HEAD4 + lines, "invoice"), table(PO, "purchase_order"))["values"]
    assert "tax_rate" not in v
    v = packet(pdf(HEAD4 + lines + ["Invoice total: USD 560.00"], "invoice"), table(PO, "purchase_order"))["values"]
    assert "tax_rate" not in v and "invoice_face_amount" not in v


def _xlsx_invoice(rate_cell, fmt):
    from openpyxl import Workbook

    book = Workbook()
    rows = [["Supplier", "Kestrel Pumps Ltd."], ["Invoice number", "INV-77"], ["Purchase order", "PO-8801"],
            ["Quantity", 40], ["Unit price", 12.5], ["VAT %", rate_cell], ["Invoice total", 540], ["Currency", "USD"]]
    for row in rows:
        book.active.append(row)
    book.active["B6"].number_format = fmt
    out = io.BytesIO()
    book.save(out)
    return _source(out.getvalue(), "inv.xlsx", "invoice")


def test_percent_formatted_cell_is_already_the_fraction():
    ex = packet(_xlsx_invoice(0.08, "0%"), table(PO, "purchase_order"))
    assert ex["values"].get("tax_rate") == "0.08"
    ex = packet(_xlsx_invoice(0.08, "General"), table(PO, "purchase_order"))
    assert ex["values"].get("tax_rate") != "0.0008" and "tax_rate" not in ex["values"]


def test_fraction_under_a_percent_label_in_text_is_ambiguous():
    lines = ["Quantity: 40", "Unit price: USD 12.50", "Tax %: 0.08", "Invoice total: USD 540.00"]
    v = packet(pdf(HEAD4 + lines, "invoice"), table(PO, "purchase_order"))["values"]
    assert v.get("tax_rate") not in ("0.0008",) and "tax_rate" not in v


@pytest.mark.parametrize(
    "lines",
    [
        ["Kestrel Pumps Ltd.", "INVOICE | 1200 Market Street"], ["Kestrel Pumps Ltd.", "INVOICE | SW1A 1AA"],
        ["Kestrel Pumps Ltd.", "INVOICE | PO-8801"], ["Kestrel Pumps Ltd.", "INVOICE", "C-10442"],
        ["Kestrel Pumps Ltd.", "CREDIT NOTE CN-4", "Original invoice: INV-77"],
        ["Kestrel Pumps Ltd.", "Invoice No. | PO-8801"],
    ],
)
def test_invoice_number_needs_an_explicit_label_and_its_own_shape(lines):
    v = packet(pdf(lines + ["Purchase order: PO-8801"], "invoice"), table(PO, "purchase_order"))["values"]
    assert v.get("invoice") in (None,)


def test_credit_note_never_gives_a_payable_face():
    lines = ["Kestrel Pumps Ltd.", "CREDIT NOTE", "Credit note no: CN-4", "Purchase order: PO-8801"] + FULL
    ex = packet(pdf(lines, "invoice"), table(PO, "purchase_order"))
    assert "invoice_face_amount" not in ex["values"] and "invoice_face_amount" in ex["conflicts"]


@pytest.mark.parametrize("ref, accepted", [("PO-8801", True), ("PO 8801", True), ("PO-8801-1", False), ("PO 88-01", False)])
def test_po_keys_keep_separators_between_digit_groups(ref, accepted):
    receipt = pdf(["Receipt no. | WE-1", f"Purchase order | {ref}", "Received | 30 BOX"], "receipt")
    ex, _ = received(receipt)
    assert ("received_quantity" in ex["values"]) is accepted
    from folio.provenance import purchase_order_key

    assert purchase_order_key("PO-0732") == purchase_order_key("po0732") == purchase_order_key("PO 0732")
    assert purchase_order_key("PO-8801-1") != purchase_order_key("PO-88011")


# ---- round 7: reversed, cancelled, returned, draft or non-receipt movements


@pytest.mark.parametrize(
    "marker",
    ["Movement | Reversal of GR-0", "Cancellation of goods receipt GR-0", "VOID", "RETURN TO SUPPLIER",
     "Return delivery", "DRAFT - NOT POSTED", "Status | Cancelled", "Remarks | Reversed by GR-3", "Pro-forma"],
)
def test_reversed_or_unposted_receipt_supports_nothing(marker):
    receipt = pdf([marker, "Receipt no. | GR-1", "Purchase order | PO-8801", "Line | 1", "Received | 40 BOX"], "receipt")
    original = pdf(["Receipt no. | GR-0", "Purchase order | PO-8801", "Line | 1", "Received | 40 BOX"], "receipt")
    for docs in ((receipt,), (original, receipt)):
        ex, m = received(*docs)
        assert "RECEIPT_REVERSAL_OR_UNPOSTED" in m["errors"] and "received_quantity" not in ex["values"]


@pytest.mark.parametrize("title", [["Return to vendor"], ["Status", "Cancelled"]])
def test_reversed_receipt_tables_support_nothing(title):
    rows = [title, ["Receipt no.", "GR-1"], ["Purchase order", "PO-8801"], ["Item", "Description", "Received", "UoM"],
            ["IK-40", "Impeller kit", "40", "BOX"]]
    ex, m = received(table(rows, "receipt"), po=table(PO_ITEM, "purchase_order"))
    assert "received_quantity" not in ex["values"]


@pytest.mark.parametrize("codes, ok", [(["101", "101"], True), (["101", "102"], False), (["101", "122"], False)])
def test_movement_type_must_be_a_plain_goods_receipt(codes, ok):
    rows = [["Receipt", "Line", "Received quantity", "Unit", "Movement type"],
            ["GR-0", "1", "20", "BOX", codes[0]], ["GR-1", "1", "20", "BOX", codes[1]]]
    ex, m = received(table(rows, "receipt"))
    assert (ex["values"].get("received_quantity") == "40") is ok
    if not ok:
        assert "RECEIPT_REVERSAL_OR_UNPOSTED" in m["errors"]


@pytest.mark.parametrize("marker, ok", [("Reprint of GR-0", False), ("Copy of GR-0", False), ("Replaces GR-0", False),
                                        ("Reprint of GR-1", True), ("DUPLICATE COPY", True)])
def test_copy_of_another_receipt_supports_nothing(marker, ok):
    receipt = pdf([marker, "Receipt no. | GR-1", "Purchase order | PO-8801", "Line | 1", "Received | 40 BOX"], "receipt")
    ex, m = received(receipt)
    assert ("received_quantity" in ex["values"]) is ok
    if not ok:
        assert "RECEIPT_COPY_IDENTITY_MISMATCH" in m["errors"]


@pytest.mark.parametrize("reference, ok", [("PO-DIM-EUR-KG", True), ("PO-OTHER-REF", False)])
def test_alphabetic_po_reference_is_keyed_and_compared(reference, ok):
    po = pdf(["Purchase order PO-DIM-EUR-KG", "Supplier Aster Supply Co.", "Ordered quantity 120"], "purchase_order")
    receipt = pdf(["Goods receipt GR-EUR-KG", f"Purchase order {reference}", "Received quantity 100",
                   "Unit kg", "Currency EUR"], "receipt")
    ex = packet(po, receipt)
    assert (ex["values"].get("received_quantity") == "100") is ok
    if not ok:
        assert unresolved(ex, "received_quantity")
