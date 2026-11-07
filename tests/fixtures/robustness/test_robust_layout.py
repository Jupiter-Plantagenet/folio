"""folio-layout-v2 field reading on invoices and purchase orders."""

from robust_helpers import (
    INVOICE,
    PURCHASE_ORDER,
    csv_source,
    ocr_source,
    receipt_rows,
    source,
    stacked,
    words,
    xlsx_source,
)

from folio.provenance import extract_packet, validate_citation


def packet(invoice_rows, po_rows=PURCHASE_ORDER, receipt=None):
    sources = [
        source(invoice_rows, "invoice") if isinstance(invoice_rows, list) else invoice_rows,
        source(po_rows, "purchase_order") if isinstance(po_rows, list) else po_rows,
        receipt or source(receipt_rows(), "receipt"),
    ]
    return sources, extract_packet(sources)


def unresolved(ex, field):
    return field in ex["missing"] or field in ex["conflicts"]


def citations_roundtrip(sources, ex):
    for entries in ex["field_provenance"].values():
        for entry in entries:
            src = entry.get("source") or {}
            if "start" in src:
                validate_citation(sources, src)


def test_standard_layout_output_is_unchanged_by_v2():
    sources, ex = packet(INVOICE)
    assert ex["values"]["invoice_quantity"] == "120"
    assert ex["values"]["invoice_face_amount"] == "6220.80"
    assert ex["conflicts"] == [] and ex["missing"] == []
    # v1 provenance is kept: no v2 entry replaces an agreeing v1 reading
    assert all("reader" not in e for e in ex["field_provenance"]["invoice_quantity"])
    assert ex["supplier_check"]["status"] == "match"


def test_two_column_layout_keeps_each_label_with_its_value():
    rows = [
        (60, 60, "ASTER SUPPLY CO."), (330, 60, "Unit price USD 48.00"),
        (60, 86, "Invoice INV-7001"), (330, 86, "Tax rate 8%"),
        (60, 112, "Purchase order PO-7001"), (330, 112, "Invoice face total USD 6220.80"),
        (60, 138, "Quantity 120"), (330, 138, "Remit within 30 days."),
    ]
    po = [(60, 60, "NORTHSTAR STUDIO"), (330, 60, "Ordered quantity 120"),
          (60, 86, "Purchase order PO-7001"), (330, 86, "Unit price USD 48.00"),
          (60, 112, "Supplier Aster Supply Co.")]
    sources, ex = packet(rows, po)
    v = ex["values"]
    assert (v["invoice_quantity"], v["ordered_quantity"], v["supplier"]) == ("120", "120", "Aster Supply Co.")
    assert v["unit_price"] == "48.00" and v["tax_rate"] == "0.08"
    assert not ex["conflicts"]
    citations_roundtrip(sources, ex)


def test_label_synonyms_colons_and_quantity_at_price():
    rows = stacked([
        "COBALT FASTENERS INC", "TAX INVOICE", "Bill to: NORTHSTAR STUDIO",
        "Our ref / Invoice INV-7001", "Customer PO: PO-7001",
        "Item: Shipping cartons Qty billed: 120 @ USD 48.00 ea",
        "Sales tax 8% applied per line", "Amount due USD 6220.80",
    ])
    po = stacked(["NORTHSTAR STUDIO", "PURCHASE ORDER", "PO number: PO-7001",
                  "Vendor: Cobalt Fasteners Inc", "Shipping cartons - order qty 120 at USD 48.00"])
    sources, ex = packet(rows, po)
    v = ex["values"]
    assert v["invoice_quantity"] == "120" and v["unit_price"] == "48.00"
    assert v["invoice_face_amount"] == "6220.80" and v["tax_rate"] == "0.08"
    assert v["ordered_quantity"] == "120" and v["supplier"] == "Cobalt Fasteners Inc"
    # "applied per line" is prose, not a PO line declaration
    assert "RECEIPT_PO_LINE_BINDING_MISMATCH" not in ex["receipt_manifest"]["errors"]
    assert ex["supplier_check"]["status"] == "match"
    citations_roundtrip(sources, ex)


def test_table_columns_align_by_x_position_and_line_total_is_not_face_total():
    rows = [
        (60, 60, "ASTER SUPPLY CO."), (60, 86, "Invoice INV-7001"), (60, 112, "Purchase order PO-7001"),
        (60, 160, "Description"), (300, 160, "Qty"), (360, 160, "Unit Price"), (470, 160, "Total"),
        # right-aligned numbers sit to the right of their header's start
        (60, 184, "Cartons, 40 x 30 cm"), (310, 184, "120"), (372, 184, "48.00"), (470, 184, "5,760.00"),
        (360, 230, "Subtotal"), (470, 230, "5,760.00"),
        (360, 254, "Tax rate 8%"), (360, 278, "Total due USD 6,220.80"),
    ]
    sources, ex = packet(rows)
    v = ex["values"]
    assert v["invoice_quantity"] == "120" and v["unit_price"] == "48.00"
    assert v["invoice_face_amount"] == "6220.80"  # never the 5,760.00 line total or subtotal
    assert not ex["conflicts"]
    citations_roundtrip(sources, ex)


def test_multi_row_table_is_a_conflict_not_a_sum_or_first_row():
    rows = [
        (60, 60, "ASTER SUPPLY CO."), (60, 86, "Invoice INV-7001"),
        (60, 160, "Item"), (300, 160, "Qty"), (360, 160, "Unit Price"),
        (60, 184, "Cartons"), (300, 184, "60"), (360, 184, "48.00"),
        (60, 208, "Tape"), (300, 208, "60"), (360, 208, "48.00"),
        (360, 260, "Total due USD 6,220.80"),
    ]
    _, ex = packet(rows)
    assert "invoice_quantity" in ex["conflicts"] and "invoice_quantity" not in ex["values"]


def test_label_above_value_in_boxes():
    rows = [
        (60, 50, "ASTER SUPPLY CO."), (60, 80, "Invoice INV-7001"), (60, 104, "Purchase order PO-7001"),
        (66, 150, "Quantity"), (196, 150, "Unit price"), (326, 150, "Tax rate"), (456, 150, "Invoice total"),
        (66, 176, "120"), (196, 176, "USD 48.00"), (326, 176, "8%"), (456, 176, "USD 6220.80"),
    ]
    sources, ex = packet(rows)
    v = ex["values"]
    assert (v["invoice_quantity"], v["unit_price"], v["tax_rate"], v["invoice_face_amount"]) == (
        "120", "48.00", "0.08", "6220.80")
    assert not ex["conflicts"]
    citations_roundtrip(sources, ex)


def test_locale_numbers_use_document_evidence():
    rows = stacked(["ASTER SUPPLY CO.", "Invoice INV-7001", "Purchase order PO-7001", "Menge 1.200",
                    "Einzelpreis USD 4,00", "MwSt 8 %", "Gesamtbetrag USD 5.184,00"])
    po = stacked(["NORTHSTAR STUDIO", "Purchase order PO-7001", "Supplier Aster Supply Co.",
                  "Ordered quantity 1,200", "Unit price USD 4.00"])
    sources, ex = packet(rows, po)
    v = ex["values"]
    assert v["invoice_quantity"] == "1200" and v["unit_price"] == "4.00"
    assert v["invoice_face_amount"] == "5184.00" and v["ordered_quantity"] == "1200"
    entry = ex["field_provenance"]["invoice_quantity"][0]
    assert entry["source"]["quote"] == "1.200"
    assert entry["normalization"] == {"rule": "folio-number-v2", "decimal": ",", "ocr": False}
    # v1 read "1,200" as invalid; v2 repaired the same cited span
    assert "ordered_quantity" not in ex["conflicts"]
    citations_roundtrip(sources, ex)


def test_ambiguous_and_split_numbers_stay_unresolved():
    no_evidence = stacked(["ASTER SUPPLY CO.", "Invoice INV-7001", "Quantity 1,200",
                           "Unit price USD 5", "Invoice face total USD 6000"])
    _, ex = packet(no_evidence)
    assert unresolved(ex, "invoice_quantity")
    split = stacked(["ASTER SUPPLY CO.", "Invoice INV-7001", "Quantity 1 200",
                     "Unit price USD 5.00", "Invoice face total USD 6000.00"])
    _, ex = packet(split)
    # v1 alone read "1"; the split digit group vetoes it
    assert unresolved(ex, "invoice_quantity") and ex["values"].get("invoice_quantity") != "1"


def test_conflicting_readings_are_conflicts():
    rows = stacked(["ASTER SUPPLY CO.", "Invoice INV-7001", "Quantity 120", "Qty billed: 130",
                    "Unit price USD 48.00", "Invoice face total USD 6220.80"])
    _, ex = packet(rows)
    assert "invoice_quantity" in ex["conflicts"]


def test_foreign_currency_amount_is_never_a_usd_value():
    rows = stacked(["ASTER SUPPLY CO.", "Invoice INV-7001", "Quantity 120", "Unit price EUR 48.00",
                    "Invoice total EUR 6220.80"])
    _, ex = packet(rows)
    assert "currency" in ex["conflicts"]
    assert "unit_price" not in ex["values"] and "invoice_face_amount" not in ex["values"]


def test_ocr_invoice_repairs_digits_and_refuses_low_confidence():
    good = ocr_source(
        [words("ASTER", "SUPPLY", "CO."), words("Invoice", "INV-7001"), words("Quantity", "12O"),
         words("Unit", "price", "USD", "48.00"), words("Tax", "rate", "8%"),
         words("Invoice", "total", "USD", "6220.80")],  # confirm-or-refuse (round 6)
        "invoice",
    )
    _, ex = packet(good)
    assert ex["values"]["invoice_quantity"] == "120"
    entry = ex["field_provenance"]["invoice_quantity"][0]
    assert entry["source"]["quote"] == "12O" and entry["normalization"]["ocr"] is True
    low = ocr_source(
        [words("ASTER", "SUPPLY", "CO."), words("Invoice", "INV-7001"), words("Quantity", "120")],
        "invoice", overrides={"120": 55.0},
    )
    _, ex = packet(low)
    assert unresolved(ex, "invoice_quantity")  # present but unreadable: never accepted


def test_formula_cells_are_never_read_as_values():
    inv = xlsx_source([["Invoice", "INV-7001"], ["Quantity", 1], ["Unit price USD", "48.00"]],
                      "invoice", formulas=[("B2", "=100+20")])
    sources, ex = packet(inv)
    assert ex["values"].get("invoice_quantity") != "120"
    assert "invoice_quantity" not in ex["values"]


def test_csv_purchase_order_label_beside_value_cells():
    po = csv_source([["Purchase order", "PO-7001"], ["Supplier", "Aster Supply Co."],
                     ["Ordered quantity", "120"]], "purchase_order")
    sources, ex = packet(INVOICE, po)
    assert ex["values"]["ordered_quantity"] == "120" and ex["values"]["supplier"] == "Aster Supply Co."
    assert not ex["conflicts"]


def test_prose_is_never_a_label():
    rows = stacked(["ASTER SUPPLY CO.", "Invoice INV-7001", "Purchase order PO-7001", "Quantity 120",
                    "Unit price USD 48.00", "Tax rate 8%", "Invoice face total USD 6220.80",
                    "Remit within 30 days from the invoice date.",
                    "Our price list and rates change each quarter."])
    _, ex = packet(rows)
    assert not ex["conflicts"] and ex["supplier_check"]["status"] == "match"


def test_v1_conflict_between_documents_survives_a_single_v2_reading():
    """Regression: v1 conflicts carry no provenance; v2 must not erase them."""
    second = csv_source([["Invoice", "INV-CONFLICT"]], "invoice")
    sources = [source(INVOICE, "invoice"), second, source(PURCHASE_ORDER, "purchase_order"),
               source(receipt_rows(), "receipt")]
    ex = extract_packet(sources)
    assert "invoice" in ex["conflicts"] and "invoice" in ex["missing"]
