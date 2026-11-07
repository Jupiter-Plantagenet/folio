from uuid import uuid4

from authority_fixture import attestation
from fastapi.testclient import TestClient

from folio.identity import service_header


def test_atomic_target_receipt_replays_before_obsolete_precondition():
    import psycopg

    from folio.target import DSN, app, migrate, seed

    migrate()
    wid, rid, op = "11111111-1111-4111-8111-111111111111", str(uuid4()), str(uuid4())
    seed(wid, rid)
    client = TestClient(app)
    prefix = f"/api/v1/workspaces/{wid}"
    broker = service_header("broker")
    dispatch = {
        **service_header("dispatcher"),
        "If-Match": '"payable-v7"',
        "Idempotency-Key": op,
    }
    patch = dict(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    from folio.target import intent_hash

    digest = intent_hash(wid, rid, 7, patch)
    assert (
        client.post(
            prefix + "/operation-fences",
            headers=broker,
            json=dict(operation_key=op, intent_digest=digest, monotonic_fence=42),
        ).status_code
        == 200
    )
    grant = client.post(
        prefix + "/operation-grants",
        headers=broker,
        json=dict(
            operation_key=op,
            intent_digest=digest,
            target_id=rid,
            expected_revision=7,
            target_fence=42,
            exact_patch=patch,
            approval=attestation(wid, op, digest, rid),
        ),
    ).json()
    body = dict(
        intent_digest=digest, grant_jti=grant["jti"], fence=42, exact_patch=patch
    )
    first = client.patch(prefix + "/payables/" + rid, headers=dispatch, json=body)
    assert first.status_code == 200, first.text
    second = client.patch(prefix + "/payables/" + rid, headers=dispatch, json=body)
    assert second.status_code == 200 and second.json()["replayed"]
    assert first.json()["after_revision"] == 8
    wrong = client.patch(
        prefix + "/payables/" + rid,
        headers=dispatch,
        json={**body, "intent_digest": "f" * 64},
    )
    assert wrong.status_code == 409
    with psycopg.connect(DSN) as db:
        assert (
            db.execute(
                "select count(*) from operations where workspace=%s and operation_key=%s",
                (wid, op),
            ).fetchone()[0]
            == 1
        )
        row = db.execute(
            "select body from payables where workspace=%s and id=%s", (wid, rid)
        ).fetchone()[0]
        assert row["revision"] == 8 and row["invoice_face_amount"] == "6220.80"
