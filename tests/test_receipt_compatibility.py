"""Read-only ADR-003/RV-S01 compatibility on isolated PG and socket HTTP."""

import copy
import json
import os
from pathlib import Path

import httpx
import pytest
from test_domain_completion import session
from test_receipt_allocations import receipt
from test_receipt_history_integrity import amount, scaffold, snapshot
from test_receipt_legacy_approval import approval, legacy
from test_receipt_legacy_approval import dispose_scaffolds as dispose_scaffolds
from test_receipt_legacy_approval import real_http_api as real_http_api
from test_receipt_physical_concurrency import PREFIX, effects, headers
from test_receipt_physical_concurrency import isolated_only as isolated_only

from folio import workbench as wb
from folio.persisted_receipts import manifest, number, proposal
from folio.receipts import receipt_manifest


def old_version(p):
    m = p["allocation_manifest"]
    m["case_version"] = m.pop("input_case_version")


def initial(p, c):
    # Initial unsigned fixture construction only, never a runtime migration.
    p.pop("digest")
    p["digest"] = wb.digest(p)
    c["proposal"] = p
    with wb.connect() as db:
        wb.save_proposal(db, p)
        wb.save_case(db, c)


def positive(item, p, endpoint, label, prior=None):
    c = item[0]
    before = snapshot()
    target_before = httpx.get(
        f"{wb.TARGET_URL}{PREFIX}/payables/{p['target']}",
        headers=wb.service_header("verifier"),
    ).json()
    if endpoint == "approve":
        client, url, h, b = approval(p)
    else:
        client = httpx.Client(base_url=os.environ["FOLIO_CYCLE2_API"])
        auth = session(client)
        url = PREFIX + f"/cases/{c['id']}/decisions"
        h = headers(auth, 2)
        b = dict(
            mode="partial",
            quantity="60",
            reason="Compatibility review",
            evidence_digest=c["source_digest"],
        )
    response = client.post(url, headers=h, json=b)
    assert response.status_code == 201, response.text
    replay = client.post(url, headers=h, json=b)
    assert replay.status_code == 201 and replay.json() == response.json()
    after = snapshot()
    with wb.connect() as db:
        saved = wb.load_proposal(db, wb.WORKSPACE, p["id"])
        saved_case = wb.load_case(db, wb.WORKSPACE, c["id"])
        if endpoint == "approve":
            for key, value in p.items():
                if key != "state":
                    assert saved[key] == value
        else:
            new = response.json()["proposal"]["allocation_manifest"]
            assert new["input_case_version"] == 2 and "case_version" not in new
        assert saved_case["sources"] == c["sources"]
        assert saved_case["receipt_manifest"] == c["receipt_manifest"]
        if prior:
            assert wb.load_proposal(db, wb.WORKSPACE, prior["id"]) == prior
    target_after = httpx.get(
        f"{wb.TARGET_URL}{PREFIX}/payables/{p['target']}",
        headers=wb.service_header("verifier"),
    ).json()
    assert target_after == target_before
    assert effects([c["id"]]) == {
        "jobs": int(endpoint == "approve"),
        "ledger_effects": 0,
    }
    out = Path("evidence/documents/receipt-concurrency/compatibility")
    out.mkdir(parents=True, exist_ok=True)
    (out / (label + "-" + endpoint + ".json")).write_text(
        json.dumps(
            dict(
                status=response.status_code,
                response=response.json(),
                replay=replay.json(),
                before=before,
                after=after,
                target_before=target_before,
                target_after=target_after,
            ),
            default=str,
            indent=2,
        )
    )
    client.close()


@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
@pytest.mark.parametrize(
    "state,gate",
    [
        ("pending", None),
        ("approved", "prepared"),
        ("rejected", "seal_pending"),
        ("superseded", "sealed"),
        ("expired", "uncertain"),
        ("pending", "applied"),
    ],
)
def test_legacy_v1_history_40_new60(endpoint, state, gate):
    item, p = legacy("valid", "csv")

    def change(old):
        amount("40")(old)
        old_version(old)

    prior = scaffold(p, change, state=state, gate=gate)
    positive(item, p, endpoint, f"old-history-{state}-{gate}", prior)


@pytest.mark.parametrize("dual", [False, True])
def test_pending_old_v1_approval_preserves_frozen_identity(dual):
    item, p = legacy("valid", "csv")
    if dual:
        p["allocation_manifest"]["case_version"] = 1
    else:
        old_version(p)
    initial(p, item[0])
    positive(item, p, "approve", "pending-old-dual-" + str(dual))


@pytest.mark.parametrize(
    "fields",
    [{"case_version": x} for x in [None, True, "1", 1.0, 0, -1, 2]]
    + [{"case_version": 1, "input_case_version": x} for x in [None, True, "1", 1.0, 2]]
    + [{}],
)
def test_malformed_version_fields_fail_closed(fields):
    item, p = legacy("valid", "csv")
    candidate = copy.deepcopy(p)
    candidate["allocation_manifest"].pop("input_case_version")
    candidate["allocation_manifest"].update(fields)
    before = copy.deepcopy(candidate)
    with pytest.raises(ValueError):
        proposal(candidate, item[0], wb.WORKSPACE, current=True)
    assert candidate == before


def large_support():
    return receipt_manifest(
        [
            receipt("one.csv", "BIG-A", "500000000000"),
            receipt("two.csv", "BIG-B", "500000000000"),
        ]
    )


@pytest.mark.parametrize(
    "value", ["1000000000000", "1e99", "NaN", "Infinity", True, 60, "0.0000001", "-60"]
)
def test_scalar_scope_not_widened(value):
    with pytest.raises(ValueError):
        number(value)


@pytest.mark.parametrize(
    "total",
    ["1000000000001", "999999999999", "1e1000000", "NaN", None, True, "1" * 10000],
)
def test_aggregate_must_equal_validated_rows(total):
    m = large_support()
    m["total"] = total
    with pytest.raises(ValueError):
        manifest(m)


@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
def test_two_bounded_receipts_support_13_digit_total(endpoint):
    item, p = legacy("valid", "csv")
    c = item[0]
    sources = [
        receipt("one.csv", "BIG-A", "500000000000"),
        receipt("two.csv", "BIG-B", "500000000000"),
    ]
    c["sources"] = sources
    c["source_digest"] = wb.digest(sources)
    c["receipt_manifest"] = receipt_manifest(sources)
    assert c["receipt_manifest"]["total"] == "1000000000000"
    p["source_digest"] = c["source_digest"]
    p["allocation_manifest"].update(
        source_digest=c["source_digest"],
        rows=[
            {
                **copy.deepcopy(c["receipt_manifest"]["rows"][0]),
                "supplier": c["supplier"],
                "allocated": "60",
                "prior_allocated": "0",
            }
        ],
    )
    initial(p, c)
    positive(item, p, endpoint, "large-support")
