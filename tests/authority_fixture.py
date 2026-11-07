"""Synthetic target-contract authority, not an application approval-flow proof."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from folio.identity import sign_approval


def attestation(workspace, operation, intent, target):
    return sign_approval(
        {
            "id": str(uuid4()),
            "workspace": workspace,
            "operation_key": operation,
            "target_digest": intent,
            "target": target,
            "target_revision": 7,
            "source_digest": "a" * 64,
            "case_version": 1,
            "prepared_by": "synthetic-preparer",
            "approved_by": "synthetic-independent-approver",
            "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }
    )
