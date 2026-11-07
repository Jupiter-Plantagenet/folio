"""Authenticated local backup envelopes; keys are kept outside dump/evidence directories."""

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"FOLIO-AESGCM-1\n"


def seal_backup(raw, destination, key_path, context):
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("BACKUP_MEMORY_ADMISSION_LIMIT")
    destination = Path(destination)
    key_path = Path(key_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    encrypted = MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, context.encode())
    with key_path.open("xb") as stream:
        stream.write(key)
    key_path.chmod(0o600)
    with destination.open("xb") as stream:
        stream.write(encrypted)
    destination.chmod(0o600)
    return {
        "encrypted": True,
        "algorithm": "AES-256-GCM",
        "encrypted_sha256": hashlib.sha256(encrypted).hexdigest(),
        "encrypted_bytes": len(encrypted),
        "plaintext_sha256": hashlib.sha256(raw).hexdigest(),
    }


def open_backup(path, key_path, context):
    raw = Path(path).read_bytes()
    key = Path(key_path).read_bytes()
    if not raw.startswith(MAGIC):
        raise ValueError("BACKUP_FORMAT_INVALID")
    offset = len(MAGIC)
    nonce = raw[offset : offset + 12]
    try:
        return AESGCM(key).decrypt(nonce, raw[offset + 12 :], context.encode())
    except (InvalidTag, ValueError) as error:
        raise ValueError("BACKUP_AUTHENTICATION_FAILED") from error
