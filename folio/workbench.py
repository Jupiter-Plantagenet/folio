"""Local Folio API: source-linked review, bounded model delegation, independently approved effects."""

import hashlib
import hmac
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext
from ipaddress import ip_address
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartException, MultiPartParser

from folio.dbpool import PoolTimeout
from folio.identity import service_header, sign_approval
from folio.intake_limits import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PACKET,
    MAX_LEGACY_JSON_BYTES,
    MAX_MULTIPART_FIELD_BYTES,
    MAX_MULTIPART_REQUEST_BYTES,
    source_within_packet_limits,
)
from folio.maintenance_gate import MaintenanceGate, maintenance_snapshot
from folio.privacy import PrivateApiHeaders

DSN = os.getenv(
    "FOLIO_DSN",
    # No password in source: supply FOLIO_DSN from the private profile, or PGPASSWORD/pgpass.
    "postgresql://folio_workbench@127.0.0.1:5541/folio_workbench",
)
WORKSPACE = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
ORIGIN = os.getenv("FOLIO_UI_ORIGIN", "http://127.0.0.1:5101")
INTAKE_SLOTS = threading.BoundedSemaphore(1)


class IntakeFilePartLimit(MultiPartException):
    """A multipart file exceeded its byte quota before any excess was spooled."""


class BoundedIntakeMultiPartParser(MultiPartParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._part_open = False
        self._saw_final_boundary = False

    def on_part_begin(self):
        super().on_part_begin()
        self._current_file_bytes = 0
        self._part_open = True

    def on_part_end(self):
        super().on_part_end()
        self._part_open = False

    def on_end(self):
        if self._part_open:
            raise MultiPartException("Multipart ended before the final part completed.")
        self._saw_final_boundary = True

    async def parse(self):
        form = await super().parse()
        if not self._saw_final_boundary or self._part_open:
            for spooled in self._files_to_close_on_error:
                spooled.close()
            raise MultiPartException("Multipart final boundary is missing or invalid.")
        return form

    def on_part_data(self, data, start, end):
        byte_count = end - start
        if self._current_part.file is not None:
            if self._current_file_bytes + byte_count > MAX_FILE_BYTES:
                raise IntakeFilePartLimit("FILE_LIMIT")
            self._current_file_bytes += byte_count
        super().on_part_data(data, start, end)


def _cookie_secure_setting():
    parsed = urlsplit(ORIGIN)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("FOLIO_UI_ORIGIN must be an http(s) origin")
    host = parsed.hostname.lower()
    local = host == "localhost" or host.endswith(".localhost")
    try:
        local = local or ip_address(host).is_loopback
    except ValueError:
        pass
    configured = os.getenv("FOLIO_COOKIE_SECURE")
    if configured is not None and configured.lower() not in ("true", "false"):
        raise RuntimeError("FOLIO_COOKIE_SECURE must be true or false")
    secure = configured.lower() == "true" if configured is not None else parsed.scheme == "https"
    if parsed.scheme != "https" and not local:
        raise RuntimeError("non-local FOLIO_UI_ORIGIN must use HTTPS")
    if parsed.scheme == "https" and not secure:
        raise RuntimeError("HTTPS Folio origins require Secure session cookies")
    if not local and not secure:
        raise RuntimeError("non-local Folio deployments require Secure session cookies")
    return secure


COOKIE_SECURE = _cookie_secure_setting()
TARGET_URL = os.getenv("FOLIO_TARGET_URL", "http://127.0.0.1:8201")
ACCOUNTS = {
    "reviewer": ["reviewer"],
    "approver": ["approver"],
    "viewer": ["viewer"],
    "admin": ["admin"],
    "auditor": ["auditor"],
}


def connect():
    """One dedicated connection (scripts, startup DDL checks, tests). Requests use pooled()."""
    return psycopg.connect(DSN)


POOL_MAX = int(os.getenv("FOLIO_DB_POOL_MAX", "10"))
POOL_TIMEOUT = float(os.getenv("FOLIO_DB_POOL_TIMEOUT", "10"))


def pooled(workspace=WORKSPACE):
    """Bounded pooled lease = one transaction with transaction-local tenant context.

    Commit on success, rollback on error, DISCARD ALL before reuse (folio/dbpool.py).
    Pool exhaustion raises PoolTimeout, which the API maps to 503 DATABASE_BUSY.
    """
    from folio.dbpool import pool_for

    return pool_for(DSN, POOL_MAX, POOL_TIMEOUT, "folio-api").lease(workspace)


def digest(v):
    return hashlib.sha256(
        json.dumps(
            v, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def command_route_fingerprint(method, path):
    return hashlib.sha256((method.upper() + "\n" + path).encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def fail(code, status=409):
    raise HTTPException(status, code)


def migrate():
    if os.getenv("FOLIO_SCHEMA_MODE") == "runtime":
        from folio.schema_journal import check_runtime

        with connect() as db:
            check_runtime(db)
        return
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users(username text PRIMARY KEY,salt text NOT NULL,password_hash text NOT NULL,roles jsonb NOT NULL,active boolean NOT NULL DEFAULT true);
        CREATE TABLE IF NOT EXISTS sessions(token text PRIMARY KEY,username text REFERENCES users(username),csrf text NOT NULL,expires timestamptz NOT NULL);
        CREATE TABLE IF NOT EXISTS cases(workspace uuid NOT NULL,id uuid NOT NULL,body jsonb NOT NULL,PRIMARY KEY(workspace,id));
        CREATE TABLE IF NOT EXISTS proposals(workspace uuid NOT NULL,id uuid PRIMARY KEY,case_id uuid NOT NULL,body jsonb NOT NULL,FOREIGN KEY(workspace,case_id) REFERENCES cases(workspace,id));
        CREATE TABLE IF NOT EXISTS jobs(id uuid PRIMARY KEY,workspace uuid NOT NULL,proposal uuid NOT NULL REFERENCES proposals(id),kind text NOT NULL DEFAULT 'dispatch',state text NOT NULL DEFAULT 'queued',attempt uuid,fence bigint NOT NULL DEFAULT 40,lease_until timestamptz,tries int NOT NULL DEFAULT 0,result jsonb);
        CREATE TABLE IF NOT EXISTS commands(workspace uuid NOT NULL,actor text NOT NULL,route text NOT NULL,key uuid NOT NULL,digest text NOT NULL,result jsonb NOT NULL,PRIMARY KEY(workspace,actor,route,key));
        CREATE TABLE IF NOT EXISTS originals(workspace uuid NOT NULL,id uuid PRIMARY KEY,case_id uuid NOT NULL,sha256 text NOT NULL,raw bytea,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),deleted_at timestamptz,FOREIGN KEY(workspace,case_id) REFERENCES cases(workspace,id));
        CREATE TABLE IF NOT EXISTS analysis_runs(id uuid PRIMARY KEY,workspace uuid NOT NULL,case_id uuid NOT NULL,body jsonb NOT NULL,status text NOT NULL DEFAULT 'queued',lease_until timestamptz,attempt uuid,tries int NOT NULL DEFAULT 0,FOREIGN KEY(workspace,case_id) REFERENCES cases(workspace,id));
        CREATE TABLE IF NOT EXISTS events(sequence bigserial PRIMARY KEY,workspace uuid NOT NULL,case_id uuid,kind text NOT NULL,actor text NOT NULL,at timestamptz NOT NULL DEFAULT clock_timestamp(),body jsonb NOT NULL);
        """)
        from pathlib import Path

        from folio.exports import install as install_exports
        from folio.schema_journal import install as install_migration

        install_migration(
            db,
            2,
            Path(__file__).resolve().parents[1] / "ops/migrations/002-workbench.sql",
        )
        install_exports(db)
        install_migration(  # round 6: expand-only read-path indexes (idempotent)
            db,
            5,
            Path(__file__).resolve().parents[1]
            / "ops/migrations/005-read-path-indexes.sql",
        )
        install_migration(
            db,
            8,
            Path(__file__).resolve().parents[1] / "ops/migrations/008-access-administration.sql",
        )
        for name, roles in ACCOUNTS.items():
            salt = "folio-local-" + name
            password = hashlib.scrypt(
                ("folio-" + name + "-local").encode(),
                salt=salt.encode(),
                n=16384,
                r=8,
                p=1,
            ).hex()
            db.execute(
                "INSERT INTO users(username,salt,password_hash,roles) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (name, salt, password, Jsonb(roles)),
            )


def event(db, case, kind, actor, body):
    db.execute(
        "INSERT INTO events(workspace,case_id,kind,actor,body) VALUES(%s,%s,%s,%s,%s)",
        (WORKSPACE, case, kind, actor, Jsonb(body)),
    )


def create_case(sources=None, target_id=TARGET, db=None):
    cid = str(uuid4())
    sources = sources or []
    c = dict(
        id=cid,
        label="CASE-" + cid[:6].upper(),
        workspace=WORKSPACE,
        version=1,
        state="needs_review",
        invoice="INV-1048",
        supplier="Aster Supply Co.",
        source_digest=digest(sources),
        sources=sources,
        target_id=target_id,
        target_revision=7,
        invoice_quantity="120",
        ordered_quantity="120",
        received_quantity="100",
        unit_price="48.00",
        tax_rate="0.08",
        invoice_face_amount="6220.80",
        currency="USD",
        unit="each",
        proposal=None,
        verification=None,
        model_gate="blocked: no application-authorized model runtime/weights/provider",
    )
    from contextlib import nullcontext

    with nullcontext(db) if db is not None else connect() as db:
        db.execute("INSERT INTO cases VALUES(%s,%s,%s)", (WORKSPACE, cid, Jsonb(c)))
        event(
            db,
            cid,
            "DOC-intake-accepted",
            "reviewer",
            {
                "source_digest": c["source_digest"],
                "authorship": "Folio independently authored synthetic fixture",
            },
        )
    return c


@asynccontextmanager
async def lifespan(app):
    migrate()
    yield


app = FastAPI(title="Folio document operations API", version="0.2.0", lifespan=lifespan)
app.add_middleware(PrivateApiHeaders)


def _restore_mode_probe():
    from folio.restore_mode import database_read_only

    with pooled() as db:
        return database_read_only(db)


def _restore_mode_guard(app):
    from folio.restore_mode import RestoreModeGuard

    return RestoreModeGuard(
        app,
        probe=_restore_mode_probe,
        exempt=("/api/v1/auth/login", "/api/v1/auth/logout"),
    )


# Read-only restore mode (DOC-T-RESTORE): refuses every API write with 503
# READ_ONLY_RESTORE_MODE while FOLIO_READ_ONLY or the database flag is set.
app.add_middleware(_restore_mode_guard)
app.add_middleware(MaintenanceGate)
P = "/api/v1/workspaces/{workspace}"


@app.exception_handler(PoolTimeout)
async def database_busy(req: Request, _error: PoolTimeout):
    # Bounded pool: shed load explicitly instead of opening connections without limit.
    return JSONResponse(
        status_code=503,
        content={"detail": "DATABASE_BUSY"},
        headers={"Retry-After": "1", "Cache-Control": "no-store"},
    )


@app.exception_handler(Exception)
async def unexpected_request_error(req: Request, _error: Exception):
    # ServerErrorMiddleware is outside user middleware, so set privacy headers
    # here too. Never return exception messages or connection details to clients.
    headers = (
        {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if req.url.path.startswith("/api/v1/")
        else {}
    )
    return JSONResponse(
        status_code=500, content={"detail": "INTERNAL_ERROR"}, headers=headers
    )


def identity(req, role=None):
    token = hashlib.sha256(req.cookies.get("folio_session", "").encode()).hexdigest()
    with pooled() as db:
        row = db.execute(
            "SELECT u.username,u.roles,s.csrf FROM sessions s JOIN users u USING(username) WHERE s.token=%s AND s.expires>clock_timestamp() AND u.active",
            (token,),
        ).fetchone()
    if not row:
        fail("SESSION_REQUIRED", 401)
    if role and role not in row[1]:
        fail("PERMISSION_DENIED", 403)
    if req.method not in ("GET", "HEAD"):
        if req.headers.get("origin") != ORIGIN or not hmac.compare_digest(
            req.headers.get("x-csrf-token", ""), row[2]
        ):
            fail("CSRF_DENIED", 403)
    return {"username": row[0], "roles": row[1], "csrf": row[2], "workspace": WORKSPACE}


def identity_in_transaction(db, req, role=None):
    """Revalidate and lock the current session through the installed read capability."""
    token = hashlib.sha256(req.cookies.get("folio_session", "").encode()).hexdigest()
    row = db.execute(
        "SELECT username,roles,csrf FROM public.folio_export_lock_actor(%s)",
        (token,),
    ).fetchone()
    if not row:
        fail("SESSION_REQUIRED", 401)
    if role and role not in row[1]:
        fail("PERMISSION_DENIED", 403)
    if req.method not in ("GET", "HEAD"):
        if req.headers.get("origin") != ORIGIN or not hmac.compare_digest(
            req.headers.get("x-csrf-token", ""), row[2]
        ):
            fail("CSRF_DENIED", 403)
    return {"username": row[0], "roles": row[1], "csrf": row[2], "workspace": WORKSPACE}


def scope(workspace):
    if workspace != WORKSPACE:
        fail("NOT_FOUND_OR_FORBIDDEN", 404)


def object_id(value):
    # A malformed object ID is an unknown object (404), never a uuid cast error (500).
    try:
        UUID(value)
    except ValueError:
        fail("NOT_FOUND", 404)
    return value


def load_case(db, workspace, cid):
    object_id(cid)
    row = db.execute(
        "SELECT body FROM cases WHERE workspace=%s AND id=%s FOR UPDATE",
        (workspace, cid),
    ).fetchone()
    if not row:
        fail("NOT_FOUND", 404)
    return row[0]


def save_case(db, c):
    db.execute(
        "UPDATE cases SET body=%s WHERE workspace=%s AND id=%s",
        (Jsonb(c), c["workspace"], c["id"]),
    )


def load_proposal(db, workspace, pid):
    row = db.execute(
        "SELECT body FROM proposals WHERE workspace=%s AND id=%s FOR UPDATE",
        (workspace, pid),
    ).fetchone()
    if not row:
        fail("NOT_FOUND", 404)
    return row[0]


def save_proposal(db, p):
    db.execute("UPDATE proposals SET body=%s WHERE id=%s", (Jsonb(p), p["id"]))


def command(db, req, user, b):
    key = req.headers.get("idempotency-key", "")
    try:
        UUID(key)
    except ValueError:
        fail("IDEMPOTENCY_KEY_UUID_REQUIRED", 422)
    fingerprint = digest({"body": b, "if_match": req.headers.get("if-match")})
    # Serialize concurrent browser replays on their actual scoped command identity.
    operation_hash = command_route_fingerprint(req.method, req.url.path)
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (user["username"] + operation_hash + key,),
    )
    row = db.execute(
        "SELECT digest,result FROM commands WHERE workspace=%s AND actor=%s AND route=%s AND key=%s",
        (WORKSPACE, user["username"], operation_hash, key),
    ).fetchone()
    # Read old raw-path receipts during the transition. New receipts always use
    # the method+route hash, which is safe to persist in a browser blocker.
    if not row:
        row = db.execute(
            "SELECT digest,result FROM commands WHERE workspace=%s AND actor=%s AND route=%s AND key=%s",
            (WORKSPACE, user["username"], req.url.path, key),
        ).fetchone()
    if row:
        if row[0] != fingerprint:
            fail("IDEMPOTENCY_DIGEST_MISMATCH")
        result = row[1]
        # Idempotent replay is still an ordinary response path. If the recorded
        # response contains a proposal, apply the same private-archive-backed
        # projection used by case detail without changing the receipt bytes.
        proposal = result.get("proposal") if isinstance(result, dict) else None
        if isinstance(proposal, dict) and proposal.get("id"):
            ref = db.execute(
                "SELECT case_id,body FROM proposals WHERE workspace=%s AND id=%s",
                (WORKSPACE, proposal["id"]),
            ).fetchone()
            if ref:
                case_row = db.execute(
                    "SELECT body FROM cases WHERE workspace=%s AND id=%s",
                    (WORKSPACE, ref[0]),
                ).fetchone()
                if case_row:
                    from folio.retention_backend import project_proposal_reason

                    result = dict(result)
                    result["proposal"] = project_proposal_reason(
                        WORKSPACE, proposal["id"], ref[0], ref[1], case_row[0]
                    )
        return result
    return None


def recorded(db, req, user, b, result):
    db.execute(
        "INSERT INTO commands VALUES(%s,%s,%s,%s,%s,%s)",
        (
            WORKSPACE,
            user["username"],
            command_route_fingerprint(req.method, req.url.path),
            req.headers["idempotency-key"],
            digest({"body": b, "if_match": req.headers.get("if-match")}),
            Jsonb(result),
        ),
    )
    return result


def etag(req, kind, version):
    if req.headers.get("if-match") != f'"{kind}-v{version}"':
        fail("STALE_VERSION", 412)


JOB_AGE_ALARM_SECONDS = 120
VERIFIER_LAG_ALARM_SECONDS = 120


def queue_health(db):
    """Queue ages, verifier lag and unknown outcomes (DOC-T-RECOVERY observability).

    Job age is measured from approval (the job is inserted in the approval transaction).
    Verifier lag is the age of the oldest sealed-but-unverified effect: a non-terminal job
    whose proposal already holds a target grant. Both use the database clock.
    """
    live = "(j.state IN ('queued','leased') OR (j.state='uncertain' AND j.tries<5))"
    row = db.execute(
        "SELECT count(*) FILTER (WHERE j.state IN ('queued','leased')),"
        " count(*) FILTER (WHERE j.state='uncertain' AND j.tries<5),"
        " count(*) FILTER (WHERE j.state='exhausted'),"
        " extract(epoch FROM max(clock_timestamp()-(p.body->>'approved_at')::timestamptz)"
        f" FILTER (WHERE {live})),"
        " extract(epoch FROM max(clock_timestamp()-(p.body->'grant'->>'sealed_at')::timestamptz)"
        f" FILTER (WHERE {live} AND p.body ? 'grant'))"
        " FROM jobs j JOIN proposals p ON p.id=j.proposal"
    ).fetchone()
    runs = db.execute(
        "SELECT count(*), extract(epoch FROM max(clock_timestamp()-(body->>'created_at')::timestamptz))"
        " FROM analysis_runs WHERE status IN ('queued','running')"
    ).fetchone()
    exports = None
    try:
        with db.transaction():
            exports = db.execute(
                "SELECT count(*), extract(epoch FROM max(clock_timestamp()-created_at))"
                " FROM export_jobs WHERE state IN ('queued','leased')"
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        pass
    oldest = round(float(row[3] or 0), 1)
    lag = round(float(row[4] or 0), 1)
    alarms = []
    if oldest > JOB_AGE_ALARM_SECONDS:
        alarms.append("JOB_AGE_GT_120S")
    if lag > VERIFIER_LAG_ALARM_SECONDS:
        alarms.append("VERIFIER_LAG_GT_120S")
    if row[2]:
        alarms.append("UNKNOWN_OUTCOME_REQUIRES_ACTION")
    queues = {
        "pending_jobs": row[0],
        "retrying_jobs": row[1],
        "unknown_outcome_jobs": row[2],
        "oldest_job_age_s": oldest,
        "verifier_lag_s": lag,
        "analysis_runs_pending": runs[0],
        "oldest_analysis_run_age_s": round(float(runs[1] or 0), 1),
    }
    if exports is not None:
        queues["export_jobs_pending"] = exports[0]
        queues["oldest_export_job_age_s"] = round(float(exports[1] or 0), 1)
        if (exports[1] or 0) > JOB_AGE_ALARM_SECONDS:
            alarms.append("EXPORT_JOB_AGE_GT_120S")
    return queues, alarms


def worker_health():
    """Worker heartbeat written by folio.worker --loop (FOLIO_WORKER_HEARTBEAT)."""
    path = os.getenv("FOLIO_WORKER_HEARTBEAT")
    if not path:
        return None, []
    try:
        with open(path, encoding="utf-8") as beat:
            body = json.load(beat)
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(body["at"])
        ).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unknown"}, ["WORKER_HEARTBEAT_MISSING"]
    alarms = []
    if age > 60:
        alarms.append("WORKER_HEARTBEAT_STALE")
    if body.get("state") != "ok":
        alarms.append("WORKER_" + str(body.get("state", "unknown")).upper())
    return {
        "state": body.get("state"),
        "heartbeat_age_s": round(age, 1),
        "consecutive_failures": body.get("consecutive_failures"),
    }, alarms


@app.get("/health")
def health(req: Request):
    maintenance = maintenance_snapshot()
    if maintenance["maintenance"] or req.scope.get("state", {}).get("maintenance_gate_active"):
        # Maintenance polling must remain process-local and database-independent.
        return JSONResponse(
            status_code=200,
            content={
                "status": "maintenance",
                "database": "not_checked_during_maintenance",
                "release_ready": False,
                **{**maintenance, "maintenance": True},
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    worker, worker_alarms = worker_health()
    try:
        with pooled() as db:
            db.execute("SELECT 1")
            queues, alarms = queue_health(db)
            from folio.restore_mode import database_read_only, env_read_only

            write_mode = (
                "read_only_env"
                if env_read_only()
                else "read_only_database"
                if database_read_only(db)
                else "read_write"
            )
    except (psycopg.OperationalError, PoolTimeout) as error:
        # Readiness, not liveness: report the dependency outage explicitly (no details).
        return JSONResponse(
            status_code=503,
            content={
                "status": "unready",
                "database": "unavailable",
                "reason": type(error).__name__,
                **({"worker": worker} if worker is not None else {}),
                "alarms": ["DATABASE_UNAVAILABLE"] + worker_alarms,
                "release_ready": False,
                **maintenance_snapshot(),
            },
            headers={"Cache-Control": "no-store"},
        )
    return {
        "status": "ready",
        "database": "PostgreSQL",
        **queues,
        **({"worker": worker} if worker is not None else {}),
        "write_mode": write_mode,
        "alarms": alarms + worker_alarms,
        "model": model_readiness(),
        "model_quality_gate": "not_passed",
        "release_ready": False,
        **maintenance_snapshot(),
    }


def model_readiness():
    """Loopback-only probe of the operator-launched local model; never a provider."""
    import httpx

    marker = os.getenv("FOLIO_MODEL_PAUSE_MARKER")
    if marker and os.path.exists(marker):
        return (
            "paused"  # operator-paused via live_supervisor pause-model; never "ready"
        )
    try:
        with httpx.Client(trust_env=False, timeout=0.5, follow_redirects=False) as c:
            ok = c.get("http://127.0.0.1:18301/health").status_code == 200
    except httpx.HTTPError:
        ok = False
    return "ready-local-loopback" if ok else "unavailable"


@app.post("/api/v1/auth/login")
def login(b: dict, response: Response):
    with pooled() as db:
        row = db.execute(
            "SELECT salt,password_hash,roles FROM users WHERE username=%s AND active",
            (b.get("username"),),
        ).fetchone()
        if not row:
            fail("INVALID_LOGIN", 401)
        candidate = hashlib.scrypt(
            str(b.get("password", "")).encode(), salt=row[0].encode(), n=16384, r=8, p=1
        ).hex()
        if not hmac.compare_digest(candidate, row[1]):
            fail("INVALID_LOGIN", 401)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO sessions VALUES(%s,%s,%s,clock_timestamp()+interval '8 hours')",
            (hashlib.sha256(token.encode()).hexdigest(), b["username"], csrf),
        )
    response.set_cookie(
        "folio_session",
        token,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
        max_age=28800,
    )
    return {
        "username": b["username"],
        "roles": row[2],
        "csrf": csrf,
        "workspace": WORKSPACE,
    }


@app.get("/api/v1/auth/session")
def session(req: Request):
    return identity(req)


@app.post("/api/v1/auth/logout")
def logout(req: Request, response: Response):
    user = identity(req)
    body = {}
    with pooled() as db:
        user = identity_in_transaction(db, req)
        prior = command(db, req, user, body)
        if prior is not None:
            response.delete_cookie("folio_session")
            return prior
        result = {"logged_out": True}
        recorded(db, req, user, body, result)
        db.execute(
            "DELETE FROM sessions WHERE token=%s",
            (
                hashlib.sha256(
                    req.cookies.get("folio_session", "").encode()
                ).hexdigest(),
            ),
        )
    response.delete_cookie("folio_session")
    return result


@app.get("/api/v1/auth/mutation-receipts/{idempotency_key}")
def mutation_receipt_status(idempotency_key: str, operation_hash: str, req: Request):
    """Minimal status lookup scoped to the current server-authenticated principal."""
    from folio.access_administration import lookup_command_status

    user = identity(req)
    try:
        key = str(UUID(idempotency_key))
    except (TypeError, ValueError):
        fail("NOT_FOUND", 404)
    if key != idempotency_key:
        fail("NOT_FOUND", 404)
    with pooled() as db:
        user = identity_in_transaction(db, req)
        return lookup_command_status(
            db, WORKSPACE, user["username"], key, operation_hash
        )


@app.get(P + "/settings")
def workspace_settings(workspace: str, req: Request):
    from folio.access_administration import settings_snapshot

    user = identity(req)
    scope(workspace)
    with pooled() as db:
        user = identity_in_transaction(db, req)
        return settings_snapshot(db, workspace, user["username"], user["roles"])


@app.post(P + "/settings/access-requests")
def create_access_request(workspace: str, req: Request, body: dict):
    from folio.access_administration import authorize_mutation, create_request

    user = identity(req, "admin")
    scope(workspace)
    with pooled() as db:
        user = authorize_mutation(db, req, workspace, "create")
        prior = command(db, req, user, body)
        if prior is not None:
            return prior
        result = create_request(
            db, req, workspace, body, req.headers.get("if-match", "")
        )
        return recorded(db, req, user, body, result)


@app.post(P + "/settings/access-requests/{request_id}/review")
def review_access_request(workspace: str, request_id: str, req: Request, body: dict):
    from folio.access_administration import authorize_mutation, review_request

    user = identity(req)
    scope(workspace)
    try:
        request_uuid = str(UUID(request_id))
    except (TypeError, ValueError):
        fail("NOT_FOUND", 404)
    if request_uuid != request_id:
        fail("NOT_FOUND", 404)
    with pooled() as db:
        user = authorize_mutation(db, req, workspace, "review", request_uuid)
        prior = command(db, req, user, body)
        if prior is not None:
            return prior
        result = review_request(
            db, req, workspace, request_uuid, req.headers.get("if-match", ""), body,
        )
        return recorded(db, req, user, body, result)


@app.get(P + "/cases")
def cases(workspace: str, req: Request):
    identity(req)
    scope(workspace)
    # Only explicitly imported user packets populate the operational queue. Fault-harness
    # records remain individually inspectable by UUID and in evidence, not fabricated work.
    # Round 6: list items are summaries. Parsed page text/words/boxes and field provenance
    # are only in GET /cases/{id}; shipping them for every queue row dominated read p95.
    with pooled() as db:
        from folio.retention_backend import project_current_decision

        items = db.execute(
            "SELECT id,(body - 'sources' - 'field_provenance') || jsonb_build_object("
            "'source_count', CASE jsonb_typeof(body->'sources') WHEN 'array' "
            "THEN jsonb_array_length(body->'sources') ELSE 0 END)"
            " FROM cases WHERE workspace=%s AND (body ? 'sample_digest' OR body ? 'intake_digest') ORDER BY id",
            (workspace,),
        ).fetchall()
        projected = []
        for case_id, item in items:
            item["decision"] = project_current_decision(workspace, case_id, item)
            projected.append(item)
        return {
            "projection": "queue-summary-v1",
            "items": projected,
        }


@app.get(P + "/cases/{cid}")
def detail(workspace: str, cid: str, req: Request):
    user = identity(req)
    scope(workspace)
    with pooled() as db:
        c = load_case(db, workspace, cid)
        from folio.retention_backend import (
            project_current_decision,
            project_proposal_reason,
        )

        c["decision"] = project_current_decision(workspace, cid, c)
        if c.get("proposal"):
            c["proposal"] = load_proposal(db, workspace, c["proposal"]["id"])
            c["proposal"] = project_proposal_reason(
                workspace, c["proposal"]["id"], cid, c["proposal"], c
            )
        c["jobs"] = [
            {
                "id": str(r[0]),
                "state": r[1],
                "fence": r[2],
                "attempt": str(r[3]) if r[3] else None,
                "result": r[4],
            }
            for r in db.execute(
                "SELECT id,state,fence,attempt,result FROM jobs WHERE workspace=%s AND proposal IN(SELECT id FROM proposals WHERE case_id=%s)",
                (workspace, cid),
            )
        ]
        c["analysis_runs"] = [
            r[0]
            for r in db.execute(
                "SELECT body FROM analysis_runs WHERE workspace=%s AND case_id=%s ORDER BY body->>'created_at'",
                (workspace, cid),
            )
        ]
        # Response-only advisory metadata; the download route rechecks every byte.
        from folio.original_download import original_availability

        c["original_availability"] = original_availability(
            db, workspace, cid, c, user["roles"]
        )
        return c


@app.post(P + "/cases/{cid}/target-match")
def target_match(workspace: str, cid: str, b: dict, req: Request):
    import httpx

    user = identity(req, "reviewer")
    scope(workspace)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        if c.get("proposal"):
            p = load_proposal(db, workspace, c["proposal"]["id"])
            if p.get("gate") in ("seal_pending", "sealed", "uncertain", "applied"):
                fail("TOO_LATE_SEALED_OR_PENDING")
            p["state"] = "superseded"
            save_proposal(db, p)
        if not c.get("invoice") or not c.get("supplier"):
            fail("BUSINESS_IDENTITY_REQUIRED", 422)
        try:
            response = httpx.get(
                TARGET_URL + f"/api/v1/workspaces/{workspace}/payables",
                params={"invoice_key": c["invoice"], "supplier": c["supplier"]},
                headers=service_header("verifier"),
                timeout=5,
            )
            response.raise_for_status()
            observed = response.json()
        except httpx.HTTPError:
            fail("TARGET_UNAVAILABLE", 503)
        matches = [
            r
            for r in observed["items"]
            if r["currency"] == c.get("currency")
            and r.get("unit", "each") == c.get("unit")
            and r["invoice_face_amount"] == c.get("invoice_face_amount")
        ]
        selected = b.get("selected_target")
        if selected and selected not in [r["id"] for r in matches]:
            fail("TARGET_NOT_IN_INDEPENDENT_MATCH_SET", 422)
        chosen = (
            next((r for r in matches if r["id"] == selected), None)
            if selected
            else (
                matches[0] if len(matches) == 1 and not observed["truncated"] else None
            )
        )
        c["target_candidates"] = [
            {
                "id": r["id"],
                "revision": r["revision"],
                "invoice_key": r["invoice_key"],
                "supplier": r["supplier"],
            }
            for r in matches
        ]
        c["target_id"] = chosen["id"] if chosen else None
        c["target_revision"] = chosen["revision"] if chosen else None
        c["target_match"] = {
            "kind": "independent_lookup",
            "at": now(),
            "selected_by": user["username"],
            "match_count": len(matches),
            "truncated": observed["truncated"],
            "record_digest": digest(chosen) if chosen else None,
        }
        c["version"] += 1
        c["state"] = (
            "needs_review"
            if chosen
            else ("ambiguous_match" if matches else "awaiting_info")
        )
        save_case(db, c)
        event(db, cid, "DOC-target-matched", user["username"], c["target_match"])
        return recorded(
            db,
            req,
            user,
            b,
            {
                "target_id": c["target_id"],
                "state": c["state"],
                "case_version": c["version"],
                "candidates": c["target_candidates"],
            },
        )


@app.post(P + "/cases/{cid}/analysis-runs", status_code=202)
def enqueue_analysis(workspace: str, cid: str, b: dict, req: Request):
    user = identity(req, "reviewer")
    scope(workspace)
    if (
        set(b) - {"prepare_proposal"}
        or type(b.get("prepare_proposal", False)) is not bool
    ):
        fail("ANALYSIS_COMMAND_HAS_NO_PROVIDER_OR_TOOL_OVERRIDES", 422)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        if b.get("prepare_proposal", False):
            from folio.analysis_authority import requester_problem

            problem = requester_problem(db, user["username"])
            if problem:
                fail(problem, 403 if problem == "REQUESTER_AUTHORITY_REVOKED" else 503)
        run = {
            "id": str(uuid4()),
            "case_id": cid,
            "workspace": workspace,
            "case_version": c["version"],
            "source_digest": c["source_digest"],
            "requested_by": user["username"],
            "prepare_proposal": b.get("prepare_proposal", False),
            "status": "queued",
            "created_at": now(),
        }
        db.execute(
            "INSERT INTO analysis_runs(id,workspace,case_id,body) VALUES(%s,%s,%s,%s)",
            (run["id"], workspace, cid, Jsonb(run)),
        )
        event(db, cid, "DOC-analysis-enqueued", user["username"], {"run_id": run["id"]})
        return recorded(db, req, user, b, {"run_id": run["id"], "status": "queued"})


@app.get(P + "/cases/{cid}/analysis-runs/{rid}")
def analysis_detail(workspace: str, cid: str, rid: str, req: Request):
    identity(req)
    scope(workspace)
    with pooled() as db:
        load_case(db, workspace, cid)
        row = db.execute(
            "SELECT body FROM analysis_runs WHERE workspace=%s AND case_id=%s AND id=%s",
            (workspace, cid, object_id(rid)),
        ).fetchone()
        if not row:
            fail("NOT_FOUND", 404)
        return row[0]


def apply_decision(db, c, b, actor):
    """Shared server-validated decision command; callers supply authenticated authority.
    Model callers cannot supply actor, workspace, target patch or approval authority.
    """
    workspace, cid = c["workspace"], c["id"]
    if c["state"] in ("seal_pending", "sealed", "uncertain", "verified"):
        fail("DISPATCH_ALREADY_SEALED")
    if not str(b.get("reason", "")).strip():
        fail("REASON_REQUIRED", 422)
    if b.get("evidence_digest") != c["source_digest"]:
        fail("SOURCE_STALE", 412)
    mode = b.get("mode")
    if mode not in ("partial", "hold", "escalate", "no_change"):
        fail("MODE_INVALID", 422)
    from folio.allocations import allocate, invalidate

    if "receipt_manifest" in c and mode in ("partial", "no_change"):
        from folio.persisted_receipts import manifest as validate_manifest

        try:
            validate_manifest(c["receipt_manifest"])
        except ValueError as error:
            fail(str(error), 422)
    if c.get("sources"):
        from folio.receipts import receipt_manifest

        c["receipt_manifest"] = receipt_manifest(c["sources"])
    if mode in ("partial", "no_change"):
        if any(
            request.get("state") == "open"
            for request in c.get("information_requests", [])
        ):
            fail("INFORMATION_REQUEST_OPEN", 422)
        if any(source.get("original_deleted") for source in c["sources"]):
            fail("SOURCE_ORIGINAL_UNAVAILABLE", 422)
        from folio.provenance import FIELDS, validate_value

        try:
            for field in FIELDS:
                validate_value(field, c.get(field, ""))
        except ValueError as error:
            fail(str(error), 422)
        if c.get("missing_fields") or c.get("conflicting_fields"):
            fail("REQUIRED_SUPPORT_UNRESOLVED", 422)
        if c.get("receipt_manifest") and any(
            row["unit"] != c["unit"]
            or row.get("currency") not in (None, c["currency"])
            for row in c["receipt_manifest"]["rows"]
        ):
            fail("CURRENCY_OR_UNIT_MISMATCH", 422)
        from folio.money import scale_ok

        if not scale_ok(c["invoice_face_amount"], c["currency"]):
            fail("DECIMAL_SCALE_INVALID", 422)
        if "receipt_manifest" in c and (
            c["receipt_manifest"]["errors"] or not c["receipt_manifest"]["rows"]
        ):
            fail("RECEIPT_SUPPORT_UNRESOLVED", 422)
        if c.get("receipt_manifest") and Decimal(c["received_quantity"]) > Decimal(
            c["receipt_manifest"]["total"]
        ):
            fail("QUANTITY_EXCEEDS_SUPPORTED_RECEIPT", 422)
    invalidate(db, c)
    if mode == "no_change":
        from folio.verifier import check_no_change

        verified = check_no_change(c)
        if verified["outcome"] != "no_change":
            fail(
                "NO_CHANGE_NOT_INDEPENDENTLY_MATCHED",
                503 if verified["outcome"] == "unavailable" else 422,
            )
        c["verification"] = verified
    c["decision"] = {
        "mode": mode,
        "reason": b["reason"],
        "actor": actor,
        "at": now(),
    }
    if mode == "partial":
        if any(source.get("original_deleted") for source in c["sources"]):
            fail("SOURCE_ORIGINAL_UNAVAILABLE", 422)
        if not c.get("target_id") or any(
            not c.get(k)
            for k in [
                "invoice_quantity",
                "ordered_quantity",
                "received_quantity",
                "unit_price",
                "tax_rate",
                "invoice_face_amount",
                "currency",
                "unit",
                "supplier",
                "invoice",
            ]
        ):
            fail("MISSING_EVIDENCE_OR_TARGET_MATCH", 422)
        import re

        if not re.fullmatch(r"(0|[1-9]\d*)(\.\d{1,6})?", str(b.get("quantity", ""))):
            fail("QUANTITY_INVALID", 422)
        q = Decimal(b["quantity"])
        if (
            not 0
            < q
            <= min(
                Decimal(c["received_quantity"]),
                Decimal(c["invoice_quantity"]),
                Decimal(c["ordered_quantity"]),
            )
        ):
            fail("QUANTITY_EXCEEDS_SUPPORTED_RECEIPT", 422)
        try:
            allocation = allocate(db, c, b["quantity"])
        except ValueError as error:
            fail(str(error), 422)
        with localcontext() as arithmetic_context:
            from folio.money import minor_units

            arithmetic_context.prec = 64
            quantum = Decimal(1).scaleb(-minor_units(c["currency"]))
            net = (q * Decimal(c["unit_price"])).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
            tax = (net * Decimal(c["tax_rate"])).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
        patch = dict(
            recognized_quantity=format(q.normalize(), "f"),
            net_amount=str(net),
            tax_amount=str(tax),
            gross_amount=str(net + tax),
            status="partially_matched",
        )
        if net + tax > Decimal(c["invoice_face_amount"]):
            fail("GROSS_EXCEEDS_FACE", 422)
        # Independent target's canonical versioned wire intent is implemented here, not imported.
        import httpx

        try:
            observed = httpx.get(
                f"{TARGET_URL}/api/v1/workspaces/{workspace}/payables/{c['target_id']}",
                headers=service_header("verifier"),
                timeout=5,
            )
            observed.raise_for_status()
        except httpx.HTTPError:
            fail("TARGET_UNAVAILABLE", 503)
        before = observed.json()
        if before.get("unit", "each") != c["unit"]:
            fail("TARGET_BUSINESS_IDENTITY_CONFLICT", 412)
        if before["revision"] != c["target_revision"]:
            fail("TARGET_STALE", 412)
        if any(
            before.get(k) != v
            for k, v in {
                "currency": c["currency"],
                "invoice_key": c["invoice"],
                "supplier": c["supplier"],
                "invoice_face_amount": c["invoice_face_amount"],
            }.items()
        ):
            fail("TARGET_BUSINESS_IDENTITY_CONFLICT", 412)
        target_digest = digest(
            dict(
                schema="northstar-intent-v1",
                workspace=workspace,
                target=c["target_id"],
                revision=c["target_revision"],
                patch=patch,
            )
        )
        p = dict(
            id=str(uuid4()),
            case_id=cid,
            workspace=workspace,
            version=1,
            case_version=c["version"] + 1,
            source_digest=c["source_digest"],
            target=c["target_id"],
            target_revision=c["target_revision"],
            patch=patch,
            operation_key=str(uuid4()),
            prepared_by=actor,
            expires=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            state="pending",
            target_digest=target_digest,
            reason=b["reason"],
        )
        if allocation is not None:
            p["allocation_manifest"] = allocation
        p["retained_balance"] = str(Decimal(c["invoice_face_amount"]) - net - tax)
        p["before_snapshot"] = before
        p["before_snapshot_digest"] = digest(before)
        p["digest"] = digest(p)
        c["proposal"] = p
        c["state"] = "awaiting_approval"
        db.execute(
            "INSERT INTO proposals VALUES(%s,%s,%s,%s)",
            (workspace, p["id"], cid, Jsonb(p)),
        )
    else:
        c["proposal"] = None
        c["state"] = {
            "hold": "held",
            "escalate": "escalated",
            "no_change": "no_change",
        }[mode]
    c["version"] += 1
    save_case(db, c)
    event(db, cid, "DOC-decision-saved", actor, c["decision"])
    return {
        "case_version": c["version"],
        "proposal": c["proposal"],
        "state": c["state"],
    }


@app.post(P + "/cases/{cid}/information-requests", status_code=201)
def request_information(workspace: str, cid: str, b: dict, req: Request):
    from folio.allocations import invalidate
    from folio.provenance import FIELDS

    user = identity(req, "reviewer")
    scope(workspace)
    fields = b.get("fields")
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(f, str) or f not in FIELDS for f in fields)
        or not isinstance(b.get("reason"), str)
        or not b["reason"].strip()
        or len(b["reason"]) > 1000
    ):
        fail("INFORMATION_REQUEST_INVALID", 422)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        unresolved = set(c.get("missing_fields", [])) | set(
            c.get("conflicting_fields", [])
        )
        if not set(fields).issubset(unresolved):
            fail("INFORMATION_REQUEST_FIELDS_NOT_UNRESOLVED", 422)
        invalidate(db, c)
        item = {
            "id": str(uuid4()),
            "fields": sorted(set(fields)),
            "reason": b["reason"],
            "actor": user["username"],
            "at": now(),
            "source_digest": c["source_digest"],
            "state": "open",
        }
        c.setdefault("information_requests", []).append(item)
        c["version"] += 1
        c["state"] = "awaiting_info"
        save_case(db, c)
        event(db, cid, "DOC-information-requested", user["username"], item)
        return recorded(
            db, req, user, b, {"case_version": c["version"], "request": item}
        )


@app.post(P + "/cases/{cid}/decisions", status_code=201)
def decide(workspace: str, cid: str, b: dict, req: Request):
    user = identity(req, "reviewer")
    scope(workspace)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        result = apply_decision(db, c, b, user["username"])
        return recorded(db, req, user, b, result)


@app.post(P + "/proposals/{pid}/approvals", status_code=201)
def approve(workspace: str, pid: str, b: dict, req: Request):
    user = identity(req, "approver")
    scope(workspace)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        # Consistent order: case then proposal, matching corrections and worker seal.
        ref = db.execute(
            "SELECT case_id FROM proposals WHERE workspace=%s AND id=%s",
            (workspace, object_id(pid)),
        ).fetchone()
        if not ref:
            fail("NOT_FOUND", 404)
        c = load_case(db, workspace, str(ref[0]))
        p = load_proposal(db, workspace, pid)
        from folio.persisted_receipts import expiry
        from folio.persisted_receipts import proposal as validate_proposal

        try:
            validate_proposal(p, c, workspace, current=True)
            expires = expiry(p)
        except ValueError:
            fail("SOURCE_TARGET_POLICY_STALE", 412)
        etag(req, "proposal", p["version"])
        if p["prepared_by"] == user["username"]:
            fail("SELF_APPROVAL", 403)
        if (
            p["state"] != "pending"
            or p["case_version"] != c["version"]
            or p["source_digest"] != c["source_digest"]
            or expires <= datetime.now(timezone.utc)
        ):
            fail("SOURCE_TARGET_POLICY_STALE", 412)
        if b.get("intent_digest") != p["digest"]:
            fail("DIGEST_MISMATCH", 422)
        if b.get("confirmation") == "approve":
            import httpx

            try:
                latest = httpx.get(
                    f"{TARGET_URL}/api/v1/workspaces/{workspace}/payables/{p['target']}",
                    headers=service_header("verifier"),
                    timeout=5,
                )
                latest.raise_for_status()
            except httpx.HTTPError:
                fail("TARGET_UNAVAILABLE", 503)
            if digest(latest.json()) != p.get("before_snapshot_digest"):
                fail("TARGET_STALE", 412)
            if (
                c.get("sources")
                or "receipt_manifest" in c
                or "allocation_manifest" in p
            ):
                from folio.allocations import allocate, receipt_lock

                # Case -> proposal -> receipt lock, as prepare. Slow HTTP is outside
                # the workspace lock; promotion and its final checks are inside it.
                receipt_lock(db, workspace)
                if (
                    p["state"] != "pending"
                    or p["case_version"] != c["version"]
                    or p["source_digest"] != c["source_digest"]
                    or expiry(p) <= datetime.now(timezone.utc)
                    or not p.get("allocation_manifest")
                    or not c.get("receipt_manifest")
                ):
                    fail("SOURCE_TARGET_POLICY_STALE", 412)
                try:
                    # Uploaded and legacy packets require original-source authority,
                    # regardless of kind/role. Caches cannot opt out of this gate.
                    # Re-read support only: never repair a case or frozen intent here.
                    from folio.receipts import support_matches_sources

                    manifest = c["receipt_manifest"]
                    if (
                        not isinstance(manifest, dict)
                        or manifest.get("policy") != "receipt-allocation-v1"
                        or manifest.get("errors") != []
                        or not isinstance(manifest.get("rows"), list)
                        or not manifest["rows"]
                        or not isinstance(p["allocation_manifest"], dict)
                        or any(s.get("original_deleted") for s in c.get("sources", []))
                        or (
                            c.get("sources")
                            and not support_matches_sources(manifest, c["sources"])
                        )
                    ):
                        fail("SOURCE_TARGET_POLICY_STALE", 412)
                    allocate(
                        db,
                        c,
                        p["patch"]["recognized_quantity"],
                        frozen=p["allocation_manifest"],
                    )
                except ValueError:
                    fail("SOURCE_TARGET_POLICY_STALE", 412)
                if expiry(p) <= datetime.now(timezone.utc):
                    fail("SOURCE_TARGET_POLICY_STALE", 412)
        if b.get("confirmation") == "reject":
            if not b.get("reason", "").strip():
                fail("REASON_REQUIRED", 422)
            p["state"] = "rejected"
            c["state"] = "needs_review"
            jid = None
        elif b.get("confirmation") == "approve":
            p["state"] = "approved"
            p["approved_by"] = user["username"]
            p["approved_at"] = now()
            p["approval_attestation"] = sign_approval(p)
            p["gate"] = "prepared"
            jid = str(uuid4())
            c["state"] = "dispatching"
            db.execute(
                "INSERT INTO jobs(id,workspace,proposal) VALUES(%s,%s,%s)",
                (jid, workspace, pid),
            )
        else:
            fail("CONFIRMATION_REQUIRED", 422)
        save_proposal(db, p)
        save_case(db, c)
        event(
            db,
            c["id"],
            "DOC-approval-changed",
            user["username"],
            {"proposal": pid, "state": p["state"]},
        )
        return recorded(
            db,
            req,
            user,
            b,
            {
                "approval_state": p["state"],
                "job_id": jid,
                "operation_key": p["operation_key"],
            },
        )


@app.patch(P + "/cases/{cid}/fields/{field}")
def correct(workspace: str, cid: str, field: str, b: dict, req: Request):
    user = identity(req, "reviewer")
    scope(workspace)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        if c.get("proposal"):
            p = load_proposal(db, workspace, c["proposal"]["id"])
            if p.get("gate") in ("seal_pending", "sealed", "uncertain", "applied"):
                fail("TOO_LATE_SEALED_OR_PENDING")
            p["state"] = "superseded"
            save_proposal(db, p)
        from folio.provenance import validate_citation, validate_value

        if (
            not isinstance(b.get("reason"), str)
            or not b["reason"].strip()
            or len(b["reason"]) > 1000
        ):
            fail("REASON_REQUIRED", 422)
        try:
            value = validate_value(field, b.get("typed_value"))
            source = (
                validate_citation(c["sources"], b["source"])
                if b.get("source")
                else None
            )
        except ValueError as error:
            fail(str(error), 422)
        versions = c.setdefault("field_provenance", {}).setdefault(field, [])
        correction = {
            "field": field,
            "old": c.get(field),
            "new": value,
            "previous_value": c.get(field),
            "value": value,
            "reason": b["reason"],
            "actor": user["username"],
            "kind": "human",
            "at": now(),
            "version": len(versions) + 1,
            "source": source,
        }
        versions.append(correction)
        c.setdefault("corrections", []).append(correction)
        c[field] = value
        c["conflicting_fields"] = [
            f for f in c.get("conflicting_fields", []) if f != field
        ]
        from folio.allocations import refresh_support

        refresh_support(c)
        c["source_digest"] = digest(
            {"sources": c["sources"], "field_provenance": c["field_provenance"]}
        )
        c["version"] += 1
        save_case(db, c)
        event(db, cid, "DOC-field-corrected", user["username"], c["corrections"][-1])
        return recorded(db, req, user, b, {"case_version": c["version"]})


def existing_original_packet(db, workspace, sources):
    hashes = sorted(source["sha256"] for source in sources)
    # The same byte identity linearizes both authored and uploaded intake routes.
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (digest(hashes),)
    )
    return db.execute(
        "SELECT id FROM cases WHERE workspace=%s AND (body ? 'sample_digest' OR body ? 'intake_digest') AND jsonb_array_length(body->'sources')=%s AND body->'sources' @> %s ORDER BY id LIMIT 1",
        (workspace, len(sources), Jsonb([{"sha256": sha} for sha in set(hashes)])),
    ).fetchone()


def _limited_intake_request(req: Request, limit: int) -> Request:
    declared = req.headers.get("content-length")
    if declared:
        try:
            if int(declared) < 0 or int(declared) > limit:
                fail("INTAKE_REQUEST_LIMIT", 413)
        except ValueError:
            fail("INTAKE_CONTENT_LENGTH_INVALID", 400)
    received = 0

    async def receive_limited():
        nonlocal received
        message = await req.receive()
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                fail("INTAKE_REQUEST_LIMIT", 413)
        return message

    return Request(req.scope, receive_limited)


async def _read_intake_json(req: Request) -> dict:
    bounded = _limited_intake_request(req, MAX_LEGACY_JSON_BYTES)
    raw = bytearray()
    async for chunk in bounded.stream():
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        fail("INTAKE_JSON_INVALID", 400)
    if not isinstance(value, dict):
        fail("INTAKE_JSON_INVALID", 400)
    return value


async def _hash_upload(upload) -> str:
    upload.file.seek(0)
    hasher = hashlib.sha256()
    while True:
        chunk = upload.file.read(1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
    upload.file.seek(0)
    return hasher.hexdigest()


@app.post(P + "/intakes/files", status_code=202)
async def import_files(workspace: str, req: Request):
    from folio.original_download import UPLOADED_AUTHORSHIP
    from folio.provenance import FIELDS, extract_packet
    from folio.sandbox import parse_isolated

    user = identity(req, "reviewer")
    scope(workspace)
    if not INTAKE_SLOTS.acquire(blocking=False):
        fail("PARSER_CAPACITY_RETRY", 503)
    try:
        content_type = req.headers.get("content-type", "").lower()
        if content_type.startswith("multipart/form-data"):
            bounded = _limited_intake_request(req, MAX_MULTIPART_REQUEST_BYTES)
            try:
                form = await BoundedIntakeMultiPartParser(
                    headers=Headers(scope=bounded.scope),
                    stream=bounded.stream(),
                    max_files=MAX_FILES_PER_PACKET,
                    max_fields=MAX_FILES_PER_PACKET + 2,
                    max_part_size=MAX_MULTIPART_FIELD_BYTES,
                ).parse()
            except IntakeFilePartLimit:
                fail("FILE_LIMIT", 413)
            except MultiPartException:
                fail("FILE_COUNT_OR_TOTAL_LIMIT", 422)
            try:
                uploads = form.getlist("files")
                roles = form.getlist("document_roles")
                rights = form.getlist("rights_attestation")
                case_ids = form.getlist("case_id")
                if (
                    rights != ["authored-synthetic"]
                    or len(case_ids) > 1
                    or set(form.keys())
                    - {"files", "document_roles", "rights_attestation", "case_id"}
                    or not 1 <= len(uploads) <= MAX_FILES_PER_PACKET
                    or len(roles) != len(uploads)
                    or any(not isinstance(role, str) for role in roles)
                    or any(not hasattr(upload, "file") for upload in uploads)
                ):
                    fail("FILE_COUNT_OR_TOTAL_LIMIT", 422)
                case_id = case_ids[0] if case_ids else None
                if case_id is not None and not isinstance(case_id, str):
                    fail("FILE_METADATA_INVALID", 422)
                b = {
                    "rights_attestation": "authored-synthetic",
                    **({"case_id": case_id} if case_id else {}),
                    "files": [],
                }
                files = [
                    {
                        "name": upload.filename,
                        "document_role": role,
                        "upload": upload,
                    }
                    for upload, role in zip(uploads, roles)
                ]
                return await _process_intake_files(
                    workspace,
                    req,
                    user,
                    b,
                    files,
                    multipart=True,
                    authored_by=UPLOADED_AUTHORSHIP,
                    parse=run_in_threadpool,
                    parse_function=parse_isolated,
                    fields=FIELDS,
                    extract_packet=extract_packet,
                )
            finally:
                await form.close()
        elif content_type.startswith("application/json"):
            b = await _read_intake_json(req)
            if b.get("rights_attestation") != "authored-synthetic":
                fail("ONLY_USER_AUTHORED_SYNTHETIC_INPUTS_AUTHORIZED", 422)
            files = b.get("files")
            if (
                not isinstance(files, list)
                or not 1 <= len(files) <= MAX_FILES_PER_PACKET
                or len(json.dumps(b)) > MAX_LEGACY_JSON_BYTES
            ):
                fail("FILE_COUNT_OR_TOTAL_LIMIT", 422)
            return await _process_intake_files(
                workspace,
                req,
                user,
                b,
                files,
                multipart=False,
                authored_by=UPLOADED_AUTHORSHIP,
                parse=run_in_threadpool,
                parse_function=parse_isolated,
                fields=FIELDS,
                extract_packet=extract_packet,
            )
        else:
            fail("INTAKE_CONTENT_TYPE_UNSUPPORTED", 415)
    finally:
        INTAKE_SLOTS.release()


async def _process_intake_files(
    workspace,
    req,
    user,
    b,
    files,
    *,
    multipart,
    authored_by,
    parse,
    parse_function,
    fields,
    extract_packet,
):
    import base64

    from folio.intake_limits import MAX_PAGES_PER_FILE

    if b.get("rights_attestation") != "authored-synthetic":
        fail("ONLY_USER_AUTHORED_SYNTHETIC_INPUTS_AUTHORIZED", 422)
    sources = []
    rejected = []
    raw_by_id = {}
    manifest_items = []
    packet_pages = 0
    for item in files:
        raw = None
        upload = item.get("upload") if isinstance(item, dict) else None
        file_sha = None
        try:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or len(item["name"]) > 200
                # C0/DEL controls (U+0000 cannot even be stored in jsonb)
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in item["name"])
            ):
                raise ValueError("FILE_METADATA_INVALID")
            if item.get("document_role") not in (
                "invoice",
                "purchase_order",
                "receipt",
            ):
                raise ValueError("DOCUMENT_ROLE_REQUIRED")
            if multipart:
                if (
                    upload.size is None
                    or upload.size <= 0
                    or upload.size > MAX_FILE_BYTES
                ):
                    raise ValueError("FILE_LIMIT")
                upload.file.seek(0)
                raw = upload.file.read(MAX_FILE_BYTES + 1)
                upload.file.seek(0)
                if not raw or len(raw) > MAX_FILE_BYTES:
                    raise ValueError("FILE_LIMIT")
                file_sha = hashlib.sha256(raw).hexdigest()
            else:
                encoded = item.get("base64")
                max_encoded = 4 * ((MAX_FILE_BYTES + 2) // 3)
                if not isinstance(encoded, str) or len(encoded) > max_encoded:
                    raise ValueError("FILE_LIMIT")
                raw = base64.b64decode(encoded, validate=True)
                if not raw or len(raw) > MAX_FILE_BYTES:
                    raise ValueError("FILE_LIMIT")
                file_sha = hashlib.sha256(raw).hexdigest()
            parsed = await parse(parse_function, raw, item["name"])
            page_count = len(parsed.get("pages", []))
            if not 1 <= page_count <= MAX_PAGES_PER_FILE:
                raise ValueError("PAGE_LIMIT")
            if not source_within_packet_limits(
                len(sources), packet_pages, page_count
            ):
                raise ValueError("PACKET_PAGE_LIMIT")
            if multipart:
                manifest_items.append(
                    {
                        "name": item["name"],
                        "document_role": item["document_role"],
                        "sha256": file_sha,
                    }
                )
            source = {
                **parsed,
                "id": str(uuid4()),
                "name": item["name"],
                "document_role": item["document_role"],
                "version": 1,
                "authorship": authored_by,
            }
            sources.append(source)
            packet_pages += page_count
            raw_by_id[source["id"]] = upload if multipart else raw
        except (ValueError, KeyError, TypeError) as error:
            rejected.append(
                {
                    "name": str(item.get("name", "unnamed"))[:200].replace(
                        "\x00", "\\x00"
                    )
                    if isinstance(item, dict)
                    else "invalid",
                    "reason": str(error)[:250],
                }
            )
            if multipart:
                safe_name = item.get("name") if isinstance(item, dict) else None
                safe_role = item.get("document_role") if isinstance(item, dict) else None
                manifest_items.append(
                    {
                        "name": safe_name if isinstance(safe_name, str) else "",
                        "document_role": safe_role
                        if safe_role in ("invoice", "purchase_order", "receipt")
                        else None,
                        "sha256": file_sha or (
                            await _hash_upload(upload)
                            if upload is not None
                            and (upload.size is None or upload.size <= MAX_FILE_BYTES)
                            else ""
                        ),
                    }
                )
    if multipart:
        b["files"] = manifest_items
    if not sources:
        fail({"code": "NO_ACCEPTED_FILES", "rejected": rejected}, 422)
    def read_original(source_id):
        stored = raw_by_id[source_id]
        if isinstance(stored, bytes):
            return stored
        stored.file.seek(0)
        raw = stored.file.read(MAX_FILE_BYTES + 1)
        stored.file.seek(0)
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise ValueError("FILE_LIMIT")
        return raw

    content_digest = digest(sorted((s["sha256"], s["document_role"]) for s in sources))
    with pooled() as db:
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (content_digest,)
        )
        replay = command(db, req, user, b)
        if replay:
            return replay
        if b.get("case_id"):
            from folio.allocations import invalidate, refresh_support

            c = load_case(db, workspace, b["case_id"])
            etag(req, "case", c["version"])
            if c["state"] in ("seal_pending", "sealed", "uncertain", "verified"):
                fail("TOO_LATE_SEALED_OR_PENDING")
            old_hashes = {(s["sha256"], s["document_role"]) for s in c["sources"]}
            additions = []
            accepted_sources = []
            current_pages = sum(len(s.get("pages", [])) for s in c["sources"])
            total_sources = len(c["sources"])
            for source in sources:
                key = (source["sha256"], source["document_role"])
                if key in old_hashes:
                    accepted_sources.append(source)
                    continue
                page_count = len(source.get("pages", []))
                if not source_within_packet_limits(
                    total_sources, current_pages, page_count
                ) and total_sources >= MAX_FILES_PER_PACKET:
                    rejected.append(
                        {
                            "name": source["name"],
                            "reason": "FILE_COUNT_OR_TOTAL_LIMIT",
                        }
                    )
                    continue
                if not source_within_packet_limits(
                    total_sources, current_pages, page_count
                ):
                    rejected.append(
                        {"name": source["name"], "reason": "PACKET_PAGE_LIMIT"}
                    )
                    continue
                additions.append(source)
                accepted_sources.append(source)
                old_hashes.add(key)
                total_sources += 1
                current_pages += page_count
            sources = accepted_sources
            if additions:
                invalidate(db, c)
                c["sources"] += additions
                extracted = extract_packet(c["sources"])
                c.update({field: extracted["values"].get(field, "") for field in fields})
                c["field_provenance"] = extracted["field_provenance"]
                c["receipt_manifest"] = extracted["receipt_manifest"]
                c["conflicting_fields"] = extracted["conflicts"]
                c["invoice_supplier"] = extracted["invoice_supplier"]
                c["supplier_check"] = extracted["supplier_check"]
                for correction in c.get("corrections", []):
                    field = correction["field"]
                    c.setdefault("field_provenance", {}).setdefault(field, []).append(
                        correction
                    )
                    if field != "received_quantity":
                        c[field] = correction["value"]
                        c["conflicting_fields"] = [
                            f for f in c["conflicting_fields"] if f != field
                        ]
                c["source_digest"] = digest(
                    {"sources": c["sources"], "field_provenance": c["field_provenance"]}
                )
                c["version"] += 1
                refresh_support(c)
                for source in additions:
                    db.execute(
                        "INSERT INTO originals(workspace,id,case_id,sha256,raw) VALUES(%s,%s,%s,%s,%s)",
                        (
                            workspace,
                            source["id"],
                            c["id"],
                            source["sha256"],
                            read_original(source["id"]),
                        ),
                    )
                save_case(db, c)
                event(
                    db,
                    c["id"],
                    "DOC-support-attached",
                    user["username"],
                    {
                        "source_ids": [s["id"] for s in additions],
                        "source_digest": c["source_digest"],
                    },
                )
            return recorded(
                db,
                req,
                user,
                b,
                {
                    "case_id": c["id"],
                    "duplicate": not additions,
                    "accepted": [s["name"] for s in sources],
                    "rejected": rejected,
                },
            )
        row = existing_original_packet(db, workspace, sources)
        if row:
            return recorded(
                db,
                req,
                user,
                b,
                {
                    "case_id": str(row[0]),
                    "duplicate": True,
                    "accepted": [s["name"] for s in sources],
                    "rejected": rejected,
                },
            )
        extracted = extract_packet(sources)
        c = create_case(sources, target_id=None, db=db)
        c.update({field: extracted["values"].get(field, "") for field in fields})
        c.update(
            receipt_manifest=extracted["receipt_manifest"],
            field_provenance=extracted["field_provenance"],
            missing_fields=extracted["missing"],
            conflicting_fields=extracted["conflicts"],
            invoice_supplier=extracted["invoice_supplier"],
            supplier_check=extracted["supplier_check"],
            state="awaiting_info",
            target_id=None,
            target_revision=None,
            intake_digest=content_digest,
        )
        c["source_digest"] = digest(
            {"sources": sources, "field_provenance": c["field_provenance"]}
        )
        for s in sources:
            db.execute(
                "INSERT INTO originals(workspace,id,case_id,sha256,raw) VALUES(%s,%s,%s,%s,%s)",
                (workspace, s["id"], c["id"], s["sha256"], read_original(s["id"])),
            )
        save_case(db, c)
        return recorded(
            db,
            req,
            user,
            b,
            {
                "case_id": c["id"],
                "duplicate": False,
                "accepted": [s["name"] for s in sources],
                "rejected": rejected,
            },
        )


@app.post(P + "/intakes/sample", status_code=202)
def sample(workspace: str, req: Request):
    user = identity(req, "reviewer")
    scope(workspace)
    from pathlib import Path

    from folio.parser import parse_pdf

    root = Path(__file__).resolve().parents[1] / "fixtures/public"
    manifest = json.loads((root / "rights-manifest.json").read_text())
    sources = []
    for entry in manifest["files"]:
        raw = (root / entry["name"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            fail("FIXTURE_INTEGRITY", 422)
        parsed = parse_pdf(raw)
        sources.append(
            {
                **parsed,
                "id": str(uuid4()),
                "name": entry["name"],
                "document_role": {
                    "invoice.pdf": "invoice",
                    "purchase-order.pdf": "purchase_order",
                    "receipt.pdf": "receipt",
                }[entry["name"]],
                "authorship": manifest["authorship"],
                "version": 1,
            }
        )
    content_digest = digest([s["sha256"] for s in sources])
    with pooled() as db:
        # Singleton authored packet import; content duplicates link, never create another payable.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (content_digest,)
        )
        replay = command(db, req, user, {"fixture_digest": content_digest})
        if replay:
            return replay
        existing = existing_original_packet(db, workspace, sources)
        if existing:
            return recorded(
                db,
                req,
                user,
                {"fixture_digest": content_digest},
                {"case_id": str(existing[0]), "duplicate": True},
            )
        # The intake, packet identity, command replay result and event commit together.
        c = create_case(sources, db=db)
        from folio.provenance import extract_packet

        extracted = extract_packet(sources)
        if extracted["missing"] or extracted["conflicts"]:
            fail("AUTHORED_FIXTURE_FIELD_EXTRACTION_INCOMPLETE", 422)
        c.update(extracted["values"])
        c["field_provenance"] = extracted["field_provenance"]
        c["invoice_supplier"] = extracted["invoice_supplier"]
        c["supplier_check"] = extracted["supplier_check"]
        c["missing_fields"] = []
        c["conflicting_fields"] = []
        c["source_digest"] = digest(
            {"sources": sources, "field_provenance": c["field_provenance"]}
        )
        c["sample_digest"] = content_digest
        save_case(db, c)
        return recorded(
            db,
            req,
            user,
            {"fixture_digest": content_digest},
            {"case_id": c["id"], "duplicate": False},
        )


@app.get(P + "/events")
def event_stream(workspace: str, req: Request, follow: bool = True):
    import re
    import time

    from fastapi.responses import StreamingResponse

    identity(req)
    scope(workspace)
    raw = req.headers.get("last-event-id", "0")
    if not re.fullmatch(r"0|[1-9][0-9]{0,17}", raw):
        fail("EVENT_CURSOR_INVALID", 422)
    cursor = int(raw)
    with pooled() as db:
        maximum = db.execute(
            "SELECT coalesce(max(sequence),0) FROM events WHERE workspace=%s",
            (workspace,),
        ).fetchone()[0]
        if cursor > maximum:
            fail("EVENT_CURSOR_AHEAD_REFRESH_REQUIRED", 409)

    def stream():
        position = cursor
        started = time.monotonic()
        while True:
            if maintenance_snapshot()["maintenance"]:
                yield "event: maintenance\ndata: {\"state\":\"quiescing\"}\n\n"
                return
            try:
                identity(req)
            except HTTPException:
                yield "event: authorization-lost\ndata: {}\n\n"
                return
            with pooled() as db:
                rows = db.execute(
                    "SELECT e.sequence,e.kind,e.case_id,e.at,e.body,c.body "
                    "FROM events e LEFT JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
                    "WHERE e.workspace=%s AND e.sequence>%s ORDER BY e.sequence LIMIT 200",
                    (workspace, position),
                ).fetchall()
            from folio.retention_backend import project_history_event

            for sequence, kind, cid, at, body, case in rows:
                position = sequence
                if cid is not None and kind == "DOC-decision-saved":
                    body = project_history_event(workspace, sequence, cid, at, body, case or {})
                yield (
                    "id: "
                    + str(sequence)
                    + "\ndata: "
                    + json.dumps(
                        {
                            "kind": kind,
                            "case_id": str(cid) if cid else None,
                            "body": body,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            if not follow:
                return
            if time.monotonic() - started >= 25:
                yield ": reconnect-with-durable-cursor\n\n"
                return
            if not rows:
                yield ": keepalive\n\n"
                time.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get(P + "/activity")
def activity(workspace: str, req: Request, actor_type: str = "all"):
    identity(req)
    scope(workspace)
    if actor_type not in ("all", "human", "system"):
        fail("FILTER_INVALID", 422)
    with pooled() as db:
        rows = db.execute(
            "SELECT e.sequence,e.case_id,e.kind,e.actor,e.at,e.body,c.body "
            "FROM events e LEFT JOIN cases c ON c.workspace=e.workspace AND c.id=e.case_id "
            "WHERE e.workspace=%s ORDER BY e.sequence DESC LIMIT 200",
            (workspace,),
        ).fetchall()
    from folio.retention_backend import project_history_event

    return {
        "events": [
            {
                "sequence": str(r[0]),
                "case_id": str(r[1]),
                "kind": r[2],
                "actor": r[3],
                "at": r[4].isoformat(),
                "body": project_history_event(workspace, r[0], r[1], r[4], r[5], r[6] or {})
                if r[2] == "DOC-decision-saved" and r[1] is not None else r[5],
            }
            for r in rows
            if actor_type == "all" or (r[3] == "system") == (actor_type == "system")
        ]
    }


@app.post(P + "/approvals/{pid}/revocations")
def revoke(workspace: str, pid: str, b: dict, req: Request):
    user = identity(req, "approver")
    scope(workspace)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        ref = db.execute(
            "SELECT case_id FROM proposals WHERE workspace=%s AND id=%s",
            (workspace, object_id(pid)),
        ).fetchone()
        if not ref:
            fail("NOT_FOUND", 404)
        c = load_case(db, workspace, str(ref[0]))
        p = load_proposal(db, workspace, pid)
        etag(req, "proposal", p["version"])
        if p.get("gate") in ("seal_pending", "sealed", "uncertain", "applied"):
            fail("TOO_LATE_SEALED_OR_PENDING")
        if p.get("approved_by") != user["username"]:
            fail("NOT_AUTHORIZED_APPROVER", 403)
        if not b.get("reason", "").strip():
            fail("REASON_REQUIRED", 422)
        p["state"] = "revoked"
        p["revocation"] = {"by": user["username"], "reason": b["reason"], "at": now()}
        c["state"] = "needs_review"
        save_proposal(db, p)
        save_case(db, c)
        event(db, c["id"], "DOC-approval-changed", user["username"], p["revocation"])
        return recorded(db, req, user, b, {"revoked": True})


@app.post(P + "/cases/{cid}/retention-holds")
def retention_hold(workspace: str, cid: str, b: dict, req: Request):
    user = identity(req, "admin")
    scope(workspace)
    if (
        type(b.get("enabled")) is not bool
        or not isinstance(b.get("reason"), str)
        or not b["reason"].strip()
    ):
        fail("HOLD_FLAG_AND_REASON_REQUIRED", 422)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        retention = c.setdefault(
            "retention",
            {"original_days": 90, "decision_evidence_days": 365, "legal_hold": False},
        )
        retention["legal_hold"] = b["enabled"]
        c["version"] += 1
        save_case(db, c)
        event(
            db,
            cid,
            "DOC-retention-hold-changed",
            user["username"],
            {"enabled": b["enabled"], "reason": b["reason"]},
        )
        return recorded(
            db, req, user, b, {"retention": retention, "case_version": c["version"]}
        )


def delete_original_bytes(db, c, actor, reason, *, expired_only=False):
    if c.get("retention", {}).get("legal_hold"):
        fail("LEGAL_HOLD_ACTIVE")
    if c.get("proposal"):
        p = load_proposal(db, c["workspace"], c["proposal"]["id"])
        if p.get("gate") in ("seal_pending", "sealed", "uncertain"):
            fail("RECONCILE_TARGET_BEFORE_SOURCE_DELETION")
        if p.get("gate") != "applied":
            p["state"] = "superseded"
            save_proposal(db, p)
    from folio.retention_storage import delete_original_rows

    rows = delete_original_rows(db, c["workspace"], c["id"], expired_only=expired_only)
    if not rows:
        fail("NO_STORED_ORIGINALS_AVAILABLE", 422)
    deleted = {str(r[0]) for r in rows}
    for source in c["sources"]:
        if source["id"] in deleted:
            source["original_deleted"] = True
    retention = c.setdefault(
        "retention",
        {"original_days": 90, "decision_evidence_days": 365, "legal_hold": False},
    )
    retention["originals_deleted_at"] = now()
    c["source_digest"] = digest(
        {"sources": c["sources"], "field_provenance": c.get("field_provenance", {})}
    )
    c["last_business_state"] = c["state"]
    c["state"] = "source_expired"
    c["version"] += 1
    save_case(db, c)
    event(
        db,
        c["id"],
        "DOC-original-bytes-deleted",
        actor,
        {
            "reason": reason,
            "tombstones": [{"source_id": str(r[0]), "sha256": r[1]} for r in rows],
            "decision_text_retained": True,
            "backup_expiry_complete": False,
        },
    )
    return {
        "deleted_sources": len(rows),
        "case_version": c["version"],
        "decision_text_retained": True,
        "backup_expiry_complete": False,
    }


@app.get(P + "/cases/{cid}/sources/{sid}/original")
def download_original(workspace: str, cid: str, sid: str, req: Request):
    from folio.original_download import (
        OriginalUnavailable,
        may_export_originals,
        read_original,
    )

    security_headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        user = identity(req)
        scope(workspace)
        if not may_export_originals(user["roles"]):
            fail("ORIGINAL_EXPORT_DENIED", 403)
        with pooled() as db:
            raw, headers = read_original(db, workspace, cid, sid)
    except OriginalUnavailable as error:
        raise HTTPException(
            status_code=error.status, detail=error.code, headers=security_headers
        ) from None
    except HTTPException as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.detail,
            headers={**(error.headers or {}), **security_headers},
        ) from None
    return Response(content=raw, media_type="application/octet-stream", headers=headers)


@app.post(P + "/cases/{cid}/original-deletions")
def delete_originals(workspace: str, cid: str, b: dict, req: Request):
    user = identity(req, "admin")
    scope(workspace)
    if not isinstance(b.get("reason"), str) or not b["reason"].strip():
        fail("REASON_REQUIRED", 422)
    with pooled() as db:
        replay = command(db, req, user, b)
        if replay:
            return replay
        c = load_case(db, workspace, cid)
        etag(req, "case", c["version"])
        return recorded(
            db,
            req,
            user,
            b,
            delete_original_bytes(db, c, user["username"], b["reason"]),
        )


@app.post(P + "/cases/{cid}/exports", status_code=202)
def export(workspace: str, cid: str, b: dict, req: Request):
    from folio.exports import enqueue

    return enqueue(workspace, cid, b, req)


@app.get(P + "/artifacts/{aid}")
def artifact(workspace: str, aid: str, req: Request, download: bool = False):
    from folio.exports import retrieve

    return retrieve(workspace, aid, req, download)
