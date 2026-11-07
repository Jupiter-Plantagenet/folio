"""Real-PG human review and independent arithmetic for explicit dimensions."""

from copy import deepcopy
from fractions import Fraction
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_domain_completion import session
from test_receipt_allocations import receipt

from folio import target
from folio import workbench as wb
from folio.persisted_receipts import manifest as validate_manifest
from folio.receipts import receipt_manifest


def setup_case(currency, unit, price, face):
    wb.migrate()
    rid = str(uuid4())
    before = target.seed(wb.WORKSPACE, rid)
    before.update(currency=currency, unit=unit, invoice_face_amount=face)
    with psycopg.connect(target.DSN) as db:
        db.execute(
            "UPDATE payables SET body=%s WHERE workspace=%s AND id=%s",
            (Jsonb(before), wb.WORKSPACE, rid),
        )
    sources = [
        receipt("review.csv", "DIM-" + uuid4().hex, "100", unit=unit, currency=currency)
    ]
    case = wb.create_case(sources=sources, target_id=rid)
    case.update(
        currency=currency,
        unit=unit,
        unit_price=price,
        invoice_face_amount=face,
        receipt_manifest=receipt_manifest(sources),
    )
    with wb.connect() as db:
        wb.save_case(db, case)
    client = TestClient(wb.app)
    headers = session(client)
    return client, headers, case, before


def decision(client, headers, case, quantity="99.125", mode="partial"):
    return client.post(
        f"/api/v1/workspaces/{wb.WORKSPACE}/cases/{case['id']}/decisions",
        headers={
            **headers,
            "Idempotency-Key": str(uuid4()),
            "If-Match": f'"case-v{case["version"]}"',
        },
        json=dict(
            mode=mode,
            quantity=quantity,
            reason="Review explicit currency and units",
            evidence_digest=case["source_digest"],
        ),
    )


@pytest.mark.parametrize(
    "currency,unit,price,face,places",
    [
        ("EUR", "kg", "48.00", "6220.80", 2),
        ("JPY", "m", "48", "6221", 0),
        ("KWD", "box", "48.125", "6237.000", 3),
    ],
)
def test_human_partial_approval_and_no_change_preserve_dimensions(
    currency, unit, price, face, places
):
    client, headers, case, before = setup_case(currency, unit, price, face)
    response = decision(client, headers, case)
    assert response.status_code == 201, response.text
    proposal = response.json()["proposal"]
    support = deepcopy(case["receipt_manifest"])
    assert proposal["allocation_manifest"]["rows"][0]["currency"] == currency
    assert proposal["allocation_manifest"]["rows"][0]["unit"] == unit
    assert case["receipt_manifest"] == support
    factor = 10**places

    def half_up(value):
        return (value.numerator * 2 + value.denominator) // (2 * value.denominator)

    def money(value):
        return (
            str(value)
            if places == 0
            else f"{value // factor}.{value % factor:0{places}d}"
        )

    net = half_up(Fraction("99.125") * Fraction(price) * factor)
    tax = half_up(Fraction(net) * Fraction("0.08"))
    assert proposal["patch"] == dict(
        recognized_quantity="99.125",
        net_amount=money(net),
        tax_amount=money(tax),
        gross_amount=money(net + tax),
        status="partially_matched",
    )
    approver = TestClient(wb.app)
    approval_headers = session(approver, "approver")
    approve = approver.post(
        f"/api/v1/workspaces/{wb.WORKSPACE}/proposals/{proposal['id']}/approvals",
        headers={**approval_headers, "If-Match": '"proposal-v1"'},
        json={"intent_digest": proposal["digest"], "confirmation": "approve"},
    )
    assert approve.status_code == 201, approve.text
    from folio.verifier import check_no_change
    from folio.worker import claim, execute

    with wb.connect() as db:
        job_id = db.execute(
            "SELECT id FROM jobs WHERE proposal=%s", (proposal["id"],)
        ).fetchone()[0]
    job = claim(str(job_id))
    assert job and execute(job)["outcome"] == "matched"
    with psycopg.connect(target.DSN) as db:
        observed = db.execute(
            "SELECT body FROM payables WHERE workspace=%s AND id=%s",
            (wb.WORKSPACE, case["target_id"]),
        ).fetchone()[0]
    assert observed["currency"] == currency and observed["unit"] == unit
    assert {
        k: v
        for k, v in observed.items()
        if k not in set(proposal["patch"]) | {"revision"}
    } == {
        k: v
        for k, v in before.items()
        if k not in set(proposal["patch"]) | {"revision"}
    }
    matched_case = {
        **case,
        "invoice_quantity": "99.125",
        "ordered_quantity": "99.125",
        "received_quantity": "99.125",
    }
    assert check_no_change(matched_case)["outcome"] == "no_change"
    assert check_no_change({**matched_case, "unit": "each"})["outcome"] == "conflict"


@pytest.mark.parametrize("changed", [{"currency": "USD"}, {"unit": "each"}])
def test_receipt_dimensions_must_agree_with_case(changed):
    client, headers, case, _ = setup_case("EUR", "kg", "48.00", "6220.80")
    case.update(changed)
    with wb.connect() as db:
        wb.save_case(db, case)
    response = decision(client, headers, case)
    assert response.status_code == 422 and "CURRENCY_OR_UNIT_MISMATCH" in response.text


def test_mixed_dimension_receipt_manifest_never_sums_incompatible_quantities():
    rows = [
        receipt("a.csv", "DIM-A", "10", unit="kg", currency="EUR"),
        receipt("b.csv", "DIM-B", "10", unit="each", currency="EUR"),
    ]
    value = receipt_manifest(rows)
    before = deepcopy(value)
    with pytest.raises(ValueError):
        validate_manifest(value)
    assert value == before


@pytest.mark.parametrize("currency,unit", [("XYZ", "kg"), ("EUR", "stones")])
def test_unpinned_receipt_dimensions_remain_invalid(currency, unit):
    value = receipt_manifest([receipt("a.csv", "DIM-A", "10")])
    value["rows"][0].update(currency=currency, unit=unit)
    with pytest.raises(ValueError):
        validate_manifest(value)


def test_human_correction_cannot_introduce_fractional_zero_decimal_face_amount():
    client, headers, case, _ = setup_case("JPY", "kg", "48", "6220.80")
    response = decision(client, headers, case)
    assert response.status_code == 422 and "DECIMAL_SCALE_INVALID" in response.text


def test_target_unit_disagreement_blocks_human_proposal_without_dispatch():
    client, headers, case, before = setup_case("EUR", "kg", "48.00", "6220.80")
    before["unit"] = "each"
    with psycopg.connect(target.DSN) as db:
        db.execute(
            "UPDATE payables SET body=%s WHERE workspace=%s AND id=%s",
            (Jsonb(before), wb.WORKSPACE, case["target_id"]),
        )
    response = decision(client, headers, case)
    assert (
        response.status_code == 412
        and "TARGET_BUSINESS_IDENTITY_CONFLICT" in response.text
    )
    with wb.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM proposals WHERE case_id=%s", (case["id"],)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "currency,bad_amount", [("USD", "10"), ("JPY", "10.00"), ("KWD", "10.00")]
)
def test_target_independently_refuses_wrong_currency_scale(currency, bad_amount):
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="AMOUNT_CURRENCY_SCALE_INVALID"):
        target.validate_amount_scale(
            {"currency": currency},
            {name: bad_amount for name in ("net_amount", "tax_amount", "gross_amount")},
        )
