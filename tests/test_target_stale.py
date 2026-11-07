from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb


def test_target_edit_after_proposal_is_denied_before_approval_enqueue():
    from folio.target import DSN, seed
    from folio.workbench import WORKSPACE, app, connect, create_case, migrate

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    case = create_case(target_id=rid)
    c = TestClient(app)

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

    prefix = f"/api/v1/workspaces/{WORKSPACE}"
    p = c.post(
        prefix + f"/cases/{case['id']}/decisions",
        headers={**login("reviewer"), "If-Match": '"case-v1"'},
        json=dict(
            mode="partial",
            quantity="100",
            reason="Initially current source",
            evidence_digest=case["source_digest"],
        ),
    ).json()["proposal"]
    with psycopg.connect(DSN) as db:
        record = db.execute(
            "select body from payables where workspace=%s and id=%s", (WORKSPACE, rid)
        ).fetchone()[0]
        record["revision"] = 8
        record["due_date"] = "2026-12-01"
        db.execute(
            "update payables set body=%s where workspace=%s and id=%s",
            (Jsonb(record), WORKSPACE, rid),
        )
    r = c.post(
        prefix + f"/proposals/{p['id']}/approvals",
        headers={**login("approver"), "If-Match": '"proposal-v1"'},
        json=dict(intent_digest=p["digest"], confirmation="approve"),
    )
    assert r.status_code == 412, r.text
    with connect() as db:
        assert (
            db.execute(
                "select count(*) from jobs where proposal=%s", (p["id"],)
            ).fetchone()[0]
            == 0
        )
