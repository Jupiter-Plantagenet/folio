from uuid import uuid4

from fastapi.testclient import TestClient


def test_separate_approval_durable_job_and_source_invalidation():
    from folio.workbench import app, connect, create_case, migrate

    migrate()
    c = TestClient(app)
    login = c.post(
        "/api/v1/auth/login",
        json={"username": "reviewer", "password": "folio-reviewer-local"},
    )
    assert login.status_code == 200
    csrf = login.json()["csrf"]
    wid = login.json()["workspace"]
    from folio.target import seed

    rid = str(uuid4())
    seed(wid, rid)
    case = create_case(target_id=rid)
    prefix = f"/api/v1/workspaces/{wid}"
    headers = {
        "X-CSRF-Token": csrf,
        "Origin": "http://127.0.0.1:5101",
        "Idempotency-Key": str(uuid4()),
        "If-Match": f'"case-v{case["version"]}"',
    }
    r = c.post(
        prefix + f"/cases/{case['id']}/decisions",
        headers=headers,
        json=dict(
            mode="partial",
            quantity="100",
            reason="Only confirmed receipt",
            evidence_digest=case["source_digest"],
        ),
    )
    assert r.status_code == 201, r.text
    proposal = r.json()["proposal"]
    assert proposal["patch"]["gross_amount"] == "5184.00"
    a = c.post(
        prefix + f"/proposals/{proposal['id']}/approvals",
        headers={
            **headers,
            "If-Match": '"proposal-v1"',
            "Idempotency-Key": str(uuid4()),
        },
        json=dict(intent_digest=proposal["digest"], confirmation="approve"),
    )
    assert a.status_code == 403
    # A second actual app identity, not a role-switch claim.
    login = c.post(
        "/api/v1/auth/login",
        json={"username": "approver", "password": "folio-approver-local"},
    ).json()
    a = c.post(
        prefix + f"/proposals/{proposal['id']}/approvals",
        headers={
            **headers,
            "X-CSRF-Token": login["csrf"],
            "If-Match": '"proposal-v1"',
            "Idempotency-Key": str(uuid4()),
        },
        json=dict(intent_digest=proposal["digest"], confirmation="approve"),
    )
    assert a.status_code == 201, a.text
    with connect() as db:
        assert (
            db.execute(
                "select count(*) from jobs where proposal=%s", (proposal["id"],)
            ).fetchone()[0]
            == 1
        )
    login = c.post(
        "/api/v1/auth/login",
        json={"username": "reviewer", "password": "folio-reviewer-local"},
    ).json()
    case = c.get(prefix + f"/cases/{case['id']}").json()
    edit = c.patch(
        prefix + f"/cases/{case['id']}/fields/invoice",
        headers={
            **headers,
            "X-CSRF-Token": login["csrf"],
            "If-Match": f'"case-v{case["version"]}"',
            "Idempotency-Key": str(uuid4()),
        },
        json=dict(typed_value="INV-CORRECTED", reason="Human correction"),
    )
    assert edit.status_code == 200, edit.text
    with connect() as db:
        p = db.execute(
            "select body from proposals where id=%s", (proposal["id"],)
        ).fetchone()[0]
        assert p["state"] == "superseded"
    # Reusing command key with different intent must not create another effect.
    mismatch = c.post(
        prefix + f"/cases/{case['id']}/decisions",
        headers={**headers, "X-CSRF-Token": login["csrf"]},
        json=dict(
            mode="partial",
            quantity="99",
            reason="Different request",
            evidence_digest=case["source_digest"],
        ),
    )
    assert mismatch.status_code == 409
