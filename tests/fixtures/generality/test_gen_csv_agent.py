"""folio-csv-dialect-v1 (in the parser code) and decision order in the broker (round 7)."""

import pytest
from gen_helpers import csv_raw, source

from folio.agent import RECOVERABLE, SYSTEM_PROMPT, ToolBroker, scope_conflicts
from folio.formats import csv_delimiter, parse_document
from folio.parser_output import validate
from folio.provenance import extract_packet

ROWS = [["Invoice", "INV-77"], ["Quantity", "60"], ["Unit price", "EUR 12,40"], ["Total", "EUR 744,00"]]


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_allowlisted_delimiters_are_detected_and_byte_ranges_validate(delimiter):
    raw = csv_raw(ROWS, delimiter)
    parsed = parse_document(raw, "invoice.csv")
    assert parsed["csv"]["dialect"]["delimiter"] == delimiter
    validate(parsed, raw)  # host-side validator: every cell's bytes and literal
    cells = {(c["row"], c["column"]): c["value"] for c in parsed["cells"]}
    assert cells[(3, 2)] == "EUR 12,40" and cells[(2, 2)] == "60"


def test_semicolon_csv_with_decimal_commas_extracts():
    src = source(csv_raw(ROWS + [["Tax rate", "0%"]], ";"), "invoice", "inv.csv")
    ex = extract_packet([src])
    assert ex["values"]["unit_price"] == "12.40" and ex["values"]["invoice_face_amount"] == "744.00"
    assert ex["values"]["currency"] == "EUR"


@pytest.mark.parametrize(
    "text",
    [
        "Supplier;Tamsin Craft, Ltd\r\nInvoice;TC-1, page 1\r\n",
        "a|b\tc\r\nd|e\tf\r\n",
    ],
)
def test_ambiguous_dialects_are_refused(text):
    with pytest.raises(ValueError, match="CSV_DIALECT_AMBIGUOUS"):
        parse_document(text.encode(), "x.csv")


def test_single_column_and_quoted_delimiters_keep_their_meaning():
    assert csv_delimiter("Only one column\r\nsecond\r\n") == ","
    assert csv_delimiter('Item;"Bolt; zinc";4\r\nItem;"Nut, brass";5\r\n') == ";"
    assert csv_delimiter('Note,"a; b"\r\nQty,5\r\n') == ","


def test_formula_text_in_a_semicolon_csv_stays_literal():
    raw = csv_raw([["Received quantity", "=10+10"], ["Receipt", "GR-1"], ["Unit", "each"]], ";")
    parsed = parse_document(raw, "r.csv")
    assert any(c["value"] == "=10+10" for c in parsed["cells"])
    ex = extract_packet([{**parsed, "id": "r", "document_role": "receipt", "version": 1}])
    assert "received_quantity" not in ex["values"]


# ------------------------------------------------------------------ decision order

def case(**kw):
    base = {
        "invoice_quantity": "120", "ordered_quantity": "120", "received_quantity": "100",
        "supplier": "Aster Supply Co.", "currency": "USD", "unit": "each",
        "missing_fields": [], "conflicting_fields": [],
        "sources": [{"id": "s", "sha256": "0" * 64, "pages": [{"number": 1, "text": "x"}]}],
    }
    base.update(kw)
    return base


def broker(c, payable_currency="USD"):
    pay = {"supplier": "Aster Supply Co.", "currency": payable_currency, "recognized_quantity": "0"}
    b = ToolBroker(c, lambda: pay)
    b.execute("read_sources", {})
    b.execute("lookup_payable", {})
    return b


def rec(mode, quantity="0"):
    return {"mode": mode, "quantity": quantity if mode == "partial" else "0", "reason": "test reason"}


def test_payable_currency_mismatch_escalates_only():
    b = broker(case(currency="EUR"))
    m = b.execute("compute_match", {})
    assert m["escalation_required"] == ["currency"] and m["currency_conflict"] is True
    assert m["executable_partial_supported"] is False
    for mode in ("partial", "no_change", "hold"):
        with pytest.raises(ValueError, match="ESCALATION_REQUIRED"):
            b.execute("recommend_outcome", rec(mode, "100"))
    assert b.execute("recommend_outcome", rec("escalate"))["accepted"]


def test_detected_unit_conflict_escalates_even_when_support_is_missing():
    c = case(missing_fields=["received_quantity"], conflicting_fields=["unit"], received_quantity="")
    b = broker(c)
    assert b.execute("compute_match", {})["escalation_required"] == ["unit"]
    with pytest.raises(ValueError, match="ESCALATION_REQUIRED"):
        b.execute("recommend_outcome", rec("hold"))
    assert b.execute("recommend_outcome", rec("escalate"))["accepted"]


def test_supplier_conflict_with_missing_support_escalates():
    c = case(missing_fields=["received_quantity"], conflicting_fields=["supplier"])
    b = broker(c)
    with pytest.raises(ValueError, match="ESCALATION_REQUIRED"):
        b.execute("recommend_outcome", rec("hold"))
    with pytest.raises(ValueError, match="SUPPLIER_IDENTITY_CONFLICT"):
        b.execute("recommend_outcome", rec("partial", "100"))


def test_missing_support_without_a_detected_mismatch_still_holds():
    b = broker(case(missing_fields=["received_quantity"], conflicting_fields=["invoice_quantity"]))
    assert b.execute("compute_match", {})["escalation_required"] == []
    assert b.execute("recommend_outcome", rec("hold"))["accepted"]


def test_matching_non_usd_currency_is_not_a_conflict():
    b = broker(case(currency="JPY"), payable_currency="JPY")
    assert scope_conflicts(b.case, b.payable) == []
    assert b.execute("recommend_outcome", rec("partial", "100"))["accepted"]


def test_prompt_and_recovery_name_the_new_order():
    assert "escalation_required" in SYSTEM_PROMPT and "not in USD" not in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.index("Detected mismatch") < SYSTEM_PROMPT.index("Missing support")
    assert "ESCALATION_REQUIRED" in RECOVERABLE
