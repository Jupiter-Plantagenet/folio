"""Prepare, sign and consume a bounded restore-reconciliation authorization.

The signer key is separate from Folio payment approval. The clear command is
operator-only and will not open the write gate unless both restored databases
remain flagged, the exact pair matches the signed report, and no client remains
connected. Keep the service-level FOLIO_READ_ONLY hold active until the
operator has restarted the reviewed pair.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCHEMA = "folio-restore-reconciliation-v1"
MAX_REPORT_BYTES = 1_048_576
MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_TTL = timedelta(hours=24)
BODY_KEYS = {
    "schema",
    "workspace",
    "restore_id",
    "workbench_database",
    "workbench_database_oid",
    "target_database",
    "target_database_oid",
    "postgres_system_identifier",
    "reconciliation_sha256",
    "reconciled",
    "unresolved_findings",
    "prepared_by",
    "approved_by",
    "issued_at",
    "expires_at",
}
DB_NAME = re.compile(r"folio_[a-z0-9_]{1,57}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("RESTORE_REPORT_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("RESTORE_REPORT_INVALID_JSON")


def _load_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if not raw or len(raw) > MAX_REPORT_BYTES:
        raise ValueError("RESTORE_REPORT_SIZE_INVALID")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_pairs,
        parse_constant=_reject_constant,
    )


def _evidence_digest(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_EVIDENCE_BYTES:
                raise ValueError("RESTORE_RECONCILIATION_SIZE_INVALID")
            digest.update(chunk)
    if size == 0:
        raise ValueError("RESTORE_RECONCILIATION_EMPTY")
    return digest.hexdigest()


def _timestamp(value, *, now, reject_future=False):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("RESTORE_REPORT_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("RESTORE_REPORT_INVALID") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("RESTORE_REPORT_INVALID")
    if reject_future and result > now + timedelta(minutes=5):
        raise ValueError("RESTORE_REPORT_NOT_YET_VALID")
    return result


def validate_body(body, *, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(body, dict) or set(body) != BODY_KEYS:
        raise ValueError("RESTORE_REPORT_SCHEMA_INVALID")
    if (
        body.get("schema") != SCHEMA
        or body.get("workspace") != "11111111-1111-4111-8111-111111111111"
    ):
        raise ValueError("RESTORE_REPORT_SCHEMA_INVALID")
    try:
        restore_id = body.get("restore_id")
        if not isinstance(restore_id, str) or str(UUID(restore_id)) != restore_id:
            raise ValueError
    except ValueError as error:
        raise ValueError("RESTORE_REPORT_INVALID") from error
    for field in ("workbench_database", "target_database"):
        value = body.get(field)
        if not isinstance(value, str) or not DB_NAME.fullmatch(value):
            raise ValueError("RESTORE_REPORT_DATABASE_INVALID")
    if body["workbench_database"] == body["target_database"]:
        raise ValueError("RESTORE_REPORT_DATABASE_INVALID")
    for field in ("workbench_database_oid", "target_database_oid"):
        if type(body.get(field)) is not int or body[field] <= 0:
            raise ValueError("RESTORE_REPORT_DATABASE_INVALID")
    if not re.fullmatch(r"[0-9]{1,20}", str(body.get("postgres_system_identifier", ""))):
        raise ValueError("RESTORE_REPORT_DATABASE_INVALID")
    if (
        not isinstance(body.get("reconciliation_sha256"), str)
        or not HASH.fullmatch(body["reconciliation_sha256"])
    ):
        raise ValueError("RESTORE_REPORT_DIGEST_INVALID")
    if (
        body.get("reconciled") is not True
        or type(body.get("unresolved_findings")) is not int
        or body["unresolved_findings"] != 0
    ):
        raise ValueError("RESTORE_RECONCILIATION_INCOMPLETE")
    prepared, approved = body.get("prepared_by"), body.get("approved_by")
    if (
        not isinstance(prepared, str)
        or not prepared.strip()
        or prepared != prepared.strip()
        or len(prepared) > 128
        or not isinstance(approved, str)
        or not approved.strip()
        or approved != approved.strip()
        or len(approved) > 128
        or prepared.casefold() == approved.casefold()
    ):
        raise ValueError("RESTORE_APPROVER_SEPARATION_REQUIRED")
    pinned_approver = os.environ.get("FOLIO_RESTORE_APPROVER", "").strip()
    if not pinned_approver or approved != pinned_approver:
        raise ValueError("RESTORE_APPROVER_IDENTITY_MISMATCH")
    issued = _timestamp(body.get("issued_at"), now=now, reject_future=True)
    expires = _timestamp(body.get("expires_at"), now=now)
    if expires <= now or expires <= issued or expires - issued > MAX_TTL:
        raise ValueError("RESTORE_REPORT_EXPIRED_OR_TTL_INVALID")
    return body


def _read_public_key():
    value = os.environ.get("FOLIO_RESTORE_PUBLIC_KEY", "")
    try:
        raw = base64.b64decode(value, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as error:
        raise ValueError("RESTORE_TRUST_KEY_NOT_CONFIGURED") from error


def _verify_report(report_path, evidence_path):
    report = _load_json(report_path)
    if not isinstance(report, dict) or set(report) != {"body", "signature"}:
        raise ValueError("RESTORE_REPORT_SCHEMA_INVALID")
    body = validate_body(report["body"])
    signature = report.get("signature")
    try:
        raw_signature = base64.b64decode(signature, validate=True)
        _read_public_key().verify(raw_signature, canonical(body))
    except Exception as error:
        raise ValueError("RESTORE_REPORT_SIGNATURE_INVALID") from error
    if _evidence_digest(evidence_path) != body["reconciliation_sha256"]:
        raise ValueError("RESTORE_RECONCILIATION_DIGEST_MISMATCH")
    return body


def _database_identity(dsn):
    with psycopg.connect(dsn, connect_timeout=5) as db:
        name, oid = db.execute(
            "SELECT current_database(), oid FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        flagged = bool(db.execute(
            "SELECT coalesce(bool_or(c = 'folio.restore_mode=read_only'), false) "
            "FROM pg_db_role_setting s CROSS JOIN LATERAL unnest(s.setconfig) c "
            "WHERE s.setrole=0 AND s.setdatabase="
            "(SELECT oid FROM pg_database WHERE datname=current_database())"
        ).fetchone()[0])
    info = conninfo_to_dict(dsn)
    host, port = info.get("host"), info.get("port", "5432")
    hostaddr = info.get("hostaddr")
    if (
        host not in ("127.0.0.1", "localhost")
        or (hostaddr and hostaddr not in ("127.0.0.1", "::1"))
        or info.get("service")
        or str(port) != "5541"
    ):
        raise ValueError("RESTORE_DATABASE_ENDPOINT_UNSUPPORTED")
    restore_admin_dsn = os.environ.get("FOLIO_RESTORE_ADMIN_DSN")
    if not restore_admin_dsn:
        raise ValueError("RESTORE_ADMIN_DSN_REQUIRED")
    admin_info = conninfo_to_dict(restore_admin_dsn)
    admin_info.pop("hostaddr", None)
    admin_info.pop("service", None)
    admin_info.update(host=host, port=str(port), dbname=name)
    with psycopg.connect(make_conninfo(**admin_info), connect_timeout=5) as db:
        admin_name, admin_oid = db.execute(
            "SELECT current_database(), oid FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        system_identifier = str(
            db.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0]
        )
    if admin_name != name or admin_oid != oid:
        raise ValueError("RESTORE_DATABASE_IDENTITY_MISMATCH")
    if not re.fullmatch(r"[0-9]{1,20}", system_identifier):
        raise ValueError("RESTORE_CLUSTER_IDENTITY_UNVERIFIABLE")
    return name, oid, flagged, system_identifier


def _docker_system_identifier():
    value = _admin_query("SELECT system_identifier FROM pg_control_system()")
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("RESTORE_CLUSTER_IDENTITY_UNVERIFIABLE")
    return value


def _pair(*, require_flagged=True):
    workbench_dsn = os.environ.get("FOLIO_DSN")
    target_dsn = os.environ.get("FOLIO_TARGET_DSN")
    if not workbench_dsn or not target_dsn:
        raise ValueError("RESTORE_DATABASE_DSN_REQUIRED")
    workbench_info, target_info = map(conninfo_to_dict, (workbench_dsn, target_dsn))
    for key in ("host", "port"):
        if workbench_info.get(key) != target_info.get(key):
            raise ValueError("RESTORE_DATABASE_PAIR_CLUSTER_MISMATCH")
    wb_name, wb_oid, wb_flag, wb_system = _database_identity(workbench_dsn)
    target_name, target_oid, target_flag, target_system = _database_identity(target_dsn)
    docker_system = _docker_system_identifier()
    if (
        not DB_NAME.fullmatch(wb_name)
        or not DB_NAME.fullmatch(target_name)
        or wb_name == target_name
        or wb_system != target_system
        or wb_system != docker_system
        or (require_flagged and (not wb_flag or not target_flag))
    ):
        raise ValueError("RESTORE_PAIR_OR_CLUSTER_IDENTITY_INVALID")
    return (wb_name, wb_oid, wb_system), (target_name, target_oid, target_system)


def _admin_query(query):
    container = os.environ.get("FOLIO_POSTGRES_CONTAINER", "folio-postgres")
    result = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d", "postgres", "-At", "-v", "ON_ERROR_STOP=1"],
        input=query.encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError("RESTORE_ADMIN_CHECK_FAILED")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _require_no_clients(pair):
    names = [pair[0][0], pair[1][0]]
    query = sql.SQL(
        "SELECT count(*) FROM pg_stat_activity WHERE datname IN ({}) AND backend_type='client backend'"
    ).format(sql.SQL(",").join(sql.Literal(name) for name in names))
    count = _admin_query(query.as_string())
    if count != "0":
        raise ValueError("RESTORE_SERVICES_STILL_CONNECTED")


def _alter(database, *, readonly):
    from folio.database_security import admin

    command = (
        sql.SQL("ALTER DATABASE {} SET folio.restore_mode='read_only'")
        if readonly
        else sql.SQL("ALTER DATABASE {} RESET folio.restore_mode")
    ).format(sql.Identifier(database)).as_string()
    admin(command)


def _hold_required():
    from folio.restore_mode import env_read_only

    if not env_read_only():
        raise ValueError("RESTORE_PROCESS_HOLD_REQUIRED")


def prepare(args):
    _hold_required()
    evidence_hash = _evidence_digest(args.reconciliation)
    pair = _pair()
    _require_no_clients(pair)
    if not args.reconciled or args.unresolved_findings != 0:
        raise ValueError("RESTORE_RECONCILIATION_INCOMPLETE")
    if args.prepared_by == args.approved_by:
        raise ValueError("RESTORE_APPROVER_SEPARATION_REQUIRED")
    now = datetime.now(timezone.utc)
    body = {
        "schema": SCHEMA,
        "workspace": "11111111-1111-4111-8111-111111111111",
        "restore_id": str(UUID(args.restore_id)) if args.restore_id else str(uuid4()),
        "workbench_database": pair[0][0],
        "workbench_database_oid": pair[0][1],
        "target_database": pair[1][0],
        "target_database_oid": pair[1][1],
        "postgres_system_identifier": pair[0][2],
        "reconciliation_sha256": evidence_hash,
        "reconciled": args.reconciled,
        "unresolved_findings": args.unresolved_findings,
        "prepared_by": args.prepared_by,
        "approved_by": args.approved_by,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=args.expires_hours)).isoformat(),
    }
    validate_body(body)
    output = Path(args.output)
    with output.open("xb") as stream:
        stream.write(json.dumps(body, sort_keys=True, indent=2).encode("utf-8"))
    print(json.dumps({"status": "prepared_unsigned", "restore_id": body["restore_id"], "body": str(output)}))


def sign(args):
    body = validate_body(_load_json(args.body))
    key_path = Path(args.private_key).resolve(strict=True)
    if key_path == ROOT or ROOT in key_path.parents:
        raise ValueError("RESTORE_SIGNING_KEY_MUST_BE_EXTERNAL")
    if key_path.stat().st_size != 32:
        raise ValueError("RESTORE_SIGNING_KEY_INVALID")
    key_raw = key_path.read_bytes()
    if len(key_raw) != 32:
        raise ValueError("RESTORE_SIGNING_KEY_INVALID")
    private_key = Ed25519PrivateKey.from_private_bytes(key_raw)
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    pinned = _read_public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public != pinned:
        raise ValueError("RESTORE_SIGNING_KEY_DOES_NOT_MATCH_PIN")
    signature = private_key.sign(canonical(body))
    output = Path(args.output)
    with output.open("xb") as stream:
        stream.write(json.dumps(
            {"body": body, "signature": base64.b64encode(signature).decode("ascii")},
            sort_keys=True,
            indent=2,
        ).encode("utf-8"))
    try:
        output.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({"status": "signed", "restore_id": body["restore_id"], "report": str(output)}))


def clear(args):
    _hold_required()
    body = _verify_report(args.report, args.reconciliation)
    pair = _pair()
    expected = (
        (
            body["workbench_database"],
            body["workbench_database_oid"],
            body["postgres_system_identifier"],
        ),
        (
            body["target_database"],
            body["target_database_oid"],
            body["postgres_system_identifier"],
        ),
    )
    if pair != expected:
        raise ValueError("RESTORE_REPORT_DATABASE_MISMATCH")
    _require_no_clients(pair)
    wb, target = pair[0][0], pair[1][0]
    try:
        _alter(wb, readonly=False)
        _alter(target, readonly=False)
    except Exception as error:
        try:
            _alter(wb, readonly=True)
            _alter(target, readonly=True)
        except Exception as compensation_error:
            raise RuntimeError("RESTORE_MODE_COMPENSATION_FAILED; keep services stopped") from compensation_error
        raise RuntimeError("RESTORE_MODE_CLEAR_FAILED; both flags restored") from error
    try:
        after = _pair(require_flagged=False)
        if after != pair:
            raise ValueError("RESTORE_PAIR_CHANGED_DURING_CLEAR")
        # Clear is verified by fresh direct metadata probes, never by the
        # cached middleware read.
        for name, _oid in pair:
            dsn = os.environ["FOLIO_DSN"] if name == wb else os.environ["FOLIO_TARGET_DSN"]
            _, oid, flagged, _system_identifier = _database_identity(dsn)
            if flagged or oid != _oid:
                raise ValueError("RESTORE_MODE_CLEAR_VERIFY_FAILED")
    except Exception as error:
        try:
            _alter(wb, readonly=True)
            _alter(target, readonly=True)
        except Exception as compensation_error:
            raise RuntimeError("RESTORE_MODE_COMPENSATION_FAILED; keep services stopped") from compensation_error
        raise RuntimeError("RESTORE_MODE_CLEAR_VERIFY_FAILED; both flags restored") from error
    print(json.dumps({"status": "cleared", "restore_id": body["restore_id"], "workbench_database": wb, "target_database": target}))


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="bind a reconciled evidence file to the flagged database pair")
    prep.add_argument("--reconciliation", required=True)
    prep.add_argument("--output", required=True)
    prep.add_argument("--prepared-by", required=True)
    prep.add_argument("--approved-by", required=True)
    prep.add_argument("--reconciled", action="store_true")
    prep.add_argument("--unresolved-findings", type=int, required=True)
    prep.add_argument("--restore-id")
    prep.add_argument("--expires-hours", type=int, default=1)
    prep.set_defaults(run=prepare)
    signer = commands.add_parser("sign", help="sign a validated restore body with the dedicated restore key")
    signer.add_argument("--body", required=True)
    signer.add_argument("--private-key", required=True, help="external raw 32-byte Ed25519 key file")
    signer.add_argument("--output", required=True)
    signer.set_defaults(run=sign)
    clr = commands.add_parser("clear", help="verify and consume a signed reconciliation report")
    clr.add_argument("--report", required=True)
    clr.add_argument("--reconciliation", required=True)
    clr.set_defaults(run=clear)
    return ap


def main():
    args = parser().parse_args()
    try:
        args.run(args)
    except (OSError, ValueError, RuntimeError, psycopg.Error, subprocess.SubprocessError) as error:
        message = str(error)
        if not re.fullmatch(r"[A-Za-z0-9_ ;:-]{1,200}", message):
            message = "RESTORE_COMMAND_FAILED"
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
