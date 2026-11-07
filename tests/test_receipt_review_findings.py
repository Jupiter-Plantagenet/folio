"""RV regressions: literal source identities and direct HTTP over isolated PostgreSQL."""

import base64
from uuid import uuid4

import pytest
from test_receipt_allocations import packet_files, receipt, receipt_file
from test_receipt_lifecycle import hdr, setup

from folio.receipts import receipt_manifest


@pytest.mark.parametrize(
    "receipt_id,line",
    [
        ("GR/A", "1"),
        ("GR:A", "1"),
        ("GR-A.", "1"),
        ("GR-A", "1/2"),
        ("GR-A", "1:2"),
        ("GR-A", "1.2"),
        ("GR-A", "1,"),
    ],
)
def test_rv02_unsupported_complete_tokens_reject_without_prefix_rows(receipt_id, line):
    source = receipt("literal.xlsx", receipt_id, "1", line=line)
    text = source["pages"][0]["text"]
    result = receipt_manifest([source])
    assert result["errors"] == ["RECEIPT_IDENTITY_INVALID"], result
    assert result["rows"] == []
    assert source["pages"][0]["text"] == text
    assert receipt_id in text and line in text


def test_rv02_underscore_ids_preserve_distinct_identity_and_exact_alias_dedup():
    sources = [
        receipt("a.csv", "GR_A", "1"),
        receipt("b.xlsx", "GR_B", "2"),
        receipt("alias.xlsx", "GR_A", "1"),
    ]
    result = receipt_manifest(sources)
    assert not result["errors"]
    assert [r["key"] for r in result["rows"]] == ["GR_A:1", "GR_B:1"]
    assert result["total"] == "3"
    assert [len(r["evidence"]) for r in result["rows"]] == [2, 1]
    conflict = receipt_manifest([sources[0], receipt("changed.xlsx", "GR_A", "2")])
    assert conflict["errors"] == ["RECEIPT_IDENTITY_CONFLICT"]
    assert conflict["rows"][0]["receipt_id"] == "GR_A"


def test_rv02_line_underscore_preserved_without_aliasing():
    result = receipt_manifest(
        [
            receipt("a.csv", "GR_A", "1", line="L_A"),
            receipt("b.csv", "GR_A", "1", line="L_B"),
        ]
    )
    assert [r["key"] for r in result["rows"]] == ["GR_A:L_A", "GR_A:L_B"]
    assert result["errors"] == ["RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED"]


def test_rv03_complete_case_request_rejected_by_server_without_mutation():
    client, auth, prefix, url = setup(
        packet_files() + [receipt_file("GR-" + uuid4().hex, "100")]
    )
    c = client.get(url).json()
    response = client.post(
        url + "/information-requests",
        headers=hdr(auth, c),
        json={"fields": ["received_quantity"], "reason": "Unnecessary request"},
    )
    assert response.status_code == 422, response.text
    assert "INFORMATION_REQUEST_FIELDS_NOT_UNRESOLVED" in response.text
    assert client.get(url).json() == c


@pytest.mark.parametrize("mode", ["partial", "no_change"])
def test_rv03_open_request_blocks_preparation_even_without_missing_flags(mode):
    # Simulate an already persisted request created before this fix, not a new API loophole.
    from psycopg.types.json import Jsonb

    from folio.workbench import connect

    client, auth, prefix, url = setup(
        packet_files() + [receipt_file("GR-" + uuid4().hex, "100")]
    )
    c = client.get(url).json()
    c["information_requests"] = [
        {"id": str(uuid4()), "fields": ["received_quantity"], "state": "open"}
    ]
    with connect() as db:
        db.execute("UPDATE cases SET body=%s WHERE id=%s", (Jsonb(c), c["id"]))
    response = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": mode,
            "quantity": "100",
            "reason": "Cannot bypass open request",
            "evidence_digest": c["source_digest"],
        },
    )
    assert (
        response.status_code == 422 and "INFORMATION_REQUEST_OPEN" in response.text
    ), response.text
    assert client.get(url).json()["proposal"] is None
    for safe in ["hold", "escalate"]:
        c = client.get(url).json()
        response = client.post(
            url + "/decisions",
            headers=hdr(auth, c),
            json={
                "mode": safe,
                "reason": "Wait for resolution",
                "evidence_digest": c["source_digest"],
            },
        )
        assert response.status_code == 201 and response.json()["proposal"] is None


def test_rv03_conflict_request_resolves_only_after_versioned_correction():
    files = packet_files() + [
        receipt_file("GR-" + uuid4().hex, "100"),
        csv_file("Invoice,INV-CONFLICT\n", "invoice"),
    ]
    client, auth, prefix, url = setup(files)
    c = client.get(url).json()
    assert "invoice" in c["conflicting_fields"]
    response = client.post(
        url + "/information-requests",
        headers=hdr(auth, c),
        json={"fields": ["invoice"], "reason": "Resolve conflicting invoice"},
    )
    assert response.status_code == 201
    c = client.get(url).json()
    response = client.patch(
        url + "/fields/invoice",
        headers=hdr(auth, c),
        json={"typed_value": "INV-1048", "reason": "Confirm PDF identity"},
    )
    assert response.status_code == 200
    resolved = client.get(url).json()
    assert resolved["version"] == c["version"] + 1
    assert resolved["information_requests"][0]["state"] == "resolved"
    assert resolved["field_provenance"]["invoice"][-1]["kind"] == "human"


def test_rv03_request_replay_partial_correction_and_source_resolution():
    client, auth, prefix, url = setup(packet_files())
    c = client.get(url).json()
    body = {
        "fields": ["received_quantity", "unit"],
        "reason": "Receiving evidence required",
    }
    headers = hdr(auth, c)
    response = client.post(url + "/information-requests", headers=headers, json=body)
    assert response.status_code == 201, response.text
    replay = client.post(url + "/information-requests", headers=headers, json=body)
    assert replay.status_code == 201 and replay.json() == response.json()
    c = client.get(url).json()
    assert len(c["information_requests"]) == 1
    correction_body = {
        "typed_value": "each",
        "reason": "Confirm unit, quantity still missing",
    }
    headers = hdr(auth, c)
    correction = client.patch(
        url + "/fields/unit", headers=headers, json=correction_body
    )
    assert correction.status_code == 200
    assert (
        client.patch(url + "/fields/unit", headers=headers, json=correction_body).json()
        == correction.json()
    )
    c = client.get(url).json()
    assert c["information_requests"][0]["state"] == "open"
    for mode in ["partial", "no_change"]:
        blocked = client.post(
            url + "/decisions",
            headers=hdr(auth, c),
            json={
                "mode": mode,
                "quantity": "100",
                "reason": "Still unresolved",
                "evidence_digest": c["source_digest"],
            },
        )
        assert blocked.status_code == 422
    body = {
        "rights_attestation": "authored-synthetic",
        "case_id": c["id"],
        "files": [receipt_file("GR_" + uuid4().hex, "100", xlsx=True)],
    }
    headers = hdr(auth, c)
    attached = client.post(prefix + "/intakes/files", headers=headers, json=body)
    assert attached.status_code == 202, attached.text
    assert (
        client.post(prefix + "/intakes/files", headers=headers, json=body).json()
        == attached.json()
    )
    resolved = client.get(url).json()
    assert (
        resolved["version"] > c["version"]
        and resolved["information_requests"][0]["state"] == "resolved"
    )
    assert not resolved["missing_fields"] and not resolved["conflicting_fields"]
    duplicate = client.post(
        prefix + "/intakes/files", headers=hdr(auth, resolved), json=body
    )
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
    assert client.get(url).json()["version"] == resolved["version"]


def test_rvs01_allocation_names_input_version_without_rewriting_prior_proposal():
    from folio.target import seed
    from folio.workbench import WORKSPACE

    client, auth, prefix, url = setup(
        packet_files() + [receipt_file("GR-" + uuid4().hex, "100")]
    )
    c = client.get(url).json()
    target = str(uuid4())
    seed(WORKSPACE, target)
    assert (
        client.post(
            url + "/target-match",
            headers=hdr(auth, c),
            json={"selected_target": target},
        ).status_code
        == 200
    )
    c = client.get(url).json()
    response = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Version-bound allocation",
            "evidence_digest": c["source_digest"],
        },
    )
    assert response.status_code == 201, response.text
    p = response.json()["proposal"]
    assert p["allocation_manifest"]["input_case_version"] == c["version"]
    assert "case_version" not in p["allocation_manifest"]
    assert p["case_version"] == c["version"] + 1 == client.get(url).json()["version"]


def test_rv01_cached_manifest_cannot_bypass_current_source_line_policy():
    from psycopg.types.json import Jsonb

    from folio.workbench import connect

    client, auth, prefix, url = setup(
        packet_files() + [receipt_file("GR-" + uuid4().hex, "100")]
    )
    c = client.get(url).json()
    # Pre-fix persisted cache: source now has an explicit, conflicting PO line,
    # cache still lacks the new validation error. No production record is changed.
    c["sources"].append(
        {
            "id": str(uuid4()),
            "sha256": "f" * 64,
            "version": 1,
            "document_role": "purchase_order",
            "pages": [{"number": 1, "text": "Line 2", "words": []}],
        }
    )
    with connect() as db:
        db.execute("UPDATE cases SET body=%s WHERE id=%s", (Jsonb(c), c["id"]))
    response = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Must validate source, not stale cache",
            "evidence_digest": c["source_digest"],
        },
    )
    assert (
        response.status_code == 422 and "RECEIPT_SUPPORT_UNRESOLVED" in response.text
    ), response.text


def csv_file(text, role="receipt"):
    return {
        "name": uuid4().hex + ".csv",
        "document_role": role,
        "base64": base64.b64encode(text.encode()).decode(),
    }


def test_rv01_distinct_lines_rounding_counterexample_rejected_before_proposal():
    from folio.workbench import connect

    files = [
        csv_file(
            "Invoice,INV-RV01\nQuantity,2\nUnit price USD,0.05\nTax,8%\nTotal USD,0.11\n",
            "invoice",
        ),
        csv_file("Supplier,Aster Supply Co.\nOrdered quantity,2\n", "purchase_order"),
    ]
    for line in ["1", "2"]:
        files.append(
            csv_file(
                f"Receipt,GR-{uuid4().hex}\nLine,{line}\nReceived quantity,1\nUnit,each\nCurrency,USD\n"
            )
        )
    client, auth, prefix, url = setup(files)
    c = client.get(url).json()
    response = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "2",
            "reason": "Reject cross-line aggregate rounding",
            "evidence_digest": c["source_digest"],
        },
    )
    assert response.status_code == 422, response.text
    corrected = client.patch(
        url + "/fields/received_quantity",
        headers=hdr(auth, c),
        json={
            "typed_value": "2",
            "reason": "Aggregate assertion cannot authorize different PO lines",
        },
    )
    assert corrected.status_code == 200
    c = client.get(url).json()
    denied = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "2",
            "reason": "No cross-line accounting",
            "evidence_digest": c["source_digest"],
        },
    )
    assert denied.status_code == 422 and "RECEIPT_SUPPORT_UNRESOLVED" in denied.text, (
        denied.text
    )
    # Rejection must be about unsupported line authority, not absent target.
    assert "RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED" in c["receipt_manifest"]["errors"]
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


@pytest.mark.parametrize("role", ["invoice", "purchase_order"])
def test_rv01_declared_document_line_must_bind_receipt(role):
    # RV-01 refinement 2026-11-04: invoice line numbers are vendor-local; only
    # purchase-order lines (or an invoice's explicit "PO line") bind receipts.
    r = receipt("r.csv", "GR-A", "1", line="1")
    declaration = receipt("d.csv", "unused", "1", line="2")
    declaration["document_role"] = role
    errors = receipt_manifest([r, declaration])["errors"]
    if role == "purchase_order":
        assert "RECEIPT_PO_LINE_BINDING_MISMATCH" in errors
    else:
        assert "RECEIPT_PO_LINE_BINDING_MISMATCH" not in errors


@pytest.mark.parametrize("label", ["PO line", "PO ln"])
def test_rv01_invoice_po_line_declaration_binds_receipt(label):
    from test_receipt_allocations import parsed

    r = receipt("r.csv", "GR-A", "1", line="1")
    invoice = parsed("i.csv", f"Invoice,INV-1\n{label},2\n".encode(), "invoice")
    assert "RECEIPT_PO_LINE_BINDING_MISMATCH" in receipt_manifest([r, invoice])["errors"]
    same = parsed("j.csv", f"Invoice,INV-1\n{label},1\n".encode(), "invoice")
    assert "RECEIPT_PO_LINE_BINDING_MISMATCH" not in receipt_manifest([r, same])["errors"]
