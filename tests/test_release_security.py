"""Domain negatives before widening the supported surface."""

from uuid import uuid4

from fastapi.testclient import TestClient

from folio.identity import service_header


def test_malformed_target_commands_are_422_not_unhandled_exceptions():
    from folio.target import app

    c = TestClient(app, raise_server_exceptions=False)
    root = "/api/v1/workspaces/11111111-1111-4111-8111-111111111111"
    for route in ["operation-fences", "operation-grants"]:
        r = c.post(root + "/" + route, json={}, headers=service_header("broker"))
        assert r.status_code == 422, r.text


def test_target_rejects_negative_and_non_decimal_patch_before_sealing():
    from folio.target import app, intent_hash, migrate, seed

    migrate()
    c = TestClient(app)
    wid, rid, op = "11111111-1111-4111-8111-111111111111", str(uuid4()), str(uuid4())
    seed(wid, rid)
    root = f"/api/v1/workspaces/{wid}"
    patch = dict(
        recognized_quantity="-100",
        net_amount="NaN",
        tax_amount="0.00",
        gross_amount="0.00",
        status="partially_matched",
    )
    d = intent_hash(wid, rid, 7, patch)
    h = service_header("broker")
    assert (
        c.post(
            root + "/operation-fences",
            headers=h,
            json=dict(operation_key=op, intent_digest=d, monotonic_fence=41),
        ).status_code
        == 200
    )
    r = c.post(
        root + "/operation-grants",
        headers=h,
        json=dict(
            operation_key=op,
            intent_digest=d,
            target_id=rid,
            expected_revision=7,
            target_fence=41,
            exact_patch=patch,
        ),
    )
    assert r.status_code == 422, r.text
