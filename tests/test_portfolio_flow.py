"""Northstar 100-quantity correction end to end: exact target diff, and hold writes nothing."""

import psycopg
from fastapi.testclient import TestClient
from test_currency_unit_decisions import decision, setup_case
from test_domain_completion import session

from folio import target
from folio import workbench as wb


def _target_body(record_id):
    with psycopg.connect(target.DSN) as db:
        return db.execute(
            "SELECT body FROM payables WHERE workspace=%s AND id=%s",
            (wb.WORKSPACE, record_id),
        ).fetchone()[0]


def test_northstar_hundred_correction_changes_exactly_the_proposed_fields():
    from folio.worker import claim, execute

    client, headers, case, before = setup_case("USD", "each", "48.00", "6220.80")
    response = decision(client, headers, case, quantity="100")
    assert response.status_code == 201, response.text
    proposal = response.json()["proposal"]
    assert proposal["patch"] == dict(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    approver = TestClient(wb.app)
    approve = approver.post(
        f"/api/v1/workspaces/{wb.WORKSPACE}/proposals/{proposal['id']}/approvals",
        headers={**session(approver, "approver"), "If-Match": '"proposal-v1"'},
        json={"intent_digest": proposal["digest"], "confirmation": "approve"},
    )
    assert approve.status_code == 201, approve.text
    with wb.connect() as db:
        job_id = db.execute(
            "SELECT id FROM jobs WHERE proposal=%s", (proposal["id"],)
        ).fetchone()[0]
    job = claim(str(job_id))
    assert job and execute(job)["outcome"] == "matched"

    after = _target_body(case["target_id"])
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == set(proposal["patch"]) | {"revision"}
    assert {k: after[k] for k in proposal["patch"]} == proposal["patch"]
    assert after["revision"] == before["revision"] + 1


def test_hold_decision_creates_no_job_and_leaves_target_unchanged():
    client, headers, case, before = setup_case("USD", "each", "48.00", "6220.80")
    response = decision(client, headers, case, quantity="0", mode="hold")
    assert response.status_code in (200, 201), response.text
    with wb.connect() as db:
        jobs = db.execute(
            "SELECT count(*) FROM jobs j JOIN proposals p ON p.id = j.proposal "
            "WHERE p.case_id = %s",
            (case["id"],),
        ).fetchone()[0]
    assert jobs == 0
    assert _target_body(case["target_id"]) == before
