"""Explicit real-weight smoke, not the normative held-out evaluation."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from folio.identity import service_header

ROOT = Path(__file__).resolve().parents[1]
ROLES = {
    "invoice.pdf": "invoice",
    "purchase-order.pdf": "purchase_order",
    "receipt.pdf": "receipt",
}


def authored_packet_sources(*, roles=True):
    """Sources shaped exactly as `/intakes/sample` (and `/intakes/files`) build them.

    Receipt authority (receipt-allocation-v1) is read only from sources whose
    declared `document_role` is `receipt`, so a role-less source list carries no
    receipt rows and every partial/no_change fails closed. `roles=False` is the
    pre-receipt-authority fixture shape, kept only to pin that fail-closed rule.
    """
    from folio.parser import parse_pdf

    root = ROOT / "fixtures/public"
    manifest = json.loads((root / "rights-manifest.json").read_text())
    sources = []
    for entry in manifest["files"]:
        raw = (root / entry["name"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        source = {"id": str(uuid4()), "name": entry["name"], **parse_pdf(raw)}
        if roles:
            source.update(
                document_role=ROLES[entry["name"]],
                authorship=manifest["authorship"],
                version=1,
            )
        sources.append(source)
    return sources


def authored_case(target_id):
    """Create the case the way `/intakes/sample` does, bound to `target_id`."""
    from folio.provenance import extract_packet
    from folio.workbench import connect, create_case, digest, save_case

    sources = authored_packet_sources()
    extracted = extract_packet(sources)
    assert extracted["missing"] == [] and extracted["conflicts"] == []
    manifest = extracted["receipt_manifest"]
    assert manifest["errors"] == [] and manifest["total"] == "100"
    assert [(r["key"], r["quantity"]) for r in manifest["rows"]] == [
        ("GR-208:1", "100")
    ]
    with connect() as db:
        case = create_case(sources, target_id=target_id, db=db)
        case.update(extracted["values"])
        case.update(
            field_provenance=extracted["field_provenance"],
            invoice_supplier=extracted["invoice_supplier"],
            supplier_check=extracted["supplier_check"],
            missing_fields=[],
            conflicting_fields=[],
        )
        case["source_digest"] = digest(
            {"sources": sources, "field_provenance": case["field_provenance"]}
        )
        save_case(db, case)
    return case


def test_authored_packet_receipt_authority_requires_declared_document_roles():
    """Deterministic pin (no model) for the real-model fixture below.

    Same bytes, same parser: with intake document roles the receipt manifest is
    error-free with GR-208:1 = 100; without roles there is no receipt authority,
    and a partial decision fails closed with RECEIPT_SUPPORT_UNRESOLVED.
    """
    from fastapi.testclient import TestClient
    from test_domain_completion import session

    from folio.receipts import receipt_manifest
    from folio.target import seed
    from folio.workbench import WORKSPACE, app, create_case, migrate

    roled = receipt_manifest(authored_packet_sources())
    assert roled["errors"] == [] and roled["total"] == "100"
    assert [(r["key"], r["quantity"]) for r in roled["rows"]] == [("GR-208:1", "100")]
    roleless_sources = authored_packet_sources(roles=False)
    roleless = receipt_manifest(roleless_sources)
    assert roleless["errors"] == [] and roleless["rows"] == []
    assert roleless["total"] == "0"

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    client = TestClient(app)
    body = {
        "mode": "partial",
        "quantity": "100",
        "reason": "Only received quantity is recognized",
    }
    stale = create_case(sources=roleless_sources, target_id=rid)
    denied = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{stale['id']}/decisions",
        headers={**session(client), "If-Match": '"case-v1"'},
        json={**body, "evidence_digest": stale["source_digest"]},
    )
    assert denied.status_code == 422, denied.text
    assert "RECEIPT_SUPPORT_UNRESOLVED" in denied.text
    # No accepted partial here: it would reserve GR-208:1 in the workspace and
    # (correctly) block the real-model test's preparation with
    # QUANTITY_EXCEEDS_UNALLOCATED_RECEIPT. That test proves the accepted path.


@pytest.mark.skipif(
    __import__("os").environ.get("FOLIO_REAL_MODEL_TEST") != "1",
    reason="explicit bounded model runtime required",
)
def test_real_local_model_reads_sources_and_independent_target():
    from folio.agent import run_bounded
    from folio.target import seed
    from folio.workbench import TARGET_URL, WORKSPACE, migrate

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    case = authored_case(rid)

    def lookup():
        response = httpx.get(
            f"{TARGET_URL}/api/v1/workspaces/{WORKSPACE}/payables/{rid}",
            headers=service_header("verifier"),
        )
        response.raise_for_status()
        return response.json()

    result = run_bounded(case, lookup)
    path = ROOT / "evidence/documents/model/local-smoke"
    path.mkdir(parents=True, exist_ok=True)
    (path / "actual-run.json").write_text(
        json.dumps({"case": case, "run": result}, indent=2), encoding="utf-8"
    )
    assert result["status"] == "recommended", result
    assert {"read_sources", "lookup_payable", "recommend_outcome"} <= {
        x.get("tool") for x in result["trace"]
    }
    assert result["recommendation"]["mode"] == "partial"
    assert result["recommendation"]["quantity"] == "100"
    assert result["target_writes"] == 0


@pytest.mark.skipif(
    __import__("os").environ.get("FOLIO_REAL_MODEL_TEST") != "1",
    reason="explicit bounded model runtime required",
)
def test_real_agent_autonomously_prepares_version_bound_proposal_without_human_field_decision():
    from fastapi.testclient import TestClient
    from test_domain_completion import session

    from folio.target import seed
    from folio.workbench import TARGET_URL, WORKSPACE, app, migrate
    from folio.worker import analyze_one

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    # Built as the product builds it (declared document roles), so the
    # round-2 receipt-identity rules can read GR-208:1 = 100 as authority.
    case = authored_case(rid)
    client = TestClient(app)
    h = session(client)
    request = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/analysis-runs",
        headers={**h, "If-Match": '"case-v1"'},
        json={"prepare_proposal": True},
    )
    assert request.status_code == 202, request.text
    run = analyze_one(request.json()["run_id"])
    (ROOT / "evidence/documents/model/local-smoke/autonomous-api-run.json").write_text(
        json.dumps(run, indent=2), encoding="utf-8"
    )
    assert run["status"] == "prepared", run
    after = client.get(f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}").json()
    assert after["state"] == "awaiting_approval"
    assert (
        after["proposal"]["prepared_by"] == "reviewer"
        and after["proposal"]["state"] == "pending"
    )
    assert after["proposal"]["patch"]["recognized_quantity"] == "100"
    assert after["proposal"]["patch"]["gross_amount"] == "5184.00"
    assert after["jobs"] == []
    observed = httpx.get(
        f"{TARGET_URL}/api/v1/workspaces/{WORKSPACE}/payables/{rid}",
        headers=service_header("verifier"),
    ).json()
    assert observed["revision"] == 7 and observed["gross_amount"] == "0.00"
