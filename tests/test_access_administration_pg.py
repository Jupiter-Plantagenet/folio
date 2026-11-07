"""Disposable PostgreSQL proof for access workflow and runtime grants.

Set FOLIO_ACCESS_SCRATCH_OWNER_DSN and FOLIO_ACCESS_SCRATCH_RUNTIME_DSN only for
an owner/runtime pair whose database name starts with ``folio_access_scratch_``.
The test refuses any other database name and never accepts the application's usual
FOLIO_DSN as a substitute.
"""

import hashlib
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from folio import workbench
from folio.database_security import (
    access_administration_grants,
    actor_lock_grants,
    export_grants,
)
from folio.schema_journal import check_runtime


def _login(client, username, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": workbench.ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def _signed_headers(session, key, if_match=None):
    headers = {
        "Origin": workbench.ORIGIN,
        "X-CSRF-Token": session["csrf"],
        "Idempotency-Key": key,
    }
    if if_match:
        headers["If-Match"] = if_match
    return headers


def _seed_account(db, username, roles, password):
    salt = "scratch-access-" + username
    password_hash = hashlib.scrypt(
        password.encode(), salt=salt.encode(), n=16384, r=8, p=1
    ).hex()
    db.execute(
        "INSERT INTO users(username,salt,password_hash,roles,active) VALUES(%s,%s,%s,%s,true) "
        "ON CONFLICT(username) DO UPDATE SET salt=excluded.salt,password_hash=excluded.password_hash,roles=excluded.roles,active=true",
        (username, salt, password_hash, Jsonb(roles)),
    )


def test_pg_role_workflow_receipts_session_revocation_and_narrow_runtime_grants(monkeypatch):
    owner_dsn = os.getenv("FOLIO_ACCESS_SCRATCH_OWNER_DSN")
    runtime_dsn = os.getenv("FOLIO_ACCESS_SCRATCH_RUNTIME_DSN")
    if not owner_dsn or not runtime_dsn:
        pytest.skip("explicit disposable access scratch owner/runtime DSNs were not supplied")
    owner_info = conninfo_to_dict(owner_dsn)
    runtime_info = conninfo_to_dict(runtime_dsn)
    database = owner_info.get("dbname") or owner_info.get("database")
    runtime_database = runtime_info.get("dbname") or runtime_info.get("database")
    if not database or not database.startswith("folio_access_scratch_") or runtime_database != database:
        pytest.fail("scratch test requires matching folio_access_scratch_* owner/runtime databases")

    monkeypatch.setattr(workbench, "DSN", owner_dsn)
    monkeypatch.delenv("FOLIO_SCHEMA_MODE", raising=False)
    workbench.migrate()

    with psycopg.connect(owner_dsn) as owner:
        runtime_role = owner.execute("SELECT %s", (runtime_info.get("user"),)).fetchone()[0]
        assert runtime_role
        owner.execute(export_grants(runtime_role))
        owner.execute(actor_lock_grants(runtime_role))
        owner.execute(access_administration_grants(runtime_role))
        _seed_account(owner, "access-admin-b", ["approver"], "scratch-approver-password")
        _seed_account(owner, "access-admin-c", ["admin"], "scratch-independent-admin-password")
        _seed_account(owner, "access-review-target", ["reviewer"], "scratch-target-password")

    with psycopg.connect(runtime_dsn) as runtime:
        check_runtime(runtime)
        assert runtime.execute(
            "SELECT has_column_privilege(current_user,'users','roles','UPDATE'),"
            "has_column_privilege(current_user,'users','active','UPDATE'),"
            "has_column_privilege(current_user,'users','password_hash','UPDATE'),"
            "has_table_privilege(current_user,'users','INSERT'),"
            "has_table_privilege(current_user,'users','UPDATE'),"
            "has_table_privilege(current_user,'folio_access_requests','INSERT'),"
            "has_table_privilege(current_user,'folio_access_requests','UPDATE')"
        ).fetchone() == (False, False, False, False, False, False, False)
        assert runtime.execute(
            "SELECT has_table_privilege(current_user,'users','SELECT'),"
            "has_table_privilege(current_user,'folio_access_requests','SELECT'),"
            "has_function_privilege(current_user,'folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid)','EXECUTE'),"
            "has_function_privilege(current_user,'folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)','EXECUTE'),"
            "has_function_privilege(current_user,'folio_access_admin_v1.authenticate(text,text)','EXECUTE')"
        ).fetchone() == (True, True, True, True, False)
        assert runtime.execute(
            "SELECT p.prosecdef,r.rolsuper,r.rolbypassrls,p.proconfig "
            "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
            "WHERE p.oid='folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)'::regprocedure"
        ).fetchone() == (True, False, False, ["search_path=pg_catalog, pg_temp", "lock_timeout=5s"])
        assert runtime.execute(
            "SELECT count(*)=0 FROM pg_proc p, LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a "
            "WHERE p.oid IN ('folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid)'::regprocedure,"
            "'folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text)'::regprocedure) "
            "AND a.grantee=0 AND a.privilege_type='EXECUTE'"
        ).fetchone()[0]
        assert runtime.execute(
            "SELECT folio_access_admin_v1.authorize_mutation(%s,%s,%s,%s,%s)",
            ("not-a-session-secret", "not-a-csrf-token", workbench.WORKSPACE, "create", None),
        ).fetchone()[0] == {"error": "SESSION_REQUIRED", "status": 401}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.transaction():
                runtime.execute("UPDATE users SET roles=roles WHERE false")
        assert runtime.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='folio_access_requests'::regclass"
        ).fetchone() == (True, True)

    # Startup must reject any effective DML grant on account/access tables,
    # including unexpected columns and inherited/table-wide permissions.
    drift_grants = (
        ("GRANT UPDATE(username) ON public.users TO {}", "REVOKE UPDATE(username) ON public.users FROM {}"),
        ("GRANT DELETE ON public.users TO {}", "REVOKE DELETE ON public.users FROM {}"),
        ("GRANT INSERT ON public.folio_access_requests TO {}", "REVOKE INSERT ON public.folio_access_requests FROM {}"),
    )
    for grant_template, revoke_template in drift_grants:
        with psycopg.connect(owner_dsn) as owner:
            owner.execute(sql.SQL(grant_template).format(sql.Identifier(runtime_role)))
        with psycopg.connect(runtime_dsn) as runtime:
            with pytest.raises(RuntimeError, match="^ACCESS_ADMIN_PRIVILEGE_DRIFT$"):
                check_runtime(runtime)
        with psycopg.connect(owner_dsn) as owner:
            owner.execute(sql.SQL(revoke_template).format(sql.Identifier(runtime_role)))
        with psycopg.connect(runtime_dsn) as runtime:
            check_runtime(runtime)

    monkeypatch.setattr(workbench, "DSN", runtime_dsn)
    monkeypatch.setenv("FOLIO_SCHEMA_MODE", "runtime")
    with TestClient(workbench.app) as admin_client, TestClient(workbench.app) as reviewer_client, TestClient(workbench.app) as independent_admin_client, TestClient(workbench.app) as target_client:
        admin = _login(admin_client, "admin", "folio-admin-local")
        independent_approver = _login(reviewer_client, "access-admin-b", "scratch-approver-password")
        independent_admin = _login(independent_admin_client, "access-admin-c", "scratch-independent-admin-password")
        target_session = _login(target_client, "access-review-target", "scratch-target-password")
        workspace = workbench.WORKSPACE
        approver_cannot_request = reviewer_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests",
            json={
                "target_username": "access-review-target",
                "action": "grant_role",
                "role": "admin",
                "reason": "Approvers cannot request account changes.",
            },
            headers=_signed_headers(independent_approver, str(uuid4()), '"access-v1"'),
        )
        assert approver_cannot_request.status_code == 403
        assert approver_cannot_request.json()["detail"] == "PERMISSION_DENIED"
        settings = admin_client.get(
            f"/api/v1/workspaces/{workspace}/settings",
            headers={"Origin": workbench.ORIGIN},
        )
        assert settings.status_code == 200
        version = settings.json()["settings_version"]
        self_grant = admin_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests",
            json={
                "target_username": "admin",
                "action": "grant_role",
                "role": "approver",
                "reason": "Must be refused as self grant.",
            },
            headers=_signed_headers(admin, str(uuid4()), f'"access-v{version}"'),
        )
        assert self_grant.status_code == 403
        request_body = {
            "target_username": "access-review-target",
            "action": "grant_role",
            "role": "approver",
            "reason": "Scratch independent-review test.",
        }
        command_key = str(uuid4())
        request_headers = _signed_headers(admin, command_key, f'"access-v{version}"')
        first = admin_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests",
            json=request_body,
            headers=request_headers,
        )
        assert first.status_code == 200
        created = first.json()
        replay = admin_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests",
            json=request_body,
            headers=request_headers,
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == created["id"]
        assert replay.json()["status"] == "pending"
        receipt = admin_client.get(
            f"/api/v1/auth/mutation-receipts/{command_key}",
            headers={"Origin": workbench.ORIGIN},
            params={"operation_hash": workbench.command_route_fingerprint(
                "POST", f"/api/v1/workspaces/{workspace}/settings/access-requests")},
        )
        assert receipt.status_code == 200
        assert receipt.json() == {"status": "committed"}
        foreign_receipt = reviewer_client.get(
            f"/api/v1/auth/mutation-receipts/{command_key}",
            headers={"Origin": workbench.ORIGIN},
            params={"operation_hash": workbench.command_route_fingerprint(
                "POST", f"/api/v1/workspaces/{workspace}/settings/access-requests")},
        )
        assert foreign_receipt.status_code == 200
        assert foreign_receipt.json() == {"status": "not_recorded"}
        wrong_route_receipt = admin_client.get(
            f"/api/v1/auth/mutation-receipts/{command_key}",
            headers={"Origin": workbench.ORIGIN},
            params={"operation_hash": workbench.command_route_fingerprint("POST", "/api/v1/auth/logout")},
        )
        assert wrong_route_receipt.status_code == 200
        assert wrong_route_receipt.json() == {"status": "not_recorded"}

        admin_only_review = independent_admin_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests/{created['id']}/review",
            json={"decision": "approve", "reason": "Admin alone cannot review an approver grant."},
            headers=_signed_headers(
                independent_admin,
                str(uuid4()),
                f'"access-request-v{created["version"]}"',
            ),
        )
        assert admin_only_review.status_code == 403
        assert admin_only_review.json()["detail"] == "PERMISSION_DENIED"
        target_review = target_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests/{created['id']}/review",
            json={"decision": "approve", "reason": "Target cannot review its own request."},
            headers=_signed_headers(
                target_session,
                str(uuid4()),
                f'"access-request-v{created["version"]}"',
            ),
        )
        assert target_review.status_code == 403
        assert target_review.json()["detail"] == "INDEPENDENT_REVIEW_REQUIRED"
        reviewer_settings = reviewer_client.get(
            f"/api/v1/workspaces/{workspace}/settings",
            headers={"Origin": workbench.ORIGIN},
        )
        assert reviewer_settings.status_code == 200
        assert [row["username"] for row in reviewer_settings.json()["members"]] == ["access-admin-b"]
        assert created["id"] in [row["id"] for row in reviewer_settings.json()["requests"]]
        approved = reviewer_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests/{created['id']}/review",
            json={"decision": "approve", "reason": "Independent scratch approval."},
            headers=_signed_headers(
                independent_approver,
                str(uuid4()),
                f'"access-request-v{created["version"]}"',
            ),
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert target_client.get("/api/v1/auth/session").status_code == 401

        latest_settings = admin_client.get(
            f"/api/v1/workspaces/{workspace}/settings",
            headers={"Origin": workbench.ORIGIN},
        ).json()
        admin_grant = admin_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests",
            json={
                "target_username": "access-review-target",
                "action": "grant_role",
                "role": "admin",
                "reason": "Test admin-only account review.",
            },
            headers=_signed_headers(
                admin,
                str(uuid4()),
                f'"access-v{latest_settings["settings_version"]}"',
            ),
        )
        assert admin_grant.status_code == 200
        approver_cannot_review_admin = reviewer_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests/{admin_grant.json()['id']}/review",
            json={"decision": "deny", "reason": "Approver cannot review admin grants."},
            headers=_signed_headers(
                independent_approver,
                str(uuid4()),
                f'"access-request-v{admin_grant.json()["version"]}"',
            ),
        )
        assert approver_cannot_review_admin.status_code == 403
        assert approver_cannot_review_admin.json()["detail"] == "PERMISSION_DENIED"
        admin_review = independent_admin_client.post(
            f"/api/v1/workspaces/{workspace}/settings/access-requests/{admin_grant.json()['id']}/review",
            json={"decision": "deny", "reason": "Independent admin review."},
            headers=_signed_headers(
                independent_admin,
                str(uuid4()),
                f'"access-request-v{admin_grant.json()["version"]}"',
            ),
        )
        assert admin_review.status_code == 200
        assert admin_review.json()["status"] == "denied"

    with psycopg.connect(owner_dsn) as owner:
        roles = owner.execute(
            "SELECT roles FROM users WHERE username='access-review-target'"
        ).fetchone()[0]
        assert roles == ["approver", "reviewer"]
        audit = owner.execute(
            "SELECT count(*) FROM events WHERE workspace=%s AND kind IN ('DOC-access-requested','DOC-access-reviewed')",
            (workbench.WORKSPACE,),
        ).fetchone()[0]
        assert audit >= 2
