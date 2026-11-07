"""Bounded persisted JSON read-boundary regressions: real PG and socket HTTP."""

import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb
from test_domain_completion import session
from test_receipt_legacy_approval import (
    DISPOSABLE_CASES,
    approval,
    legacy,
)
from test_receipt_legacy_approval import (
    dispose_scaffolds as dispose_scaffolds,
)
from test_receipt_legacy_approval import (
    real_http_api as real_http_api,
)
from test_receipt_physical_concurrency import PREFIX, headers
from test_receipt_physical_concurrency import isolated_only as isolated_only

from folio import workbench as wb

OUT = Path("evidence/documents/receipt-concurrency/history-correction")


def snapshot():
    with wb.connect() as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("cases", "proposals", "events", "jobs", "commands")
        }


def scaffold(p, mutation, state="approved", gate="prepared"):
    prior = copy.deepcopy(p)
    prior.update(
        id=str(uuid4()),
        case_id=str(uuid4()),
        target=str(uuid4()),
        state=state,
        gate=gate,
    )
    mutation(prior)
    DISPOSABLE_CASES.append(prior["case_id"])
    with wb.connect() as db:
        c = wb.load_case(db, wb.WORKSPACE, p["case_id"])
        c.update(id=prior["case_id"], target_id=prior["target"], proposal=prior)
        db.execute(
            "INSERT INTO cases VALUES(%s,%s,%s)", (wb.WORKSPACE, c["id"], Jsonb(c))
        )
        db.execute(
            "INSERT INTO proposals VALUES(%s,%s,%s,%s)",
            (wb.WORKSPACE, prior["id"], prior["case_id"], Jsonb(prior)),
        )
    return prior


def amount(value):
    def change(p):
        p["allocation_manifest"]["rows"][0]["allocated"] = value
        p["allocation_manifest"]["total"] = value
        p["patch"]["recognized_quantity"] = value

    return change


def denial(item, p, endpoint, label):
    if endpoint == "approve":
        client, url, h, b = approval(p)
    else:
        client = httpx.Client(base_url=os.environ["FOLIO_CYCLE2_API"])
        auth = session(client)
        url = PREFIX + f"/cases/{p['case_id']}/decisions"
        h = headers(auth, 2)
        b = {
            "mode": "partial",
            "quantity": "60",
            "reason": "Integrity regression",
            "evidence_digest": item[0]["source_digest"],
        }
    before = snapshot()
    target_url = f"{wb.TARGET_URL}{PREFIX}/payables/{p['target']}"
    target_before = httpx.get(target_url, headers=wb.service_header("verifier")).json()
    response = client.post(url, headers=h, json=b)
    after = snapshot()
    target_after = httpx.get(target_url, headers=wb.service_header("verifier")).json()
    OUT.mkdir(parents=True, exist_ok=True)
    (
        OUT
        / (
            os.environ.get("HISTORY_PHASE", "green")
            + "-"
            + label
            + "-"
            + endpoint
            + ".json"
        )
    ).write_text(
        json.dumps(
            {
                "status": response.status_code,
                "body": response.text,
                "before": before,
                "after": after,
                "target_before": target_before,
                "target_after": target_after,
            },
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert response.status_code == (412 if endpoint == "approve" else 422), (
        response.text
    )
    assert after == before
    assert target_after == target_before
    assert len(after["jobs"]) == len(before["jobs"])
    client.close()


@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
def test_negative_history_cannot_credit_capacity(endpoint):
    item, p = legacy("valid", "csv")
    scaffold(p, amount("100"))
    scaffold(p, amount("-60"))
    denial(item, p, endpoint, "negative-100-minus60-new60")


@pytest.mark.parametrize("expiry", [None, "garbage", "2999-01-01T00:00:00"])
@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
def test_expiry_is_controlled_read_boundary(expiry, endpoint):
    item, p = legacy("valid", "csv")
    scaffold(p, lambda old: old.update(expires=expiry), state="pending", gate=None)
    denial(item, p, endpoint, "prior-expiry-" + str(expiry).replace(":", "-"))


@pytest.mark.parametrize("expiry", [None, "garbage", "2999-01-01T00:00:00"])
def test_current_expiry_is_controlled(expiry):
    item, p = legacy("valid", "csv")
    # Initial malformed persisted candidate, not an approved/signed intent.
    p["expires"] = expiry
    with wb.connect() as db:
        wb.save_proposal(db, p)
    denial(item, p, "approve", "current-expiry-" + str(expiry).replace(":", "-"))


@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
def test_null_present_history_is_not_absent_legacy(endpoint):
    item, p = legacy("valid", "csv")
    scaffold(p, lambda old: old.update(allocation_manifest=None))
    denial(item, p, endpoint, "null-history")


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
@pytest.mark.parametrize("value", ["-60", "0", "NaN", "Infinity", "1e999999", "101"])
def test_active_protected_amount_matrix(state, gate, value):
    item, p = legacy("valid", "csv")
    scaffold(p, amount(value), state=state, gate=gate)
    denial(item, p, "prepare", f"matrix-{state}-{gate}-{value}")


BAD_SHAPES = [
    "null",
    "list",
    "empty",
    "rows-null",
    "rows-dict",
    "row-null",
    "row-list",
    "missing",
    "duplicate",
    "supplier",
    "identity",
    "currency",
    "unit",
    "policy",
    "total",
    "quantity",
    "prior",
    "numeric-type",
    "evidence",
]


def corrupt(shape):
    def change(p):
        m = p["allocation_manifest"]
        r = m["rows"][0]
        if shape in ("null", "list", "empty"):
            p["allocation_manifest"] = {"null": None, "list": [], "empty": {}}[shape]
        elif shape.startswith("rows-"):
            m["rows"] = None if shape == "rows-null" else {}
        elif shape.startswith("row-"):
            m["rows"] = [None if shape == "row-null" else []]
        elif shape == "missing":
            del r["allocated"]
        elif shape == "duplicate":
            m["rows"].append(copy.deepcopy(r))
        elif shape in ("supplier", "currency", "unit"):
            r[shape] = "untruthful"
        elif shape == "identity":
            r["key"] = "other:1"
        elif shape == "policy":
            m["policy"] = "unsupported"
        elif shape == "total":
            m["total"] = "59"
        elif shape == "quantity":
            r["quantity"] = "-100"
        elif shape == "prior":
            r["prior_allocated"] = "-1"
        elif shape == "numeric-type":
            r["allocated"] = True
        elif shape == "evidence":
            r["evidence"] = [None]

    return change


@pytest.mark.parametrize("shape", BAD_SHAPES)
@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
def test_historical_structure_matrix(shape, endpoint):
    item, p = legacy("valid", "csv")
    scaffold(p, corrupt(shape))
    denial(item, p, endpoint, "shape-" + shape)


@pytest.mark.parametrize("overlap", [True, False])
def test_absent_legacy_manifest_immutable_overlap_policy(overlap):
    item, p = legacy("valid", "csv")

    def change(old):
        old.pop("allocation_manifest")

    prior = scaffold(p, change)
    if overlap:
        denial(item, p, "prepare", "absent-overlap")
    else:
        # Separate source-free historical record inserted before command execution.
        with wb.connect() as db:
            c = wb.load_case(db, wb.WORKSPACE, prior["case_id"])
            c["sources"] = []
            c.pop("receipt_manifest", None)
            wb.save_case(db, c)
        client, url, h, b = approval(p)
        response = client.post(url, headers=h, json=b)
        assert response.status_code == 201, response.text
        assert client.post(url, headers=h, json=b).json() == response.json()
        with wb.connect() as db:
            assert wb.load_proposal(db, wb.WORKSPACE, prior["id"]) == prior
        client.close()


@pytest.mark.parametrize("endpoint", ["approve", "prepare"])
def test_combined_positive_history_overcapacity_fails(endpoint):
    item, p = legacy("valid", "csv")
    scaffold(p, amount("60"))
    scaffold(p, amount("60"))
    denial(item, p, endpoint, "combined-positive-overcapacity")


@pytest.mark.parametrize(
    "shape", ["uncached", "null-cache", "list-cache", "null-sources"]
)
def test_legacy_case_read_boundary_is_not_a_repair_path(shape):
    item, p = legacy("valid", "csv")
    prior = scaffold(p, lambda old: old.pop("allocation_manifest"))
    with wb.connect() as db:
        c = wb.load_case(db, wb.WORKSPACE, prior["case_id"])
        if shape == "uncached":
            c.pop("receipt_manifest")
        elif shape == "null-cache":
            c["receipt_manifest"] = None
        elif shape == "list-cache":
            c["receipt_manifest"] = []
        else:
            c["sources"] = None
        wb.save_case(db, c)
    denial(item, p, "prepare", "legacy-case-" + shape)


def test_valid_history_40_plus_new60_and_exact_replay():
    item, p = legacy("valid", "csv")
    prior = scaffold(p, amount("40"))
    client, url, h, b = approval(p)
    response = client.post(url, headers=h, json=b)
    assert response.status_code == 201, response.text
    assert client.post(url, headers=h, json=b).json() == response.json()
    with wb.connect() as db:
        assert wb.load_proposal(db, wb.WORKSPACE, prior["id"]) == prior
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE proposal=%s", (p["id"],)
            ).fetchone()[0]
            == 1
        )
    client.close()
