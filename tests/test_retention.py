import base64
from uuid import uuid4

from fastapi.testclient import TestClient
from test_domain_completion import session


def test_legal_hold_blocks_original_deletion_and_admin_delete_leaves_a_digest_tombstone():
    from folio.workbench import WORKSPACE, app, connect, migrate

    migrate()
    client = TestClient(app)
    h = session(client)
    imported = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/intakes/files",
        headers=h,
        json={
            "rights_attestation": "authored-synthetic",
            "files": [
                {
                    "name": "retention.csv",
                    "document_role": "receipt",
                    "base64": base64.b64encode(
                        b"Received quantity,1\nUnit,each\n"
                    ).decode(),
                }
            ],
        },
    ).json()
    cid = imported["case_id"]
    url = f"/api/v1/workspaces/{WORKSPACE}/cases/{cid}"
    admin = session(client, "admin")
    hold = client.post(
        url + "/retention-holds",
        headers={**admin, "If-Match": '"case-v1"'},
        json={"enabled": True, "reason": "Preserve review evidence"},
    )
    assert hold.status_code == 200, hold.text
    denied = client.post(
        url + "/original-deletions",
        headers={**admin, "Idempotency-Key": str(uuid4()), "If-Match": '"case-v2"'},
        json={"reason": "Delete original bytes"},
    )
    assert denied.status_code == 409
    release = client.post(
        url + "/retention-holds",
        headers={**admin, "Idempotency-Key": str(uuid4()), "If-Match": '"case-v2"'},
        json={"enabled": False, "reason": "Hold released by authorized owner"},
    )
    assert release.status_code == 200
    deleted = client.post(
        url + "/original-deletions",
        headers={**admin, "Idempotency-Key": str(uuid4()), "If-Match": '"case-v3"'},
        json={"reason": "Owner requested original-byte deletion"},
    )
    assert deleted.status_code == 200, deleted.text
    with connect() as db:
        rows = db.execute(
            "SELECT raw,sha256,deleted_at FROM originals WHERE workspace=%s AND case_id=%s",
            (WORKSPACE, cid),
        ).fetchall()
    assert (
        len(rows) == 1
        and rows[0][0] is None
        and len(rows[0][1]) == 64
        and rows[0][2] is not None
    )
    after = client.get(url).json()
    assert (
        after["state"] == "source_expired"
        and after["sources"][0]["original_deleted"] is True
    )
    assert after["retention"]["decision_evidence_days"] == 365


def test_automatic_ninety_day_original_expiry_respects_legal_hold():
    import hashlib

    from folio.workbench import WORKSPACE, connect, create_case, migrate, save_case
    from folio.worker import expire_originals

    migrate()
    sid = str(uuid4())
    raw = b"Authored synthetic retention storage fixture"
    sha = hashlib.sha256(raw).hexdigest()
    case = create_case(sources=[{"id": sid, "sha256": sha, "pages": []}])
    case["retention"] = {
        "legal_hold": True,
        "original_days": 90,
        "decision_evidence_days": 365,
    }
    with connect() as db:
        save_case(db, case)
        db.execute(
            "INSERT INTO originals(workspace,id,case_id,sha256,raw,created_at) VALUES(%s,%s,%s,%s,%s,clock_timestamp()-interval '91 days')",
            (WORKSPACE, sid, case["id"], sha, raw),
        )
    assert expire_originals() == 0
    with connect() as db:
        case["retention"]["legal_hold"] = False
        save_case(db, case)
    assert expire_originals() == 1
    with connect() as db:
        assert (
            db.execute("SELECT raw FROM originals WHERE id=%s", (sid,)).fetchone()[0]
            is None
        )


def test_expired_originals_cannot_authorize_a_new_target_proposal():
    from pathlib import Path

    from folio.parser import parse_pdf
    from folio.target import seed
    from folio.workbench import WORKSPACE, app, create_case, migrate

    migrate()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    sources = [
        {"id": str(uuid4()), "original_deleted": True, **parse_pdf(p.read_bytes())}
        for p in (Path(__file__).resolve().parents[1] / "fixtures/public").glob("*.pdf")
    ]
    case = create_case(sources, target_id=rid)
    client = TestClient(app)
    h = session(client)
    result = client.post(
        f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/decisions",
        headers={**h, "If-Match": '"case-v1"'},
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Must not reuse expired original authority",
            "evidence_digest": case["source_digest"],
        },
    )
    assert result.status_code == 422 and "SOURCE_ORIGINAL_UNAVAILABLE" in result.text


def test_held_old_packets_do_not_starve_other_due_original_expiry():
    import hashlib

    from folio.workbench import WORKSPACE, connect, create_case, migrate, save_case
    from folio.worker import expire_originals

    migrate()
    packets = []
    raw = b"CC0 authored retention fairness fixture"
    sha = hashlib.sha256(raw).hexdigest()
    with connect() as db:
        for _ in range(21):
            sid = str(uuid4())
            case = create_case([{"id": sid, "sha256": sha, "pages": []}], db=db)
            packets.append(case)
            db.execute(
                "INSERT INTO originals(workspace,id,case_id,sha256,raw,created_at) VALUES(%s,%s,%s,%s,%s,clock_timestamp()-interval '91 days')",
                (WORKSPACE, sid, case["id"], sha, raw),
            )
        for index, case in enumerate(sorted(packets, key=lambda c: c["id"])):
            case["retention"] = {"legal_hold": index < 20}
            save_case(db, case)
    assert expire_originals() == 1


def test_scheduled_expiry_keeps_newer_originals_in_the_same_case():
    """Requires the isolated PostgreSQL fixture, not retained application data."""
    import hashlib

    from folio.workbench import WORKSPACE, connect, create_case, migrate
    from folio.worker import expire_originals

    migrate()
    old_id, new_id = str(uuid4()), str(uuid4())
    old_bytes = b"Authored old original"
    new_bytes = b"Authored newly added original"
    sources = [
        {"id": old_id, "sha256": hashlib.sha256(old_bytes).hexdigest(), "pages": []},
        {"id": new_id, "sha256": hashlib.sha256(new_bytes).hexdigest(), "pages": []},
    ]
    case = create_case(sources=sources)
    with connect() as db:
        for source, raw, age_days in (
            (sources[0], old_bytes, 91),
            (sources[1], new_bytes, 1),
        ):
            db.execute(
                "INSERT INTO originals(workspace,id,case_id,sha256,raw,created_at) "
                "VALUES(%s,%s,%s,%s,%s,clock_timestamp()-(%s * interval '1 day'))",
                (WORKSPACE, source["id"], case["id"], source["sha256"], raw, age_days),
            )
    assert expire_originals() == 1
    with connect() as db:
        stored = {
            str(sid): (None if raw is None else bytes(raw), deleted_at)
            for sid, raw, deleted_at in db.execute(
                "SELECT id,raw,deleted_at FROM originals WHERE workspace=%s AND case_id=%s",
                (WORKSPACE, case["id"]),
            ).fetchall()
        }
        body = db.execute(
            "SELECT body FROM cases WHERE workspace=%s AND id=%s",
            (WORKSPACE, case["id"]),
        ).fetchone()[0]
    assert stored[old_id][0] is None and stored[old_id][1] is not None
    assert stored[new_id] == (new_bytes, None)
    statuses = {source["id"]: source.get("original_deleted", False) for source in body["sources"]}
    assert statuses[old_id] is True and statuses[new_id] is False

