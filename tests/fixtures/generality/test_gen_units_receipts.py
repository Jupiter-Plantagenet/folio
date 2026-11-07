"""folio-unit-v2 and the generalised receipt reader (round 7)."""

import copy

import pytest
from gen_helpers import invoice, po, receipt, source, stacked, table_rows

from folio.persisted_receipts import manifest as validate_manifest
from folio.provenance import extract_packet, validate_citation
from folio.receipts import receipt_manifest
from folio.units import canonical_unit, heading_unit, recompute

INV = ["ORCHARD TOOLS", "Invoice INV-501", "Purchase order PO-501", "Quantity 120 pcs",
       "Unit price USD 2.00", "Tax rate 0%", "Total due USD 240.00"]
PO = ["NORTHSTAR STUDIO", "Purchase order PO-501", "Supplier Orchard Tools", "Ordered quantity 120 pcs",
      "Unit price USD 2.00"]


@pytest.mark.parametrize(
    "word,unit",
    [("each", "each"), ("EA", "each"), ("pcs", "each"), ("PCS.", "each"), ("Stk", "each"), ("units", "each"),
     ("kg", "kg"), ("KGS", "kg"), ("L", "l"), ("litres", "l"), ("m", "m"), ("cm", "cm"), ("boxes", "box"),
     ("ctns", "carton"), ("pairs", "pair"), ("dozen", "dozen"), ("pallets", "pallet"), ("m²", "m2")],
)
def test_pinned_unit_table(word, unit):
    assert canonical_unit(word) == unit


@pytest.mark.parametrize("word", ["kilometres-ish", "widgets", "", "approx", "12"])
def test_unknown_unit_words_are_not_guessed(word):
    assert canonical_unit(word) is None


@pytest.mark.parametrize("symbol,canonical", [("m", "m"), ("g", "g")])
def test_case_sensitive_unit_symbols_are_not_case_folded(symbol, canonical):
    assert canonical_unit(symbol) == canonical
    assert canonical_unit(symbol.upper()) is None


def test_unit_words_remain_case_insensitive_and_litre_alias_is_explicit():
    assert canonical_unit("METERS") == "m"
    assert canonical_unit("GRAMS") == "g"
    assert canonical_unit("L") == "l"


@pytest.mark.parametrize("symbol", ["M", "G"])
def test_case_conflicting_unit_symbol_stays_unresolved_in_receipt_packet(symbol):
    base = "m" if symbol == "M" else "g"
    inv = invoice("ACME", "Invoice INV-CASE", "Purchase order PO-CASE",
                  f"Quantity 5 {base}", "Unit price USD 2.00", "Tax rate 0%", "Total USD 10.00")
    order = po("NORTHSTAR STUDIO", "Purchase order PO-CASE", "Supplier ACME",
               f"Ordered quantity 5 {base}", "Unit price USD 2.00")
    ex = extract_packet([inv, order, receipt("Goods receipt GR-CASE", "Received quantity 5",
                                             f"Unit {symbol}")])
    assert "RECEIPT_UNIT_MISMATCH" in ex["receipt_manifest"]["errors"]
    assert "received_quantity" in ex["missing"]


def test_heading_unit_and_recompute():
    assert heading_unit("(pcs)") == "each" and heading_unit("Qty") is None
    assert recompute("PCS", {"rule": "folio-unit-v2", "unit": "each"}) == "each"
    with pytest.raises(ValueError):
        recompute("PCS", {"rule": "folio-unit-v2", "unit": "kg"})
    with pytest.raises(ValueError):
        recompute("crates", {"rule": "folio-unit-v2", "unit": "each"})


def ok(m, total):
    assert m["errors"] == [], m["errors"]
    assert m["total"] == total
    return m["rows"][0]


def test_count_spellings_are_each_and_revalidate_in_persisted_history():
    s = receipt("NORTHSTAR STUDIO", "Goods receipt GR-1", "Purchase order PO-501", "Received quantity 118 PCS")
    row = ok(receipt_manifest([s]), "118")
    assert row["unit"] == "each"
    assert row["field_evidence"]["unit"][0]["normalization"] == {"rule": "folio-unit-v2", "unit": "each"}
    validate_manifest(receipt_manifest([s]))
    tampered = copy.deepcopy(receipt_manifest([s]))
    tampered["rows"][0]["field_evidence"]["unit"][0]["quote"] = "KG"
    with pytest.raises(ValueError):
        validate_manifest(tampered)


def test_quantity_only_receipt_has_no_currency_and_invoice_currency_is_case_basis():
    source = receipt("NORTHSTAR STUDIO", "Goods receipt GR-Q", "Purchase order PO-501",
                     "Received quantity 12 kg", "Unit kg")
    support = receipt_manifest([source])
    assert support["errors"] == []
    row = support["rows"][0]
    assert row["currency"] is None and row["currency_status"] == "quantity_only"
    assert "currency" not in row["field_evidence"]
    validate_manifest(support)

    sources = [
        invoice(*INV[:3], "Quantity 12 kg", "Unit price EUR 2.00", "Tax rate 0%", "Total due EUR 24.00"),
        po(*PO[:3], "Ordered quantity 12 kg", "Unit price EUR 2.00"),
        source,
    ]
    extracted = extract_packet(sources)
    assert extracted["receipt_manifest"]["errors"] == []
    assert extracted["values"]["currency"] == "EUR"
    assert extracted["values"]["received_quantity"] == "12"
    assert extracted["values"]["unit"] == "kg"


@pytest.mark.parametrize(
    "lines",
    [
        ("Received quantity 12", "Unit each", "Amount 10.00"),
        ("Received quantity 12", "Unit each", "Amount EUR 10.00", "Currency USD"),
        ("Received quantity 12", "Unit each", "Currency USD", "Currency EUR"),
    ],
)
def test_priced_missing_or_conflicting_receipt_currency_is_blocked(lines):
    result = receipt_manifest([receipt("Goods receipt GR-V", *lines)])
    assert result["errors"] and not result["rows"]


@pytest.mark.parametrize(
    "line,qty,unit",
    [("Received quantity 412.5 kg", "412.5", "kg"), ("Received qty: 860 m", "860", "m"),
     ("Quantity received: 18 boxes", "18", "box"), ("Received quantity 412.5kg", "412.5", "kg")],
)
def test_receipt_units_beyond_each(line, qty, unit):
    row = ok(receipt_manifest([receipt("Goods receipt GR-2", line)]), qty)
    assert row["unit"] == unit


def test_unit_mismatch_between_documents_is_a_unit_conflict_never_converted():
    ex = extract_packet([invoice(*INV), po(*PO), receipt("Goods receipt GR-3", "Purchase order PO-501",
                                                         "Received quantity 10", "Unit dozen")])
    assert "unit" in ex["conflicts"] and "unit" not in ex["values"]
    ex = extract_packet([invoice(*INV), po(*PO[:3], "Ordered quantity 10 boxes"),
                         receipt("Goods receipt GR-3", "Received quantity 120", "Unit each")])
    assert "unit" in ex["conflicts"]


def test_agreeing_units_give_the_packet_unit_with_citations():
    srcs = [invoice(*INV), po(*PO), receipt("Goods receipt GR-4", "Purchase order PO-501",
                                            "Received quantity 100", "Unit pcs")]
    ex = extract_packet(srcs)
    assert ex["values"]["unit"] == "each" and "unit" not in ex["conflicts"]
    srcs = [invoice(*INV[:3], "Quantity 50 kg", *INV[4:]), po(*PO[:3], "Ordered quantity 50 kg"),
            receipt("Goods receipt GR-5", "Received quantity 50", "Unit kg")]
    ex = extract_packet(srcs)
    assert ex["values"]["unit"] == "kg"
    for entry in ex["field_provenance"]["unit"]:
        for cited in [entry["source"]] + [s for i in entry.get("inputs", []) for s in i.get("sources", [])]:
            if "start" in cited:
                validate_citation(srcs, cited)


def test_unit_word_starting_a_label_is_not_consumed():
    # (tax and total printed: money fields are reported only when consistent, round 6)
    ex = extract_packet([invoice("ACME", "Invoice INV-9", "Quantity 120 Unit price USD 2.00", "Tax rate 0%",
                                 "Invoice total USD 240.00")])
    assert ex["values"]["invoice_quantity"] == "120" and ex["values"]["unit_price"] == "2.00"


def test_thermal_slip_glued_labels_dot_leaders_and_timestamps():
    rows = stacked(["DOCK 2", "GOODS IN SLIP", "GRN#:50418", "PO#:PO-77120", "QTY RCVD....236 PCS",
                    "CHECKED BY: J. MBEKI", "2026-10-12 14:03"], step=16)
    row = ok(receipt_manifest([source(rows, "receipt")]), "236")
    assert row["receipt_id"] == "50418" and row["unit"] == "each"


def test_receipt_title_line_needs_a_labelled_identity_elsewhere():
    ok(receipt_manifest([receipt("DELIVERY NOTE", "Delivery note no. DN-1", "Delivered quantity 5", "Unit each")]), "5")
    m = receipt_manifest([receipt("Goods receipt", "GR-77", "Received quantity 5", "Unit each")])
    assert m["errors"] and not m["rows"]


def test_bare_po_reference_is_ignored_only_when_it_is_the_declared_po():
    inv = invoice(*INV)
    good = receipt("Receipt no. DR-1", "PO-501", "Received quantity 5", "Unit each")
    assert receipt_manifest([inv, good])["rows"]
    other = receipt("Receipt no. DR-1", "PO-999", "Received quantity 5", "Unit each")
    m = receipt_manifest([inv, other])
    assert m["errors"] and not m["rows"]


def test_unit_word_label_and_weak_verb_sentences():
    row = ok(receipt_manifest([receipt("Receipt no. DR-2", "Pallets received: 3")]), "3")
    assert row["unit"] == "pallet"
    ok(receipt_manifest([receipt("Goods receipt GR-6", "Received quantity 100", "Unit each",
                                 "Received 2 pallets in good condition, checked by dock team.")]), "100")
    m = receipt_manifest([receipt("Goods receipt GR-6", "Received quantity 100", "Unit each",
                                  "Received quantity 100 approved by manager")])
    assert m["errors"] and not m["rows"]


@pytest.mark.parametrize(
    "lines,total",
    [
        (["Quantity received: 300", "Accepted quantity: 294", "Rejected quantity: 6"], "294"),
        (["Quantity received: 300", "Accepted quantity: 300"], "300"),
        (["Accepted qty 12"], "12"),
    ],
)
def test_inspection_accepted_quantity_is_supported_when_consistent(lines, total):
    row = ok(receipt_manifest([receipt("Inspection report no. IR-1", *lines, "UoM: units")]), total)
    validate_manifest(receipt_manifest([receipt("Inspection report no. IR-1", *lines, "UoM: units")]))
    assert row["quantity"] == total


@pytest.mark.parametrize(
    "lines",
    [
        ["Quantity received: 300", "Accepted quantity: 290", "Rejected quantity: 6"],
        ["Quantity received: 300", "Accepted quantity: 310"],
        ["Quantity received: 300", "Rejected quantity: 6"],
        ["Quantity received: 300", "Accepted quantity: 294", "Accepted quantity: 290"],
    ],
)
def test_inconsistent_or_subtractive_inspection_fails_closed(lines):
    m = receipt_manifest([receipt("Inspection report no. IR-2", *lines, "UoM: units")])
    assert m["errors"] and not m["rows"]


LOG = [(60, 60, "RECEIVING LOG"), (60, 86, "Log no. RL-1"), (60, 112, "Purchase order PO-501")]


def log_receipt(rows):
    return source(LOG + table_rows(["SKU", "Description", "Qty received", "UoM"], rows, [60, 150, 330, 430]), "receipt")


def test_multi_row_receipt_table_binds_the_po_item():
    inv = invoice(*INV[:3], "SKU: SKU-2", *INV[3:])
    rows = [["SKU-1", "Oat drink", "480", "pcs"], ["SKU-2", "Soy drink", "540", "pcs"]]
    m = receipt_manifest([inv, log_receipt(rows)])
    assert m["errors"] == [] and m["total"] == "540"
    for item in m["rows"][0]["field_evidence"]["quantity"]:
        assert item["quote"] == "540"


def test_multi_row_receipt_table_without_a_single_match_fails_closed():
    rows = [["SKU-1", "Oat drink", "480", "pcs"], ["SKU-2", "Soy drink", "540", "pcs"]]
    for inv in (invoice(*INV), invoice(*INV[:3], "SKU: SKU-9", *INV[3:])):
        m = receipt_manifest([inv, log_receipt(rows)])
        assert m["errors"] and not m["rows"]


def test_a_bad_row_anywhere_in_a_receipt_table_fails_closed():
    inv = invoice(*INV[:3], "SKU: SKU-2", *INV[3:])
    rows = [["SKU-1", "Oat drink", "4?8", "pcs"], ["SKU-2", "Soy drink", "540", "pcs"]]
    m = receipt_manifest([inv, log_receipt(rows)])
    assert m["errors"] and not m["rows"]


def test_receipt_currency_other_than_usd_is_read_and_compared():
    srcs = [invoice("ACME", "Invoice INV-7", "Purchase order PO-7", "Quantity 5", "Unit price EUR 2.00",
                    "Tax rate 0%", "Total EUR 10.00"),
            receipt("Goods receipt GR-7", "Received quantity 5", "Unit each", "Currency EUR")]
    ex = extract_packet(srcs)
    assert ex["values"]["currency"] == "EUR" and "currency" not in ex["conflicts"]
    srcs[1] = receipt("Goods receipt GR-7", "Received quantity 5", "Unit each", "Currency GBP")
    assert "currency" in extract_packet(srcs)["conflicts"]
