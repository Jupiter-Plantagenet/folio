"""Independent Northstar Ledger service. No workbench database imports."""

import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from psycopg.types.json import Jsonb

from folio.commands import FenceCommand, GrantCommand, MutationCommand, canonical_uuid
from folio.identity import authorize, config, verify_approval
from folio.maintenance_gate import MaintenanceGate, maintenance_snapshot

DSN = os.getenv(
    "FOLIO_TARGET_DSN",
    # No password in source: supply FOLIO_TARGET_DSN from the private profile or pgpass.
    "postgresql://folio_target@127.0.0.1:5541/folio_target",
)
READ_DSN = os.getenv(
    "FOLIO_TARGET_READ_DSN",
    "postgresql://folio_verifier@127.0.0.1:5541/folio_target",
)


def read_connection():
    return psycopg.connect(READ_DSN)


FIELDS = {"recognized_quantity", "net_amount", "tax_amount", "gross_amount", "status"}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def intent_hash(workspace, record, revision, patch):
    return digest(
        dict(
            schema="northstar-intent-v1",
            workspace=workspace,
            target=record,
            revision=revision,
            patch=patch,
        )
    )


def validate_amount_scale(record, patch):
    """Target-owned currency decides wire scale; no rounding or conversion."""
    import re

    from folio.money import minor_units

    places = minor_units(record.get("currency"))
    if places is None:
        raise HTTPException(422, "AMOUNT_CURRENCY_SCALE_INVALID")
    pattern = r"(0|[1-9]\d{0,13})" + (rf"\.\d{{{places}}}" if places else "")
    if any(
        re.fullmatch(pattern, patch[name]) is None
        for name in ("net_amount", "tax_amount", "gross_amount")
    ):
        raise HTTPException(422, "AMOUNT_CURRENCY_SCALE_INVALID")


def migrate():
    if os.getenv("FOLIO_SCHEMA_MODE") == "runtime":
        with psycopg.connect(DSN) as db:
            if not db.execute(
                "SELECT 1 FROM schema_versions WHERE version=2"
            ).fetchone():
                raise RuntimeError("SCHEMA_MIGRATION_REQUIRED")
        return
    with psycopg.connect(DSN) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS payables(workspace uuid NOT NULL,id uuid NOT NULL,body jsonb NOT NULL,PRIMARY KEY(workspace,id));
        CREATE TABLE IF NOT EXISTS authority(workspace uuid NOT NULL,operation_key uuid NOT NULL,digest text NOT NULL,fence bigint NOT NULL,grant_body jsonb,expires timestamptz,PRIMARY KEY(workspace,operation_key));
        CREATE TABLE IF NOT EXISTS operations(workspace uuid NOT NULL,operation_key uuid NOT NULL,target_id uuid NOT NULL,body jsonb NOT NULL,PRIMARY KEY(workspace,operation_key),FOREIGN KEY(workspace,target_id) REFERENCES payables(workspace,id));
        CREATE TABLE IF NOT EXISTS target_sessions(token text PRIMARY KEY,expires timestamptz NOT NULL);
        GRANT USAGE ON SCHEMA public TO folio_verifier;
        GRANT SELECT ON payables,operations,authority TO folio_verifier;""")
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict

        reader = sql.Identifier(conninfo_to_dict(READ_DSN)["user"])
        db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(reader))
        db.execute(
            sql.SQL("GRANT SELECT ON payables,operations,authority TO {}").format(
                reader
            )
        )


def seed(workspace, record):
    body = dict(
        id=record,
        revision=7,
        supplier="Aster Supply Co.",
        invoice_key="INV-1048",
        invoice_face_amount="6220.80",
        recognized_quantity="0",
        net_amount="0.00",
        tax_amount="0.00",
        gross_amount="0.00",
        status="unmatched",
        bank_fingerprint="synthetic-bank-unchanged",
        due_date="2026-11-25",
        currency="USD",
        unit="each",
    )
    with psycopg.connect(DSN) as db:
        db.execute(
            "INSERT INTO payables VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (workspace, record, Jsonb(body)),
        )
    return body


@asynccontextmanager
async def lifespan(app):
    config("target")
    migrate()
    yield


app = FastAPI(title="Northstar Ledger independent target", lifespan=lifespan)
app.add_middleware(MaintenanceGate)


def _restore_mode_guard(app):
    """Restored target stays read-only: no fence, grant or PATCH replays (DOC-T-RESTORE)."""
    from folio.restore_mode import RestoreModeGuard, database_read_only

    def probe():
        with psycopg.connect(READ_DSN) as db:
            return database_read_only(db)

    return RestoreModeGuard(app, probe=probe, prefix="/api/v1/", exempt=())


app.add_middleware(_restore_mode_guard)
P = "/api/v1/workspaces/{workspace}"


@app.middleware("http")
async def service_boundary(request, call_next):
    from fastapi.responses import JSONResponse

    if request.url.path.startswith("/api/v1/workspaces/"):
        # Browser identities are never service principals, even with fixture tokens.
        if request.headers.get("origin") or request.headers.get("sec-fetch-site"):
            return JSONResponse({"detail": "SERVICE_BROWSER_DENIED"}, status_code=403)
        role = (
            "verifier"
            if request.method == "GET"
            else ("dispatcher" if request.method == "PATCH" else "broker")
        )
        workspace = request.url.path.split("/")[4]
        try:
            identifier(workspace, "workspace")
        except HTTPException as error:
            return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        if not authorize(request.headers.get("authorization", ""), role, workspace):
            return JSONResponse({"detail": "SERVICE_SCOPE"}, status_code=403)
    return await call_next(request)


def auth(header, role):
    # Workspace-scoped authentication is performed before body parsing by middleware.
    pass


def identifier(value, field):
    try:
        return canonical_uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            422, {"code": "CANONICAL_UUID_REQUIRED", "field": field, "input": value}
        ) from None


def lock_identity(workspace, key):
    # One accepted spelling per UUID: writer and primary reader MUST share this key.
    return identifier(workspace, "workspace") + identifier(key, "operation_key")


def lock(db, workspace, key):
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (lock_identity(workspace, key),),
    )


def get_record(db, workspace, record, for_update=True):
    row = db.execute(
        "SELECT body FROM payables WHERE workspace=%s AND id=%s"
        + (" FOR UPDATE" if for_update else ""),
        (workspace, record),
    ).fetchone()
    if not row:
        raise HTTPException(404, "NOT_FOUND")
    return row[0]


@app.get("/health")
def health():
    maintenance = maintenance_snapshot()
    if maintenance["maintenance"]:
        return {"status": "maintenance", **maintenance}
    with psycopg.connect(DSN) as db:
        db.execute("SELECT 1")
    return {"status": "ready", "database": "PostgreSQL", "service": "Northstar", **maintenance}


@app.post(P + "/operation-fences")
def fence(workspace: str, b: FenceCommand, authorization: str = Header("")):
    auth(authorization, "broker")
    b = b.model_dump()
    with psycopg.connect(DSN) as db:
        lock(db, workspace, b["operation_key"])
        r = db.execute(
            "SELECT digest,fence FROM authority WHERE workspace=%s AND operation_key=%s",
            (workspace, b["operation_key"]),
        ).fetchone()
        if r and (r[0] != b["intent_digest"] or r[1] > b["monotonic_fence"]):
            raise HTTPException(409, "OLD_FENCE_OR_DIGEST")
        db.execute(
            "INSERT INTO authority(workspace,operation_key,digest,fence) VALUES(%s,%s,%s,%s) ON CONFLICT(workspace,operation_key) DO UPDATE SET fence=excluded.fence",
            (workspace, b["operation_key"], b["intent_digest"], b["monotonic_fence"]),
        )
    return {"current_fence": b["monotonic_fence"]}


@app.post(P + "/operation-grants")
def grant(workspace: str, b: GrantCommand, authorization: str = Header("")):
    auth(authorization, "broker")
    b = b.model_dump()
    with psycopg.connect(DSN) as db:
        lock(db, workspace, b["operation_key"])
        row = db.execute(
            "SELECT digest,fence,grant_body,expires>clock_timestamp() FROM authority WHERE workspace=%s AND operation_key=%s",
            (workspace, b["operation_key"]),
        ).fetchone()
        if not row or row[0] != b["intent_digest"] or row[1] != b["target_fence"]:
            raise HTTPException(409, "STALE_FENCE_OR_DIGEST")
        prior = row[2]
        if prior and not b.get("renew_prior_jti"):
            return prior  # replay of the existing generation, even if expired
        if b.get("renew_prior_jti"):
            # Explicit renewal (blueprint section 7): names the prior JTI, carries a
            # strictly greater registered fence, only after the prior grant expired, and
            # never once an operation receipt exists. Same key and business digest; the
            # approval, revision and intent are re-verified below as for a new grant.
            if not prior or b["renew_prior_jti"] != prior["jti"]:
                raise HTTPException(409, "RENEWAL_PRIOR_JTI_MISMATCH")
            if b["target_fence"] <= prior["fence"]:
                raise HTTPException(409, "RENEWAL_FENCE_NOT_GREATER")
            if row[3]:
                raise HTTPException(409, "PRIOR_GRANT_UNEXPIRED")
            if db.execute(
                "SELECT 1 FROM operations WHERE workspace=%s AND operation_key=%s",
                (workspace, b["operation_key"]),
            ).fetchone():
                raise HTTPException(409, "OPERATION_ALREADY_COMMITTED")
        if not verify_approval(
            b.get("approval"),
            workspace,
            b["operation_key"],
            b["intent_digest"],
            b["target_id"],
            b["expected_revision"],
        ):
            raise HTTPException(403, "APPROVED_INTENT_ATTESTATION_REQUIRED")
        record = get_record(db, workspace, b["target_id"])
        validate_amount_scale(record, b["exact_patch"])
        if record["revision"] != b["expected_revision"]:
            raise HTTPException(412, "TARGET_CHANGED")
        if (
            set(b["exact_patch"]) != FIELDS
            or intent_hash(
                workspace, b["target_id"], b["expected_revision"], b["exact_patch"]
            )
            != b["intent_digest"]
        ):
            raise HTTPException(422, "INTENT_INVALID")
        g = dict(
            jti=str(uuid4()),
            operation_key=b["operation_key"],
            intent_digest=b["intent_digest"],
            target_id=b["target_id"],
            revision=b["expected_revision"],
            patch=b["exact_patch"],
            fence=b["target_fence"],
            sealed_at=datetime.now(timezone.utc).isoformat(),
            approval=b["approval"],
        )
        if prior:
            g.update(
                generation=prior.get("generation", 1) + 1,
                prior_jti=prior["jti"],
                history=prior.get("history", [])
                + [
                    {
                        "jti": prior["jti"],
                        "fence": prior["fence"],
                        "sealed_at": prior["sealed_at"],
                        "generation": prior.get("generation", 1),
                    }
                ],
            )
        db.execute(
            "UPDATE authority SET grant_body=%s,expires=clock_timestamp()+interval '60 seconds' WHERE workspace=%s AND operation_key=%s",
            (Jsonb(g), workspace, b["operation_key"]),
        )
    return g


@app.patch(P + "/payables/{record}")
def mutate(
    workspace: str,
    record: str,
    b: MutationCommand,
    authorization: str = Header(""),
    idempotency_key: str = Header(""),
    if_match: str = Header(""),
):
    auth(authorization, "dispatcher")
    identifier(record, "record")
    identifier(idempotency_key, "Idempotency-Key")
    b = b.model_dump()
    with psycopg.connect(DSN) as db:
        lock(db, workspace, idempotency_key)
        old = db.execute(
            "SELECT body FROM operations WHERE workspace=%s AND operation_key=%s",
            (workspace, idempotency_key),
        ).fetchone()
        if old:
            if (
                old[0]["intent_digest"] != b["intent_digest"]
                or old[0]["target_id"] != record
                or old[0]["exact_patch"] != b["exact_patch"]
            ):
                raise HTTPException(409, "KEY_DIGEST_MISMATCH")
            return {**old[0], "replayed": True}
        # Lock order is always operation advisory -> payable row (also used by grant).
        # Validate authority/expiry AFTER any payable-row wait, under the same operation lock.
        before = get_record(db, workspace, record)
        validate_amount_scale(before, b["exact_patch"])
        a = db.execute(
            "SELECT digest,fence,grant_body,expires>clock_timestamp() FROM authority WHERE workspace=%s AND operation_key=%s",
            (workspace, idempotency_key),
        ).fetchone()
        if not a or a[0] != b["intent_digest"] or a[1] != b["fence"]:
            raise HTTPException(409, "STALE_FENCE_OR_DIGEST")
        g = a[2]
        if g and g["fence"] != b["fence"]:
            raise HTTPException(409, "GRANT_FENCE_SUPERSEDED")
        if not g or not a[3] or g["jti"] != b["grant_jti"]:
            raise HTTPException(403, "GRANT_EXPIRED_OR_SCOPE")
        if (
            g["operation_key"] != idempotency_key
            or g["target_id"] != record
            or g["patch"] != b["exact_patch"]
            or set(b["exact_patch"]) != FIELDS
        ):
            raise HTTPException(422, "PATCH_INVALID")
        if (
            if_match != f'"payable-v{before["revision"]}"'
            or before["revision"] != g["revision"]
        ):
            raise HTTPException(412, "REVISION_CONFLICT")
        after = {**before, **b["exact_patch"], "revision": before["revision"] + 1}
        receipt = dict(
            operation_key=idempotency_key,
            intent_digest=b["intent_digest"],
            target_id=record,
            before_revision=before["revision"],
            after_revision=after["revision"],
            before_digest=digest(before),
            after_digest=digest(after),
            before_snapshot=before,
            after_snapshot=after,
            exact_patch=b["exact_patch"],
            grant=g,
            committed_at=datetime.now(timezone.utc).isoformat(),
            replayed=False,
        )
        db.execute(
            "UPDATE payables SET body=%s WHERE workspace=%s AND id=%s",
            (Jsonb(after), workspace, record),
        )
        db.execute(
            "INSERT INTO operations VALUES(%s,%s,%s,%s)",
            (workspace, idempotency_key, record, Jsonb(receipt)),
        )
    return receipt


@app.get(P + "/operations/{operation}")
def operation(workspace: str, operation: str, authorization: str = Header("")):
    auth(authorization, "verifier")
    identity = lock_identity(workspace, operation)
    with read_connection() as db:
        db.autocommit = True
        # Acquire session key lock BEFORE snapshot creation. Connection close releases it.
        db.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (identity,))
        with db.transaction():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            row = db.execute(
                "SELECT body FROM operations WHERE workspace=%s AND operation_key=%s",
                (workspace, operation),
            ).fetchone()
            if not row:
                authority = db.execute(
                    "SELECT digest,fence,grant_body,expires>clock_timestamp() FROM authority WHERE workspace=%s AND operation_key=%s",
                    (workspace, operation),
                ).fetchone()
                raise HTTPException(
                    404,
                    {
                        "authoritative": True,
                        "not_applied": True,
                        "operation_key": operation,
                        "workspace": workspace,
                        "intent_digest": authority[0] if authority else None,
                        "fence": authority[1] if authority else None,
                        "grant": authority[2] if authority else None,
                        "grant_unexpired": authority[3] if authority else None,
                        "primary_transaction": db.execute(
                            "SELECT txid_current()"
                        ).fetchone()[0],
                    },
                )
            body = row[0]
            current = get_record(db, workspace, body["target_id"], False)
            history = [
                r[0]
                for r in db.execute(
                    "SELECT body FROM operations WHERE workspace=%s AND target_id=%s ORDER BY body->>'committed_at'",
                    (workspace, body["target_id"]),
                )
            ]
            return dict(
                receipt=body,
                current_record=current,
                effect_history=history,
                authoritative=True,
                read_principal=db.execute("SELECT current_user").fetchone()[0],
            )


@app.get(P + "/payables")
def lookup_payables(workspace: str, invoice_key: str, supplier: str):
    identifier(workspace, "workspace")
    if len(invoice_key) > 200 or len(supplier) > 200:
        raise HTTPException(422, "LOOKUP_LIMIT")
    with read_connection() as db:
        rows = db.execute(
            "SELECT body FROM payables WHERE workspace=%s AND body->>'invoice_key'=%s AND body->>'supplier'=%s ORDER BY id LIMIT 101",
            (workspace, invoice_key, supplier),
        ).fetchall()
        return {
            "items": [r[0] for r in rows[:100]],
            "truncated": len(rows) > 100,
            "authoritative": True,
            "read_principal": db.execute("SELECT current_user").fetchone()[0],
        }


@app.get(P + "/payables/{record}")
def read(workspace: str, record: str, authorization: str = Header("")):
    auth(authorization, "verifier")
    identifier(workspace, "workspace")
    identifier(record, "record")
    with read_connection() as db:
        return get_record(db, workspace, record, False)


@app.get("/", response_class=HTMLResponse)
def console():
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "target-ledger/index.html").read_text(
        encoding="utf-8"
    )


def operator_password():
    """Operator password from env or the private .local store; never from source."""
    value = os.getenv("FOLIO_TARGET_OPERATOR_PASSWORD")
    if value:
        return value
    from pathlib import Path

    store = Path(__file__).resolve().parents[1] / ".local/target-operator.json"
    if store.exists():
        return json.loads(store.read_text()).get("password") or None
    return None


@app.post("/operator/login")
def operator_login(req: Request, response: Response, b: dict):
    import hmac
    import secrets

    if req.headers.get("origin") != os.getenv(
        "FOLIO_TARGET_URL", "http://127.0.0.1:8201"
    ):
        raise HTTPException(403, "ORIGIN_DENIED")
    expected = operator_password()
    if expected is None:
        raise HTTPException(503, "OPERATOR_LOGIN_NOT_PROVISIONED")
    if not hmac.compare_digest(
        str(b.get("username", "")), "operator"
    ) or not hmac.compare_digest(str(b.get("password", "")), expected):
        raise HTTPException(401, "INVALID_LOGIN")
    token = secrets.token_urlsafe(32)
    with psycopg.connect(DSN) as db:
        db.execute(
            "INSERT INTO target_sessions VALUES(%s,clock_timestamp()+interval '1 hour')",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
    response.set_cookie(
        "northstar_session", token, httponly=True, samesite="strict", max_age=3600
    )
    return {"identity": "northstar-local-operator", "write_scope": False}


@app.get("/operator/records")
def operator_records(req: Request):
    with psycopg.connect(DSN) as db:
        token = hashlib.sha256(
            req.cookies.get("northstar_session", "").encode()
        ).hexdigest()
        if not db.execute(
            "SELECT 1 FROM target_sessions WHERE token=%s AND expires>clock_timestamp()",
            (token,),
        ).fetchone():
            raise HTTPException(401, "SESSION_REQUIRED")
        records = [
            r[0] for r in db.execute("SELECT body FROM payables ORDER BY id LIMIT 100")
        ]
        count = db.execute("SELECT count(*) FROM operations").fetchone()[0]
    return {
        "records": records,
        "operations": count,
        "service": "Northstar independent PostgreSQL target",
        "read_only": True,
    }
