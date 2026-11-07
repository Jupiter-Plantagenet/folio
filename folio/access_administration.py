"""Workspace account and role administration with independently reviewed changes."""

from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

ROLES = frozenset({"reviewer", "approver", "viewer", "auditor", "admin"})
ADMIN_REVIEW_ACTIONS = frozenset({"grant_role", "revoke_role", "activate", "deactivate"})


def require(condition, code, status=409):
    if not condition:
        raise HTTPException(status, code)


def etag_matches(header, version, kind="access"):
    require(header == f'"{kind}-v{version}"', "STALE_VERSION", 412)


def normalized_roles(value):
    require(type(value) is list and all(type(role) is str and role in ROLES for role in value), "INVALID_ROLE", 422)
    return sorted(set(value))


def last_admin_after(target, next_roles, next_active, admin_rows):
    """Reject any transition that would leave no active privileged administrator."""
    admins = [
        (username, active, roles)
        for username, active, roles in admin_rows
        if username != target and active and "admin" in roles
    ]
    if next_active and "admin" in next_roles:
        admins.append((target, next_active, next_roles))
    return bool(admins)


def decision_allowed(requester, target, reviewer, roles, action, role):
    if reviewer == requester or reviewer == target:
        return False
    if action == "grant_role" and role == "approver":
        return "approver" in roles
    return "admin" in roles


def settings_snapshot(db, workspace, actor, actor_roles):
    access = db.execute(
        "SELECT settings_version,policy_version,auth_epoch FROM folio_workspace_access WHERE workspace=%s",
        (workspace,),
    ).fetchone()
    require(bool(access), "ACCESS_SCHEMA_REQUIRED", 503)
    output = {
        "settings_version": access[0],
        "policy_version": access[1],
        "auth_epoch": access[2],
        "members": [],
        "requests": [],
    }
    # Only administrators can enumerate accounts or access requests. Others receive
    # their own server-derived identity from the existing /auth/session endpoint.
    admin = "admin" in actor_roles
    if admin:
        output["members"] = [
            {"username": row[0], "roles": row[1], "active": row[2]}
            for row in db.execute(
                "SELECT username,roles,active FROM users ORDER BY username"
            ).fetchall()
        ]
        request_rows = db.execute(
            "SELECT id,requested_by,target_username,action,role,reason,status,version,"
            "created_at,reviewed_by,reviewed_at,review_reason FROM folio_access_requests "
            "WHERE workspace=%s ORDER BY created_at DESC,id LIMIT 100",
            (workspace,),
        ).fetchall()
        output["requests"] = [access_request_json(row) for row in request_rows]
    else:
        row = db.execute("SELECT username,roles,active FROM users WHERE username=%s", (actor,)).fetchone()
        if row:
            output["members"] = [{"username": row[0], "roles": row[1], "active": row[2]}]
        if "approver" in actor_roles:
            request_rows = db.execute(
                "SELECT id,requested_by,target_username,action,role,reason,status,version,"
                "created_at,reviewed_by,reviewed_at,review_reason FROM folio_access_requests "
                "WHERE workspace=%s AND action='grant_role' AND role='approver' "
                "ORDER BY created_at DESC,id LIMIT 100",
                (workspace,),
            ).fetchall()
            output["requests"] = [access_request_json(row) for row in request_rows]
    return output


def access_request_json(row):
    return {
        "id": str(row[0]), "requested_by": row[1], "target_username": row[2],
        "action": row[3], "role": row[4], "reason": row[5], "status": row[6],
        "version": row[7], "created_at": row[8].isoformat(), "reviewed_by": row[9],
        "reviewed_at": row[10].isoformat() if row[10] else None,
        "review_reason": row[11],
    }


def _access_function_result(db, function, args):
    result = db.execute(f"SELECT folio_access_admin_v1.{function}({','.join(['%s'] * len(args))})", args).fetchone()[0]
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(int(result.get("status", 409)), result["error"])
    require(isinstance(result, dict), "ACCESS_ADMIN_FUNCTION_REQUIRED", 503)
    return result


def authorize_mutation(db, request, workspace, operation, request_id=None):
    auth = _access_function_result(
        db,
        "authorize_mutation",
        (
            request.cookies.get("folio_session", ""),
            request.headers.get("x-csrf-token", ""),
            workspace,
            operation,
            request_id,
        ),
    )
    auth["csrf"] = request.headers.get("x-csrf-token", "")
    auth["workspace"] = workspace
    return auth


def create_request(db, request, workspace, body, if_match):
    request_id = uuid4()
    return _access_function_result(
        db,
        "apply_mutation",
        (
            request.cookies.get("folio_session", ""),
            request.headers.get("x-csrf-token", ""),
            workspace,
            "create",
            request_id,
            Jsonb(body),
            if_match,
        ),
    )


def review_request(db, request, workspace, request_id, if_match, body):
    return _access_function_result(
        db,
        "apply_mutation",
        (
            request.cookies.get("folio_session", ""),
            request.headers.get("x-csrf-token", ""),
            workspace,
            "review",
            request_id,
            Jsonb(body),
            if_match,
        ),
    )


def lookup_command_status(db, workspace, actor, idempotency_key, operation_hash):
    """Look up only the exact method/route command represented by the blocker."""
    require(
        type(operation_hash) is str
        and len(operation_hash) == 64
        and all(char in "0123456789abcdef" for char in operation_hash),
        "NOT_FOUND",
        404,
    )
    row = db.execute(
        "SELECT 1 FROM commands WHERE workspace=%s AND actor=%s AND route=%s AND key=%s LIMIT 1",
        (workspace, actor, operation_hash, idempotency_key),
    ).fetchone()
    return {"status": "committed" if row else "not_recorded"}
