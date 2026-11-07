"""Version-bound receipt reservations and local missing-information lifecycle."""

from datetime import datetime, timezone
from decimal import Decimal

from folio.receipts import decimal_text


def invalidate(db, case):
    from folio.workbench import fail, load_proposal, save_proposal

    if case["state"] in ("seal_pending", "sealed", "uncertain", "verified"):
        fail("TOO_LATE_SEALED_OR_PENDING")
    if case.get("proposal"):
        old = load_proposal(db, case["workspace"], case["proposal"]["id"])
        if old.get("gate") in ("seal_pending", "sealed", "uncertain", "applied"):
            fail("TOO_LATE_SEALED_OR_PENDING")
        old["state"] = "superseded"
        save_proposal(db, old)
        case["proposal"] = None


def refresh_support(case):
    from folio.provenance import FIELDS

    case["missing_fields"] = sorted(f for f in FIELDS if case.get(f, "") == "")
    unresolved = set(case.get("missing_fields", [])) | set(
        case.get("conflicting_fields", [])
    )
    for request in case.get("information_requests", []):
        request["state"] = (
            "open" if unresolved.intersection(request["fields"]) else "resolved"
        )
    case["state"] = "awaiting_info" if unresolved else "needs_review"


def receipt_lock(db, workspace):
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (workspace + ":receipt-allocations-v1",),
    )


def currency_basis(case, *, decision_version):
    """Bind derived monetary currency to this exact, current case record."""
    import re
    from copy import deepcopy

    sources = case.get("sources")
    if not isinstance(sources, list):
        raise ValueError("CASE_CURRENCY_BASIS_UNSUPPORTED")
    source_binding = []
    for source in sources:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("id"), str)
            or not source["id"]
            or not isinstance(source.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
            or type(source.get("version", 1)) is not int
            or source.get("version", 1) < 1
            or source.get("document_role") not in ("invoice", "purchase_order", "receipt")
        ):
            raise ValueError("CASE_CURRENCY_BASIS_UNSUPPORTED")
        source_binding.append(
            {
                "id": source["id"],
                "sha256": source["sha256"],
                "version": source.get("version", 1),
                "document_role": source["document_role"],
            }
        )
    if not {item["document_role"] for item in source_binding} >= {"invoice", "purchase_order"}:
        raise ValueError("CASE_CURRENCY_BASIS_UNSUPPORTED")

    evidence = [
        deepcopy(item)
        for item in case.get("field_provenance", {}).get("currency", [])
        if item.get("value") == case.get("currency")
    ]
    if not evidence:
        raise ValueError("CASE_CURRENCY_BASIS_UNSUPPORTED")
    return {
        "currency": case["currency"],
        "case_id": case["id"],
        "workspace": case["workspace"],
        "case_version": decision_version,
        "case_source_digest": case["source_digest"],
        "case_sources": source_binding,
        "field_provenance": evidence,
    }


def allocate(db, case, quantity, *, frozen=None):
    from folio.persisted_receipts import (
        case_identity,
        dimensions,
        expiry,
        input_version,
        number,
        require,
    )
    from folio.persisted_receipts import manifest as validate_manifest
    from folio.persisted_receipts import proposal as validate_proposal

    manifest = case.get("receipt_manifest")
    if "receipt_manifest" not in case:
        return None  # Existing internal no-source fixtures, not uploaded authority.
    validate_manifest(manifest)
    number(quantity)
    if manifest["errors"] or not manifest["rows"]:
        raise ValueError("RECEIPT_SUPPORT_UNRESOLVED")
    currency, unit = dimensions(case)
    if any(
        row["unit"] != unit
        or row.get("currency") not in (None, currency)
        for row in manifest["rows"]
    ):
        raise ValueError("CURRENCY_OR_UNIT_MISMATCH")
    receipt_lock(db, case["workspace"])
    used = {}
    for (proposal,) in db.execute(
        "SELECT body FROM proposals WHERE workspace=%s", (case["workspace"],)
    ):
        require(isinstance(proposal, dict))
        require(
            proposal.get("state")
            in (
                "pending",
                "approved",
                "rejected",
                "superseded",
                "revoked",
                "expired",
                "executed",
                "blocked",
            )
        )
        require(
            proposal.get("gate")
            in (
                None,
                "prepared",
                "seal_pending",
                "sealed",
                "uncertain",
                "applied",
                "reconciled_absent",
            )
        )
        protected = proposal.get("gate") in (
            "seal_pending",
            "sealed",
            "uncertain",
            "applied",
        )
        active = proposal["state"] in ("pending", "approved")
        if active and proposal["state"] == "pending":
            active = expiry(proposal) > datetime.now(timezone.utc)
        if not (protected or active):
            continue
        case_identity(proposal.get("case_id"))
        old = db.execute(
            "SELECT body FROM cases WHERE workspace=%s AND id=%s",
            (case["workspace"], proposal["case_id"]),
        ).fetchone()
        require(old is not None)
        allocation = validate_proposal(proposal, old[0], case["workspace"])
        if proposal["case_id"] == case["id"] and not protected:
            continue
        if proposal["target"] == case["target_id"]:
            raise ValueError("TARGET_ALREADY_RESERVED_OR_RECOGNIZED")
        if "allocation_manifest" not in proposal:
            # Legacy ABSENCE is not NULL. A validated cached support can prove
            # disjoint identities. An uncached nonempty original packet cannot:
            # require explicit review, never parse/repair historical JSON here.
            if old[0]["supplier"] == case["supplier"]:
                support = old[0].get("receipt_manifest")
                if support is None and old[0].get("sources"):
                    raise ValueError("LEGACY_RECEIPT_ALLOCATION_REQUIRES_REVIEW")
                prior_keys = {r["key"] for r in support["rows"]} if support else set()
                if prior_keys.intersection(r["key"] for r in manifest["rows"]):
                    raise ValueError("LEGACY_RECEIPT_ALLOCATION_REQUIRES_REVIEW")
        for row in allocation["rows"] if allocation is not None else []:
            if row["supplier"] == case["supplier"]:
                matching = next(
                    (r for r in manifest["rows"] if r["key"] == row["key"]), None
                )
                if matching and any(
                    matching[field] != row[field]
                    for field in ("quantity", "unit")
                ):
                    raise ValueError("RECEIPT_IDENTITY_CONFLICT_ACROSS_IMPORTS")
                if matching and matching.get("currency") is not None and matching["currency"] != row["currency"]:
                    raise ValueError("RECEIPT_IDENTITY_CONFLICT_ACROSS_IMPORTS")
                used[row["key"]] = used.get(row["key"], Decimal(0)) + number(
                    row["allocated"]
                )
                if used[row["key"]] > number(row["quantity"]):
                    raise ValueError("RECEIPT_PRIOR_ALLOCATION_CONFLICT")
    basis = None
    if any(row.get("currency") is None for row in manifest["rows"]):
        basis = currency_basis(
            case,
            decision_version=case["version"] if frozen is not None else case["version"] + 1,
        )
    if frozen is not None:
        validate_manifest(
            frozen, supplier=case["supplier"], currency=currency, unit=unit
        )
        # Validate the signed selection, never choose replacement rows or rewrite it.
        if (
            frozen.get("policy") != "receipt-allocation-v1"
            or frozen.get("source_digest") != case["source_digest"]
            or input_version(frozen) != case["version"] - 1
            or Decimal(frozen.get("total", "0")) != Decimal(quantity)
            or not frozen.get("rows")
        ):
            raise ValueError("FROZEN_RECEIPT_ALLOCATION_STALE")
        selected = set()
        total = Decimal(0)
        for row in frozen["rows"]:
            support = next(
                (r for r in manifest["rows"] if r["key"] == row["key"]), None
            )
            amount = Decimal(row["allocated"])
            if (
                row["key"] in selected
                or row.get("supplier") != case["supplier"]
                or not support
                or any(
                    row.get(k) != support.get(k)
                    for k in ("key", "receipt_id", "line_id", "quantity", "unit", "evidence", "field_evidence")
                )
                or (
                    support.get("currency") is not None
                    and row.get("currency") != support["currency"]
                )
                or (
                    support.get("currency") is None
                    and (
                        row.get("currency") != currency
                        or row.get("currency_status") != "derived_from_case"
                        or row.get("currency_basis") != basis
                    )
                )
                or not amount.is_finite()
                or amount <= 0
                or amount + used.get(row["key"], Decimal(0))
                > Decimal(support["quantity"])
            ):
                raise ValueError("FROZEN_RECEIPT_ALLOCATION_STALE")
            selected.add(row["key"])
            total += amount
        if total != Decimal(quantity):
            raise ValueError("FROZEN_RECEIPT_ALLOCATION_STALE")
        return frozen
    remaining = Decimal(quantity)
    allocations = []
    for row in manifest["rows"]:
        available = Decimal(row["quantity"]) - used.get(row["key"], Decimal(0))
        if available < 0:
            raise ValueError("RECEIPT_PRIOR_ALLOCATION_CONFLICT")
        take = min(remaining, available)
        if take > 0:
            allocated = {
                **row,
                "supplier": case["supplier"],
                "prior_allocated": decimal_text(used.get(row["key"], 0)),
                "allocated": decimal_text(take),
            }
            if row.get("currency") is None:
                allocated["currency"] = currency
                allocated["currency_status"] = "derived_from_case"
                allocated["currency_basis"] = basis
            allocations.append(allocated)
            remaining -= take
    if remaining:
        raise ValueError("QUANTITY_EXCEEDS_UNALLOCATED_RECEIPT")
    return {
        "policy": "receipt-allocation-v1",
        "source_digest": case["source_digest"],
        "input_case_version": case["version"],
        "rows": allocations,
        "total": decimal_text(quantity),
    }
