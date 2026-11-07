import random
from fractions import Fraction
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _random_quantities(count, seed=20261104):
    """Seeded population of quantities up to six decimals in (0, 100]."""
    rng = random.Random(seed)
    values = []
    while len(values) < count:
        micro = rng.randint(1, 100_000_000)
        whole, frac = divmod(micro, 1_000_000)
        values.append(f"{whole}.{frac:06d}".rstrip("0").rstrip("."))
    return values


@pytest.mark.parametrize(
    "quantity",
    ["100", "99.125", "0.001", "1", "0.000105", "48.333333", "20.000001", "71.25"]
    + _random_quantities(40),
)
def test_exact_arithmetic_matches_independent_rational_minor_unit_oracle(quantity):
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
            quantity=quantity,
            reason="Independent rational arithmetic fixture",
            evidence_digest=case["source_digest"],
        ),
    )
    assert r.status_code == 201, r.text

    # Integer rounding of rational cents, independent of the production Decimal implementation.
    def half_up(rational):
        return (rational.numerator * 2 + rational.denominator) // (
            rational.denominator * 2
        )

    def cents(n):
        return f"{n // 100}.{n % 100:02d}"

    net = half_up(Fraction(quantity) * 48 * 100)
    tax = half_up(Fraction(net) * Fraction(8, 100))
    gross = net + tax
    p = r.json()["proposal"]
    assert p["patch"]["net_amount"] == cents(net)
    assert p["patch"]["tax_amount"] == cents(tax)
    assert p["patch"]["gross_amount"] == cents(gross)
    assert p.get("retained_balance") == cents(622080 - gross)
    assert len(p["patch"]) == 5
    if quantity == "100":
        # The documented Northstar case: 100 x 48 at 8% against a 6220.80 face.
        assert cents(net) == "4800.00" and cents(tax) == "384.00"
        assert cents(gross) == "5184.00"
        assert p["retained_balance"] == "1036.80"
