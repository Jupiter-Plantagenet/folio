"""Authored, opt-in isolated PG/socket proofs; no default live writes."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.skipif(
    os.getenv("FOLIO_EXPORT_PROOF") != "1"
    or "folio_finish_wb_export" not in os.getenv("FOLIO_DSN", ""),
    reason="requires scripts/export_proof.py disposable database",
)


def packet():
    from folio import workbench as w

    w.migrate()
    c = w.create_case(
        [
            {
                "id": str(uuid4()),
                "version": 1,
                "sha256": "a" * 64,
                "pages": [{"text": "PRIVATE ORIGINAL BYTES"}],
                "private_path": "PRIVATE PATH",
            }
        ]
    )
    client = TestClient(w.app)
    s = client.post(
        "/api/v1/auth/login",
        json={"username": "auditor", "password": "folio-auditor-local"},
    ).json()
    h = {"Origin": w.ORIGIN, "X-CSRF-Token": s["csrf"], "Idempotency-Key": str(uuid4())}
    return w, c, client, h, f"/api/v1/workspaces/{w.WORKSPACE}"


def submit(client, h, p, c, **extra):
    return client.post(
        p + f"/cases/{c['id']}/exports",
        headers=h,
        json={
            "case_version": c["version"],
            "include_originals": False,
            "redaction_profile": "audit-v1",
            **extra,
        },
    )


def test_enqueue_is_durable_and_worker_publishes_exact_frozen_bytes():
    w, c, client, h, p = packet()
    r = submit(client, h, p, c)
    assert r.status_code == 202, r.text
    jid = r.json()["artifact_job_id"]
    assert submit(client, h, p, c).json() == r.json()
    with w.connect() as db:
        row = db.execute(
            "SELECT state,snapshot FROM export_jobs WHERE id=%s", (jid,)
        ).fetchone()
        assert row[0] == "queued"
        c["version"] += 1
        c["sources"] = []
        c["provider_secret"] = "PRIVATE SECRET"
        w.save_case(db, c)
    assert submit(client, h, p, c).status_code == 409
    assert client.get(p + "/artifacts/" + jid + "?download=true").status_code == 409
    from folio.exportworker import run_once

    assert run_once(jid)
    meta = client.get(p + "/artifacts/" + jid)
    assert meta.status_code == 200, meta.text
    download = client.get(meta.json()["download_url"])
    assert download.status_code == 200
    raw = download.content
    assert hashlib.sha256(raw).hexdigest() == meta.json()["sha256"]
    body = json.loads(raw)
    assert body["case"]["version"] == 1 and len(body["sources"]) == 1
    assert body["verification"]["status"] == "incomplete"
    assert b"PRIVATE" not in raw and body["release_ready"] is False
    assert (
        raw
        == json.dumps(
            row[1], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    c["version"] = 1
    assert submit(client, h, p, c).json() == {"artifact_job_id": jid}
    assert client.get(meta.json()["download_url"]).content == raw
    assert (
        client.get(
            meta.json()["download_url"], headers={"Range": "bytes=0-9"}
        ).status_code
        == 416
    )


@pytest.mark.parametrize(
    "point", ["before_file", "after_stage", "before_ref", "after_commit"]
)
def test_socket_http_physical_worker_kill_same_job_recovery(point, tmp_path):
    w, c, client, h, p = packet()
    port = 18291
    log = (tmp_path / "api.log").open("w")
    server = subprocess.Popen(
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
        stdout=log,
        stderr=log,
    )
    child = None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as socket:
            deadline = time.monotonic() + 15
            while True:
                try:
                    if socket.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.05)
            auth = socket.post(
                "/api/v1/auth/login",
                json={"username": "auditor", "password": "folio-auditor-local"},
            ).json()
            h["X-CSRF-Token"] = auth["csrf"]
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier

            barrier = Barrier(3)

            def enqueue_contender(_):
                barrier.wait(timeout=5)
                return submit(socket, h, p, c)

            with ThreadPoolExecutor(max_workers=3) as pool:
                accepted = list(pool.map(enqueue_contender, range(3)))
            assert all(item.status_code == 202 for item in accepted)
            assert len({item.json()["artifact_job_id"] for item in accepted}) == 1
            r = accepted[0]
            assert r.status_code == 202, r.text
            jid = r.json()["artifact_job_id"]
            marker = tmp_path / "barrier"
            code = (
                "from folio.exportworker import run_once\nfrom pathlib import Path\nimport time\n"
                f"def checkpoint(point):\n if point == {point!r}:\n"
                f"  Path({marker.as_posix()!r}).write_text(point)\n  time.sleep(90)\n"
                f"run_once({jid!r}, checkpoint)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code], stdout=log, stderr=log
            )
            deadline = time.monotonic() + 15
            while not marker.exists():
                assert child.poll() is None, (tmp_path / "api.log").read_text()
                assert time.monotonic() < deadline
                time.sleep(0.02)
            url = p + "/artifacts/" + jid + "?download=true"
            assert socket.get(url).status_code == (
                200 if point == "after_commit" else 409
            )
            child.kill()
            child.wait(timeout=5)
            with w.connect() as db:
                before = db.execute(
                    "SELECT tries,fence FROM export_jobs WHERE id=%s", (jid,)
                ).fetchone()
                db.execute(
                    "UPDATE export_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                    (jid,),
                )
            result = subprocess.run(
                [sys.executable, "-m", "folio.exportworker", "--once", "--job", jid],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            raw = socket.get(url)
            assert raw.status_code == 200, raw.text
            meta = socket.get(p + "/artifacts/" + jid).json()
            assert hashlib.sha256(raw.content).hexdigest() == meta["sha256"]
            with w.connect() as db:
                after = db.execute(
                    "SELECT tries,fence,state FROM export_jobs WHERE id=%s", (jid,)
                ).fetchone()
                assert after == (
                    (before[0], before[1], "ready")
                    if point == "after_commit"
                    else (before[0] + 1, before[1] + 1, "ready")
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM events WHERE body->>'artifact_job_id'=%s AND kind='DOC-artifact-ready'",
                        (jid,),
                    ).fetchone()[0]
                    == 1
                )
            evidence = Path(os.environ["FOLIO_EXPORT_EVIDENCE"])
            (evidence / ("kill-" + point + ".json")).write_text(
                json.dumps(
                    {
                        "job": jid,
                        "point": point,
                        "child_exit": child.returncode,
                        "before": before,
                        "after": after,
                        "bytes": len(raw.content),
                        "sha256": meta["sha256"],
                    }
                )
            )
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        server.terminate()
        server.wait(timeout=10)
        log.close()


def test_current_auth_roles_two_workspace_isolation_and_issued_link_revocation():
    from folio.exportworker import run_once

    w, c, client, h, p = packet()
    r = submit(client, h, p, c)
    jid = r.json()["artifact_job_id"]
    assert run_once(jid)
    issued = client.get(p + "/artifacts/" + jid).json()["download_url"]
    foreign = str(uuid4())
    foreigncase = str(uuid4())
    from psycopg import sql

    from folio.database_security import admin

    with w.connect() as db:
        database = db.info.dbname
    assert database.startswith("folio_finish_wb_export")
    admin(
        sql.SQL("INSERT INTO cases VALUES({}::uuid,{}::uuid,{}::jsonb)")
        .format(
            sql.Literal(foreign),
            sql.Literal(foreigncase),
            sql.Literal(json.dumps({"id": foreigncase, "version": 1})),
        )
        .as_string(),
        database,
    )
    assert (
        submit(
            client,
            h,
            "/api/v1/workspaces/" + foreign,
            {"id": foreigncase, "version": 1},
        ).status_code
        == 404
    )
    assert (
        client.get("/api/v1/workspaces/" + foreign + "/artifacts/" + jid).status_code
        == 404
    )
    assert client.get(p + "/artifacts/" + str(uuid4())).status_code == 404
    anonymous = TestClient(w.app)
    assert anonymous.get(issued + "&token=forged&ticket=forged").status_code == 401
    with w.connect() as db:
        db.execute("UPDATE users SET roles='[]'::jsonb WHERE username='auditor'")
    try:
        for url in (p + "/artifacts/" + jid, issued):
            assert client.get(url).status_code == 403
            assert client.get(url, headers={"Range": "bytes=0-9"}).status_code == 403
        assert submit(client, h, p, c).status_code == 403
    finally:
        with w.connect() as db:
            db.execute(
                "UPDATE users SET roles='[\"auditor\"]'::jsonb WHERE username='auditor'"
            )


def test_stale_fence_expiry_legal_hold_and_tombstone_keep_frozen_history():
    from folio.exports import root
    from folio.exportworker import claim, expire_one, publish, run_once, stage

    w, c, client, h, p = packet()
    jid = submit(client, h, p, c).json()["artifact_job_id"]
    old = claim(jid)
    stagepath, ref, size = stage(old)
    os.replace(stagepath, root() / ref)
    with w.connect() as db:
        db.execute(
            "UPDATE export_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (jid,),
        )
    assert run_once(jid)
    with pytest.raises(RuntimeError, match="FENCE_OR_LEASE_LOST"):
        publish(old, ref, size)
    with w.connect() as db:
        before = db.execute(
            "SELECT snapshot,snapshot_sha256,artifact_ref FROM export_jobs WHERE id=%s",
            (jid,),
        ).fetchone()
        db.execute(
            "UPDATE export_jobs SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (jid,),
        )
        c["retention"] = {"legal_hold": True}
        w.save_case(db, c)
    assert client.get(p + "/artifacts/" + jid).status_code == 410
    assert not expire_one(jid)
    assert (root() / before[2]).exists()
    with w.connect() as db:
        c["retention"]["legal_hold"] = False
        w.save_case(db, c)
    assert expire_one(jid)
    assert expire_one(jid)
    assert not (root() / before[2]).exists()
    assert not list(root().glob(jid + ".*"))
    assert client.get(p + "/artifacts/" + jid + "?download=true").status_code == 410
    assert submit(client, h, p, c).json() == {"artifact_job_id": jid}
    with w.connect() as db:
        after = db.execute(
            "SELECT snapshot,snapshot_sha256,state FROM export_jobs WHERE id=%s", (jid,)
        ).fetchone()
        assert after == (before[0], before[1], "tombstoned")
        assert (
            db.execute(
                "SELECT count(*) FROM commands WHERE key=%s", (h["Idempotency-Key"],)
            ).fetchone()[0]
            == 1
        )


def test_artifact_corruption_is_not_served_and_current_session_revoked():
    from folio.exports import root
    from folio.exportworker import run_once

    w, c, client, h, p = packet()
    jid = submit(client, h, p, c).json()["artifact_job_id"]
    assert run_once(jid)
    with w.connect() as db:
        ref = db.execute(
            "SELECT artifact_ref FROM export_jobs WHERE id=%s", (jid,)
        ).fetchone()[0]
    (root() / ref).write_bytes(b"not the immutable snapshot")
    assert client.get(p + "/artifacts/" + jid + "?download=true").status_code == 503
    with w.connect() as db:
        db.execute("DELETE FROM sessions WHERE username='auditor'")
    assert client.get(p + "/artifacts/" + jid).status_code == 401


def test_additive_migration_grants_and_nonowner_snapshot_immutability(monkeypatch):
    import secrets

    import psycopg
    from psycopg import sql

    from folio.database_security import admin

    w, c, client, h, p = packet()
    jid = submit(client, h, p, c).json()["artifact_job_id"]
    role = "folio_export_" + uuid4().hex
    password = secrets.token_urlsafe(32)
    # New disposable principal only, never retained credentials or grants.
    admin(
        sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        )
        .format(sql.Identifier(role), sql.Literal(password))
        .as_string()
    )
    from scripts.export_migrate import apply

    with w.connect() as db:
        before = db.execute(
            "SELECT body FROM cases WHERE id=%s", (c["id"],)
        ).fetchone()[0]
        apply(db, role)
        apply(db, role)
        from psycopg import sql

        name = sql.Identifier(role)
        # Existing v2 runtime grants, followed by the v3-only additive grants above.
        db.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE ON cases,proposals TO {}").format(name)
        )
        db.execute(sql.SQL("GRANT SELECT ON users TO {}").format(name))

        db.execute(sql.SQL("GRANT SELECT,INSERT,DELETE ON sessions TO {}").format(name))
        db.execute(sql.SQL("GRANT SELECT,INSERT ON commands,events TO {}").format(name))
        db.execute(
            sql.SQL(
                "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(name)
        )
        assert (
            db.execute("SELECT body FROM cases WHERE id=%s", (c["id"],)).fetchone()[0]
            == before
        )
        assert db.execute(
            "SELECT version FROM schema_versions WHERE version=3"
        ).fetchone() == (3,)
        info = db.info
        dsn = psycopg.conninfo.make_conninfo(
            host=info.host,
            port=info.port,
            dbname=info.dbname,
            user=role,
            password=password,
        )
    with psycopg.connect(dsn) as db:
        assert db.execute(
            "SELECT snapshot FROM export_jobs WHERE id=%s", (jid,)
        ).fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute(
                    "UPDATE export_jobs SET snapshot='{}'::jsonb WHERE id=%s", (jid,)
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute("DELETE FROM export_jobs WHERE id=%s", (jid,))
        assert db.execute(
            "SELECT has_table_privilege(current_user,'export_jobs','DELETE')"
        ).fetchone() == (False,)
        assert db.execute(
            "SELECT count(*) FROM export_jobs WHERE workspace<>%s", (w.WORKSPACE,)
        ).fetchone() == (0,)
    with monkeypatch.context() as m:
        m.setattr(w, "DSN", dsn)
        assert submit(client, h, p, c).json() == {"artifact_job_id": jid}
        from folio.exportworker import run_once

        assert run_once(jid)
        assert client.get(p + "/artifacts/" + jid + "?download=true").status_code == 200


def test_payload_with_deep_or_large_metadata_is_bounded_before_hashing():
    from fastapi import HTTPException

    from folio.exports import snapshot

    w, c, client, h, p = packet()
    c["sources"][0]["pages"][0]["text"] = "x" * 1048577
    with pytest.raises(HTTPException, match="EXPORT_INPUT_TOO_LARGE"):
        snapshot(c, None)


def test_contended_claim_does_not_block_on_unrelated_exhaustion_row():
    from folio.exportworker import claim

    w, c, client, h, p = packet()
    stuck = submit(client, h, p, c).json()["artifact_job_id"]
    ready = submit(client, {**h, "Idempotency-Key": str(uuid4())}, p, c).json()[
        "artifact_job_id"
    ]
    with w.connect() as setup:
        setup.execute(
            "UPDATE export_jobs SET state='leased',tries=5,lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (stuck,),
        )
    with w.connect() as locked:
        locked.execute("SELECT 1 FROM export_jobs WHERE id=%s FOR UPDATE", (stuck,))
        started = time.monotonic()
        assert claim(ready)["id"] == ready
        assert time.monotonic() - started < 1


@pytest.mark.parametrize("role", ["reviewer", "approver", "auditor", "viewer", "admin"])
def test_export_role_csrf_profile_and_originals_policy(role):
    w, c, client, h, p = packet()
    auth = client.post(
        "/api/v1/auth/login", json={"username": role, "password": f"folio-{role}-local"}
    ).json()
    h["X-CSRF-Token"] = auth["csrf"]
    allowed = role in {"reviewer", "approver", "auditor"}
    assert submit(client, h, p, c, include_originals=True).status_code == 403
    assert submit(client, {**h, "X-CSRF-Token": "forged"}, p, c).status_code == 403
    assert (
        submit(client, {**h, "Origin": "https://untrusted.invalid"}, p, c).status_code
        == 403
    )
    assert submit(client, h, p, c, redaction_profile="all-raw-fields").status_code == (
        422 if allowed else 403
    )
    accepted = submit(client, h, p, c)
    assert accepted.status_code == (202 if allowed else 403)
    if allowed:
        other = TestClient(w.app)
        other.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "folio-viewer-local"},
        )
        assert (
            other.get(
                p + "/artifacts/" + accepted.json()["artifact_job_id"]
            ).status_code
            == 404
        )


def test_proposal_verifier_and_field_provenance_exact_digest_versions_are_frozen():
    from folio.exports import sha
    from folio.exportworker import run_once

    w, c, client, h, p = packet()
    pid = str(uuid4())
    # Inactive, authored metadata scaffold: no receipt reservation or authority.
    proposal = {
        "id": pid,
        "version": 3,
        "state": "superseded",
        "gate": None,
        "grant": {"secret": "PRIVATE GRANT"},
    }
    verification = {
        "version": 4,
        "verifier": "independent-v1",
        "outcome": "matched",
        "observation": {"authorization": "PRIVATE AUTH"},
    }
    c["proposal"] = {"id": pid}
    c["verification"] = verification
    entry = {
        "version": 2,
        "kind": "human",
        "value": "PRIVATE VALUE",
        "source": {"source_sha256": "a" * 64, "quote": "PRIVATE QUOTE"},
    }
    c["field_provenance"] = {"received_quantity": [entry]}
    with w.connect() as db:
        w.save_case(db, c)
        db.execute(
            "INSERT INTO proposals VALUES(%s,%s,%s,%s)",
            (w.WORKSPACE, pid, c["id"], Jsonb(proposal)),
        )
    jid = submit(client, h, p, c).json()["artifact_job_id"]
    with w.connect() as db:
        db.execute(
            "UPDATE proposals SET body=jsonb_set(body,'{version}','99') WHERE id=%s",
            (pid,),
        )
        c["verification"] = None
        c["version"] += 1
        w.save_case(db, c)
    assert run_once(jid)
    r = client.get(p + "/artifacts/" + jid + "?download=true")
    assert r.status_code == 200
    body = r.json()
    assert body["proposal"] == {
        "id": pid,
        "version": 3,
        "snapshot_sha256": sha(proposal),
    }
    assert body["verification"]["version"] == 4
    assert body["verification"]["snapshot_sha256"] == sha(verification)
    assert body["verification"]["status"] == "recorded-not-reverified"
    assert body["field_provenance"]["received_quantity"][0]["entry_sha256"] == sha(
        entry
    )
    assert b"PRIVATE" not in r.content


def test_version_two_restored_fixture_additive_upgrade_preserves_all_rows():
    import psycopg
    from psycopg import sql

    from folio.database_security import admin
    from scripts.export_migrate import apply

    w, c, client, h, p = packet()
    database = "folio_export_upgrade_" + uuid4().hex
    with w.connect() as db:
        owner = db.info.user
    admin(
        sql.SQL("CREATE DATABASE {} OWNER {}")
        .format(sql.Identifier(database), sql.Identifier(owner))
        .as_string()
    )
    dsn = psycopg.conninfo.make_conninfo(w.DSN, dbname=database)
    migration = Path(__file__).resolve().parents[1] / "ops/migrations/002-workbench.sql"
    with psycopg.connect(dsn) as db:
        db.execute(migration.read_text())
        from folio.schema_journal import journal_sql

        db.execute(journal_sql(2, migration.read_bytes()))
        db.execute(
            "INSERT INTO cases VALUES(%s,%s,%s)", (w.WORKSPACE, c["id"], Jsonb(c))
        )
        key = str(uuid4())
        db.execute(
            "INSERT INTO commands VALUES(%s,%s,%s,%s,%s,%s)",
            (
                w.WORKSPACE,
                "authored",
                "/retained-replay-tombstone",
                key,
                "a" * 64,
                Jsonb({"result": "frozen"}),
            ),
        )
        tables = [
            "users",
            "sessions",
            "cases",
            "proposals",
            "jobs",
            "commands",
            "originals",
            "analysis_runs",
            "events",
        ]

        def bodies():
            return {
                table: db.execute(
                    sql.SQL(
                        "SELECT row_to_json(t) FROM {} t ORDER BY row_to_json(t)::text"
                    ).format(sql.Identifier(table))
                ).fetchall()
                for table in tables
            }

        before = bodies()
        role = psycopg.conninfo.conninfo_to_dict(os.environ["FOLIO_TARGET_READ_DSN"])[
            "user"
        ]
        apply(db, role)
        apply(db, role)
        assert bodies() == before
        assert db.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall() == [(2,), (3,), (4,)]
        assert db.execute("SELECT count(*) FROM export_jobs").fetchone() == (0,)
    evidence = Path(os.environ["FOLIO_EXPORT_EVIDENCE"])
    (evidence / "version-two-upgrade.json").write_text(
        json.dumps(
            {
                "database": database,
                "versions": [2, 3, 4],
                "unchanged_tables": tables,
                "case_digest": w.digest(c),
                "command_key": key,
                "applied_twice": True,
            }
        )
    )
