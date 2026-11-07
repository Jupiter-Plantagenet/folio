"""Actual intake/decision/approval handlers; injected storage/auth, not PG proof."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_receipt_authority_round2 import allocation_case, file_source, labelled
from test_receipt_tables import EmptyReservations, table

from folio.allocations import allocate
from folio.provenance import extract_packet


def adapters(monkeypatch):
    from folio import sandbox
    from folio import workbench as wb
    from folio.formats import parse_document

    db = MagicMock()
    db.__enter__.return_value = db
    statements = []

    def execute(sql, args=None):
        statements.append((sql, args))
        result = MagicMock()
        result.fetchone.return_value = (False,) if "pg_db_role_setting" in sql else None
        return result

    db.execute.side_effect = execute
    monkeypatch.setattr(wb, "connect", lambda: db)
    monkeypatch.setattr(wb, "pooled", lambda *args, **kwargs: db)
    monkeypatch.setattr(
        wb, "identity", lambda req, role=None: {"username": "independent-approver"}
    )
    monkeypatch.setattr(wb, "command", lambda *args: None)
    monkeypatch.setattr(wb, "recorded", lambda db, req, user, body, result: result)
    monkeypatch.setattr(sandbox, "parse_isolated", parse_document)
    return wb, db, statements, TestClient(wb.app)


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "field,base", [("Received quantity", "20"), ("Unit", "each"), ("Currency", "USD")]
)
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize(
    "suffix,extra", [(" extra", False), ("\n999", False), ("\t999", False), ("", True)]
)
def test_actual_intake_and_decision_deny_bad_legacy_without_proposals_jobs(
    monkeypatch, ext, field, base, alias, suffix, extra
):
    wb, db, statements, client = adapters(monkeypatch)
    _, raw = labelled(ext, field, base + suffix, extra)
    files = [
        {
            "name": "bad." + ext,
            "document_role": "receipt",
            "base64": base64.b64encode(raw).decode(),
        }
    ]
    if alias:
        _, valid = table(ext, [["GR-A", "1", "20", "each", "USD"]])
        files.append(
            {
                "name": "valid." + ext,
                "document_role": "receipt",
                "base64": base64.b64encode(valid).decode(),
            }
        )
    prefix = f"/api/v1/workspaces/{wb.WORKSPACE}"
    response = client.post(
        prefix + "/intakes/files",
        json={"rights_attestation": "authored-synthetic", "files": files},
    )
    assert response.status_code == 202, response.text
    saved = [args[0].obj for sql, args in statements if sql.startswith("UPDATE cases")][
        -1
    ]
    assert saved["receipt_manifest"]["errors"] and saved["received_quantity"] == ""
    monkeypatch.setattr(wb, "load_case", lambda *args: deepcopy(saved))
    for mode in ("partial", "no_change"):
        denied = client.post(
            prefix + f"/cases/{saved['id']}/decisions",
            headers={"If-Match": f'"case-v{saved["version"]}"'},
            json={
                "mode": mode,
                "quantity": "1",
                "reason": "whole fields required",
                "evidence_digest": saved["source_digest"],
            },
        )
        assert denied.status_code == 422, denied.text
        assert "RECEIPT" in denied.text
    assert not any(
        "INSERT INTO proposals" in sql or "INSERT INTO jobs" in sql
        for sql, _ in statements
    )
    client.close()


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("bad_source", [False, True])
def test_actual_approval_keeps_frozen_receipt_and_signed_intent_bytes(
    monkeypatch, historical, bad_source
):
    import httpx

    wb, db, statements, client = adapters(monkeypatch)
    source, _ = labelled("csv")
    support = extract_packet([source])["receipt_manifest"]
    if historical:
        row = support["rows"][0]
        row.pop("field_evidence", None)
        for item in row["evidence"]:
            item.pop("cell", None)
            item.pop("text_artifact_sha256", None)
    if bad_source:
        source, _ = labelled("csv", "Received quantity", "20\n999")
    case = allocation_case(support)
    case.update(workspace=wb.WORKSPACE, sources=[source])
    frozen = allocate(EmptyReservations(), case, "10")
    case["version"] = 2
    before_snapshot = {"id": case["target_id"], "revision": 1}
    proposal = {
        "id": str(uuid4()),
        "case_id": case["id"],
        "workspace": wb.WORKSPACE,
        "target": case["target_id"],
        "state": "pending",
        "version": 1,
        "case_version": 2,
        "prepared_by": "separate-reviewer",
        "source_digest": case["source_digest"],
        "operation_key": str(uuid4()),
        "expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "patch": {"recognized_quantity": "10"},
        "allocation_manifest": frozen,
        "before_snapshot_digest": wb.digest(before_snapshot),
    }
    proposal["digest"] = wb.digest(proposal)
    original = deepcopy(proposal)
    original_support = deepcopy(support)
    monkeypatch.setattr(wb, "load_case", lambda *args: case)
    monkeypatch.setattr(wb, "load_proposal", lambda *args: proposal)
    monkeypatch.setattr(wb, "service_header", lambda *args: {})
    monkeypatch.setattr(wb, "sign_approval", lambda *args: "injected-test-attestation")
    response = MagicMock()
    response.json.return_value = before_snapshot
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    def execute(sql, args=None):
        statements.append((sql, args))
        result = MagicMock()
        if "pg_db_role_setting" in sql:
            result.fetchone.return_value = (False,)
        else:
            result.fetchone.return_value = (
                (case["id"],) if "SELECT case_id FROM proposals" in sql else None
            )
        result.__iter__.return_value = iter([])
        return result

    db.execute.side_effect = execute
    result = client.post(
        f"/api/v1/workspaces/{wb.WORKSPACE}/proposals/{proposal['id']}/approvals",
        headers={"If-Match": '"proposal-v1"'},
        json={"intent_digest": proposal["digest"], "confirmation": "approve"},
    )
    assert result.status_code == (412 if bad_source else 201), result.text
    assert proposal["allocation_manifest"] == original["allocation_manifest"]
    assert proposal["digest"] == original["digest"]
    assert case["receipt_manifest"] == original_support
    assert len([1 for sql, _ in statements if "INSERT INTO jobs" in sql]) == (
        0 if bad_source else 1
    )
    client.close()


@pytest.mark.parametrize("kind", ["csv", None, "digital-pdf"])
def test_labelled_unrelated_cell_projection_cannot_supply_missing_quantity(kind):
    source, _ = file_source(
        "csv",
        [
            ["Receipt", "GR-A"],
            ["Line", "1"],
            ["Purchase order", "Received quantity 20"],
            ["Unit", "each"],
            ["Currency", "USD"],
        ],
    )
    source["kind"] = kind
    packet = extract_packet([source])
    assert "received_quantity" not in packet["values"]
