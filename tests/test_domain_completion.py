from uuid import uuid4

from fastapi.testclient import TestClient


def session(client, name="reviewer"):
    result = client.post(
        "/api/v1/auth/login", json={"username": name, "password": f"folio-{name}-local"}
    ).json()
    return {
        "Origin": "http://127.0.0.1:5101",
        "X-CSRF-Token": result["csrf"],
        "Idempotency-Key": str(uuid4()),
    }


def test_general_quantity_correction_is_versioned_human_provenance_and_invalidates_source_digest():
    from folio.workbench import WORKSPACE, app, create_case, migrate

    migrate()
    case = create_case()
    client = TestClient(app)
    h = session(client)
    r = client.patch(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/fields/received_quantity",
        headers={**h, "If-Match": '"case-v1"'},
        json={
            "typed_value": "99.5",
            "reason": "Correct confirmed receipt after source review",
        },
    )
    assert r.status_code == 200, r.text
    after = client.get(f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}").json()
    assert after["received_quantity"] == "99.5" and after["version"] == 2
    assert after["source_digest"] != case["source_digest"]
    version = after["field_provenance"]["received_quantity"][-1]
    assert version["kind"] == "human" and version["version"] == 1
    assert version["value"] == "99.5" and version["previous_value"] == "100"
    assert version["source"] is None


def test_no_change_requires_independent_exact_target_match_and_creates_zero_jobs():
    import psycopg
    from psycopg.types.json import Jsonb

    from folio.target import DSN, seed
    from folio.workbench import WORKSPACE, app, connect, create_case, migrate

    migrate()
    rid = str(uuid4())
    record = seed(WORKSPACE, rid)
    client = TestClient(app)
    h = session(client)
    case = create_case(target_id=rid)
    body = {
        "mode": "no_change",
        "quantity": "100",
        "reason": "Already recognized supported receipt",
        "evidence_digest": case["source_digest"],
    }
    url = f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/decisions"
    denied = client.post(url, headers={**h, "If-Match": '"case-v1"'}, json=body)
    assert denied.status_code == 422
    record.update(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    with psycopg.connect(DSN) as db:
        db.execute(
            "UPDATE payables SET body=%s WHERE workspace=%s AND id=%s",
            (Jsonb(record), WORKSPACE, rid),
        )
    result = client.post(
        url,
        headers={**h, "Idempotency-Key": str(uuid4()), "If-Match": '"case-v1"'},
        json=body,
    )
    assert result.status_code == 201, result.text
    after = client.get(f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}").json()
    assert after["state"] == "no_change" and after["proposal"] is None
    assert after["verification"]["outcome"] == "no_change"
    assert after["verification"]["write_requests"] == 0
    with connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM proposals WHERE case_id=%s", (case["id"],)
            ).fetchone()[0]
            == 0
        )


def test_mixed_file_intake_preserves_accepted_csv_and_rejects_other_file_per_item():
    import base64

    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    client = TestClient(app)
    h = session(client)
    result = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/files",
        headers=h,
        json={
            "rights_attestation": "authored-synthetic",
            "files": [
                {
                    "name": "receipt.csv",
                    "document_role": "receipt",
                    "base64": base64.b64encode(
                        b"Goods receipt,GR-208\nReceived quantity,100\nUnit,each\n"
                    ).decode(),
                },
                {"name": "unsafe.exe", "document_role": "invoice", "base64": "AAAA"},
            ],
        },
    )
    assert result.status_code == 202, result.text
    body = result.json()
    assert len(body["accepted"]) == 1 and len(body["rejected"]) == 1
    case = client.get(f"/api/v1/workspaces/{WORKSPACE}/cases/" + body["case_id"]).json()
    assert case["state"] == "awaiting_info" and case["received_quantity"] == "100"
    assert case["invoice"] == "" and case["target_id"] is None
    p = case["field_provenance"]["received_quantity"][0]["source"]
    source = case["sources"][0]
    page = source["pages"][0]
    assert page["text"][p["start"] : p["end"]] == p["quote"] == "100"
    assert source["containment"]["network"] == "none"
    assert "base64" not in source
    duplicate = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/files",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={
            "rights_attestation": "authored-synthetic",
            "files": [
                {
                    "name": "receipt.csv",
                    "document_role": "receipt",
                    "base64": base64.b64encode(
                        b"Goods receipt,GR-208\nReceived quantity,100\nUnit,each\n"
                    ).decode(),
                }
            ],
        },
    )
    assert (
        duplicate.json()["case_id"] == case["id"]
        and duplicate.json()["duplicate"] is True
    )


def test_uploaded_three_document_packet_matches_only_independently_read_business_identity():
    import base64
    from pathlib import Path

    from folio.target import seed
    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    client = TestClient(app)
    h = session(client)
    files = [
        {
            "name": name,
            "document_role": role,
            "base64": base64.b64encode(
                (
                    Path(__file__).resolve().parents[1] / "fixtures/public" / name
                ).read_bytes()
            ).decode(),
        }
        for name, role in [
            ("invoice.pdf", "invoice"),
            ("purchase-order.pdf", "purchase_order"),
            ("receipt.pdf", "receipt"),
        ]
    ]
    result = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/files",
        headers=h,
        json={"rights_attestation": "authored-synthetic", "files": files},
    )
    assert result.status_code == 202, result.text
    cid = result.json()["case_id"]
    url = f"/api/v1/workspaces/{WORKSPACE}/cases/{cid}"
    case = client.get(url).json()
    assert not case["missing_fields"], case
    match = client.post(
        url + "/target-match",
        headers={**h, "Idempotency-Key": str(uuid4()), "If-Match": '"case-v1"'},
        json={"selected_target": rid},
    )
    assert match.status_code == 200, match.text
    after = client.get(url).json()
    assert after["target_id"] == rid and after["target_revision"] == 7
    assert after["target_match"]["kind"] == "independent_lookup"
    assert after["state"] == "needs_review"
    assert all(versions[0]["source"] for versions in after["field_provenance"].values())
