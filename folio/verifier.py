"""Independent comparison and retrieval; imports neither calculator nor target code."""

import hashlib
import json
import os
from datetime import datetime, timezone

import httpx

from folio.identity import service_header


def check_no_change(
    case, target=os.getenv("FOLIO_TARGET_URL", "http://127.0.0.1:8201")
):
    from decimal import Decimal
    from fractions import Fraction

    from folio.money import minor_units

    try:
        quantity = min(
            Fraction(case[k])
            for k in ["invoice_quantity", "ordered_quantity", "received_quantity"]
        )
        if quantity <= 0:
            return {"outcome": "conflict", "write_requests": 0}

        def round_minor(v):
            return (v.numerator * 2 + v.denominator) // (2 * v.denominator)

        places = minor_units(case["currency"])
        if places is None:
            return {"outcome": "conflict", "write_requests": 0}
        factor = 10**places

        def money(v):
            return str(v) if places == 0 else f"{v // factor}.{v % factor:0{places}d}"

        net = round_minor(quantity * Fraction(case["unit_price"]) * factor)
        tax = round_minor(Fraction(net) * Fraction(case["tax_rate"]))
        response = httpx.get(
            f"{target}/api/v1/workspaces/{case['workspace']}/payables/{case['target_id']}",
            headers=service_header("verifier"),
            timeout=5,
        )
        response.raise_for_status()
        observed = response.json()
        matches = (
            Fraction(observed["recognized_quantity"]) == quantity
            and observed["net_amount"] == money(net)
            and observed["tax_amount"] == money(tax)
            and observed["gross_amount"] == money(net + tax)
            and observed["status"] == "partially_matched"
            and observed.get("unit", "each") == case["unit"]
            and all(
                observed.get(k) == case[v]
                for k, v in [
                    ("supplier", "supplier"),
                    ("invoice_key", "invoice"),
                    ("currency", "currency"),
                    ("invoice_face_amount", "invoice_face_amount"),
                ]
            )
            and Decimal(money(net + tax)) <= Decimal(case["invoice_face_amount"])
        )
        return {
            "outcome": "no_change" if matches else "conflict",
            "current_record": observed,
            "write_requests": 0,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "verifier": "independent-rational-nochange-v2",
        }
    except (httpx.HTTPError, ValueError, KeyError, ZeroDivisionError):
        return {"outcome": "unavailable", "write_requests": 0}


def check(proposal, target=os.getenv("FOLIO_TARGET_URL", "http://127.0.0.1:8201")):
    url = f"{target}/api/v1/workspaces/{proposal['workspace']}/operations/{proposal['operation_key']}"
    try:
        r = httpx.get(url, headers=service_header("verifier"), timeout=8)
    except httpx.HTTPError as e:
        return {
            "outcome": "unavailable",
            "checks": {},
            "reason": type(e).__name__,
            "verifier": "independent-v1",
            "write_requests": 0,
        }
    if r.status_code == 404:
        observation = r.json().get("detail", {})
        if (
            observation.get("authoritative") is True
            and observation.get("not_applied") is True
            and observation.get("operation_key") == proposal["operation_key"]
            and observation.get("workspace") == proposal["workspace"]
            and observation.get("intent_digest") == proposal["target_digest"]
        ):
            return {
                "outcome": "not_applied",
                "reconciliation": observation,
                "write_requests": 0,
            }
    if r.status_code != 200:
        return {
            "outcome": "unavailable",
            "status": r.status_code,
            "observation": r.json(),
            "write_requests": 0,
        }
    observation = r.json()
    receipt = observation["receipt"]
    before = receipt["before_snapshot"]
    after = receipt["after_snapshot"]

    def sha(v):
        return hashlib.sha256(
            json.dumps(
                v, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()

    allowed = {
        "recognized_quantity",
        "net_amount",
        "tax_amount",
        "gross_amount",
        "status",
    }
    checks = {
        "authoritative": observation.get("authoritative") is True,
        "same_operation": receipt["operation_key"] == proposal["operation_key"],
        "same_intent": receipt["intent_digest"] == proposal["target_digest"],
        "same_target": receipt["target_id"] == proposal["target"],
        "before_revision": before["revision"] == proposal["target_revision"],
        "one_revision": after["revision"] == before["revision"] + 1,
        "five_exact_fields": set(proposal["patch"]) == allowed
        and all(after[k] == v for k, v in proposal["patch"].items()),
        "protected_unchanged": {
            k: v for k, v in before.items() if k not in allowed | {"revision"}
        }
        == {k: v for k, v in after.items() if k not in allowed | {"revision"}},
        "bound_before_snapshot": sha(before) == proposal.get("before_snapshot_digest"),
        "snapshot_digests": sha(before) == receipt["before_digest"]
        and sha(after) == receipt["after_digest"],
        "history_contains_once": sum(
            x["operation_key"] == proposal["operation_key"]
            for x in observation["effect_history"]
        )
        == 1,
        "sealed_intent": receipt["grant"]["intent_digest"] == proposal["target_digest"]
        and receipt["grant"]["patch"] == proposal["patch"],
    }
    return {
        "outcome": "matched" if all(checks.values()) else "conflict",
        "checks": checks,
        "observation": observation,
        "current_drift": observation["current_record"] != after,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verifier": "independent-v1",
        "write_requests": 0,
    }
