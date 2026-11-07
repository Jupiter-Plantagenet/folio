"""DOC-T-AUTH: generated roles x routes x workspace x CSRF matrix on real PostgreSQL.

Run only through scripts/isolated_tests.py (fresh folio_finish_* databases and roles,
bootstrap dev accounts, provisioned workload identities, isolated Northstar target).

The cells are generated from the live route tables (``app.routes``/``app.openapi()``);
any route missing from the expectation tables below fails the suite, so a new route
cannot silently escape. Expected outcomes come from the blueprint role table
(documents-blueprint.md §5) plus the code's own check order:
``identity(req, role)`` -> 401 SESSION_REQUIRED / 403 PERMISSION_DENIED / 403
CSRF_DENIED, then ``scope()`` -> 404 NOT_FOUND_OR_FORBIDDEN, then route permission
(exports ``authorized()``, original download reviewer/approver), then object lookup.

Every denied cell must return the expected status and code with zero effect on every
workbench (or target) table; every allowed cell must get past authentication,
membership and object scope (a 409/412/422 business result counts as authorized).
No response may contain the foreign tenant's marker. Evidence: one JSON record per
cell in evidence/documents/auth/round6-<UTC stamp>/matrix.json plus summary.json.
"""

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
EVIDENCE = ROOT / "evidence/documents/auth" / f"round6-{STAMP}"
WB = "/api/v1/workspaces/{workspace}"
MEMBERS = frozenset({"viewer", "reviewer", "approver", "admin", "auditor"})
SESSIONLESS = ("anonymous", "expired_reviewer", "inactive_reviewer")
PRINCIPALS = (
    "anonymous",
    "viewer",
    "reviewer",
    "approver",
    "admin",
    "auditor",
    "expired_reviewer",
    "inactive_reviewer",
)
CSRF = ("valid", "absent_token", "wrong_origin")
BAD_ORIGIN = "https://untrusted.invalid"
STALE_CASE = '"case-v0"'
STALE_PROPOSAL = '"proposal-v0"'
FORGED = (
    "random",
    "malformed",
    "foreign_object",
    "own_parent_foreign_child",
    "own_parent_malformed_child",
)
RESULTS = {"workbench": [], "target": [], "scenario": [], "probes": {}}
DESIGN_NOTES = [
    {
        "id": "AUTH-NOTE-1-login-origin",
        "route": "POST /api/v1/auth/login",
        "observation": "Login accepts a missing or foreign Origin (pre-session route, no CSRF token yet).",
        "assessment": "Blueprint §5 names auth routes the documented nonworkspace exception and asks for a CSRF bootstrap. Login CSRF is not reachable cross-site today: FastAPI only parses application/json bodies for the dict parameter (simple text/plain posts fail 422) and no CORS policy admits a JSON preflight. Design question, behaviour unchanged.",
    },
    {
        "id": "AUTH-NOTE-2-public-contract",
        "route": "GET /openapi.json /docs /redoc /health",
        "observation": "OpenAPI/docs and /health answer without a session.",
        "assessment": "Contract and readiness only (no tenant rows); /health exposes an aggregate pending-job count. Local single-host deployment; restrict at the reverse proxy before any multi-user exposure.",
    },
    {
        "id": "AUTH-NOTE-3-artifact-owner-only",
        "route": "GET /api/v1/workspaces/{workspace}/artifacts/{aid}",
        "observation": "Export artifacts are readable only by the actor who enqueued them; an auditor gets 404 for a reviewer's artifact.",
        "assessment": "Stricter than the blueprint auditor row ('read ... redacted export'); never an unauthorized read. Product decision whether auditors should see others' redacted exports.",
    },
    {
        "id": "AUTH-NOTE-4-role-before-scope",
        "route": "all role-gated workspace routes",
        "observation": "identity(req, role) runs before scope(), so a member lacking the role gets 403 PERMISSION_DENIED even for a foreign workspace (404 only once the role passes).",
        "assessment": "No tenant information is revealed (the answer depends only on the caller's own roles); recorded as the code convention the matrix expects.",
    },
    {
        "id": "AUTH-NOTE-5-operator-global",
        "route": "GET /operator/records (Northstar target)",
        "observation": "The target operator session lists payables of every workspace.",
        "assessment": "Blueprint §5 'Target operator: independent target app permissions/session'; the synthetic Northstar target has one global read-only operator. Not a workbench tenant boundary; recorded, unchanged.",
    },
    {
        "id": "AUTH-NOTE-6-role-grants",
        "route": "POST /api/v1/workspaces/{workspace}/settings/access-requests[/.../review]",
        "observation": "Account changes use a session-bound, fixed SECURITY DEFINER capability; the workbench runtime role has no direct users or access-table writes.",
        "assessment": "Admin requests changes. A distinct approver may review only approver-role grants; other account decisions require an admin reviewer. The runtime caller cannot assert actor or role strings as authority.",
    },
]


def roles(*names):
    return frozenset(names)


def public(why):
    return {"kind": "public", "why": why}


def ws_route(
    allowed,
    *,
    params=(),
    body=None,
    if_match=None,
    stage="pre",
    code="PERMISSION_DENIED",
    owner_only=False,
    queries=("",),
    effect=False,
    why="",
):
    return {
        "kind": "workspace",
        "allowed": allowed,
        "params": params,
        "body": body,
        "if_match": if_match,
        "stage": stage,
        "code": code,
        "owner_only": owner_only,
        "queries": queries,
        "effect": effect,
        "why": why,
    }


CASE = (("cid", "case"),)
READ = "Blueprint §5: viewer permitted case/source/status reads, auditor authorized history; every member reads"
WORKBENCH_ROUTES = {
    ("GET", "/openapi.json"): public(
        "API contract (blueprint §6 versioned OpenAPI); no tenant data"
    ),
    ("GET", "/docs"): public("Interactive API docs page; no tenant data"),
    ("GET", "/docs/oauth2-redirect"): public(
        "FastAPI docs helper page; no tenant data"
    ),
    ("GET", "/redoc"): public("API docs page; no tenant data"),
    ("GET", "/health"): public("Readiness; aggregate counts only"),
    ("POST", "/api/v1/auth/login"): {
        "kind": "login",
        "why": "Blueprint §5: auth routes are the documented nonworkspace pre-session exception",
    },
    ("GET", "/api/v1/auth/session"): {
        "kind": "session",
        "why": "Session bootstrap; any live session",
    },
    ("POST", "/api/v1/auth/logout"): {
        "kind": "logout",
        "why": "Session + CSRF (state-changing)",
    },
    ("GET", "/api/v1/auth/mutation-receipts/{idempotency_key}"): {
        "kind": "receipt",
        "query": "?operation_hash=" + "0" * 64,
        "why": "Receipt lookup is bound to current authenticated actor and exact operation hash",
    },
    ("GET", WB + "/settings"): ws_route(MEMBERS, why="Workspace settings self view; admin directory, approver grant-review subset"),
    ("POST", WB + "/settings/access-requests"): ws_route(
        roles("admin"),
        body={"target_username": "matrix-access-target", "action": "grant_role", "role": "reviewer", "reason": "auth matrix"},
        if_match='"access-v0"',
        stage="pre",
        why="Admin only: independently reviewed account-change request",
    ),
    ("POST", WB + "/settings/access-requests/{request_id}/review"): {
        "kind": "access_review",
        "params": (("request_id", "access_request"),),
        "body": {"decision": "approve", "reason": "auth matrix"},
        "why": "Approver only for approver-role grants; otherwise admin; never requester/target",
    },
    ("GET", WB + "/cases"): ws_route(MEMBERS, why=READ),
    ("GET", WB + "/cases/{cid}"): ws_route(MEMBERS, params=CASE, why=READ),
    ("POST", WB + "/cases/{cid}/target-match"): ws_route(
        roles("reviewer"),
        params=CASE,
        body={},
        if_match=STALE_CASE,
        why="Reviewer: decision/proposal preparation",
    ),
    ("POST", WB + "/cases/{cid}/analysis-runs"): ws_route(
        roles("reviewer"),
        params=CASE,
        body={},
        if_match=STALE_CASE,
        why="Reviewer: extraction run",
    ),
    ("GET", WB + "/cases/{cid}/analysis-runs/{rid}"): ws_route(
        MEMBERS, params=(("cid", "case"), ("rid", "run")), why=READ
    ),
    ("POST", WB + "/cases/{cid}/information-requests"): ws_route(
        roles("reviewer"),
        params=CASE,
        body={"fields": ["received_quantity"], "reason": "auth matrix"},
        if_match=STALE_CASE,
        why="Reviewer: review command",
    ),
    ("POST", WB + "/cases/{cid}/decisions"): ws_route(
        roles("reviewer"),
        params=CASE,
        body={"mode": "hold", "reason": "auth matrix", "evidence_digest": "0" * 64},
        if_match=STALE_CASE,
        why="Reviewer: decision/proposal",
    ),
    ("POST", WB + "/proposals/{pid}/approvals"): ws_route(
        roles("approver"),
        params=(("pid", "p_pending"),),
        body={
            "intent_digest": "0" * 64,
            "confirmation": "reject",
            "reason": "auth matrix",
        },
        if_match=STALE_PROPOSAL,
        why="Approver only; reviewer never approves; admin not implicitly approver",
    ),
    ("PATCH", WB + "/cases/{cid}/fields/{field}"): ws_route(
        roles("reviewer"),
        params=(("cid", "case"), ("field", "=received_quantity")),
        body={"typed_value": "100", "reason": "auth matrix"},
        if_match=STALE_CASE,
        why="Reviewer: evidence-backed correction",
    ),
    ("POST", WB + "/intakes/files"): ws_route(
        roles("reviewer"),
        body={"rights_attestation": "authored-synthetic", "files": []},
        why="Reviewer: intake",
    ),
    ("POST", WB + "/intakes/sample"): ws_route(
        roles("reviewer"), effect=True, why="Reviewer: intake (authored sample packet)"
    ),
    ("GET", WB + "/events"): ws_route(
        MEMBERS, queries=("?follow=false",), why=READ + " (SSE)"
    ),
    ("GET", WB + "/activity"): ws_route(MEMBERS, why=READ),
    ("POST", WB + "/approvals/{pid}/revocations"): ws_route(
        roles("approver"),
        params=(("pid", "p_approved"),),
        body={"reason": "auth matrix"},
        if_match=STALE_PROPOSAL,
        why="Approver: revoke (and only the approving user)",
    ),
    ("POST", WB + "/cases/{cid}/retention-holds"): ws_route(
        roles("admin"),
        params=CASE,
        body={"enabled": True, "reason": "auth matrix"},
        if_match=STALE_CASE,
        why="Admin: retention settings",
    ),
    ("GET", WB + "/cases/{cid}/sources/{sid}/original"): ws_route(
        roles("reviewer", "approver"),
        params=(("cid", "case_orig"), ("sid", "sid")),
        stage="post",
        code="ORIGINAL_EXPORT_DENIED",
        why="Viewer never exports originals; auditor needs a separate evidence permission",
    ),
    ("POST", WB + "/cases/{cid}/original-deletions"): ws_route(
        roles("admin"),
        params=(("cid", "case_del"),),
        body={"reason": "auth matrix"},
        if_match=STALE_CASE,
        why="Admin: retention deletion",
    ),
    ("POST", WB + "/cases/{cid}/exports"): ws_route(
        roles("reviewer", "approver", "auditor"),
        params=CASE,
        body={
            "case_version": 999,
            "include_originals": False,
            "redaction_profile": "audit-v1",
        },
        stage="post",
        code="EXPORT_DENIED",
        why="Reviewer permitted export; auditor redacted export; approver evidence inspection",
    ),
    ("GET", WB + "/artifacts/{aid}"): ws_route(
        MEMBERS,
        params=(("aid", "aid"),),
        stage="post",
        code="EXPORT_DENIED",
        owner_only=True,
        queries=("?download=false", "?download=true"),
        why="Export artifact readable by its enqueuing actor only",
    ),
}


def service(role, *, params=(), body=None, query="", headers=None):
    return {
        "kind": "service",
        "role": role,
        "params": params,
        "body": body,
        "query": query,
        "headers": headers or {},
    }


TARGET_ROUTES = {
    ("GET", "/openapi.json"): public("Target API contract; no records"),
    ("GET", "/docs"): public("Target API docs; no records"),
    ("GET", "/docs/oauth2-redirect"): public("FastAPI docs helper; no records"),
    ("GET", "/redoc"): public("Target API docs; no records"),
    ("GET", "/health"): public("Target readiness"),
    ("GET", "/"): public(
        "Static operator console HTML; records need the operator session"
    ),
    ("POST", "/operator/login"): {"kind": "operator_login"},
    ("GET", "/operator/records"): {"kind": "operator_records"},
    ("POST", WB + "/operation-fences"): service("broker", body={}),
    ("POST", WB + "/operation-grants"): service("broker", body={}),
    ("PATCH", WB + "/payables/{record}"): service(
        "dispatcher",
        params=(("record", "record"),),
        body={},
        headers={"If-Match": '"payable-v7"', "Idempotency-Key": "op"},
    ),
    ("GET", WB + "/operations/{operation}"): service(
        "verifier", params=(("operation", "operation"),)
    ),
    ("GET", WB + "/payables"): service(
        "verifier", query="?invoice_key=INV-1048&supplier=Aster%20Supply%20Co."
    ),
    ("GET", WB + "/payables/{record}"): service(
        "verifier", params=(("record", "record"),)
    ),
}
TARGET_PRINCIPALS = (
    "no_token",
    "broker",
    "dispatcher",
    "verifier",
    "forged_token",
    "browser_origin_verifier",
    "browser_fetch_site_verifier",
)


def route_inventory(app):
    out = set()
    for route in app.routes:
        for method in (getattr(route, "methods", None) or set()) - {"HEAD"}:
            out.add((method, route.path))
    return out


def openapi_inventory(app):
    return {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
    }


def test_every_route_is_classified():
    from folio.target import app as target_app
    from folio.workbench import app

    for live, table, name in (
        (app, WORKBENCH_ROUTES, "workbench"),
        (target_app, TARGET_ROUTES, "target"),
    ):
        routes = route_inventory(live)
        assert routes - set(table) == set(), f"{name} routes without expectation"
        assert set(table) - routes == set(), f"{name} expectation for missing route"
        assert openapi_inventory(live) <= routes, f"{name} OpenAPI path outside routes"
    RESULTS["probes"]["route_inventory"] = {
        "workbench": sorted(f"{m} {p}" for m, p in route_inventory(app)),
        "target": sorted(f"{m} {p}" for m, p in route_inventory(target_app)),
    }


# ---------------------------------------------------------------- shared helpers


def fingerprint_query(tables, extra=()):
    parts = [
        f"SELECT '{t}', count(*)::bigint, md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {t} x"
        for t in tables
    ]
    return " UNION ALL ".join([*parts, *extra])


WB_TABLES = (
    "users",
    "sessions",
    "cases",
    "proposals",
    "jobs",
    "commands",
    "events",
    "export_jobs",
    "analysis_runs",
    "originals",
)
WB_FP = fingerprint_query(
    WB_TABLES,
    (
        "SELECT 'originals.raw_nonnull', count(*)::bigint, '' FROM originals WHERE raw IS NOT NULL",
        "SELECT 'cases.version_sum', coalesce(sum((body->>'version')::bigint),0)::bigint, '' FROM cases",
        "SELECT 'proposals.version_sum', coalesce(sum((body->>'version')::bigint),0)::bigint, '' FROM proposals",
    ),
)
TARGET_FP = fingerprint_query(
    ("payables", "authority", "operations", "target_sessions")
)


def fingerprint(db, query):
    return {name: (n, h) for name, n, h in db.execute(query).fetchall()}


def delta(before, after):
    return {k: [before[k][0], after[k][0]] for k in before if before[k] != after.get(k)}


def code_of(response):
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("code")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            return "REQUEST_VALIDATION"
    return None


def allow(note=None):
    return {"outcome": "allowed", "status": None, "code": None, "note": note}


def deny(status, code=None):
    return {"outcome": "denied", "status": status, "code": code}


def judge(
    expected, status, code, body, effect, effect_ok, leak_markers, denied_markers
):
    if status >= 500:
        return False, "server error"
    if any(marker in body for marker in leak_markers):
        return False, "foreign tenant marker in response"
    if expected["outcome"] == "allowed":
        if status in expected.get("accept", ()):
            return True, "authorized (accepted business status)"
        if status in (401, 403) or status == 404 or status == 405:
            return False, "allowed cell did not get past authorization/scope/object"
        if effect and not effect_ok:
            return (
                False,
                "unexpected effect from an allowed read/stale-precondition cell",
            )
        return True, "authorized"
    want = expected["status"]
    if status not in (want if isinstance(want, tuple) else (want,)):
        return False, f"expected status {want}"
    if expected["code"] and code != expected["code"]:
        return False, f"expected code {expected['code']}"
    if effect:
        return False, "denied cell had an effect"
    if any(marker in body for marker in denied_markers):
        return False, "denied response contains protected data"
    return True, "denied with zero effect"


def record(matrix, cell):
    RESULTS[matrix].append(cell)
    return cell


def run_cell(
    world,
    matrix,
    *,
    client,
    method,
    route,
    url,
    principal,
    workspace,
    csrf,
    obj,
    expected,
    headers=None,
    body=None,
    effect_ok=False,
    leak_check=True,
    fp_db=None,
    fp_query=WB_FP,
    note=None,
):
    db = fp_db or world["wbdb"]
    before = fingerprint(db, fp_query)
    kwargs = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    response = client.request(method, url, **kwargs)
    after = fingerprint(db, fp_query)
    effect = delta(before, after)
    status, code, content = response.status_code, code_of(response), response.content
    passed, reason = judge(
        expected,
        status,
        code,
        content,
        effect,
        effect_ok,
        world["leak_markers"] if leak_check else (),
        world["denied_markers"],
    )
    return record(
        matrix,
        {
            "matrix": matrix,
            "route": route[1],
            "method": route[0],
            "probe": url.split("?", 1)[1] if "?" in url else "",
            "role": principal,
            "workspace": workspace,
            "csrf": csrf,
            "object": obj,
            "expected": expected,
            "actual": {"status": status, "code": code},
            "effect_delta": effect,
            "effect_allowed": effect_ok,
            "leak_checked": leak_check,
            "pass": passed,
            "reason": reason,
            **({"note": note} if note else {}),
        },
    )


def failures_text(cells):
    lines = [
        f"{c['method']} {c['route']}{'?' + c['probe'] if c['probe'] else ''} role={c['role']} "
        f"ws={c['workspace']} csrf={c['csrf']} obj={c['object']} expected={c['expected']} "
        f"actual={c['actual']} effect={c['effect_delta']} reason={c['reason']}"
        for c in cells[:40]
    ]
    return f"{len(cells)} failing cells:\n" + "\n".join(lines)


# ---------------------------------------------------------------- workbench world


def add_user(db, name, user_roles):
    from psycopg.types.json import Jsonb

    password = secrets.token_urlsafe(24)
    salt = "auth-matrix-" + name
    hashed = hashlib.scrypt(
        password.encode(), salt=salt.encode(), n=16384, r=8, p=1
    ).hex()
    db.execute(
        "INSERT INTO users(username,salt,password_hash,roles) VALUES(%s,%s,%s,%s)",
        (name, salt, hashed, Jsonb(user_roles)),
    )
    return password


def new_client():
    from folio.workbench import app

    return TestClient(app, raise_server_exceptions=False)


def login(name, password=None):
    client = new_client()
    r = client.post(
        "/api/v1/auth/login",
        json={"username": name, "password": password or f"folio-{name}-local"},
    )
    assert r.status_code == 200, (name, r.status_code)
    return {
        "client": client,
        "csrf": r.json()["csrf"],
        "user": name,
        "roles": frozenset(r.json()["roles"]),
    }


def token_hash(client):
    return hashlib.sha256(client.cookies.get("folio_session", "").encode()).hexdigest()


def write_headers(principal, csrf="valid", if_match=None, origin=None):
    from folio.workbench import ORIGIN

    headers = {"Idempotency-Key": str(uuid4())}
    if if_match:
        headers["If-Match"] = if_match
    token = principal.get("csrf") or secrets.token_urlsafe(32)
    if csrf == "valid":
        headers.update({"Origin": origin or ORIGIN, "X-CSRF-Token": token})
    elif csrf == "absent_token":
        headers["Origin"] = origin or ORIGIN
    elif csrf == "wrong_origin":
        headers.update({"Origin": BAD_ORIGIN, "X-CSRF-Token": token})
    return headers


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    import psycopg
    from psycopg.types.json import Jsonb

    from folio import workbench as w
    from folio.original_download import UPLOADED_AUTHORSHIP
    from folio.target import seed

    patch = pytest.MonkeyPatch()
    # /health must never probe the live model port (18301): the paused marker
    # short-circuits model_readiness() before any network call.
    marker = tmp_path_factory.mktemp("auth-matrix") / "model-paused"
    marker.write_text("auth matrix: model probe disabled\n")
    patch.setenv("FOLIO_MODEL_PAUSE_MARKER", str(marker))
    w.migrate()
    own = w.WORKSPACE
    other = str(uuid4())
    suffix = secrets.token_hex(4)
    foreign_marker = f"FOREIGN-TENANT-MARKER-{secrets.token_hex(8)}"
    own_raw_marker = f"AUTH-MATRIX-OWN-ORIGINAL-{secrets.token_hex(8)}"
    wbdb = psycopg.connect(os.environ["FOLIO_DSN"], autocommit=True)
    tdb = psycopg.connect(os.environ["FOLIO_TARGET_DSN"], autocommit=True)
    record_id = str(uuid4())
    seed(own, record_id)

    access_request_id = str(uuid4())
    with w.connect() as db:
        dual_name, inactive_name = f"matrix_dual_{suffix}", f"matrix_inactive_{suffix}"
        access_target = "matrix-access-target"
        access_requester = f"matrix_access_requester_{suffix}"
        dual_pw = add_user(db, dual_name, ["reviewer", "approver"])
        inactive_pw = add_user(db, inactive_name, ["reviewer"])
        access_target_password = add_user(db, access_target, ["reviewer"])
        access_requester_password = add_user(db, access_requester, ["admin"])
        db.execute(
            "INSERT INTO folio_access_requests(workspace,id,requested_by,target_username,action,role,reason) "
            "VALUES(%s,%s,%s,%s,'grant_role','approver','Auth matrix independent review fixture.')",
            (own, access_request_id, access_requester, access_target),
        )

    principals = {
        "anonymous": {
            "client": new_client(),
            "csrf": None,
            "user": None,
            "roles": frozenset(),
        }
    }
    for name in ("viewer", "reviewer", "approver", "admin", "auditor"):
        principals[name] = login(name)
    principals["expired_reviewer"] = login("reviewer")
    principals["inactive_reviewer"] = login(inactive_name, inactive_pw)
    dual = login(dual_name, dual_pw)
    access_target_principal = login(access_target, access_target_password)
    access_requester_principal = login(access_requester, access_requester_password)
    wbdb.execute(
        "UPDATE sessions SET expires=clock_timestamp()-interval '1 hour' WHERE token=%s",
        (token_hash(principals["expired_reviewer"]["client"]),),
    )
    wbdb.execute("UPDATE users SET active=false WHERE username=%s", (inactive_name,))

    def original_case():
        raw = (own_raw_marker + " " + secrets.token_hex(8)).encode()
        sid = str(uuid4())
        source = {
            "id": sid,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "name": "matrix-original.csv",
            "document_role": "invoice",
            "authorship": UPLOADED_AUTHORSHIP,
            "version": 1,
        }
        case = w.create_case(sources=[source])
        wbdb.execute(
            "INSERT INTO originals(workspace,id,case_id,sha256,raw) VALUES(%s,%s,%s,%s,%s)",
            (own, sid, case["id"], source["sha256"], raw),
        )
        return case, sid

    prefix = f"/api/v1/workspaces/{own}"
    reviewer, approver = principals["reviewer"], principals["approver"]

    def prepare(principal, case):
        r = principal["client"].post(
            f"{prefix}/cases/{case['id']}/decisions",
            headers=write_headers(principal, if_match='"case-v1"'),
            json=dict(
                mode="partial",
                quantity="100",
                reason="Auth matrix fixture",
                evidence_digest=case["source_digest"],
            ),
        )
        assert r.status_code == 201, r.text
        return r.json()["proposal"]

    def approve(principal, proposal):
        return principal["client"].post(
            f"{prefix}/proposals/{proposal['id']}/approvals",
            headers=write_headers(principal, if_match='"proposal-v1"'),
            json=dict(intent_digest=proposal["digest"], confirmation="approve"),
        )

    case = w.create_case(target_id=record_id)
    case_orig, sid = original_case()
    case_del, _ = original_case()
    run = reviewer["client"].post(
        f"{prefix}/cases/{case['id']}/analysis-runs",
        headers=write_headers(reviewer, if_match='"case-v1"'),
        json={},
    )
    assert run.status_code == 202, run.text
    p_pending = prepare(reviewer, w.create_case(target_id=record_id))
    p_approved = prepare(reviewer, w.create_case(target_id=record_id))
    approved = approve(approver, p_approved)
    assert approved.status_code == 201, approved.text
    export = reviewer["client"].post(
        f"{prefix}/cases/{case['id']}/exports",
        headers=write_headers(reviewer),
        json={
            "case_version": 1,
            "include_originals": False,
            "redaction_profile": "audit-v1",
        },
    )
    assert export.status_code == 202, export.text

    # Second tenant: real rows in another workspace, reachable only by forging paths/IDs.
    fcase, fsid, frun, fpid = (str(uuid4()) for _ in range(4))
    fraw = (foreign_marker + " original bytes").encode()
    fbody = {
        **case,
        "id": fcase,
        "workspace": other,
        "label": foreign_marker,
        "supplier": foreign_marker,
        "sources": [
            {
                "id": fsid,
                "sha256": hashlib.sha256(fraw).hexdigest(),
                "name": foreign_marker,
                "authorship": UPLOADED_AUTHORSHIP,
                "version": 1,
            }
        ],
        "sample_digest": "f" * 64,
    }
    wbdb.execute("INSERT INTO cases VALUES(%s,%s,%s)", (other, fcase, Jsonb(fbody)))
    wbdb.execute(
        "INSERT INTO proposals VALUES(%s,%s,%s,%s)",
        (
            other,
            fpid,
            fcase,
            Jsonb(
                {
                    **p_pending,
                    "id": fpid,
                    "case_id": fcase,
                    "workspace": other,
                    "reason": foreign_marker,
                }
            ),
        ),
    )
    wbdb.execute(
        "INSERT INTO analysis_runs(id,workspace,case_id,body) VALUES(%s,%s,%s,%s)",
        (
            frun,
            other,
            fcase,
            Jsonb(
                {
                    "id": frun,
                    "case_id": fcase,
                    "workspace": other,
                    "note": foreign_marker,
                }
            ),
        ),
    )
    wbdb.execute(
        "INSERT INTO originals(workspace,id,case_id,sha256,raw) VALUES(%s,%s,%s,%s,%s)",
        (other, fsid, fcase, hashlib.sha256(fraw).hexdigest(), fraw),
    )
    wbdb.execute(
        "INSERT INTO events(workspace,case_id,kind,actor,body) VALUES(%s,%s,%s,%s,%s)",
        (
            other,
            fcase,
            "DOC-foreign-tenant",
            "foreign",
            Jsonb({"marker": foreign_marker}),
        ),
    )
    foreign_export_row = True
    try:
        # export_jobs carries FORCE RLS pinned to the deployment workspace: the owner
        # itself cannot create a foreign-tenant export row (recorded as evidence).
        with wbdb.transaction():
            wbdb.execute(
                "INSERT INTO export_jobs(id,workspace,case_id,actor,snapshot,snapshot_sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                (str(uuid4()), other, fcase, "reviewer", Jsonb({}), "0" * 64),
            )
    except psycopg.Error as error:
        foreign_export_row = type(error).__name__
    RESULTS["probes"]["foreign_export_insert_under_rls"] = foreign_export_row

    world = {
        "w": w,
        "own": own,
        "other": other,
        "principals": principals,
        "dual": dual,
        "inactive_password": inactive_pw,
        "wbdb": wbdb,
        "tdb": tdb,
        "record_id": record_id,
        "prefix": prefix,
        "access_request_id": access_request_id,
        "access_target": access_target,
        "access_target_principal": access_target_principal,
        "access_requester_principal": access_requester_principal,
        "approve": approve,
        "prepare": prepare,
        "ids": {
            "own": {
                "case": case["id"],
                "case_orig": case_orig["id"],
                "sid": sid,
                "case_del": case_del["id"],
                "run": run.json()["run_id"],
                "p_pending": p_pending["id"],
                "p_approved": p_approved["id"],
                "aid": export.json()["artifact_job_id"],
            },
            "foreign": {
                "case": fcase,
                "case_orig": fcase,
                "sid": fsid,
                "case_del": fcase,
                "run": frun,
                "p_pending": fpid,
                "p_approved": fpid,
                "aid": str(uuid4()),  # RLS forbids a foreign export row
            },
        },
        "foreign_marker": foreign_marker,
        "leak_markers": (foreign_marker.encode(),),
        "denied_markers": (b"Aster Supply", own_raw_marker.encode()),
    }
    yield world
    patch.undo()
    wbdb.close()
    tdb.close()


def object_ids(world, spec, ws, obj):
    own, foreign = world["ids"]["own"], world["ids"]["foreign"]
    out = {}
    for index, (param, key) in enumerate(spec["params"]):
        if key == "access_request":
            out[param] = world["access_request_id"]
        elif key.startswith("="):
            out[param] = key[1:]
        elif obj == "real":
            out[param] = (own if ws == "own" else foreign)[key]
        elif obj == "random":
            out[param] = str(uuid4())
        elif obj == "malformed":
            out[param] = "not-a-uuid"
        elif obj == "foreign_object":
            out[param] = foreign[key]
        elif obj == "own_parent_foreign_child":
            out[param] = own[key] if index == 0 else foreign[key]
        elif obj == "own_parent_malformed_child":
            out[param] = own[key] if index == 0 else "not-a-uuid"
    return out


def expected_workspace(spec, principal_name, principal, write, ws, csrf, obj):
    """Blueprint role table x the code's documented check order (module docstring)."""
    if principal_name in SESSIONLESS:
        return deny(401, "SESSION_REQUIRED")
    have = principal["roles"]
    if spec["stage"] == "pre" and not have & spec["allowed"]:
        return deny(403, "PERMISSION_DENIED")
    if write and csrf != "valid":
        return deny(403, "CSRF_DENIED")
    if ws == "foreign":
        return deny(404, "NOT_FOUND_OR_FORBIDDEN")
    if spec["stage"] == "post" and not have & spec["allowed"]:
        return deny(403, spec["code"])
    if obj in ("malformed", "own_parent_malformed_child"):
        return deny((404, 422))
    if obj != "real":
        return deny(404)
    if spec["owner_only"] and principal_name != "reviewer":
        return deny(404, "NOT_FOUND")
    return allow()


def workspace_cells(world, method, path, spec):
    write = method not in ("GET", "HEAD")
    variants = []
    for name in PRINCIPALS:
        for ws in ("own", "foreign"):
            for csrf in CSRF if write else ("n/a",):
                variants.append((name, ws, csrf, "real"))
    if spec["params"]:
        forged = (
            FORGED
            if len([p for p in spec["params"] if not p[1].startswith("=")]) > 1
            else FORGED[:3]
        )
        for name in PRINCIPALS:
            p = world["principals"][name]
            if name in SESSIONLESS or not p["roles"] & spec["allowed"]:
                continue
            if spec["owner_only"] and name != "reviewer":
                continue
            for obj in forged:
                variants.append((name, "own", "valid" if write else "n/a", obj))
    for name, ws, csrf, obj in variants:
        principal = world["principals"][name]
        wsid = world["own"] if ws == "own" else world["other"]
        for query in spec["queries"]:
            url = (
                path.format(workspace=wsid, **object_ids(world, spec, ws, obj)) + query
            )
            headers = write_headers(principal, csrf, spec["if_match"]) if write else {}
            expected = expected_workspace(spec, name, principal, write, ws, csrf, obj)
            yield dict(
                client=principal["client"],
                method=method,
                route=(method, path),
                url=url,
                principal=name,
                workspace=ws,
                csrf=csrf,
                obj=obj,
                expected=expected,
                headers=headers,
                body=spec["body"],
                effect_ok=spec["effect"] and expected["outcome"] == "allowed",
            )


def access_review_cells(world, method, path):
    principals = world["principals"]
    review_principals = [(name, principals[name]) for name in PRINCIPALS]
    review_principals.extend(
        (
            ("dual_reviewer_approver", world["dual"]),
            ("access_requester", world["access_requester_principal"]),
            ("access_target", world["access_target_principal"]),
        )
    )
    cells = []
    for name, principal in review_principals:
        for workspace in ("own", "foreign"):
            for csrf in CSRF:
                if name in SESSIONLESS:
                    expected = deny(401, "SESSION_REQUIRED")
                elif csrf != "valid":
                    expected = deny(403, "CSRF_DENIED")
                elif workspace == "foreign":
                    expected = deny(404, "NOT_FOUND_OR_FORBIDDEN")
                elif name in ("access_requester", "access_target"):
                    expected = deny(403, "INDEPENDENT_REVIEW_REQUIRED")
                elif "approver" in principal["roles"]:
                    expected = allow("distinct active approver may review this role grant")
                else:
                    expected = deny(403, "PERMISSION_DENIED")
                ws = world["own"] if workspace == "own" else world["other"]
                url = path.format(workspace=ws, request_id=world["access_request_id"])
                cells.append(
                    run_cell(
                        world,
                        "workbench",
                        client=principal["client"],
                        method=method,
                        route=(method, path),
                        url=url,
                        principal=name,
                        workspace=workspace,
                        csrf=csrf,
                        obj="access_request",
                        expected=expected,
                        headers=write_headers(principal, csrf, '"access-request-v0"'),
                        body={"decision": "approve", "reason": "Auth matrix review."},
                    )
                )
    return cells


def test_workbench_roles_routes_matrix(world):
    from folio.workbench import app

    principals = world["principals"]
    cells = []
    for method, path in sorted(route_inventory(app)):
        spec = WORKBENCH_ROUTES[(method, path)]
        route = (method, path)
        if spec["kind"] == "public":
            for name in PRINCIPALS:
                cells.append(
                    run_cell(
                        world,
                        "workbench",
                        client=principals[name]["client"],
                        method=method,
                        route=route,
                        url=path,
                        principal=name,
                        workspace="n/a",
                        csrf="n/a",
                        obj="n/a",
                        expected=allow("public by design"),
                    )
                )
        elif spec["kind"] == "session":
            for name in PRINCIPALS:
                expected = (
                    deny(401, "SESSION_REQUIRED") if name in SESSIONLESS else allow()
                )
                cells.append(
                    run_cell(
                        world,
                        "workbench",
                        client=principals[name]["client"],
                        method=method,
                        route=route,
                        url=path,
                        principal=name,
                        workspace="n/a",
                        csrf="n/a",
                        obj="n/a",
                        expected=expected,
                    )
                )
        elif spec["kind"] == "login":
            from folio.workbench import ORIGIN

            inactive = principals["inactive_reviewer"]["user"]
            attempts = [(n, n, f"folio-{n}-local") for n in sorted(MEMBERS)] + [
                ("wrong_password", "reviewer", secrets.token_urlsafe(12)),
                (
                    "unknown_user",
                    "nobody-" + secrets.token_hex(4),
                    secrets.token_urlsafe(12),
                ),
                ("inactive_reviewer", inactive, world["inactive_password"]),
            ]
            for label, username, password in attempts:
                for origin in ("origin_ok", "no_origin", "wrong_origin"):
                    headers = {
                        "origin_ok": {"Origin": ORIGIN},
                        "no_origin": {},
                        "wrong_origin": {"Origin": BAD_ORIGIN},
                    }[origin]
                    good = label in MEMBERS
                    expected = (
                        allow("pre-session exception; see AUTH-NOTE-1")
                        if good
                        else deny(401, "INVALID_LOGIN")
                    )
                    cells.append(
                        run_cell(
                            world,
                            "workbench",
                            client=new_client(),
                            method=method,
                            route=route,
                            url=path,
                            principal=label,
                            workspace="n/a",
                            csrf=origin,
                            obj="n/a",
                            expected=expected,
                            headers=headers,
                            body={"username": username, "password": password},
                            effect_ok=good,
                        )
                    )
        elif spec["kind"] == "receipt":
            key = "00000000-0000-4000-8000-000000000000"
            for name in PRINCIPALS:
                expected = deny(401, "SESSION_REQUIRED") if name in SESSIONLESS else allow()
                cells.append(
                    run_cell(
                        world,
                        "workbench",
                        client=principals[name]["client"],
                        method=method,
                        route=route,
                        url=path.format(idempotency_key=key) + spec["query"],
                        principal=name,
                        workspace="n/a",
                        csrf="n/a",
                        obj="n/a",
                        expected=expected,
                    )
                )
        elif spec["kind"] == "access_review":
            cells.extend(access_review_cells(world, method, path))
        elif spec["kind"] == "logout":
            for name in PRINCIPALS:
                for csrf in CSRF:
                    principal = principals[name] if name in SESSIONLESS else login(name)
                    if name in SESSIONLESS:
                        expected = deny(401, "SESSION_REQUIRED")
                    elif csrf != "valid":
                        expected = deny(403, "CSRF_DENIED")
                    else:
                        expected = allow()
                    cells.append(
                        run_cell(
                            world,
                            "workbench",
                            client=principal["client"],
                            method=method,
                            route=route,
                            url=path,
                            principal=name,
                            workspace="n/a",
                            csrf=csrf,
                            obj="n/a",
                            expected=expected,
                            headers=write_headers(principal, csrf),
                            effect_ok=expected["outcome"] == "allowed",
                        )
                    )
        else:
            for cell in workspace_cells(world, method, path, spec):
                cells.append(run_cell(world, "workbench", **cell))
    failing = [c for c in cells if not c["pass"]]
    assert not failing, failures_text(failing)


# ---------------------------------------------------------------- scenarios


def scenario(
    world,
    name,
    principal,
    method,
    url,
    *,
    expected,
    headers=None,
    body=None,
    effect_ok=False,
    note=None,
):
    cell = run_cell(
        world,
        "scenario",
        client=principal["client"],
        method=method,
        route=(method, url.split("?")[0].replace(world["own"], "{workspace}")),
        url=url,
        principal=name,
        workspace="own",
        csrf="valid" if method != "GET" else "n/a",
        obj="real",
        expected=expected,
        headers=headers,
        body=body,
        effect_ok=effect_ok,
        note=note,
    )
    return cell


def test_independent_approval_self_approval_and_session_scenarios(world):
    from folio.workbench import create_case

    prefix, principals, dual = world["prefix"], world["principals"], world["dual"]
    cells = []
    case = create_case(target_id=world["record_id"])
    r = dual["client"].post(
        f"{prefix}/cases/{case['id']}/decisions",
        headers=write_headers(dual, if_match='"case-v1"'),
        json=dict(
            mode="partial",
            quantity="100",
            reason="Dual-role preparer",
            evidence_digest=case["source_digest"],
        ),
    )
    assert r.status_code == 201, r.text
    proposal = r.json()["proposal"]
    approval_url = f"{prefix}/proposals/{proposal['id']}/approvals"
    approval = dict(intent_digest=proposal["digest"], confirmation="approve")

    def approval_headers(p):
        return write_headers(p, if_match='"proposal-v1"')

    cells.append(
        scenario(
            world,
            "dual_reviewer_approver_self",
            dual,
            "POST",
            approval_url,
            expected=deny(403, "SELF_APPROVAL"),
            headers=approval_headers(dual),
            body=approval,
            note="Preparer holding the approver role cannot approve their own proposal",
        )
    )
    cells.append(
        scenario(
            world,
            "admin",
            principals["admin"],
            "POST",
            approval_url,
            expected=deny(403, "PERMISSION_DENIED"),
            headers=approval_headers(principals["admin"]),
            body=approval,
            note="Admin is not implicitly approver",
        )
    )
    cells.append(
        scenario(
            world,
            "reviewer",
            principals["reviewer"],
            "POST",
            approval_url,
            expected=deny(403, "PERMISSION_DENIED"),
            headers=approval_headers(principals["reviewer"]),
            body=approval,
        )
    )
    mixed = write_headers(principals["reviewer"], if_match='"case-v1"')
    mixed["X-CSRF-Token"] = principals["approver"]["csrf"]
    cells.append(
        scenario(
            world,
            "reviewer_with_approver_csrf",
            principals["reviewer"],
            "POST",
            f"{prefix}/cases/{case['id']}/decisions",
            expected=deny(403, "CSRF_DENIED"),
            headers=mixed,
            body=dict(mode="hold", reason="x", evidence_digest=case["source_digest"]),
            note="CSRF token is bound to the session, not just well-formed",
        )
    )
    cells.append(
        scenario(
            world,
            "approver_independent",
            principals["approver"],
            "POST",
            approval_url,
            expected=allow("positive control: independent approver"),
            headers=approval_headers(principals["approver"]),
            body=approval,
            effect_ok=True,
        )
    )
    assert cells[-1]["actual"]["status"] == 201, cells[-1]
    revoke_url = f"{prefix}/approvals/{proposal['id']}/revocations"
    cells.append(
        scenario(
            world,
            "dual_not_the_approver",
            dual,
            "POST",
            revoke_url,
            expected=deny(403, "NOT_AUTHORIZED_APPROVER"),
            headers=approval_headers(dual),
            body={"reason": "not mine"},
            note="Only the approving user may revoke",
        )
    )
    cells.append(
        scenario(
            world,
            "approver_revokes_own",
            principals["approver"],
            "POST",
            revoke_url,
            expected=allow("positive control"),
            headers=approval_headers(principals["approver"]),
            body={"reason": "auth matrix revoke"},
            effect_ok=True,
        )
    )
    cells.append(
        scenario(
            world,
            "reviewer_include_originals",
            principals["reviewer"],
            "POST",
            f"{prefix}/cases/{world['ids']['own']['case']}/exports",
            expected=deny(403, "ORIGINAL_EXPORT_DENIED"),
            headers=write_headers(principals["reviewer"]),
            body={
                "case_version": 1,
                "include_originals": True,
                "redaction_profile": "audit-v1",
            },
        )
    )
    # Logged-out cookie replay: the server-side session row is gone.
    fresh = login("reviewer")
    cookie = fresh["client"].cookies.get("folio_session")
    out = fresh["client"].post("/api/v1/auth/logout", headers=write_headers(fresh))
    assert out.status_code == 200, out.text
    replay = {
        "client": new_client(),
        "csrf": fresh["csrf"],
        "user": "reviewer",
        "roles": frozenset(),
    }
    replay["client"].cookies.set("folio_session", cookie)
    cells.append(
        scenario(
            world,
            "logged_out_cookie_replay",
            replay,
            "GET",
            "/api/v1/auth/session",
            expected=deny(401, "SESSION_REQUIRED"),
        )
    )
    cells.append(
        scenario(
            world,
            "logged_out_cookie_replay",
            replay,
            "POST",
            f"{prefix}/cases/{case['id']}/decisions",
            expected=deny(401, "SESSION_REQUIRED"),
            headers=write_headers(replay, if_match='"case-v3"'),
            body=dict(mode="hold", reason="x", evidence_digest=case["source_digest"]),
        )
    )
    # Forged cookie (random token) and a cookie of a deactivated-then-reactivated window.
    forged = {"client": new_client(), "csrf": None, "user": None, "roles": frozenset()}
    forged["client"].cookies.set("folio_session", secrets.token_urlsafe(32))
    cells.append(
        scenario(
            world,
            "forged_cookie",
            forged,
            "GET",
            f"{prefix}/activity",
            expected=deny(401, "SESSION_REQUIRED"),
        )
    )
    # A non-canonical spelling of the member workspace is not membership.
    alias = prefix.replace(world["own"], world["own"].replace("-", ""))
    cells.append(
        scenario(
            world,
            "reviewer",
            principals["reviewer"],
            "GET",
            alias + "/cases",
            expected=deny(404, "NOT_FOUND_OR_FORBIDDEN"),
            note="unhyphenated workspace alias",
        )
    )
    # Second-tenant data never appears in own-workspace listings/streams for any member.
    for name in sorted(MEMBERS):
        for url in (
            f"{prefix}/cases",
            f"{prefix}/activity",
            f"{prefix}/events?follow=false",
        ):
            cells.append(
                scenario(
                    world,
                    name,
                    principals[name],
                    "GET",
                    url,
                    expected=allow("tenant listing; foreign marker must be absent"),
                )
            )
    failing = [c for c in cells if not c["pass"]]
    assert not failing, failures_text(failing)


def test_pool_tenant_context_probe(world):
    """Record what tenant context exists on a pooled/handed-out connection.

    The blueprint asks for transaction-local tenant context on pooled connections. Probe
    the current connection layer: any custom GUC left set on a fresh connection after the
    matrix ran would be a leak. Records honestly when there is no pool or tenant GUC.
    """
    from folio import workbench as w

    pool_attrs = sorted(n for n in dir(w) if "pool" in n.lower())
    with w.connect() as db:
        gucs = db.execute(
            "SELECT name, setting FROM pg_settings WHERE name ~ '^(app|folio|request|tenant)\\.'"
        ).fetchall()
        policies = db.execute(
            "SELECT tablename, policyname, qual FROM pg_policies WHERE schemaname='public' ORDER BY 1"
        ).fetchall()
        rls = db.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relkind='r' AND relnamespace='public'::regnamespace AND relrowsecurity ORDER BY 1"
        ).fetchall()
    leaked = [(n, s) for n, s in gucs if s not in ("", None)]
    RESULTS["probes"]["pool_tenant_context"] = {
        "workbench_pool_attributes": pool_attrs,
        "custom_gucs_on_fresh_connection": [list(g) for g in gucs],
        "rls_tables": [list(r) for r in rls],
        "rls_policies": [[t, p, q] for t, p, q in policies],
        "verdict": (
            "LEAK"
            if leaked
            else (
                "probed_no_leak"
                if pool_attrs or gucs
                else "not_applicable: connect() opens a new connection per request; no pool and no per-request tenant GUC; RLS (where present) is pinned to the single deployment workspace"
            )
        ),
    }
    assert not leaked, leaked


def test_regression_malformed_object_ids_are_404_without_effect(world):
    """Round-6 defect: a malformed proposal/run ID reached a uuid cast and answered 500.

    Blueprint §5 'guessing IDs returns 404': approvals, revocations and analysis-run
    detail must treat a non-UUID object ID as an unknown object, with zero effect.
    """
    prefix, principals = world["prefix"], world["principals"]
    approver, viewer = principals["approver"], principals["viewer"]
    case = world["ids"]["own"]["case"]
    cells = []
    for label, principal, method, url, body in (
        (
            "approver",
            approver,
            "POST",
            f"{prefix}/proposals/not-a-uuid/approvals",
            {"intent_digest": "0" * 64, "confirmation": "approve"},
        ),
        (
            "approver",
            approver,
            "POST",
            f"{prefix}/approvals/not-a-uuid/revocations",
            {"reason": "malformed"},
        ),
        (
            "viewer",
            viewer,
            "GET",
            f"{prefix}/cases/{case}/analysis-runs/not-a-uuid",
            None,
        ),
    ):
        headers = (
            write_headers(principal, if_match='"proposal-v1"')
            if method == "POST"
            else {}
        )
        cells.append(
            scenario(
                world,
                label,
                principal,
                method,
                url,
                expected=deny(404, "NOT_FOUND"),
                headers=headers,
                body=body,
                note="regression: malformed object id",
            )
        )
    failing = [c for c in cells if not c["pass"]]
    assert not failing, failures_text(failing)


# ---------------------------------------------------------------- Northstar target


@pytest.fixture(scope="module")
def target_world(world):
    import psycopg
    from psycopg.types.json import Jsonb

    from folio import target as t
    from folio.identity import service_header

    t.migrate()
    other_record = str(uuid4())
    body = {
        **t.seed(world["own"], str(uuid4())),
        "id": other_record,
        "bank_fingerprint": world["foreign_marker"],
    }
    with psycopg.connect(t.DSN) as db:
        db.execute(
            "INSERT INTO payables VALUES(%s,%s,%s)",
            (world["other"], other_record, Jsonb(body)),
        )
    headers = {
        "no_token": {},
        "broker": service_header("broker"),
        "dispatcher": service_header("dispatcher"),
        "verifier": service_header("verifier"),
        "forged_token": {"Authorization": "Bearer " + secrets.token_urlsafe(48)},
        "browser_origin_verifier": {
            **service_header("verifier"),
            "Origin": "http://127.0.0.1:5101",
        },
        "browser_fetch_site_verifier": {
            **service_header("verifier"),
            "Sec-Fetch-Site": "same-origin",
        },
    }
    return {
        "app": t.app,
        "headers": headers,
        "record": world["record_id"],
        "other_record": other_record,
    }


def target_client():
    from folio.target import app

    return TestClient(app, raise_server_exceptions=False)


def expected_service(spec, principal, ws):
    if principal.startswith("browser_"):
        return deny(403, "SERVICE_BROWSER_DENIED")
    if principal != spec["role"] or ws == "foreign":
        return deny(403, "SERVICE_SCOPE")
    return allow()


def test_target_service_token_matrix(world, target_world):
    from folio.target import app

    client = target_client()
    cells = []
    tdb = world["tdb"]
    for method, path in sorted(route_inventory(app)):
        spec = TARGET_ROUTES[(method, path)]
        route = (method, path)
        if spec["kind"] == "public":
            for name in TARGET_PRINCIPALS:
                cells.append(
                    run_cell(
                        world,
                        "target",
                        client=client,
                        method=method,
                        route=route,
                        url=path,
                        principal=name,
                        workspace="n/a",
                        csrf="n/a",
                        obj="n/a",
                        expected=allow("public by design"),
                        headers=target_world["headers"][name],
                        fp_db=tdb,
                        fp_query=TARGET_FP,
                    )
                )
            continue
        if spec["kind"] != "service":
            continue  # operator routes: test_target_operator_session
        variants = [
            (name, ws, "real")
            for name in TARGET_PRINCIPALS
            for ws in ("own", "foreign")
        ]
        if spec["params"] and spec["role"] == "verifier" and path.endswith("{record}"):
            variants += [
                (spec["role"], "own", obj)
                for obj in ("random", "malformed", "foreign_object")
            ]
        for name, ws, obj in variants:
            wsid = world["own"] if ws == "own" else world["other"]
            ids = {}
            for param, key in spec["params"]:
                ids[param] = {
                    "real": target_world["record"] if key == "record" else str(uuid4()),
                    "random": str(uuid4()),
                    "malformed": "not-a-uuid",
                    "foreign_object": target_world["other_record"],
                }[obj]
            headers = {**target_world["headers"][name], **spec["headers"]}
            if "Idempotency-Key" in headers:
                headers["Idempotency-Key"] = str(uuid4())
            expected = expected_service(spec, name, ws)
            if expected["outcome"] == "allowed" and path.endswith("{operation}"):
                expected["accept"] = [
                    404
                ]  # authoritative not-applied answer for a fresh key
            if obj == "malformed":
                expected = deny(422)
            elif obj != "real":
                expected = deny(404, "NOT_FOUND")
            cells.append(
                run_cell(
                    world,
                    "target",
                    client=client,
                    method=method,
                    route=route,
                    url=path.format(workspace=wsid, **ids) + spec["query"],
                    principal=name,
                    workspace=ws,
                    csrf="n/a",
                    obj=obj,
                    expected=expected,
                    headers=headers,
                    body=spec["body"],
                    fp_db=tdb,
                    fp_query=TARGET_FP,
                )
            )
    failing = [c for c in cells if not c["pass"]]
    assert not failing, failures_text(failing)


def test_target_operator_session(world, target_world):
    target_url = os.environ.get("FOLIO_TARGET_URL", "http://127.0.0.1:8201")
    tdb = world["tdb"]
    cells = []
    good = {"username": "operator", "password": __import__("folio.target", fromlist=["operator_password"]).operator_password()}
    for label, headers, body, expected in (
        ("operator_ok", {"Origin": target_url}, good, allow()),
        ("operator_no_origin", {}, good, deny(403, "ORIGIN_DENIED")),
        (
            "operator_wrong_origin",
            {"Origin": BAD_ORIGIN},
            good,
            deny(403, "ORIGIN_DENIED"),
        ),
        (
            "operator_wrong_password",
            {"Origin": target_url},
            {"username": "operator", "password": "guess"},
            deny(401, "INVALID_LOGIN"),
        ),
        (
            "broker_token_wrong_origin",
            {**target_world["headers"]["broker"], "Origin": BAD_ORIGIN},
            good,
            deny(403, "ORIGIN_DENIED"),
        ),
    ):
        cells.append(
            run_cell(
                world,
                "target",
                client=target_client(),
                method="POST",
                route=("POST", "/operator/login"),
                url="/operator/login",
                principal=label,
                workspace="n/a",
                csrf="n/a",
                obj="n/a",
                expected=expected,
                headers=headers,
                body=body,
                effect_ok=expected["outcome"] == "allowed",
                fp_db=tdb,
                fp_query=TARGET_FP,
            )
        )
    operator = target_client()
    assert (
        operator.post(
            "/operator/login", headers={"Origin": target_url}, json=good
        ).status_code
        == 200
    )
    wb_cookie = target_client()
    wb_cookie.cookies.set(
        "folio_session",
        world["principals"]["reviewer"]["client"].cookies.get("folio_session"),
    )
    expired = target_client()
    assert (
        expired.post(
            "/operator/login", headers={"Origin": target_url}, json=good
        ).status_code
        == 200
    )
    tdb.execute(
        "UPDATE target_sessions SET expires=clock_timestamp()-interval '1 minute' WHERE token=%s",
        (
            hashlib.sha256(
                expired.cookies.get("northstar_session").encode()
            ).hexdigest(),
        ),
    )
    for label, client, headers, expected in (
        ("no_session", target_client(), {}, deny(401, "SESSION_REQUIRED")),
        (
            "operator_session",
            operator,
            {},
            allow("global read-only operator; AUTH-NOTE-5"),
        ),
        ("expired_operator_session", expired, {}, deny(401, "SESSION_REQUIRED")),
        ("workbench_session_cookie", wb_cookie, {}, deny(401, "SESSION_REQUIRED")),
        (
            "broker_token",
            target_client(),
            target_world["headers"]["broker"],
            deny(401, "SESSION_REQUIRED"),
        ),
        (
            "verifier_token",
            target_client(),
            target_world["headers"]["verifier"],
            deny(401, "SESSION_REQUIRED"),
        ),
    ):
        cells.append(
            run_cell(
                world,
                "target",
                client=client,
                method="GET",
                route=("GET", "/operator/records"),
                url="/operator/records",
                principal=label,
                workspace="n/a",
                csrf="n/a",
                obj="n/a",
                expected=expected,
                headers=headers,
                leak_check=label != "operator_session",
                fp_db=tdb,
                fp_query=TARGET_FP,
            )
        )
    failing = [c for c in cells if not c["pass"]]
    assert not failing, failures_text(failing)


def test_target_grant_requires_independent_signed_approval(world, target_world):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from folio.identity import canonical, sign_approval
    from folio.target import intent_hash, seed

    own, tdb = world["own"], world["tdb"]
    record, op = str(uuid4()), str(uuid4())
    seed(own, record)
    patch = dict(
        recognized_quantity="100",
        net_amount="4800.00",
        tax_amount="384.00",
        gross_amount="5184.00",
        status="partially_matched",
    )
    intent = intent_hash(own, record, 7, patch)
    client = target_client()
    broker = target_world["headers"]["broker"]
    prefix = f"/api/v1/workspaces/{own}"
    fence = client.post(
        prefix + "/operation-fences",
        headers=broker,
        json=dict(operation_key=op, intent_digest=intent, monotonic_fence=42),
    )
    assert fence.status_code == 200, fence.text

    def attestation(**override):
        proposal = {
            "id": str(uuid4()),
            "workspace": own,
            "operation_key": op,
            "target_digest": intent,
            "target": record,
            "target_revision": 7,
            "source_digest": "a" * 64,
            "case_version": 1,
            "prepared_by": "reviewer",
            "approved_by": "approver",
            "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }
        proposal.update(override)
        return sign_approval(proposal)

    def foreign_key_signed():
        signed = attestation()
        key = Ed25519PrivateKey.generate()
        signed["signature"] = base64.b64encode(
            key.sign(canonical(signed["body"]))
        ).decode()
        return signed

    def tampered():
        signed = attestation(prepared_by="approver")
        signed["body"]["prepared_by"] = "reviewer"  # body edited after signing
        return signed

    grant = dict(
        operation_key=op,
        intent_digest=intent,
        target_id=record,
        expected_revision=7,
        target_fence=42,
        exact_patch=patch,
    )
    cells = []
    for label, approval, expected in (
        ("no_attestation", None, deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED")),
        (
            "self_approved_preparer_is_approver",
            attestation(prepared_by="approver"),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "empty_approver",
            attestation(approved_by=""),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "signed_by_untrusted_key",
            foreign_key_signed(),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "body_tampered_after_signing",
            tampered(),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "other_workspace_attestation",
            attestation(workspace=world["other"]),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "other_operation_attestation",
            attestation(operation_key=str(uuid4())),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "expired_attestation",
            attestation(
                expires=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            ),
            deny(403, "APPROVED_INTENT_ATTESTATION_REQUIRED"),
        ),
        (
            "dispatcher_token_with_valid_attestation",
            attestation(),
            deny(403, "SERVICE_SCOPE"),
        ),
        (
            "independent_approver",
            attestation(),
            allow("positive control: independent signed approval"),
        ),
    ):
        headers = target_world["headers"][
            "dispatcher" if label.startswith("dispatcher") else "broker"
        ]
        cells.append(
            run_cell(
                world,
                "target",
                client=client,
                method="POST",
                route=("POST", WB + "/operation-grants"),
                url=prefix + "/operation-grants",
                principal=label,
                workspace="own",
                csrf="n/a",
                obj="real",
                expected=expected,
                headers=headers,
                body={**grant, "approval": approval},
                effect_ok=expected["outcome"] == "allowed",
                fp_db=tdb,
                fp_query=TARGET_FP,
                note="approval independence",
            )
        )
    assert cells[-1]["actual"]["status"] == 200, cells[-1]
    failing = [c for c in cells if not c["pass"]]
    assert not failing, failures_text(failing)


# ---------------------------------------------------------------- evidence


@pytest.fixture(scope="module", autouse=True)
def evidence_writer():
    yield
    cells = RESULTS["workbench"] + RESULTS["scenario"] + RESULTS["target"]
    if not cells:
        return
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    def count(selected):
        selected = list({id(c): c for c in selected}.values())
        return {
            "cells": len(selected),
            "pass": sum(c["pass"] for c in selected),
            "fail": sum(not c["pass"] for c in selected),
        }

    denied = [c for c in cells if c["expected"]["outcome"] == "denied"]
    clauses = {
        "zero_unauthorized_reads_or_effects": count(
            denied + [c for c in cells if c["leak_checked"]]
        ),
        "session_required": count(
            [
                c
                for c in cells
                if c["expected"].get("code") in ("SESSION_REQUIRED", "INVALID_LOGIN")
            ]
        ),
        "membership_scope": count([c for c in cells if c["workspace"] == "foreign"]),
        "object_scope": count(
            [c for c in cells if c["object"] not in ("real", "n/a")]
            + [c for c in cells if c["expected"].get("code") == "NOT_FOUND"]
        ),
        "role_permission": count(
            [
                c
                for c in cells
                if c["expected"].get("code")
                in (
                    "PERMISSION_DENIED",
                    "EXPORT_DENIED",
                    "ORIGINAL_EXPORT_DENIED",
                    "SERVICE_SCOPE",
                    "SERVICE_BROWSER_DENIED",
                )
            ]
        ),
        "csrf_on_writes": count(
            [
                c
                for c in cells
                if c["csrf"] in ("absent_token", "wrong_origin")
                and c["route"] != "/api/v1/auth/login"
            ]
            + [c for c in cells if c["expected"].get("code") == "CSRF_DENIED"]
        ),
        "approver_independence": count(
            [
                c
                for c in cells
                if c.get("note") in ("approval independence",)
                or c["role"]
                in (
                    "dual_reviewer_approver_self",
                    "dual_not_the_approver",
                    "approver_independent",
                )
            ]
            + [
                c
                for c in cells
                if c["route"].endswith("/approvals") and c["role"] == "admin"
            ]
        ),
    }
    routes = {
        "workbench": len(WORKBENCH_ROUTES),
        "target": len(TARGET_ROUTES),
    }
    summary = {
        "schema": "folio-doc-t-auth-matrix-v1",
        "stamp": STAMP,
        "acceptance_row": "DOC-T-AUTH",
        "routes": routes,
        "principals": {
            "workbench": list(PRINCIPALS),
            "target": list(TARGET_PRINCIPALS),
        },
        "totals": count(cells),
        "by_matrix": {
            m: count(RESULTS[m]) for m in ("workbench", "scenario", "target")
        },
        "by_expected": {
            "allowed": count(
                [c for c in cells if c["expected"]["outcome"] == "allowed"]
            ),
            "denied": count(denied),
        },
        "clauses": clauses,
        "failing_cells": [c for c in cells if not c["pass"]][:200],
        "design_notes": DESIGN_NOTES,
        "probes": RESULTS["probes"],
        "database": "real PostgreSQL, disposable folio_finish_* databases via scripts/isolated_tests.py",
    }
    (EVIDENCE / "matrix.json").write_text(
        json.dumps(cells, indent=1, default=list), encoding="utf-8"
    )
    (EVIDENCE / "summary.json").write_text(
        json.dumps(summary, indent=2, default=list), encoding="utf-8"
    )
