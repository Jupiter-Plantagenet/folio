from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "violation", ["csrf", "origin", "workspace", "viewer", "admin", "stale", "quantity"]
)
def test_denied_command_has_no_job_or_target_effect(violation):
    from folio.workbench import WORKSPACE, app, connect, create_case, migrate

    migrate()
    case = create_case()
    client = TestClient(app)
    name = violation if violation in ("viewer", "admin") else "reviewer"
    s = client.post(
        "/api/v1/auth/login", json={"username": name, "password": f"folio-{name}-local"}
    ).json()
    headers = {
        "Origin": "http://127.0.0.1:5101",
        "X-CSRF-Token": s["csrf"],
        "Idempotency-Key": str(uuid4()),
        "If-Match": '"case-v1"',
    }
    if violation == "csrf":
        headers["X-CSRF-Token"] = "forged"
    if violation == "origin":
        headers["Origin"] = "https://untrusted.invalid"
    if violation == "stale":
        headers["If-Match"] = '"case-v0"'
    workspace = str(uuid4()) if violation == "workspace" else WORKSPACE
    body = dict(
        mode="partial",
        quantity="101" if violation == "quantity" else "100",
        reason="Denied infrastructure test",
        evidence_digest=case["source_digest"],
    )
    r = client.post(
        f"/api/v1/workspaces/{workspace}/cases/{case['id']}/decisions",
        headers=headers,
        json=body,
    )
    assert r.status_code in (403, 404, 412, 422), r.text
    with connect() as db:
        assert (
            db.execute(
                "select count(*) from proposals where case_id=%s", (case["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute("select body from cases where id=%s", (case["id"],)).fetchone()[
                0
            ]["version"]
            == 1
        )
