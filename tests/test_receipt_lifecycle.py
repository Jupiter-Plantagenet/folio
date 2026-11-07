"""Receipt review negatives: actual parsed synthetic input and isolated PostgreSQL."""

from uuid import uuid4

from fastapi.testclient import TestClient
from test_domain_completion import session
from test_receipt_allocations import packet_files, receipt_file


def setup(files):
    from folio.workbench import WORKSPACE, app, migrate

    migrate()
    client = TestClient(app)
    auth = session(client)
    prefix = f"/api/v1/workspaces/{WORKSPACE}"
    result = client.post(
        prefix + "/intakes/files",
        headers=auth,
        json={"rights_attestation": "authored-synthetic", "files": files},
    )
    assert result.status_code == 202, result.text
    url = prefix + "/cases/" + result.json()["case_id"]
    return client, auth, prefix, url


def hdr(auth, case):
    return {
        **auth,
        "Idempotency-Key": str(uuid4()),
        "If-Match": f'"case-v{case["version"]}"',
    }


def test_genuine_identity_conflict_corrects_and_resumes_hold_escalation_and_review():
    import base64

    files = packet_files() + [receipt_file("GR-" + uuid4().hex, "100")]
    files += [
        {
            "name": "conflicting.csv",
            "document_role": "invoice",
            "base64": base64.b64encode(b"Invoice INV-CONFLICT\n").decode(),
        }
    ]
    client, auth, prefix, url = setup(files)
    c = client.get(url).json()
    assert "invoice" in c["conflicting_fields"] and "invoice" in c["missing_fields"]
    for mode in ["hold", "escalate"]:
        result = client.post(
            url + "/decisions",
            headers=hdr(auth, c),
            json={
                "mode": mode,
                "reason": "Conflicting independent invoice identity",
                "evidence_digest": c["source_digest"],
            },
        )
        assert result.status_code == 201 and result.json()["proposal"] is None
        c = client.get(url).json()
    result = client.patch(
        url + "/fields/invoice",
        headers=hdr(auth, c),
        json={
            "typed_value": "INV-1048",
            "reason": "Confirm original PDF invoice after inspecting conflicting CSV",
        },
    )
    assert result.status_code == 200, result.text
    c = client.get(url).json()
    assert (
        c["state"] == "needs_review"
        and not c["missing_fields"]
        and not c["conflicting_fields"]
    )
    assert c["field_provenance"]["invoice"][-1]["kind"] == "human"
    from folio.target import seed
    from folio.workbench import WORKSPACE

    rid = str(uuid4())
    seed(WORKSPACE, rid)
    assert (
        client.post(
            url + "/target-match", headers=hdr(auth, c), json={"selected_target": rid}
        ).status_code
        == 200
    )
    c = client.get(url).json()
    proposal = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Corrected conflict and reviewed supported receipt",
            "evidence_digest": c["source_digest"],
        },
    )
    assert proposal.status_code == 201, proposal.text
    from folio.workbench import connect

    with connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE proposal IN (SELECT id FROM proposals WHERE case_id=%s)",
                (c["id"],),
            ).fetchone()[0]
            == 0
        )


def test_partial_receipt_reservations_across_import_aliases_leave_only_exact_balance():
    from folio.target import seed
    from folio.workbench import WORKSPACE

    one, two = "GR-" + uuid4().hex, "GR-" + uuid4().hex
    first = packet_files() + [receipt_file(one, "40.1"), receipt_file(two, "59.9")]
    second = packet_files() + [receipt_file(two, "59.9", xlsx=True)]
    for files, quantity, expected in [
        (first, "60", ["40.1", "19.9"]),
        (second, "40", ["40"]),
    ]:
        client, auth, prefix, url = setup(files)
        c = client.get(url).json()
        rid = str(uuid4())
        seed(WORKSPACE, rid)
        assert (
            client.post(
                url + "/target-match",
                headers=hdr(auth, c),
                json={"selected_target": rid},
            ).status_code
            == 200
        )
        c = client.get(url).json()
        result = client.post(
            url + "/decisions",
            headers=hdr(auth, c),
            json={
                "mode": "partial",
                "quantity": quantity,
                "reason": "Allocate only remaining supported receipt capacity",
                "evidence_digest": c["source_digest"],
            },
        )
        assert result.status_code == 201, result.text
        assert [
            r["allocated"]
            for r in result.json()["proposal"]["allocation_manifest"]["rows"]
        ] == expected
    # The same XLSX case cannot increase its reservation beyond the remaining 40.
    c = client.get(url).json()
    denied = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "40.000001",
            "reason": "Must not overallocate",
            "evidence_digest": c["source_digest"],
        },
    )
    assert denied.status_code == 422 and "UNALLOCATED" in denied.text, denied.text


def test_source_alias_addition_invalidates_signed_unsealed_authority_without_changing_payload():
    from folio.target import seed
    from folio.workbench import WORKSPACE, app, connect
    from folio.worker import claim, execute

    receipt_id = "GR-" + uuid4().hex
    client, auth, prefix, url = setup(
        packet_files() + [receipt_file(receipt_id, "100")]
    )
    c = client.get(url).json()
    rid = str(uuid4())
    seed(WORKSPACE, rid)
    assert (
        client.post(
            url + "/target-match", headers=hdr(auth, c), json={"selected_target": rid}
        ).status_code
        == 200
    )
    c = client.get(url).json()
    result = client.post(
        url + "/decisions",
        headers=hdr(auth, c),
        json={
            "mode": "partial",
            "quantity": "100",
            "reason": "Supported receipt",
            "evidence_digest": c["source_digest"],
        },
    )
    assert result.status_code == 201, result.text
    p = result.json()["proposal"]
    approver = TestClient(app)
    ah = session(approver, "approver")
    approval = approver.post(
        prefix + f"/proposals/{p['id']}/approvals",
        headers={**ah, "If-Match": '"proposal-v1"'},
        json={"intent_digest": p["digest"], "confirmation": "approve"},
    )
    assert approval.status_code == 201, approval.text
    with connect() as db:
        signed = db.execute(
            "SELECT body FROM proposals WHERE id=%s", (p["id"],)
        ).fetchone()[0]
    c = client.get(url).json()
    result = client.post(
        prefix + "/intakes/files",
        headers=hdr(auth, c),
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": c["id"],
            "files": [receipt_file(receipt_id, "100", xlsx=True)],
        },
    )
    assert result.status_code == 202, result.text
    changed = client.get(url).json()
    assert (
        changed["received_quantity"] == "100"
        and changed["source_digest"] != c["source_digest"]
    )
    assert len(changed["receipt_manifest"]["rows"]) == 1
    execute(claim(approval.json()["job_id"]))
    with connect() as db:
        old = db.execute(
            "SELECT body FROM proposals WHERE id=%s", (p["id"],)
        ).fetchone()[0]
    assert old["state"] == "superseded"
    for key in ["digest", "patch", "allocation_manifest", "approval_attestation"]:
        assert old[key] == signed[key]
    from folio.verifier import check

    assert check(old)["outcome"] != "matched"


def test_foreign_workspace_and_viewer_cannot_attach_support_or_request_information():
    from folio.workbench import app

    client, auth, prefix, url = setup(
        packet_files() + [receipt_file("GR-" + uuid4().hex, "100")]
    )
    c = client.get(url).json()
    bad_scope = client.post(
        url.replace(prefix.split("/")[-1], str(uuid4())) + "/information-requests",
        headers=hdr(auth, c),
        json={"fields": ["invoice"], "reason": "Denied"},
    )
    assert bad_scope.status_code == 404
    viewer = TestClient(app)
    vh = session(viewer, "viewer")
    assert (
        viewer.post(
            url + "/information-requests",
            headers=hdr(vh, c),
            json={"fields": ["invoice"], "reason": "Denied"},
        ).status_code
        == 403
    )
    denied = viewer.post(
        prefix + "/intakes/files",
        headers=hdr(vh, c),
        json={
            "rights_attestation": "authored-synthetic",
            "case_id": c["id"],
            "files": [receipt_file("GR-DENIED", "10")],
        },
    )
    assert denied.status_code == 403
