from fastapi.testclient import TestClient


def test_northstar_console_is_independent_authenticated_and_read_only():
    from folio.target import app, migrate

    migrate()
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert c.get("/operator/records").status_code == 401
    assert (
        c.post(
            "/operator/login",
            headers={
                "Origin": __import__("os").environ.get(
                    "FOLIO_TARGET_URL", "http://127.0.0.1:8201"
                )
            },
            json={"username": "operator", "password": __import__("folio.target", fromlist=["operator_password"]).operator_password()},
        ).status_code
        == 200
    )
    r = c.get("/operator/records")
    assert r.status_code == 200 and isinstance(r.json()["records"], list)
    assert (
        c.patch(
            "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/payables/22222222-2222-4222-8222-222222222222",
            json={},
        ).status_code
        == 403
    )
