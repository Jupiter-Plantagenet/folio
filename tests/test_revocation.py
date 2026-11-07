from uuid import uuid4

from fastapi.testclient import TestClient


def approved_case():
    from folio.target import seed
    from folio.workbench import WORKSPACE, app, create_case, migrate

    rid = str(uuid4())
    seed(WORKSPACE, rid)
    migrate()
    c = TestClient(app)
    case = create_case(target_id=rid)
    prefix = f"/api/v1/workspaces/{WORKSPACE}"

    def login(name):
        s = c.post(
            "/api/v1/auth/login",
            json={"username": name, "password": f"folio-{name}-local"},
        ).json()
        return {
            "Origin": "http://127.0.0.1:5101",
            "X-CSRF-Token": s["csrf"],
            "Idempotency-Key": str(uuid4()),
        }

    h = login("reviewer")
    p = c.post(
        prefix + f"/cases/{case['id']}/decisions",
        headers={**h, "If-Match": '"case-v1"'},
        json=dict(
            mode="partial",
            quantity="100",
            reason="Synthetic infrastructure test",
            evidence_digest=case["source_digest"],
        ),
    ).json()["proposal"]
    h = login("approver")
    a = c.post(
        prefix + f"/proposals/{p['id']}/approvals",
        headers={**h, "If-Match": '"proposal-v1"'},
        json=dict(intent_digest=p["digest"], confirmation="approve"),
    ).json()
    return c, case, p, a, prefix, login


def test_preseal_revocation_blocks_worker_but_postseal_is_too_late():
    from folio.workbench import connect
    from folio.worker import claim, execute

    c, case, p, a, prefix, login = approved_case()
    h = login("approver")
    url = prefix + f"/approvals/{p['id']}/revocations"
    r = c.post(
        url,
        headers={**h, "If-Match": '"proposal-v1"'},
        json={"reason": "Revoke before seal"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] is True
    job = claim(a["job_id"])
    assert execute(job)["outcome"] == "blocked"
    with connect() as db:
        proposal = db.execute(
            "select body from proposals where id=%s", (p["id"],)
        ).fetchone()[0]
        proposal["gate"] = "seal_pending"
        from psycopg.types.json import Jsonb

        db.execute(
            "update proposals set body=%s where id=%s", (Jsonb(proposal), p["id"])
        )
    r = c.post(
        url,
        headers={**h, "Idempotency-Key": str(uuid4()), "If-Match": '"proposal-v1"'},
        json={"reason": "Cannot promise revoke during unknown seal"},
    )
    assert r.status_code == 409 and "TOO_LATE" in r.text
