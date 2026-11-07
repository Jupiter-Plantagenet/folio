"""Exact field evidence and immutable-history compatibility through consumers."""

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_receipt_authority_round2 import allocation_case, labelled
from test_receipt_tables import EmptyReservations, table

from folio.allocations import allocate
from folio.persisted_receipts import manifest
from folio.persisted_receipts import proposal as validate_proposal
from folio.provenance import extract_packet, validate_citation

FIELDS = {
    "receipt_id": "GR-A",
    "line_id": "1",
    "quantity": "20",
    "unit": "each",
    "currency": "USD",
}


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_all_five_complete_field_citations_survive_alias_and_allocation(ext):
    a, raw_a = labelled(ext)
    b, raw_b = table(ext, [["GR-A", "1", "20", "each", "USD"]])
    before = deepcopy([a, b])
    packet = extract_packet([a, b])
    support = packet["receipt_manifest"]
    assert support["errors"] == [] and support["total"] == "20"
    row = support["rows"][0]
    assert set(row["field_evidence"]) == set(FIELDS)
    for field, value in FIELDS.items():
        evidence = row["field_evidence"][field]
        assert len(evidence) == 2
        for item, source, raw in zip(evidence, [a, b], [raw_a, raw_b]):
            checked = validate_citation([source], item)
            assert checked == item
            assert item["quote"] == value
            assert item["source_version"] == source["version"]
            assert (
                item["text_artifact_sha256"]
                == hashlib.sha256(source["pages"][0]["text"].encode()).hexdigest()
            )
            cell = item["cell"]
            if ext == "csv":
                assert (
                    raw[cell["byte_start"] : cell["byte_end"]].decode() == cell["quote"]
                )
                assert cell["quote"].strip('"') == item["quote"]
            else:
                assert cell["sheet"] == "Sheet" and cell["coordinate"]
    assert row["evidence"] == row["field_evidence"]["quantity"]
    assert packet["field_provenance"]["unit"][0]["source"]["quote"] == "each"
    selected = allocate(EmptyReservations(), allocation_case(support), "10")
    assert selected["rows"][0]["field_evidence"] == row["field_evidence"]
    manifest(selected, supplier="Supplier A")
    assert [a, b] == before


@pytest.mark.parametrize(
    "field,bad", [("source_version", 4), ("text_artifact_sha256", "f" * 64)]
)
def test_current_field_citation_rejects_wrong_version_or_text_identity(field, bad):
    a, _ = labelled("csv")
    page = a["pages"][0]
    word = next(w for w in page["words"] if w["quote"] == "20")
    item = {
        "source_id": a["id"],
        "source_sha256": a["sha256"],
        "source_version": a.get("version", 1),
        "text_artifact_sha256": hashlib.sha256(page["text"].encode("utf-8")).hexdigest(),
        "page": 1,
        "start": word["start"],
        "end": word["end"],
        "quote": "20",
    }
    item[field] = bad
    with pytest.raises(ValueError, match="CITATION_INVALID"):
        validate_citation([a], item)


@pytest.mark.parametrize("field", ["source_version", "text_artifact_sha256"])
def test_current_field_citation_requires_version_and_text_identity(field):
    a, _ = labelled("csv")
    page = a["pages"][0]
    word = next(w for w in page["words"] if w["quote"] == "20")
    item = {
        "source_id": a["id"],
        "source_sha256": a["sha256"],
        "source_version": a.get("version", 1),
        "text_artifact_sha256": hashlib.sha256(page["text"].encode("utf-8")).hexdigest(),
        "page": 1,
        "start": word["start"],
        "end": word["end"],
        "quote": "20",
    }
    item.pop(field)
    with pytest.raises(ValueError, match="CITATION_INVALID"):
        validate_citation([a], item)


@pytest.mark.parametrize("field", list(FIELDS))
def test_persisted_and_allocator_reject_inconsistent_new_field_evidence(field):
    a, _ = labelled("csv")
    support = extract_packet([a])["receipt_manifest"]
    support["rows"][0]["field_evidence"][field][0]["quote"] = "bogus"
    with pytest.raises(ValueError):
        manifest(support)
    with pytest.raises(ValueError):
        allocate(EmptyReservations(), allocation_case(support), "10")


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_unpriced_quantity_receipt_keeps_currency_absent_and_line_defaulted(ext):
    from test_receipt_authority_round2 import file_source

    source, _ = file_source(
        ext, [["Receipt", "GR-A"], ["Received quantity", "20"], ["Unit", "each"]]
    )
    support = extract_packet([source])["receipt_manifest"]
    manifest(support)
    row = support["rows"][0]
    assert row["line_id"] == "1"
    assert row["currency"] is None and row["currency_status"] == "quantity_only"
    assert "currency" not in row["field_evidence"]
    forged = deepcopy(support)
    forged["rows"][0]["currency"] = "USD"
    forged["rows"][0]["currency_status"] = "derived_from_case"
    with pytest.raises(ValueError):
        manifest(forged)
    item = row["field_evidence"]["line_id"][0]
    assert item["kind"] == "policy-default" and "start" not in item


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_quantity_only_allocation_derives_money_currency_from_case_not_receipt(ext):
    from test_receipt_authority_round2 import file_source

    source, _ = file_source(
        ext,
        [["Receipt", "GR-A"], ["Received quantity", "20"], ["Unit", "each"]],
    )
    support = extract_packet([source])["receipt_manifest"]
    case = allocation_case(support)
    selected = allocate(EmptyReservations(), case, "10")
    row = selected["rows"][0]
    assert row["currency"] == case["currency"] == "USD"
    assert row["currency_status"] == "derived_from_case"
    assert row["currency_basis"]["case_source_digest"] == case["source_digest"]
    assert row["currency_basis"]["case_version"] == case["version"] + 1
    assert "currency" not in row["field_evidence"]
    manifest(selected, supplier=case["supplier"])
    broken = deepcopy(selected)
    broken["rows"][0]["currency_basis"]["currency"] = "EUR"
    with pytest.raises(ValueError):
        manifest(broken, supplier=case["supplier"])


@pytest.mark.parametrize("field", ["version", "sha256"])
def test_derived_currency_basis_rejects_changed_invoice_or_po_identity(field):
    from test_receipt_authority_round2 import file_source

    source, _ = file_source(
        "csv",
        [["Receipt", "GR-A"], ["Received quantity", "20"], ["Unit", "each"]],
    )
    support = extract_packet([source])["receipt_manifest"]
    case = allocation_case(support)
    allocation = allocate(EmptyReservations(), case, "10")
    proposal = {
        "id": str(uuid4()),
        "case_id": case["id"],
        "workspace": case["workspace"],
        "target": case["target_id"],
        "state": "pending",
        "version": 1,
        "case_version": case["version"] + 1,
        "source_digest": case["source_digest"],
        "expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "patch": {"recognized_quantity": "10"},
        "allocation_manifest": allocation,
    }
    current_case = deepcopy(case)
    current_case["version"] += 1
    validate_proposal(proposal, current_case, case["workspace"])
    forged = deepcopy(proposal)
    forged["allocation_manifest"]["rows"][0]["currency_basis"]["case_sources"][0][field] = (
        "d" * 64 if field == "sha256" else 2
    )
    with pytest.raises(ValueError):
        validate_proposal(forged, current_case, case["workspace"])


def test_digital_pdf_field_links_keep_existing_quantity_evidence():
    from test_receipt_field_boundaries import pdf_source

    source = pdf_source(["Goods receipt GR_A", "Line L_1"])
    packet = extract_packet([source])
    support = packet["receipt_manifest"]
    manifest(support)
    row = support["rows"][0]
    for items in row["field_evidence"].values():
        for item in items:
            assert validate_citation([source], item) == item
    assert packet["field_provenance"]["unit"][0]["source"]["quote"] == "each"


def test_historical_manifest_read_only_match_does_not_require_new_optional_evidence():
    from folio.receipts import support_matches_sources

    a, _ = labelled("csv")
    support = extract_packet([a])["receipt_manifest"]
    old = deepcopy(support)
    for row in old["rows"]:
        row.pop("field_evidence")
        for item in row["evidence"]:
            item.pop("text_artifact_sha256")
            item.pop("cell")
    before = deepcopy(old)
    assert support_matches_sources(old, [a])
    assert old == before
    allocation = allocate(EmptyReservations(), allocation_case(old), "10")
    assert "field_evidence" not in allocation["rows"][0]
    tampered = deepcopy(old)
    tampered["rows"][0]["quantity"] = "21"
    assert not support_matches_sources(tampered, [a])
    changed = deepcopy(support)
    changed["rows"][0]["field_evidence"]["unit"][0]["quote"] = "box"
    assert not support_matches_sources(changed, [a])
