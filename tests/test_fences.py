from uuid import uuid4

from authority_fixture import attestation
from fastapi.testclient import TestClient

from folio.identity import service_header


def test_higher_fence_invalidates_uncommitted_old_grant_even_with_new_envelope():
    from folio.target import app, intent_hash, migrate, seed

    migrate()
    client = TestClient(app)
    wid, rid, op = "11111111-1111-4111-8111-111111111111", str(uuid4()), str(uuid4())
    seed(wid, rid)
    p = f"/api/v1/workspaces/{wid}"
    broker = service_header("broker")
    patch = dict(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    d = intent_hash(wid, rid, 7, patch)
    for fence in [41]:
        assert (
            client.post(
                p + "/operation-fences",
                headers=broker,
                json=dict(operation_key=op, intent_digest=d, monotonic_fence=fence),
            ).status_code
            == 200
        )
    grant = client.post(
        p + "/operation-grants",
        headers=broker,
        json=dict(
            operation_key=op,
            intent_digest=d,
            target_id=rid,
            expected_revision=7,
            target_fence=41,
            exact_patch=patch,
            approval=attestation(wid, op, d, rid),
        ),
    ).json()
    assert (
        client.post(
            p + "/operation-fences",
            headers=broker,
            json=dict(operation_key=op, intent_digest=d, monotonic_fence=42),
        ).status_code
        == 200
    )
    r = client.patch(
        p + "/payables/" + rid,
        headers={
            **service_header("dispatcher"),
            "Idempotency-Key": op,
            "If-Match": '"payable-v7"',
        },
        json=dict(intent_digest=d, grant_jti=grant["jti"], fence=42, exact_patch=patch),
    )
    assert r.status_code == 409, r.text
    assert (
        client.post(
            p + "/operation-fences",
            headers=broker,
            json=dict(operation_key=op, intent_digest=d, monotonic_fence=41),
        ).status_code
        == 409
    )
    assert (
        client.post(
            p + "/operation-grants",
            headers=broker,
            json=dict(
                operation_key=op,
                intent_digest=d,
                target_id=rid,
                expected_revision=7,
                target_fence=41,
                exact_patch=patch,
                approval=attestation(wid, op, d, rid),
            ),
        ).status_code
        == 409
    )
