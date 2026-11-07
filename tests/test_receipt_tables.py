"""Authored table bytes through production parsing, evidence and allocation."""

import csv
import io
from copy import deepcopy
from uuid import uuid4

import pytest

from folio.allocations import allocate
from folio.formats import parse_document
from folio.persisted_receipts import manifest as validate_manifest
from folio.provenance import extract_packet, validate_citation

HEADER = ["Receipt", "Line", "Received quantity", "Unit", "Currency"]


def table(ext, rows, header=None):
    records = [HEADER if header is None else header, *rows]
    if ext == "csv":
        out = io.StringIO(newline="")
        csv.writer(out).writerows(records)
        raw = out.getvalue().encode("utf-8")
    else:
        from openpyxl import Workbook

        book = Workbook()
        for row in records:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        book.close()
        raw = out.getvalue()
    return {
        **parse_document(raw, "receipts." + ext),
        "id": str(uuid4()),
        "version": 1,
        "document_role": "receipt",
    }, raw


class EmptyReservations:
    """Only the existing allocation SQL boundary is substituted; no retained DB."""

    def execute(self, sql, args):
        assert "pg_advisory_xact_lock" in sql or "SELECT body FROM proposals" in sql
        return []


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_table_import_reconciles_exact_evidence_and_allocates_without_alias_double_count(
    ext,
):
    source, raw = table(
        ext,
        [
            ["GR-A", "1", "40.10", "each", "USD"],
            ["GR-B", "1", "59.9", "each", "USD"],
            ["GR-A", "1", "40.10", "each", "USD"],
        ],
    )
    original = deepcopy(source)
    packet = extract_packet([source])
    support = packet["receipt_manifest"]
    assert support["errors"] == []
    assert packet["values"]["received_quantity"] == "100"
    assert packet["values"]["unit"] == "each"
    assert "received_quantity" not in packet["conflicts"]
    assert len(support["rows"]) == 2
    assert len(support["rows"][0]["evidence"]) == 2
    validate_manifest(support)
    for row in support["rows"]:
        for evidence in row["evidence"]:
            validate_citation([source], evidence)
            cell = evidence["cell"]
            if ext == "csv":
                assert (
                    raw[cell["byte_start"] : cell["byte_end"]].decode()
                    == evidence["quote"]
                )
            else:
                assert cell["sheet"] == "Sheet" and cell["coordinate"].startswith("C")
    case = {
        "id": str(uuid4()),
        "workspace": str(uuid4()),
        "supplier": "Supplier A",
        "currency": "USD",
        "unit": "each",
        "target_id": str(uuid4()),
        "source_digest": "a" * 64,
        "version": 1,
        "receipt_manifest": support,
    }
    selection = allocate(EmptyReservations(), case, "60")
    assert [r["allocated"] for r in selection["rows"]] == ["40.1", "19.9"]
    validate_manifest(selection, supplier="Supplier A")
    assert source == original


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "row",
    [
        ["GR-A\nOTHER", "1", "20", "each", "USD"],
        ["GR-A", "1\tOTHER", "20", "each", "USD"],
        ["GR-A", "1", "20\n999", "each", "USD"],
        ["GR-A", "1", "20", "each", "EUR"],
        ["GR-A", "1", "20", "box", "USD"],
        ["GR-A", "1", "=10+10", "each", "USD"],
        ["GR-A", "1", "-1", "each", "USD"],
        ["GR-A", "1", "0", "each", "USD"],
        ["GR-A", "1", "1e2", "each", "USD"],
        ["GR-A", "1", "20", "each"],
        ["GR-A", "1", "20", "each", "USD", "extra"],
    ],
)
def test_table_invalid_complete_cell_or_structure_never_becomes_quantity_authority(
    ext, row
):
    source, _ = table(ext, [["GR-OK", "1", "10", "each", "USD"], row])
    packet = extract_packet([source])
    assert packet["receipt_manifest"]["errors"]
    assert not packet["receipt_manifest"]["rows"]
    assert "received_quantity" not in packet["values"]
    assert "received_quantity" in packet["conflicts"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_table_alias_conflict_and_distinct_po_lines_remain_blocked(ext):
    for rows, error in [
        (
            [["GR-A", "1", "10", "each", "USD"], ["GR-A", "1", "11", "each", "USD"]],
            "RECEIPT_IDENTITY_CONFLICT",
        ),
        (
            [["GR-A", "1", "10", "each", "USD"], ["GR-B", "2", "11", "each", "USD"]],
            "RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED",
        ),
    ]:
        source, _ = table(ext, rows)
        packet = extract_packet([source])
        assert error in packet["receipt_manifest"]["errors"]
        assert "received_quantity" not in packet["values"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_table_and_legacy_alias_keep_exact_identity_and_persisted_shape(ext):
    from test_receipt_allocations import receipt

    source, _ = table(ext, [["GR-A", "1", "100", "each", "USD"]])
    alias = receipt("alias.csv", "GR-A", "100")
    support = extract_packet([source, alias])["receipt_manifest"]
    assert support["errors"] == [] and support["total"] == "100"
    assert len(support["rows"]) == 1
    assert len(support["rows"][0]["evidence"]) == 2
    validate_manifest(support)


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_table_headers_not_reordered_or_guessed(ext):
    source, _ = table(ext, [["GR-A", "1", "100", "each", "USD"]], header=HEADER[::-1])
    packet = extract_packet([source])
    assert packet["receipt_manifest"]["errors"] == ["RECEIPT_TABLE_HEADER_INVALID"]
    assert not packet["receipt_manifest"]["rows"]


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_actual_intake_route_serializes_receipt_table_with_originals(monkeypatch, ext):
    """ASGI and actual SQL serialization; DB/sandbox adapters injected, not PG proof."""
    import base64
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from folio import sandbox, workbench

    _, raw = table(
        ext,
        [
            ["GR-A", "1", "40.1", "each", "USD"],
            ["GR-B", "1", "59.9", "each", "USD"],
        ],
    )
    db = MagicMock()
    db.__enter__.return_value = db
    statements = []

    def execute(sql, args=None):
        statements.append((sql, args))
        result = MagicMock()
        if "pg_db_role_setting" in sql:
            result.fetchone.return_value = (False,)
        if "FROM cases" in sql:
            result.fetchone.return_value = None
        return result

    db.execute.side_effect = execute
    monkeypatch.setattr(workbench, "connect", lambda: db)
    monkeypatch.setattr(workbench, "pooled", lambda: db)
    monkeypatch.setattr(
        workbench, "identity", lambda req, role=None: {"username": "test-reviewer"}
    )
    monkeypatch.setattr(workbench, "command", lambda *args: None)
    monkeypatch.setattr(
        workbench, "recorded", lambda db, req, user, body, result: result
    )
    monkeypatch.setattr(sandbox, "parse_isolated", parse_document)
    client = TestClient(workbench.app)
    response = client.post(
        f"/api/v1/workspaces/{workbench.WORKSPACE}/intakes/files",
        json={
            "rights_attestation": "authored-synthetic",
            "files": [
                {
                    "name": "receipts." + ext,
                    "document_role": "receipt",
                    "base64": base64.b64encode(raw).decode(),
                }
            ],
        },
    )
    assert response.status_code == 202, response.text
    saved = [args[0].obj for sql, args in statements if sql.startswith("UPDATE cases")]
    assert saved and saved[-1]["id"] == response.json()["case_id"]
    case = saved[-1]
    assert case["received_quantity"] == "100" and case["unit"] == "each"
    assert len(case["receipt_manifest"]["rows"]) == 2
    assert (
        case["field_provenance"]["received_quantity"][0]["inputs"]
        == case["receipt_manifest"]["rows"]
    )
    originals = [
        args for sql, args in statements if sql.startswith("INSERT INTO originals")
    ]
    assert len(originals) == 1 and originals[0][-1] == raw
    assert not any(
        "INSERT INTO proposals" in sql or "INSERT INTO jobs" in sql
        for sql, _ in statements
    )

    # The same real append-support route resumes an existing missing-receipt
    # review and persists the resolved request without creating dispatch work.
    from pathlib import Path

    sources = []
    for name, role in [
        ("invoice.pdf", "invoice"),
        ("purchase-order.pdf", "purchase_order"),
    ]:
        original = (
            Path(__file__).resolve().parents[1] / "fixtures/public" / name
        ).read_bytes()
        sources.append(
            {
                **parse_document(original, name),
                "id": str(uuid4()),
                "version": 1,
                "document_role": role,
            }
        )
    extracted = extract_packet(sources)
    existing = {
        "id": str(uuid4()),
        "workspace": workbench.WORKSPACE,
        "version": 3,
        "state": "awaiting_info",
        "sources": sources,
        "proposal": None,
        "information_requests": [{"fields": ["received_quantity"], "state": "open"}],
        **extracted["values"],
    }
    monkeypatch.setattr(workbench, "load_case", lambda db, workspace, cid: existing)
    statements.clear()
    response = client.post(
        f"/api/v1/workspaces/{workbench.WORKSPACE}/intakes/files",
        headers={"If-Match": '"case-v3"'},
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": existing["id"],
            "files": [
                {
                    "name": "receipts." + ext,
                    "document_role": "receipt",
                    "base64": base64.b64encode(raw).decode(),
                }
            ],
        },
    )
    assert response.status_code == 202, response.text
    saved = [args[0].obj for sql, args in statements if sql.startswith("UPDATE cases")][
        -1
    ]
    assert saved["version"] == 4 and saved["state"] == "needs_review"
    assert saved["received_quantity"] == "100" and not saved["missing_fields"]
    assert saved["information_requests"][0]["state"] == "resolved"
    assert len(saved["sources"]) == 3
    assert not any(
        "INSERT INTO proposals" in sql or "INSERT INTO jobs" in sql
        for sql, _ in statements
    )
    client.close()
