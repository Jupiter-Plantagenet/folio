from uuid import uuid4

from fastapi.testclient import TestClient


def test_public_source_role_tokens_cannot_register_target_authority():
    from folio.target import app

    client = TestClient(app)
    r = client.post(
        "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/operation-fences",
        headers={"Authorization": "Bearer folio-local-broker"},
        json={
            "operation_key": str(uuid4()),
            "intent_digest": "a" * 64,
            "monotonic_fence": 41,
        },
    )
    assert r.status_code == 403, r.text


def test_provisioned_broker_cannot_mint_an_unapproved_or_foreign_target_grant():
    from folio.identity import WORKSPACE, service_header
    from folio.target import app, intent_hash, seed

    client = TestClient(app)
    rid, op = [str(uuid4()) for _ in range(2)]
    seed(WORKSPACE, rid)
    patch = dict(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    intent = intent_hash(WORKSPACE, rid, 7, patch)
    root = f"/api/v1/workspaces/{WORKSPACE}"
    h = service_header("broker")
    assert (
        client.post(
            root + "/operation-fences",
            headers=h,
            json=dict(operation_key=op, intent_digest=intent, monotonic_fence=41),
        ).status_code
        == 200
    )
    r = client.post(
        root + "/operation-grants",
        headers=h,
        json=dict(
            operation_key=op,
            intent_digest=intent,
            target_id=rid,
            expected_revision=7,
            target_fence=41,
            exact_patch=patch,
        ),
    )
    assert r.status_code in (403, 422), r.text
    r = client.post(
        f"/api/v1/workspaces/{uuid4()}/operation-fences",
        headers=h,
        json=dict(operation_key=op, intent_digest=intent, monotonic_fence=41),
    )
    assert r.status_code == 403
    r = client.post(
        root + "/operation-fences",
        headers={**h, "Origin": "http://127.0.0.1:5101"},
        json=dict(operation_key=op, intent_digest=intent, monotonic_fence=41),
    )
    assert r.status_code == 403
