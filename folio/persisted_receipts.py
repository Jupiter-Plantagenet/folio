"""Read-only integrity boundary for persisted receipt reservations (policy v1).

Data errors are ValueError; programming errors are not suppressed. No repairs,
normalisation, signed-intent changes or interpretation of NULL as legacy absence.
"""

import re
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

POLICY = "receipt-allocation-v1"
ERROR = "PERSISTED_RECEIPT_HISTORY_INVALID"


def require(condition):
    if not condition:
        raise ValueError(ERROR)


def text(value):
    require(isinstance(value, str) and bool(value) and len(value) <= 512)
    return value


def expiry(proposal):
    require(isinstance(proposal, dict))
    value = proposal.get("expires")
    require(isinstance(value, str) and len(value) <= 64)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(ERROR) from error
    require(result.tzinfo is not None and result.utcoffset() is not None)
    return result


def number(value, *, zero=False):
    # Same fixed-point scope as receipt v1: at most 12 integral/6 fractional
    # digits. Reject bool, floats, exponents, NaN/Infinity before Decimal math.
    require(isinstance(value, str) and len(value) <= 19)
    require(re.fullmatch(r"(0|[1-9]\d{0,11})(\.\d{1,6})?", value) is not None)
    result = Decimal(value)
    require(result.is_finite() and (result >= 0 if zero else result > 0))
    return result


def quoted_number(item):
    """The cited literal's value; a recorded normalization is recomputed.

    folio-layout-v2 evidence may quote a printed form ("1,200", OCR "1O0")
    together with the deterministic rule that reads it. Nothing else may
    differ between the quote and the value.
    """
    quote = item.get("quote")
    if "normalization" not in item:
        return number(quote)
    from folio.numbers import NumberError, recompute

    text(quote)
    try:
        return number(recompute(quote, item["normalization"]))
    except NumberError as error:
        raise ValueError(ERROR) from error


def quoted_unit(item):
    quote = item.get("quote")
    if "normalization" not in item:
        return quote
    record = item["normalization"]
    if isinstance(record, dict) and record.get("rule") == "folio-unit-v2":
        # Pinned unit table (folio.units): the quote must map to the unit.
        from folio.units import recompute

        text(quote)
        try:
            return recompute(quote, record)
        except ValueError as error:
            raise ValueError(ERROR) from error
    require(record == {"rule": "folio-unit-v1"})
    require(isinstance(quote, str) and quote.casefold() in ("each", "ea"))
    return "each"


def field_evidence(row, quantity_evidence):
    """Additive linkage is checked when present; old signed v1 stays untouched."""
    if "field_evidence" not in row:
        return
    fields = row["field_evidence"]
    names = {"receipt_id", "line_id", "quantity", "unit"}
    require(isinstance(fields, dict) and names <= set(fields) <= names | {"currency"})
    require(fields["quantity"] == quantity_evidence)
    for name in set(fields):
        items = fields[name]
        expected = len(quantity_evidence)
        require(isinstance(items, list) and len(items) == expected)
        for item, basis in zip(items, quantity_evidence):
            require(isinstance(item, dict))
            if item.get("kind") == "policy-default":
                require(name in ("line_id", "currency"))
                require(item.get("policy") == POLICY)
                require(
                    item.get("value")
                    == row[name]
                    == {"line_id": "1", "currency": "USD"}[name]
                )
                text(item.get("reason"))
                continue
            require(
                all(
                    item.get(key) == basis.get(key)
                    for key in (
                        "source_id",
                        "source_sha256",
                        "source_version",
                        "page",
                        "text_artifact_sha256",
                    )
                )
            )
            require(
                isinstance(item.get("text_artifact_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", item["text_artifact_sha256"])
                is not None
            )
            require(type(item.get("start")) is int and type(item.get("end")) is int)
            require(0 <= item["start"] < item["end"])
            if name == "quantity":
                require(quoted_number(item) == number(row[name]))
            elif name == "unit":
                require(quoted_unit(item) == row[name])
            else:
                require(item.get("quote") == row[name])
            if "cell" in item:
                require(
                    isinstance(item["cell"], dict)
                    and not item["cell"].get("requires_review")
                )


def dimensions(value):
    """Only pinned canonical identities, with no conversions or guessing."""
    from folio.money import MINOR_UNITS
    from folio.units import UNITS

    currency, unit = value.get("currency"), value.get("unit")
    require(isinstance(currency, str) and currency in MINOR_UNITS)
    require(isinstance(unit, str) and unit in UNITS)
    return currency, unit


def manifest(value, *, supplier=None, currency=None, unit=None):
    require(isinstance(value, dict) and value.get("policy") == POLICY)
    rows = value.get("rows")
    require(isinstance(rows, list) and bool(rows))
    if supplier is None:
        require(isinstance(value.get("errors"), list))
        if value["errors"]:
            raise ValueError("RECEIPT_SUPPORT_UNRESOLVED")
    if supplier is not None:
        total = number(value.get("total"))
    else:
        # Support is a sum, not a requested/scalar quantity. Its finite lexical
        # bound and exact addition precision derive from the validated row scope
        # (12 integral / 6 fractional digits) and actual row count, not a new cap.
        digits = 12 + len(str(len(rows)))
        raw_total = value.get("total")
        require(isinstance(raw_total, str) and len(raw_total) <= digits + 7)
        require(
            re.fullmatch(rf"(0|[1-9]\d{{0,{digits - 1}}})(\.\d{{1,6}})?", raw_total)
            is not None
        )
        total = Decimal(raw_total)
    keys, summed, currencies, units = set(), Decimal(0), set(), set()
    for row in rows:
        require(isinstance(row, dict))
        receipt, line = row.get("receipt_id"), row.get("line_id")
        for token in (receipt, line):
            require(
                isinstance(token, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token) is not None
            )
        key = receipt + ":" + line
        require(row.get("key") == key and key not in keys)
        keys.add(key)
        raw_currency, row_unit = row.get("currency"), row.get("unit")
        from folio.money import MINOR_UNITS
        from folio.units import UNITS

        require(isinstance(row_unit, str) and row_unit in UNITS)
        require(raw_currency is None or (isinstance(raw_currency, str) and raw_currency in MINOR_UNITS))
        if raw_currency is None:
            require(supplier is None)
            require(row.get("currency_status") == "quantity_only")
            require(not row.get("field_evidence", {}).get("currency"))
        else:
            allowed_status = (None, "declared", "derived_from_case") if supplier is not None else (None, "declared")
            require(row.get("currency_status") in allowed_status)
        units.add(row_unit)
        if raw_currency is not None:
            currencies.add(raw_currency)
        require(len(units) == 1 and len(currencies) <= 1)
        if currency is not None or unit is not None:
            require(row_unit == unit)
            require(raw_currency in (None, currency) if supplier is None else raw_currency == currency)
        if supplier is not None and row.get("currency_status") == "derived_from_case":
            basis = row.get("currency_basis")
            require(isinstance(basis, dict))
            require(basis.get("currency") == raw_currency)
            require(isinstance(basis.get("case_sources"), list) and basis["case_sources"])
            roles = {item.get("document_role") for item in basis["case_sources"] if isinstance(item, dict)}
            require({"invoice", "purchase_order"} <= roles)
            text(basis.get("case_id"))
            text(basis.get("workspace"))
            require(type(basis.get("case_version")) is int and basis["case_version"] >= 1)
            text(basis.get("case_source_digest"))
            require(isinstance(basis.get("field_provenance"), list) and basis["field_provenance"])
        elif supplier is not None:
            require(row.get("currency_status") in (None, "declared"))
        if (raw_currency, row_unit) not in (("USD", "each"), (None, "each")):
            # Only historical USD/each v1 rows predate explicit field linkage.
            require("field_evidence" in row)
        quantity = number(row.get("quantity"))
        evidence = row.get("evidence")
        require(isinstance(evidence, list) and bool(evidence))
        for item in evidence:
            require(isinstance(item, dict))
            text(item.get("source_id"))
            sha = item.get("source_sha256")
            require(
                isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha) is not None
            )
            for field in ("source_version", "page", "start", "end"):
                require(
                    type(item.get(field)) is int
                    and item[field] >= (0 if field == "start" else 1)
                )
            require(item["end"] > item["start"])
            require(quoted_number(item) == quantity)
        field_evidence(row, evidence)
        if supplier is not None:
            require(row.get("supplier") == text(supplier))
            allocated = number(row.get("allocated"))
            prior = number(row.get("prior_allocated"), zero=True)
            require(allocated + prior <= quantity)
            summed += allocated
        else:
            with localcontext() as context:
                context.prec = 18 + len(str(len(rows)))
                summed += quantity
    require(summed == total)
    return value


def case_identity(value):
    require(isinstance(value, str))
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(ERROR) from error
    require(str(parsed) == value)
    return value


def input_version(allocation):
    """Interpret frozen v1 spellings without modifying signed historical JSON."""
    fields = [k for k in ("input_case_version", "case_version") if k in allocation]
    require(bool(fields))
    values = [allocation[k] for k in fields]
    require(all(type(v) is int and v >= 1 for v in values))
    require(all(v == values[0] for v in values))
    return values[0]


def proposal(value, case, workspace, *, current=False):
    require(isinstance(value, dict) and isinstance(case, dict))
    require(value.get("workspace") == workspace and case.get("workspace") == workspace)
    require(value.get("case_id") == case.get("id"))
    require(value.get("target") == case.get("target_id"))
    for field in ("id", "case_id", "target", "state"):
        text(value.get(field))
    expiry(value)
    if current:
        for field in ("prepared_by", "source_digest", "digest", "operation_key"):
            text(value.get(field))
        for field in ("version", "case_version"):
            require(type(value.get(field)) is int and value[field] >= 1)
        require(isinstance(value.get("patch"), dict))
    text(case.get("supplier"))
    currency, unit = dimensions(case)
    sources = case.get("sources", [])
    require(isinstance(sources, list) and all(isinstance(s, dict) for s in sources))
    if "receipt_manifest" in case:
        manifest(case["receipt_manifest"], currency=currency, unit=unit)
    if "allocation_manifest" not in value:
        return None  # Explicit legacy ABSENCE only; caller enforces overlap denial.
    allocation = manifest(
        value["allocation_manifest"],
        supplier=case.get("supplier"),
        currency=currency,
        unit=unit,
    )
    require(allocation.get("source_digest") == text(value.get("source_digest")))
    version = input_version(allocation)
    require(
        type(value.get("case_version")) is int and version == value["case_version"] - 1
    )
    patch = value.get("patch")
    require(
        isinstance(patch, dict)
        and number(patch.get("recognized_quantity")) == number(allocation["total"])
    )
    if "receipt_manifest" in case:
        support = manifest(case["receipt_manifest"], currency=currency, unit=unit)
        by_key = {r["key"]: r for r in support["rows"]}
        for row in allocation["rows"]:
            require(row["key"] in by_key)
            original = by_key[row["key"]]
            require(all(row.get(k) == original.get(k) for k in ("key", "receipt_id", "line_id", "quantity", "unit", "evidence", "field_evidence")))
            if original.get("currency") is not None:
                require(row.get("currency") == original["currency"])
            else:
                require(row.get("currency_status") == "derived_from_case")
                basis = row.get("currency_basis")
                require(isinstance(basis, dict))
                require(basis.get("currency") == currency)
                require(basis.get("case_id") == case.get("id"))
                require(basis.get("workspace") == workspace)
                require(basis.get("case_source_digest") == value.get("source_digest"))
                require(basis.get("case_version") == value.get("case_version"))
                expected_case_sources = [
                    {
                        "id": source.get("id"),
                        "sha256": source.get("sha256"),
                        "version": source.get("version", 1),
                        "document_role": source.get("document_role"),
                    }
                    for source in sources
                ]
                require(basis.get("case_sources") == expected_case_sources)
                current_currency_evidence = [
                    item
                    for item in case.get("field_provenance", {}).get("currency", [])
                    if item.get("value") == currency
                ]
                require(basis.get("field_provenance") == current_currency_evidence)
                require(bool(basis.get("field_provenance")))
                from folio.provenance import validate_citation

                for item in basis["field_provenance"]:
                    require(isinstance(item, dict) and item.get("value") == currency)
                    source = item.get("source")
                    if source is not None:
                        try:
                            require(validate_citation(sources, source) == source)
                        except ValueError as error:
                            raise ValueError(ERROR) from error
                    else:
                        require(item.get("kind") == "human")
                        text(item.get("actor"))
                        text(item.get("reason"))
    return allocation
