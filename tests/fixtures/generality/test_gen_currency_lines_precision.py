"""folio-currency-v1, folio-line-binding-v1 and folio-invoice-consistency-v1 (round 7)."""

import pytest
from gen_helpers import (
    invoice,
    ocr_source,
    po,
    receipt,
    source,
    stacked,
    table_rows,
    words,
)

from folio import money
from folio.numbers import NumberError, parse_number, recompute
from folio.provenance import extract_packet, validate_citation, validate_value

PO_LINES = ["NORTHSTAR STUDIO", "Purchase order PO-610", "Supplier Kiln Works", "Ordered quantity 40"]
RCPT = ["NORTHSTAR STUDIO", "Goods receipt GR-610", "Purchase order PO-610", "Received quantity 36", "Unit each"]


def inv(*money_lines, qty="Quantity 40"):
    return invoice("KILN WORKS", "Invoice KW-610", "Purchase order PO-610", qty, *money_lines)


def packet(*invoice_money, po_extra=(), qty="Quantity 40"):
    srcs = [inv(*invoice_money, qty=qty), po(*PO_LINES, *po_extra), receipt(*RCPT)]
    return extract_packet(srcs), srcs


# ------------------------------------------------------------------ table and marks

@pytest.mark.parametrize("code,places", [("USD", 2), ("JPY", 0), ("KRW", 0), ("KWD", 3), ("BHD", 3), ("EUR", 2), ("CLP", 0)])
def test_minor_unit_table(code, places):
    assert money.minor_units(code) == places
    validate_value("currency", code)


@pytest.mark.parametrize("code", ["XAU", "XXX", "usd", "EURO", "CLF", ""])
def test_unpinned_currency_values_are_invalid(code):
    with pytest.raises(ValueError):
        validate_value("currency", code)


def test_scale_follows_the_currency():
    assert money.scale_ok("1250", "JPY") and not money.scale_ok("1250.5", "JPY")
    assert money.scale_ok("12.345", "KWD") and not money.scale_ok("12.3456", "KWD")


def test_families_never_decide_alone():
    dollar, yen = money.read_mark("$"), money.read_mark("¥")
    assert money.resolve([dollar]) == (None, None)
    assert money.resolve([dollar, money.read_mark("CAD")]) == ("CAD", None)
    assert money.resolve([yen, money.read_mark("USD")]) == (None, "conflict")
    assert money.resolve([money.read_mark("EUR"), money.read_mark("GBP")]) == (None, "conflict")
    assert money.read_mark("all") is None and money.read_mark("TOP") is not None


# ------------------------------------------------------------------ packet currency

def test_single_currency_packet_in_eur_is_supported_with_citations():
    ex, srcs = packet("Unit price EUR 25.00", "Tax rate 0%", "Total EUR 1,000.00", po_extra=["Unit price EUR 25.00"])
    assert ex["values"]["currency"] == "EUR" and "currency" not in ex["conflicts"]
    assert ex["values"]["unit_price"] == "25.00" and ex["values"]["invoice_face_amount"] == "1000.00"
    for entry in ex["field_provenance"]["currency"]:
        validate_citation(srcs, entry["source"])


def test_invoice_and_po_currencies_disagree():
    ex, _ = packet("Unit price EUR 25.00", "Tax rate 0%", "Total EUR 1,000.00", po_extra=["Unit price USD 25.00"])
    assert "currency" in ex["conflicts"] and "currency" not in ex["values"]


def test_suffix_symbol_and_glued_code():
    ex, _ = packet("Unit price 25,00 €", "MwSt 0 %", "Gesamtbetrag 1.000,00 €")
    assert ex["values"]["currency"] == "EUR" and ex["values"]["invoice_face_amount"] == "1000.00"
    ex, _ = packet("Unit price USD25.00", "Tax rate 0%", "Total USD1,000.00")
    assert ex["values"]["currency"] == "USD" and ex["values"]["unit_price"] == "25.00"


def test_dollar_sign_needs_an_explicit_code():
    ex, _ = packet("Unit price $25.00", "Tax rate 0%", "Total $1,000.00")
    assert "currency" not in ex["values"]  # never guessed as USD
    ex, _ = packet("Currency: CAD", "Unit price $25.00", "Tax rate 0%", "Total $1,000.00")
    assert ex["values"]["currency"] == "CAD" and ex["values"]["invoice_face_amount"] == "1000.00"
    ex, _ = packet("Currency: EUR", "Unit price $25.00", "Tax rate 0%", "Total $1,000.00")
    assert "currency" in ex["conflicts"]


def test_unreadable_currency_declaration_is_a_conflict():
    ex, _ = packet("Currency: Doubloons", "Unit price USD 25.00", "Tax rate 0%", "Total USD 1,000.00")
    assert "currency" in ex["conflicts"]


def test_zero_decimal_currency_rejects_a_fractional_face():
    ex, _ = packet("Unit price JPY 980", "Tax rate 0%", "Total JPY 39,200.50")
    assert "invoice_face_amount" in ex["conflicts"]
    ex, _ = packet("Unit price JPY 980", "Tax rate 0%", "Total JPY 39,200")
    # "39,200" alone proves no separator: refused, never resolved by currency
    assert "invoice_face_amount" not in ex["values"]
    # (1,060 x 980 = 1,038,800: the face must also pass folio-invoice-consistency-v1.)
    ex, _ = packet("Unit price JPY 980", "Tax rate 0%", "Total JPY 1,038,800", qty="Quantity 1,060")
    assert ex["values"]["invoice_face_amount"] == "1038800" and ex["values"]["invoice_quantity"] == "1060"


def test_three_decimal_currency_face():
    ex, _ = packet("Unit price KWD 56.250", "Tax rate 0%", "Total KWD 2,250.000", qty="Quantity 40")
    assert ex["values"]["invoice_face_amount"] == "2250.000" and ex["values"]["unit_price"] == "56.250"


def test_space_grouped_money_needs_a_currency_mark_and_recomputes():
    rows = stacked(["KILN WORKS", "Invoice KW-610", "Purchase order PO-610", "Quantity 50",
                    "Unit price SEK 200,00", "VAT 25%", "Total due SEK 12 500,00"])
    ex = extract_packet([source(rows, "invoice")])
    assert ex["values"]["invoice_face_amount"] == "12500.00"
    (entry,) = ex["field_provenance"]["invoice_face_amount"]
    assert entry["source"]["quote"] == "12 500,00"
    assert recompute(entry["source"]["quote"], entry["normalization"]) == "12500.00"
    # quantities are never joined: "2 400" stays unreadable
    ex = extract_packet([source(stacked(["Invoice KW-1", "Quantity 2 400"]), "invoice")])
    assert "invoice_quantity" not in ex["values"]
    with pytest.raises(NumberError):
        parse_number("12 500,00")


# ------------------------------------------------------------------ line binding

INV_ROWS = [["AC-100", "Panel grey", "40", "18.00", "720.00"], ["AC-140", "Panel oak", "25", "26.00", "650.00"]]


def multi_invoice(rows=INV_ROWS):
    head = [(60, 60, "KILN WORKS"), (60, 86, "Invoice KW-7"), (60, 112, "Purchase order PO-610")]
    tail = [(380, 260, "Tax rate 0%"), (380, 284, "Total due USD 1,370.00")]
    return source(head + table_rows(["Item code", "Description", "Qty", "Unit price", "Amount"], rows,
                                    [60, 140, 320, 380, 470]) + tail, "invoice")


@pytest.mark.parametrize(
    "po_ident",
    [["Item code AC-140"], ["Description: Panel oak"], ["SKU: ac140"]],
)
def test_multi_row_invoice_binds_the_po_item(po_ident):
    srcs = [multi_invoice(), po(*PO_LINES[:3], *po_ident, "Ordered quantity 25"), receipt(*RCPT)]
    ex = extract_packet(srcs)
    assert ex["line_binding"]["status"] == "bound"
    # Round-6 owner decision (confirm-or-refuse): a multi-line invoice is not
    # checked by folio-invoice-consistency-v1, so its money fields are refused.
    assert "invoice_quantity" not in ex["values"] and "unit_price" not in ex["values"]


@pytest.mark.parametrize(
    "po_ident",
    [[], ["Item code AC-999"], ["Description: Panel"]],
)
def test_multi_row_invoice_without_a_single_match_stays_a_conflict(po_ident):
    ex = extract_packet([multi_invoice(), po(*PO_LINES[:3], *po_ident, "Ordered quantity 25"), receipt(*RCPT)])
    assert "invoice_quantity" in ex["conflicts"] and "invoice_quantity" not in ex["values"]


def test_codes_beat_descriptions_and_rows_are_never_summed():
    rows = [["AC-100", "Panel oak", "40", "18.00", "720.00"], ["AC-140", "Panel oak", "25", "26.00", "650.00"]]
    ex = extract_packet([multi_invoice(rows), po(*PO_LINES[:3], "Description: Panel oak", "Ordered quantity 25")])
    assert "invoice_quantity" in ex["conflicts"]  # two rows match the description: refused
    ex = extract_packet([multi_invoice(rows), po(*PO_LINES[:3], "Item code AC-100", "Description: Panel oak")])
    assert ex["line_binding"]["status"] == "bound" and ex["line_binding"]["identity"]["code"] == "AC-100"
    assert "invoice_quantity" not in ex["values"]  # multi-line: confirm-or-refuse (round 6)


def test_heading_words_are_not_item_names():
    ex = extract_packet([multi_invoice(), po(*PO_LINES), receipt(*RCPT)])
    assert ex["line_binding"]["identity"] is None


# ------------------------------------------------------------------ arithmetic consistency

def test_subtotal_is_never_read_as_the_total_by_the_v1_pattern():
    ex, _ = packet("Unit price USD 25.00", "Subtotal USD 1,000.00", "Tax rate 8%", "Invoice total USD 1,080.00")
    assert ex["values"]["invoice_face_amount"] == "1080.00"
    assert ex["invoice_arithmetic"]["status"] == "consistent"


@pytest.mark.parametrize("face", ["1,000.00", "80.00"])
def test_total_equal_to_net_or_tax_is_refused(face):
    ex, _ = packet("Unit price USD 25.00", "Tax rate 8%", f"TOTAL USD {face}")
    assert "invoice_face_amount" in ex["conflicts"] and "invoice_face_amount" not in ex["values"]


def test_printed_freight_explains_the_difference_and_anything_else_is_refused():
    # Round-4 owner decision: a printed freight line is an exact known
    # convention; any other difference refuses every reading involved.
    ex, _ = packet("Unit price USD 25.00", "Tax rate 8%", "Freight USD 35.00", "Total due USD 1,115.00")
    assert ex["values"]["invoice_face_amount"] == "1115.00"
    assert ex["invoice_arithmetic"]["status"] == "consistent"
    ex, _ = packet("Unit price USD 25.00", "Tax rate 8%", "Total due USD 1,115.00")
    assert "invoice_face_amount" in ex["conflicts"] and "invoice_face_amount" not in ex["values"]
    assert ex["invoice_arithmetic"]["status"] == "inconsistent_refused"


def test_inconsistent_ocr_reading_is_refused_rather_than_guessed():
    lines = [words("KILN", "WORKS"), words("Invoice", "KW-610"), words("Purchase", "order", "PO-610"),
             words("Quantity", "7"), words("Unit", "price", "USD", "48.00"), words("Tax", "rate", "8%"),
             words("Invoice", "total", "USD", "6220.80")]
    ex = extract_packet([ocr_source(lines, "invoice")])
    assert "invoice_quantity" in ex["conflicts"] and "invoice_quantity" not in ex["values"]
    ok = [*lines[:3], words("Quantity", "120"), *lines[4:]]
    ex = extract_packet([ocr_source(ok, "invoice")])
    assert ex["values"]["invoice_quantity"] == "120"
    assert ex["invoice_arithmetic"]["status"] == "consistent"
