"""IR-CONCURRENCY-LEGACY-01: initial persisted scaffolds, never rewrite old intents."""

import copy
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb
from test_domain_completion import session
from test_receipt_physical_concurrency import (
    PREFIX,
    case,
    effects,
    headers,
    retain,
    stored,
)
from test_receipt_physical_concurrency import (
    isolated_only as isolated_only,
)

from folio import workbench as wb
from folio.allocations import allocate

DISPOSABLE_CASES = []


@pytest.fixture(autouse=True)
def dispose_scaffolds(request):
    request.getfixturevalue("isolated_only")
    yield
    # Test-owned throwaway rows only, AFTER unchanged-history assertions/evidence.
    with wb.connect() as db:
        for cid in DISPOSABLE_CASES:
            db.execute(
                "DELETE FROM jobs WHERE proposal IN (SELECT id FROM proposals WHERE case_id=%s)",
                (cid,),
            )
            db.execute("DELETE FROM proposals WHERE case_id=%s", (cid,))
            db.execute("DELETE FROM events WHERE case_id=%s", (cid,))
            db.execute("DELETE FROM cases WHERE id=%s", (cid,))
    DISPOSABLE_CASES.clear()


@pytest.fixture(scope="module", autouse=True)
def real_http_api():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    os.environ["FOLIO_CYCLE2_API"] = f"http://127.0.0.1:{port}"
    # Clean clones have no evidence/ tree; create the log directory instead of relying on it.
    os.makedirs("evidence/documents/receipt-concurrency/fix-cycle-2", exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "folio.workbench:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=(
            wb_path := open(
                "evidence/documents/receipt-concurrency/fix-cycle-2/api.log", "w"
            )
        ),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(os.environ["FOLIO_CYCLE2_API"] + "/health").status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
        else:
            raise AssertionError("ISOLATED_API_NOT_READY")
        yield
    finally:
        process.terminate()
        process.wait(timeout=10)
        wb_path.close()


def legacy(shape="both", source="csv"):
    item = case(
        "GR-" + uuid4().hex,
        alias=source == "alias",
        supplier="Legacy fixture " + uuid4().hex,
    )
    c, client, auth = item
    DISPOSABLE_CASES.append(c["id"])
    if source == "reviewer":
        c["sources"] = [
            {
                "document_role": "receipt",
                "pages": [
                    {"text": "Receipt GR-1\nLine 1\nReceived quantity 100\nUnit each"}
                ],
            }
        ]
    elif source == "sourcefree":
        c["sources"] = []
        c.pop("receipt_manifest", None)
    elif source in ("digital-pdf", "ocr-or-mixed-pdf", "docx", "eml", "unlabelled"):
        # Legacy packet content can live outside a receipt-role CSV/XLSX source.
        c["sources"] = [
            {
                "kind": source,
                "document_role": "invoice",
                "pages": [
                    {"text": "Receipt GR-1\nLine 1\nReceived quantity 100\nUnit each"}
                ],
            }
        ]
    c["source_digest"] = wb.digest(c["sources"])
    before = httpx.get(
        f"{wb.TARGET_URL}{PREFIX}/payables/{c['target_id']}",
        headers=wb.service_header("verifier"),
    ).json()
    with wb.connect() as db:
        allocation = allocate(db, c, "60") if source in ("csv", "alias") else None
    p = dict(
        id=str(uuid4()),
        workspace=wb.WORKSPACE,
        case_id=c["id"],
        version=1,
        prepared_by="reviewer",
        state="pending",
        case_version=2,
        source_digest=c["source_digest"],
        expires=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        target=c["target_id"],
        target_revision=c["target_revision"],
        before_snapshot=before,
        before_snapshot_digest=wb.digest(before),
        patch={
            "recognized_quantity": "60",
            "net_amount": "2880.00",
            "tax_amount": "230.40",
            "gross_amount": "3110.40",
            "status": "partially_matched",
        },
        operation_key=str(uuid4()),
        allocation_manifest=allocation,
    )
    if shape in ("both", "case"):
        c.pop("receipt_manifest", None)
    if shape in ("both", "allocation"):
        p.pop("allocation_manifest", None)
    if shape in ("empty", "malformed", "null", "list", "bad-row"):
        value = {
            "empty": {},
            "malformed": {"rows": "bad"},
            "null": None,
            "list": [],
            "bad-row": {
                "policy": "receipt-allocation-v1",
                "rows": [{}],
                "errors": [],
                "total": "100",
            },
        }[shape]
        c["receipt_manifest"] = copy.deepcopy(value)
        p["allocation_manifest"] = copy.deepcopy(value)
    if shape.startswith("allocation-"):
        value = {
            "empty": {},
            "null": None,
            "list": [],
            "rows": {"rows": "bad"},
            "bad-row": {**allocation, "rows": [{}]},
        }[shape.removeprefix("allocation-")]
        p["allocation_manifest"] = copy.deepcopy(value)
    if shape == "false-cache":
        c["receipt_manifest"]["total"] = "200"
    p["target_digest"] = wb.digest(
        dict(
            schema="northstar-intent-v1",
            workspace=wb.WORKSPACE,
            target=p["target"],
            revision=p["target_revision"],
            patch=p["patch"],
        )
    )
    p["digest"] = wb.digest(p)
    c.update(version=2, state="awaiting_approval", proposal=p)
    # Initial hand-authored legacy record construction only, before any approval.
    with wb.connect() as db:
        wb.save_case(db, c)
        db.execute(
            "INSERT INTO proposals VALUES(%s,%s,%s,%s)",
            (wb.WORKSPACE, p["id"], c["id"], Jsonb(p)),
        )
    return item, p


def approval(p):
    client = httpx.Client(base_url=os.environ["FOLIO_CYCLE2_API"])
    auth = session(client, "approver")
    h = headers(auth, 1, "proposal")
    b = {"intent_digest": p["digest"], "confirmation": "approve"}
    url = PREFIX + f"/proposals/{p['id']}/approvals"
    return client, url, h, b


@pytest.mark.parametrize(
    "shape,source",
    [
        ("both", "reviewer"),
        ("both", "csv"),
        ("both", "alias"),
        ("case", "csv"),
        ("allocation", "csv"),
        *[
            ("allocation-" + x, "csv")
            for x in ("empty", "null", "list", "rows", "bad-row")
        ],
        *[
            (x, "csv")
            for x in ("empty", "malformed", "null", "list", "bad-row", "false-cache")
        ],
        *[
            ("both", x)
            for x in ("digital-pdf", "ocr-or-mixed-pdf", "docx", "eml", "unlabelled")
        ],
    ],
)
def test_legacy_missing_authority_no_promotion_or_effects(shape, source):
    item, p = legacy(shape, source)
    c = copy.deepcopy(item[0])
    client, url, h, b = approval(p)
    response = client.post(url, headers=h, json=b)
    after = stored([c["id"]])[0]
    observed = effects([c["id"]])
    retain(
        "cycle2-" + shape + "-" + source,
        {
            "status": response.status_code,
            "body": response.json(),
            "proposal_unchanged": after == p,
            "proposal_before": p,
            "proposal_after": after,
            "case_before": c,
            "effects": observed,
        },
    )
    assert response.status_code == 412, response.text
    assert after == p
    with wb.connect() as db:
        assert wb.load_case(db, wb.WORKSPACE, c["id"]) == c
    assert observed == {"jobs": 0, "ledger_effects": 0}
    target_after = httpx.get(
        f"{wb.TARGET_URL}{PREFIX}/payables/{p['target']}",
        headers=wb.service_header("verifier"),
    ).json()
    assert target_after == p["before_snapshot"]


@pytest.mark.parametrize("source", ["csv", "alias", "sourcefree"])
def test_full_authority_or_sourcefree_and_exact_replay(source):
    item, p = legacy("both" if source == "sourcefree" else "valid", source)
    client, url, h, b = approval(p)
    response = client.post(url, headers=h, json=b)
    assert response.status_code == 201, response.text
    replay = client.post(url, headers=h, json=b)
    assert replay.status_code == 201 and replay.json() == response.json()
    assert effects([item[0]["id"]]) == {"jobs": 1, "ledger_effects": 0}


def test_explicit_legacy_reprepare_preserves_old_frozen_intent():
    item, p = legacy("both", "csv")
    c, reviewer, auth = item
    client, url, h, b = approval(p)
    denied = client.post(url, headers=h, json=b)
    assert denied.status_code == 412
    response = reviewer.post(
        PREFIX + f"/cases/{c['id']}/decisions",
        headers=headers(auth, 2),
        json={
            "mode": "partial",
            "quantity": "60",
            "reason": "Explicit legacy review and new proposal",
            "evidence_digest": c["source_digest"],
        },
    )
    assert response.status_code == 201, response.text
    new = response.json()["proposal"]
    old = next(x for x in stored([c["id"]]) if x["id"] == p["id"])
    assert old == {**p, "state": "superseded"}
    assert "allocation_manifest" not in old
    client, url, h, b = approval(new)
    assert client.post(url, headers=h, json=b).status_code == 201
    assert effects([c["id"]]) == {"jobs": 1, "ledger_effects": 0}
