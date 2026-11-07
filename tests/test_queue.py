from fastapi.testclient import TestClient


def test_browser_queue_excludes_infrastructure_test_objects():
    from folio.workbench import WORKSPACE, app, create_case, migrate

    migrate()
    infra = create_case()
    c = TestClient(app)
    c.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "folio-viewer-local"},
    )
    r = c.get(f"/api/v1/workspaces/{WORKSPACE}/cases")
    assert r.status_code == 200
    assert infra["id"] not in [item["id"] for item in r.json()["items"]]
    assert all(
        "sample_digest" in item or "intake_digest" in item for item in r.json()["items"]
    )
