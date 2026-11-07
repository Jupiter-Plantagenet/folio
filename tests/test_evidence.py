from uuid import uuid4

from fastapi.testclient import TestClient


def test_audit_and_export_are_membership_scoped_and_originals_denied():
    from folio.workbench import WORKSPACE, app, create_case, migrate

    migrate()
    case = create_case()
    c = TestClient(app)
    prefix = f"/api/v1/workspaces/{WORKSPACE}"
    assert c.get(prefix + "/activity").status_code == 401
    s = c.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "folio-viewer-local"},
    ).json()
    h = {
        "Origin": "http://127.0.0.1:5101",
        "X-CSRF-Token": s["csrf"],
        "Idempotency-Key": str(uuid4()),
    }
    assert c.get(prefix + "/activity").status_code == 200
    assert c.get("/api/v1/workspaces/" + str(uuid4()) + "/activity").status_code == 404
    assert (
        c.post(
            prefix + f"/cases/{case['id']}/exports",
            headers=h,
            json={"include_originals": False},
        ).status_code
        == 403
    )
    s = c.post(
        "/api/v1/auth/login",
        json={"username": "auditor", "password": "folio-auditor-local"},
    ).json()
    h = {**h, "X-CSRF-Token": s["csrf"]}
    bad = c.post(
        prefix + f"/cases/{case['id']}/exports",
        headers=h,
        json={"include_originals": True},
    )
    assert bad.status_code == 403
    ok = c.post(
        prefix + f"/cases/{case['id']}/exports",
        headers={**h, "Idempotency-Key": str(uuid4())},
        json={
            "include_originals": False,
            "case_version": 1,
            "redaction_profile": "audit-v1",
        },
    )
    assert ok.status_code == 202
    from folio.exportworker import run_once

    jid = ok.json()["artifact_job_id"]
    assert run_once(jid)
    meta = c.get(prefix + "/artifacts/" + jid)
    assert meta.status_code == 200
    assert (
        meta.json()["manifest"]["scope"] == "case-evidence-metadata-originals-excluded"
    )
    assert meta.json()["sha256"]
    assert c.get(meta.json()["download_url"]).status_code == 200
