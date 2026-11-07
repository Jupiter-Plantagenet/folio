from uuid import uuid4

from fastapi.testclient import TestClient


def test_proposal_displays_and_binds_independently_fetched_before_snapshot():
    from folio.target import seed
    from folio.workbench import WORKSPACE, app, create_case, migrate

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    case = create_case(target_id=rid)
    c = TestClient(app)
    s = c.post(
        "/api/v1/auth/login",
        json={"username": "reviewer", "password": "folio-reviewer-local"},
    ).json()
    r = c.post(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/decisions",
        headers={
            "Origin": "http://127.0.0.1:5101",
            "X-CSRF-Token": s["csrf"],
            "Idempotency-Key": str(uuid4()),
            "If-Match": '"case-v1"',
        },
        json=dict(
            mode="partial",
            quantity="100",
            reason="Inspect exact before values",
            evidence_digest=case["source_digest"],
        ),
    )
    assert r.status_code == 201, r.text
    p = r.json()["proposal"]
    assert p.get("before_snapshot", {}).get("gross_amount") == "0.00"
    assert p["before_snapshot"]["revision"] == 7
    assert p["before_snapshot"]["invoice_face_amount"] == "6220.80"
