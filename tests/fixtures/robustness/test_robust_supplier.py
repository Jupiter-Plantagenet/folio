"""Invoice-issuer extraction, supplier normalisation and the deterministic gate."""

import pytest
from robust_helpers import (
    INVOICE,
    PURCHASE_ORDER,
    ocr_source,
    receipt_rows,
    source,
    stacked,
    words,
)

from folio.agent import ToolBroker, supplier_identity
from folio.layout import normalize_supplier
from folio.provenance import extract_packet


@pytest.mark.parametrize(
    "a,b",
    [
        ("Aster Supply Co.", "ASTER SUPPLY CO"),
        ("Aster Supply Co.", "Aster Supply Company"),
        ("Bramble & Finch Limited", "BRAMBLE AND FINCH LTD"),
        ("Falkner Logistik GmbH & Co. KG", "Falkner Logistik GmbH"),
        ("Larkspur Ceramics S.A.", "Larkspur Ceramics SA"),
        ("Cobalt Fasteners, Inc.", "Cobalt Fasteners Incorporated"),
        ("The Quarry Stone Supply LLC", "Quarry Stone Supply"),
        ("  Oakhurst   Foods, L.L.C. ", "Oakhurst Foods LLC"),
    ],
)
def test_same_company_normalises_equal(a, b):
    assert normalize_supplier(a) == normalize_supplier(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("Silverline Optics", "Pinecrest Chemicals"),
        ("Aster Supply Co.", "Aster Supplies Co."),
        ("Northfield Cables", "Northfield Cable Works"),
        ("Copperleaf Trading Ltd", "Juniper Office GmbH"),
    ],
)
def test_different_companies_stay_different(a, b):
    assert normalize_supplier(a) != normalize_supplier(b)


def packet(invoice_rows, po_rows=PURCHASE_ORDER):
    sources = [
        source(invoice_rows, "invoice") if isinstance(invoice_rows, list) else invoice_rows,
        source(po_rows, "purchase_order"),
        source(receipt_rows(), "receipt"),
    ]
    return extract_packet(sources)


BODY = INVOICE[1:]


def test_letterhead_matching_po_supplier_is_a_match():
    ex = packet(INVOICE)
    assert ex["supplier_check"]["status"] == "match"
    assert ex["invoice_supplier"]["value"] == "ASTER SUPPLY CO."
    assert "supplier" not in ex["conflicts"]


def test_letterhead_naming_another_company_is_a_supplier_conflict():
    ex = packet(stacked(["SILVERLINE OPTICS"] + [t for _, _, t in BODY]))
    assert "supplier" in ex["conflicts"]
    assert ex["values"]["supplier"] == "Aster Supply Co."  # PO supplier kept for review
    assert ex["supplier_check"]["status"] == "conflict"
    assert ex["invoice_supplier"]["value"] == "SILVERLINE OPTICS"


def test_banner_and_title_lines_before_the_issuer_do_not_cause_conflicts():
    ex = packet(stacked(["FOLIO AUTHOR-CREATED SYNTHETIC FIXTURE", "TAX INVOICE", "ASTER SUPPLY CO."]
                        + [t for _, _, t in BODY]))
    assert ex["supplier_check"]["status"] == "match" and "supplier" not in ex["conflicts"]


def test_explicit_issuer_label_decides():
    ex = packet(stacked(["INVOICE", "From: Copperleaf Trading Ltd"] + [t for _, _, t in BODY]))
    assert "supplier" in ex["conflicts"] and ex["supplier_check"]["method"] == "explicit-label"
    ex = packet(stacked(["INVOICE", "Seller: Aster Supply Company"] + [t for _, _, t in BODY]))
    assert "supplier" not in ex["conflicts"] and ex["supplier_check"]["status"] == "match"


def test_no_identifiable_issuer_is_not_determined_not_a_conflict():
    for head in (["INVOICE"], [], ["Page 1 of 2"], ["Thank you for your business"]):
        ex = packet(stacked(head + [t for _, _, t in BODY]))
        assert ex["supplier_check"]["status"] == "not_determined", head
        assert "supplier" not in ex["conflicts"]


def test_buyer_block_is_not_the_issuer():
    ex = packet(stacked(["Bill to: NORTHSTAR STUDIO"] + [t for _, _, t in BODY]))
    assert ex["supplier_check"]["status"] == "not_determined"


def test_low_confidence_ocr_letterhead_never_creates_a_conflict():
    inv = ocr_source(
        [words("ASTFR", "SUPPIY", "CO."), words("Invoice", "INV-7001"), words("Quantity", "120")],
        "invoice", overrides={"ASTFR": 61.0, "SUPPIY": 58.0},
    )
    ex = packet(inv)
    assert "supplier" not in ex["conflicts"]


# ------------------------------------------------------------- tool layer

def case(**kw):
    base = {
        "supplier": "Aster Supply Co.",
        "invoice_quantity": "120",
        "ordered_quantity": "120",
        "received_quantity": "100",
        "currency": "USD",
        "unit": "each",
        "missing_fields": [],
        "conflicting_fields": [],
        "sources": [{"id": "s", "sha256": "0" * 64, "pages": [{"number": 1, "text": "x"}]}],
        "invoice_supplier": {"value": "ASTER SUPPLY CO."},
    }
    base.update(kw)
    return base


def broker(c, payable_supplier="Aster Supply Co"):
    b = ToolBroker(c, lambda: {"supplier": payable_supplier, "recognized_quantity": "0"})
    b.execute("read_sources", {})
    b.execute("lookup_payable", {})
    return b


def rec(mode, q="100"):
    return {"mode": mode, "quantity": q if mode == "partial" else ("0" if mode != "no_change" else q), "reason": "test reason"}


def test_normalised_equal_suppliers_allow_partial():
    b = broker(case())
    assert b.execute("recommend_outcome", rec("partial"))["accepted"]
    m = b.match()
    assert m["supplier_identity_conflict"] is False and m["executable_partial_supported"] is True
    assert m["invoice_header_supplier"] == "ASTER SUPPLY CO."


@pytest.mark.parametrize(
    "kw,payable",
    [
        ({"conflicting_fields": ["supplier"]}, "Aster Supply Co"),
        ({"invoice_supplier": {"value": "SILVERLINE OPTICS"}}, "Aster Supply Co"),
        ({}, "Redwood Logistics Ltd"),
    ],
)
@pytest.mark.parametrize("mode", ["partial", "no_change"])
def test_supplier_conflict_refuses_partial_and_no_change(kw, payable, mode):
    b = broker(case(**kw), payable)
    with pytest.raises(ValueError, match="SUPPLIER_IDENTITY_CONFLICT"):
        b.execute("recommend_outcome", rec(mode))
    assert b.match()["supplier_identity_conflict"] is True
    assert b.match()["executable_partial_supported"] is False
    # escalate and hold remain available
    assert b.execute("recommend_outcome", rec("escalate"))["accepted"]


def test_identity_helper_reports_unknown_as_none():
    out = supplier_identity({"supplier": "", "invoice_supplier": None}, {"supplier": "X"})
    assert out["po_equals_payable"] is None and out["invoice_equals_payable"] is None
    assert out["conflict"] is False


def test_ocr_letterhead_near_the_po_name_is_not_a_conflict_but_a_real_mismatch_is():
    near = ocr_source([words("ASTER", "SUPPIY", "CO."), words("Invoice", "INV-7001")], "invoice")
    ex = packet(near)
    assert "supplier" not in ex["conflicts"]
    assert ex["supplier_check"]["status"] == "not_determined"
    assert ex["invoice_supplier"] is None
    other = ocr_source([words("SILVERLINE", "OPTICS"), words("Invoice", "INV-7001")], "invoice")
    ex = packet(other)
    assert "supplier" in ex["conflicts"]
    # a digital letterhead one letter off is a different printed name: strict
    ex = packet(stacked(["ASTER SUPPLIES CO."] + [t for _, _, t in BODY]))
    assert "supplier" in ex["conflicts"]


def test_supplier_name_stops_at_the_next_labelled_field():
    po = stacked(["NORTHSTAR STUDIO", "Purchase order PO-7001",
                  "Supplier Aster Supply Co. Ordered quantity 120", "Unit price USD 48.00"])
    ex = packet(INVOICE, po)
    assert ex["values"]["supplier"] == "Aster Supply Co." and ex["values"]["ordered_quantity"] == "120"
    assert "supplier" not in ex["conflicts"]
    po = stacked(["NORTHSTAR STUDIO", "Purchase order PO-7001", "Supplier Acme Total Logistics",
                  "Ordered quantity 120"])
    ex = packet(stacked(["ACME TOTAL LOGISTICS"] + [t for _, _, t in BODY]), po)
    assert ex["values"]["supplier"] == "Acme Total Logistics" and "supplier" not in ex["conflicts"]
