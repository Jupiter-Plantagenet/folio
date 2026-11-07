from uuid import uuid4

from fastapi.testclient import TestClient


def test_authored_sample_has_real_provenance_and_duplicate_links_case():
    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    c = TestClient(app)
    s = c.post(
        "/api/v1/auth/login",
        json={"username": "reviewer", "password": "folio-reviewer-local"},
    ).json()
    h = {
        "Origin": "http://127.0.0.1:5101",
        "X-CSRF-Token": s["csrf"],
        "Idempotency-Key": str(uuid4()),
    }
    url = f"/api/v1/workspaces/{WORKSPACE}/intakes/sample"
    a = c.post(url, headers=h)
    assert a.status_code == 202, a.text
    item = c.get(f"/api/v1/workspaces/{WORKSPACE}/cases/" + a.json()["case_id"]).json()
    assert len(item["sources"]) == 3
    assert item["invoice_quantity"] == "120" and item["received_quantity"] == "100"
    for source in item["sources"]:
        for page in source["pages"]:
            for word in page["words"]:
                assert page["text"][word["start"] : word["end"]] == word["quote"]
    replay = c.post(url, headers={**h, "Idempotency-Key": str(uuid4())})
    assert replay.json()["case_id"] == item["id"] and replay.json()["duplicate"]


def test_cross_route_same_original_packet_links_existing_case_without_fabricating_new_work():
    import base64
    from pathlib import Path

    from test_domain_completion import session

    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    client = TestClient(app)
    h = session(client)
    sample = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/sample", headers=h
    ).json()
    root = Path(__file__).resolve().parents[1] / "fixtures/public"
    files = [
        {
            "name": name,
            "document_role": role,
            "base64": base64.b64encode((root / name).read_bytes()).decode(),
        }
        for name, role in [
            ("invoice.pdf", "invoice"),
            ("purchase-order.pdf", "purchase_order"),
            ("receipt.pdf", "receipt"),
        ]
    ]
    result = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/files",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={"rights_attestation": "authored-synthetic", "files": files},
    )
    assert result.status_code == 202, result.text
    assert (
        result.json()["duplicate"] is True
        and result.json()["case_id"] == sample["case_id"]
    )
