"""Provisioned workspace-scoped workload identities and signed approval attestations.
Target stores only service token hashes + approval public key. Worker has no signing key.
Local OS account/disk is trusted; this is not protection from the host administrator.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = "11111111-1111-4111-8111-111111111111"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def config(kind):
    path = Path(
        os.getenv(
            "FOLIO_" + kind.upper() + "_IDENTITY", str(ROOT / ".local" / f"{kind}.json")
        )
    )
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError("WORKLOAD_IDENTITY_NOT_PROVISIONED:" + kind) from error


def service_header(role):
    identity = config("worker" if role in ("broker", "dispatcher") else "verifier")
    return {"Authorization": "Bearer " + identity["tokens"][role]}


def authorize(header, role, workspace):
    trust = config("target")
    expected = trust["principals"].get(role)
    supplied = hashlib.sha256(
        (header or "").removeprefix("Bearer ").encode()
    ).hexdigest()
    return bool(
        expected
        and workspace in expected["workspaces"]
        and header.startswith("Bearer ")
        and hmac.compare_digest(expected["token_sha256"], supplied)
    )


def sign_approval(proposal):
    authority = config("approver")
    body = {
        "schema": "folio-approved-intent-v1",
        "workspace": proposal["workspace"],
        "proposal": proposal["id"],
        "operation_key": proposal["operation_key"],
        "intent_digest": proposal["target_digest"],
        "target_id": proposal["target"],
        "target_revision": proposal["target_revision"],
        "source_digest": proposal["source_digest"],
        "case_version": proposal["case_version"],
        "prepared_by": proposal["prepared_by"],
        "approved_by": proposal["approved_by"],
        "expires": proposal["expires"],
    }
    key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(authority["private_key"])
    )
    return {
        "body": body,
        "signature": base64.b64encode(key.sign(canonical(body))).decode(),
    }


def verify_approval(attestation, workspace, operation, intent, target, revision):
    try:
        trust = config("target")
        body = attestation["body"]
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(trust["approval_public_key"])
        )
        key.verify(
            base64.b64decode(attestation["signature"], validate=True), canonical(body)
        )
        return (
            body["schema"] == "folio-approved-intent-v1"
            and body["workspace"] == workspace
            and workspace in trust["approval_workspaces"]
            and body["operation_key"] == operation
            and body["intent_digest"] == intent
            and body["target_id"] == target
            and body["target_revision"] == revision
            and body["prepared_by"] != body["approved_by"]
            and bool(body["approved_by"])
            and datetime.fromisoformat(body["expires"]) > datetime.now(timezone.utc)
        )
    except Exception:
        return False


def provision(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        kind: directory / f"{kind}.json"
        for kind in ["worker", "verifier", "target", "approver"]
    }
    if any(p.exists() for p in paths.values()):
        if not all(p.exists() for p in paths.values()):
            raise RuntimeError("PARTIAL_IDENTITY_PROVISIONING; do not rotate silently")
        return {
            f"FOLIO_{kind.upper()}_IDENTITY": str(path) for kind, path in paths.items()
        }
    tokens = {
        role: secrets.token_urlsafe(48) for role in ["broker", "dispatcher", "verifier"]
    }
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    values = {
        "worker": {"tokens": {r: tokens[r] for r in ["broker", "dispatcher"]}},
        "verifier": {"tokens": {"verifier": tokens["verifier"]}},
        "target": {
            "principals": {
                r: {
                    "token_sha256": hashlib.sha256(t.encode()).hexdigest(),
                    "workspaces": [WORKSPACE],
                }
                for r, t in tokens.items()
            },
            "approval_public_key": public,
            "approval_workspaces": [WORKSPACE],
        },
        "approver": {
            "private_key": base64.b64encode(
                key.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ).decode()
        },
    }
    for kind, path in paths.items():
        with path.open("x") as stream:
            json.dump(values[kind], stream)
        path.chmod(0o600)
    return {f"FOLIO_{kind.upper()}_IDENTITY": str(path) for kind, path in paths.items()}
