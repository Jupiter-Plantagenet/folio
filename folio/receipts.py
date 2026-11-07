"""Receipt policy v1: labelled pages or strict CSV/XLSX receipt tables.
Aliases are evidence, never additional received quantity. No source filename,
model assertion or corrected aggregate establishes quantity.
"""

import hashlib
import re
from copy import deepcopy
from decimal import Decimal


def decimal_text(value):
    return format(Decimal(value).normalize(), "f")


# 128 ASCII characters covers UUIDs and common external business keys while
# bounding persisted keys/locks/evidence. This is a whole-field limit, not a slice.
MAX_ID_LENGTH = 128


def _receipt_currency(currency, text):
    """Return an explicit receipt code, or a typed unpriced quantity state."""
    from folio.money import MINOR_UNITS, read_mark, split_mark

    amount_codes = {
        code
        for code in re.findall(
            r"\b(?:unit\s+price|price|amount|subtotal|total|value|cost|charge)\s+([A-Z]{3})\s+(?=\d)",
            text or "",
            re.IGNORECASE,
        )
    }
    monetary_value = re.compile(
        r"\b(?:unit\s+price|price|amount|subtotal|total|value|cost|charge)\b"
        r"\s*(?:[:#=-]\s*)?(?:(?:[A-Z]{3})\s*)?[$€£¥₹₩]?\s*\d",
        re.IGNORECASE,
    )
    monetary_mark = re.compile(r"(?:[$€£¥₹₩]\s*\d|\d\s*[$€£¥₹₩])")
    if any(code not in MINOR_UNITS for code in amount_codes):
        raise ValueError("RECEIPT_CURRENCY_MISMATCH")
    if currency is not None:
        if not isinstance(currency, str) or currency not in MINOR_UNITS:
            raise ValueError("RECEIPT_CURRENCY_MISMATCH")
        if amount_codes - {currency}:
            raise ValueError("RECEIPT_CURRENCY_MISMATCH")
        for line in (text or "").splitlines():
            if not re.search(
                monetary_value.pattern + "|" + monetary_mark.pattern,
                line,
                re.IGNORECASE,
            ):
                continue
            for token in re.findall(r"\S+", line):
                mark = read_mark(token.strip("()[]{}.,;:"))
                split = split_mark(token.strip("()[]{}.,;:"))
                candidate = mark or (split[0] if split else None)
                if candidate and not candidate.compatible(currency):
                    raise ValueError("RECEIPT_CURRENCY_MISMATCH")
        return currency, "declared"
    # A quantity-only goods receipt has no monetary dimension. Do not infer its
    # currency from an invoice, the locale, or the historical USD default.
    # Any priced or monetary amount field without its own code is unsupported.
    if amount_codes or monetary_value.search(text or "") or monetary_mark.search(text or ""):
        raise ValueError("RECEIPT_CURRENCY_REQUIRED")
    return None, "quantity_only"


CELL_LABELS = {
    "receipt",
    "goods receipt",
    "line",
    "received quantity",
    "unit",
    "currency",
    "purchase order",
}


# A row whose label starts like a receipt field is never a note: an unknown
# spelling of a field ("Line no", "Received qty") fails closed instead.
FIELD_WORDS = {
    "receipt", "goods", "grn", "line", "received", "delivered", "quantity",
    "qty", "unit", "uom", "currency", "purchase", "po",
}


def note_record(record):
    """A free-text row ("Remarks | left at dock 3") is a note, never a field.

    Its label must be words of letters (one of three or more letters), not a
    receipt label, in column 1, with at most one plain value cell beside it.
    Bare identifiers, numbers or punctuation ("/B") are not notes: they may be
    a wrapped continuation of an identity and stay fail-closed.
    """
    label = record[0].get("value")
    if (
        not isinstance(label, str)
        or label.lower() in CELL_LABELS
        or record[0].get("column") != 1
        or len(record) > 2
        or any(c.get("requires_review") for c in record)
        or (len(record) == 2 and record[1].get("column") != 2)
        or (len(record) == 2 and not isinstance(record[1].get("value"), str))
        or label.split(" ", 1)[0].strip(":").lower() in FIELD_WORDS
    ):
        return False
    return bool(
        re.fullmatch(r"[^\W\d_][^\W\d_ '.,-]*(?: [^\W\d_][^\W\d_'.,-]*)*:?", label)
        and re.search(r"[^\W\d_]{3}", label)
    )


def cell_identity(source):
    """Only labelled two-column receipt records have supported cell boundaries."""
    cells = source.get("cells")
    if not cells:
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    records = {}
    for cell in cells:
        records.setdefault((cell.get("sheet"), cell["row"]), []).append(cell)
    fields = {}
    for record in records.values():
        record = sorted(record, key=lambda c: c["column"])
        if note_record(record):
            continue
        label = record[0].get("value")
        if not isinstance(label, str) or label.lower() not in CELL_LABELS:
            raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
        if record[0]["column"] != 1 or record[0].get("requires_review"):
            raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
        if len(record) != 2 or record[1]["column"] != 2:
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        if record[1].get("requires_review"):
            raise ValueError("RECEIPT_FORMULA_UNSUPPORTED")
        value = record[1].get("value")
        if label.lower() not in {"receipt", "goods receipt", "line"}:
            key = label.lower()
            if key in fields:
                raise ValueError("RECEIPT_IDENTITY_REQUIRED")
            fields[key] = value
            if key == "received quantity" and (
                not isinstance(value, (str, int))
                or isinstance(value, bool)
                or not re.fullmatch(r"(0|[1-9]\d{0,11})(\.\d{1,6})?", str(value))
                or Decimal(str(value)) <= 0
            ):
                raise ValueError("RECEIPT_QUANTITY_INVALID")
            if key == "unit" and value != "each":
                raise ValueError("RECEIPT_UNIT_MISMATCH")
            if key == "currency" and value not in (None, ""):
                from folio.money import MINOR_UNITS

                if value not in MINOR_UNITS:
                    raise ValueError("RECEIPT_CURRENCY_MISMATCH")
            continue
        if (
            record[1].get("requires_review")
            or not isinstance(value, (str, int))
            or isinstance(value, bool)
        ):
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        key = "line" if label.lower() == "line" else "receipt"
        if key in fields:
            raise ValueError("RECEIPT_IDENTITY_REQUIRED")
        fields[key] = str(value)
    if "receipt" not in fields:
        raise ValueError("RECEIPT_IDENTITY_REQUIRED")
    return fields["receipt"], fields.get("line")


def text_identity(source, page):
    """Identity occupies one complete physical line; no wrapped continuations.

    PDF word-order text inserts spaces, so physical lines require geometry.
    OCR identities remain unsupported for automatic receipt authority.
    """
    if source.get("kind") == "ocr-or-mixed-pdf" or page.get("ocr_engine"):
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    if source.get("kind") == "digital-pdf":
        physical = []
        for word in page.get("words", []):
            boxes = word.get("boxes")
            if not boxes or word.get("requires_review"):
                raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
            top = min(b[1] for b in boxes)
            bottom = max(b[3] for b in boxes)
            if bottom - top > 30:
                raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
            if not physical or abs(top - physical[-1][0]) > 3:
                physical.append((top, [word["quote"]]))
            else:
                physical[-1][1].append(word["quote"])
        logical = [" ".join(words) for _, words in physical]
    else:
        if "\n" not in page["text"]:
            raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
        logical = page["text"].splitlines()
    fields = {}
    in_region = False
    for line in logical:
        match = re.fullmatch(r"(Goods receipt|Receipt|Line) ([^\r\n]*)", line, re.I)
        if match:
            key = "line" if match[1].lower() == "line" else "receipt"
            if key in fields:
                raise ValueError("RECEIPT_IDENTITY_REQUIRED")
            fields[key] = match[2]
            in_region = True
        elif re.match(r"^(?:Goods receipt|Receipt|Line)\b", line, re.I):
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        elif in_region and not re.fullmatch(
            r"(?:Received quantity|Unit|Currency|Purchase order) [^\r\n]+", line, re.I
        ):
            raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    if "receipt" not in fields:
        raise ValueError("RECEIPT_IDENTITY_REQUIRED")
    return fields["receipt"], fields.get("line")


def support_matches_sources(stored, sources):
    """Read-only v1 comparison: optional new provenance never rewrites history.

    Core authority and every previously stored evidence key must match. Only
    absence of the specifically additive field/cell/text linkage is compatible.
    Malformed historical cells still fail the fresh complete-cell boundary.
    """
    fresh = receipt_manifest(sources)
    if not isinstance(stored, dict) or fresh["errors"]:
        return False
    candidate = deepcopy(fresh)
    old_rows = stored.get("rows")
    if not isinstance(old_rows, list) or len(old_rows) != len(candidate["rows"]):
        return False
    for old, new in zip(old_rows, candidate["rows"]):
        if not isinstance(old, dict):
            return False
        if "field_evidence" not in old:
            new.pop("field_evidence", None)
        previous = old.get("evidence")
        if not isinstance(previous, list) or len(previous) != len(new["evidence"]):
            return False
        for before, after in zip(previous, new["evidence"]):
            if not isinstance(before, dict):
                return False
            for optional in ("text_artifact_sha256", "cell"):
                if optional not in before:
                    after.pop(optional, None)
    return stored == candidate


TABLE_HEADER = ["Receipt", "Line", "Received quantity", "Unit", "Currency"]
RECEIPT_FIELDS = ["receipt_id", "line_id", "quantity", "unit", "currency"]


def cell_citation(source, cell):
    page = source["pages"][0]
    words = [
        w
        for w in page.get("words", [])
        if all(
            w.get("cell", {}).get(k) == cell.get(k) for k in ("row", "column", "sheet")
        )
    ]
    value = str(cell["value"])
    if len(words) != 1 or words[0]["quote"] != value:
        raise ValueError("RECEIPT_TABLE_BOUNDARY_UNSUPPORTED")
    word = words[0]
    if page["text"][word["start"] : word["end"]] != value:
        raise ValueError("RECEIPT_TABLE_BOUNDARY_UNSUPPORTED")
    return {
        "source_id": source["id"],
        "source_sha256": source["sha256"],
        "source_version": source.get("version", 1),
        "page": page["number"],
        "text_artifact_sha256": hashlib.sha256(page["text"].encode()).hexdigest(),
        "start": word["start"],
        "end": word["end"],
        "quote": value,
        "cell": {k: v for k, v in cell.items() if k != "value"},
    }


def labelled_receipt(source, receipt_count):
    """Validated whole cells are authority; the text projection is never parsed."""
    rid, line = cell_identity(source)
    if len({c.get("sheet") for c in source["cells"]}) != 1:
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    records = {}
    for cell in source["cells"]:
        records.setdefault(cell["row"], []).append(cell)
    fields = {}
    for record in records.values():
        record = sorted(record, key=lambda c: c["column"])
        if note_record(record):
            continue
        label = record[0]["value"].lower()
        key = {
            "receipt": "receipt_id",
            "goods receipt": "receipt_id",
            "line": "line_id",
            "received quantity": "quantity",
        }.get(label, label)
        fields[key] = record[1]
    if "quantity" not in fields:
        raise ValueError("RECEIPT_QUANTITY_REQUIRED")
    if line is None and receipt_count > 1:
        raise ValueError("RECEIPT_LINE_REQUIRED")
    line = line if line is not None else "1"
    if any(
        not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}", v) for v in (rid, line)
    ):
        raise ValueError("RECEIPT_IDENTITY_INVALID")
    if "unit" not in fields:
        raise ValueError("RECEIPT_UNIT_MISMATCH")
    evidence = {
        key: [cell_citation(source, fields[key])]
        for key in RECEIPT_FIELDS
        if key in fields
        and (key != "currency" or fields[key].get("value") not in (None, ""))
    }
    currency_value = fields.get("currency", {}).get("value")
    if currency_value == "":
        currency_value = None
    currency, currency_status = _receipt_currency(
        currency_value, source["pages"][0].get("text", "")
    )
    if "line_id" not in evidence:
        key, value = "line_id", "1"
        evidence[key] = [
            {
                "kind": "policy-default",
                "policy": "receipt-allocation-v1",
                "value": value,
                "reason": "omitted in supported historical labelled layout",
            }
        ]
    if currency is not None and "currency" not in evidence:
        raise ValueError("RECEIPT_CURRENCY_EVIDENCE_INVALID")
    if currency is None:
        evidence.pop("currency", None)
    elif "currency" not in evidence:
        raise ValueError("RECEIPT_CURRENCY_EVIDENCE_INVALID")
    return {
        "key": rid + ":" + line,
        "receipt_id": rid,
        "line_id": line,
        "quantity": decimal_text(str(fields["quantity"]["value"])),
        "unit": "each",
        "currency": currency,
        "currency_status": currency_status,
        "evidence": deepcopy(evidence["quantity"]),
        "field_evidence": evidence,
    }


def table_receipts(source):
    """Strict single-sheet table; authoritative complete cells, never text prefixes.

    Return None for the historical two-column labelled layout. Table evidence
    retains the same manifest policy so reservations/approvals need no migration.
    """
    cells = source.get("cells", [])
    if not cells:
        return None
    records = {}
    for cell in cells:
        records.setdefault((cell.get("sheet"), cell["row"]), []).append(cell)
    first = sorted(next(iter(records.values())), key=lambda c: c["column"])
    if len(first) <= 2:
        return None
    if len({sheet for sheet, _ in records}) != 1:
        raise ValueError("RECEIPT_TABLE_BOUNDARY_UNSUPPORTED")
    header = [c.get("value") for c in first]
    if header != TABLE_HEADER or any(c.get("requires_review") for c in first):
        raise ValueError("RECEIPT_TABLE_HEADER_INVALID")
    if [c["column"] for c in first] != list(range(1, 6)):
        raise ValueError("RECEIPT_TABLE_HEADER_INVALID")
    rows = []
    for record in list(records.values())[1:]:
        record = sorted(record, key=lambda c: c["column"])
        if [c["column"] for c in record] != list(range(1, 6)):
            raise ValueError("RECEIPT_TABLE_ROW_INVALID")
        if any(c.get("requires_review") for c in record):
            raise ValueError("RECEIPT_FORMULA_UNSUPPORTED")
        rid, line, quantity, unit, currency = [c.get("value") for c in record]
        if any(
            not isinstance(v, (str, int))
            or isinstance(v, bool)
            or not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}", str(v))
            for v in (rid, line)
        ):
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        if (
            not isinstance(quantity, (str, int))
            or isinstance(quantity, bool)
            or not re.fullmatch(r"(0|[1-9]\d{0,11})(\.\d{1,6})?", str(quantity))
            or Decimal(str(quantity)) <= 0
        ):
            raise ValueError("RECEIPT_QUANTITY_INVALID")
        if unit != "each":
            raise ValueError("RECEIPT_UNIT_MISMATCH")
        if currency not in (None, ""):
            from folio.money import MINOR_UNITS

            if currency not in MINOR_UNITS:
                raise ValueError("RECEIPT_CURRENCY_MISMATCH")
        else:
            currency = None
        currency, currency_status = _receipt_currency(
            currency, source["pages"][0].get("text", "")
        )
        field_evidence = {
            key: [cell_citation(source, cell)]
            for key, cell in zip(RECEIPT_FIELDS, record)
            if key != "currency" or currency is not None
        }
        if currency is not None and "currency" not in field_evidence:
            raise ValueError("RECEIPT_CURRENCY_EVIDENCE_INVALID")
        rows.append(
            {
                "key": str(rid) + ":" + str(line),
                "receipt_id": str(rid),
                "line_id": str(line),
                "quantity": decimal_text(str(quantity)),
                "unit": unit,
                "currency": currency,
                "currency_status": currency_status,
                "evidence": deepcopy(field_evidence["quantity"]),
                "field_evidence": field_evidence,
            }
        )
    if not rows:
        raise ValueError("RECEIPT_TABLE_ROW_INVALID")
    declared_currencies = {row["currency"] for row in rows if row["currency"] is not None}
    if len(declared_currencies) > 1:
        raise ValueError("RECEIPT_CURRENCY_MISMATCH")
    return rows


def merge_receipt(rows, errors, row):
    key = row["key"]
    if key in rows:
        if rows[key]["quantity"] != row["quantity"]:
            errors.append("RECEIPT_IDENTITY_CONFLICT")
        if rows[key]["unit"] != row["unit"]:
            errors.append("RECEIPT_UNIT_MISMATCH")
        if (
            rows[key].get("currency") is not None
            and row.get("currency") is not None
            and rows[key]["currency"] != row["currency"]
        ):
            errors.append("RECEIPT_CURRENCY_MISMATCH")
        elif rows[key].get("currency") is None and row.get("currency") is not None:
            rows[key]["currency"] = row["currency"]
            rows[key]["currency_status"] = "declared"
            rows[key].setdefault("field_evidence", {}).setdefault("currency", []).extend(
                row.get("field_evidence", {}).get("currency", [])
            )
        rows[key]["evidence"].extend(row["evidence"])
        if "field_evidence" in row:
            for field, citations in row["field_evidence"].items():
                rows[key].setdefault("field_evidence", {}).setdefault(field, []).extend(
                    citations
                )
    else:
        rows[key] = row


def v1_page(source, page, receipt_count):
    """Receipt policy v1 strict labelled-page grammar (unchanged semantics).

    Returns a row, None when the page declares no received quantity, or
    raises ValueError(code).
    """
    text = page["text"]
    authoritative = (
        cell_identity(source)
        if source.get("kind") in {"csv", "xlsx"} or "cells" in source
        else text_identity(source, page)
    )
    quantities = list(re.finditer(r"Received quantity\s+(\S+)", text, re.I))
    units = list(re.finditer(r"\bUnit\s+(\S+)", text, re.I))
    currencies = list(re.finditer(r"\bCurrency\s+(\S+)", text, re.I))
    if not quantities:
        return None
    if len(quantities) != 1:
        raise ValueError("RECEIPT_IDENTITY_REQUIRED")
    if authoritative[1] is None and receipt_count > 1:
        raise ValueError("RECEIPT_LINE_REQUIRED")
    match = quantities[0]
    value = match.group(1)
    if not re.fullmatch(r"(0|[1-9]\d{0,11})(\.\d{1,6})?", value):
        raise ValueError("RECEIPT_QUANTITY_INVALID")
    if len(units) != 1 or units[0].group(1) != "each":
        raise ValueError("RECEIPT_UNIT_MISMATCH")
    if len(currencies) > 1:
        raise ValueError("RECEIPT_CURRENCY_MISMATCH")
    currency_value = currencies[0].group(1) if currencies else None
    if currency_value is not None and currency_value != currency_value.upper():
        raise ValueError("RECEIPT_CURRENCY_MISMATCH")
    currency, currency_status = _receipt_currency(currency_value, text)
    if any(
        w["end"] > match.start(1)
        and w["start"] < match.end(1)
        and w.get("cell", {}).get("requires_review")
        for w in page.get("words", [])
    ):
        raise ValueError("RECEIPT_FORMULA_UNSUPPORTED")
    receipt_id, declared_line = authoritative
    line_id = declared_line if declared_line is not None else "1"
    # Validate the entire authoritative field; never strip or slice an ID.
    # Colon is reserved for the key delimiter, all other punctuation unsupported.
    if any(
        not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}", token)
        for token in (receipt_id, line_id)
    ):
        raise ValueError("RECEIPT_IDENTITY_INVALID")
    key = receipt_id + ":" + line_id
    evidence = {
        "source_id": source["id"],
        "source_sha256": source["sha256"],
        "source_version": source.get("version", 1),
        "text_artifact_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "page": page["number"],
        "start": match.start(1),
        "end": match.end(1),
        "quote": value,
    }
    row = {
        "key": key,
        "receipt_id": receipt_id,
        "line_id": line_id,
        "quantity": decimal_text(value),
        "unit": "each",
        "currency": currency,
        "currency_status": currency_status,
        "evidence": [evidence],
    }
    field_evidence = {"quantity": [deepcopy(evidence)]}
    for field, label, literal in (
        ("receipt_id", r"(?:Goods receipt|Receipt)", receipt_id),
        ("line_id", "Line", declared_line),
        ("unit", "Unit", "each"),
        ("currency", "Currency", currency),
    ):
        if literal is None:
            if field == "currency":
                continue
            field_evidence[field] = [
                {
                    "kind": "policy-default",
                    "policy": "receipt-allocation-v1",
                    "value": row[field],
                    "reason": "omitted in supported historical labelled layout",
                }
            ]
            continue
        found = re.search(
            r"\b" + label + r"\s+(" + re.escape(literal) + r")(?!\S)",
            text,
            re.I,
        )
        if found:
            field_evidence[field] = [
                {
                    **evidence,
                    "start": found.start(1),
                    "end": found.end(1),
                    "quote": found.group(1),
                }
            ]
        elif field == "currency":
            raise ValueError("RECEIPT_CURRENCY_EVIDENCE_INVALID")
    row["field_evidence"] = field_evidence
    return row


WEAK_LABELS = {"received", "delivered", "counted", "count", "accepted", "rejected"}
DATE_TIME = re.compile(
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}:\d{2}(?::\d{2})?|[AaPp]\.?[Mm]\.?|\d{1,2}[-/.]\d{1,2}"
)
RECEIPT_COUNT_WORDS = {"received", "delivered", "counted", "accepted", "rcvd"}


MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-zä]*\.?"
NAMED_DATE = re.compile(rf"\d{{1,2}}\.? {MONTH},? \d{{4}}|{MONTH} \d{{1,2}},? \d{{4}}", re.I)


def _date_line(seg):
    """A date/time stamp ("2026-10-12 14:03", "09 July 2026") is a note, not an identity part."""
    if seg and NAMED_DATE.fullmatch(" ".join(t.text for t in seg)):
        return True
    return bool(seg) and all(DATE_TIME.fullmatch(t.text.rstrip(",;")) for t in seg) and any(
        re.search(r"[-/.:]", t.text) for t in seg
    )


def _declared_units(lines):
    """Units a receipt page declares by label ("Unit | EA", "Currency / unit: USD / EA")."""
    from folio.layout import match_label
    from folio.units import canonical_unit

    units = set()
    for line in lines:
        toks = [t for seg in line for t in seg]
        for i in range(len(toks)):
            m = match_label(toks, i, "receipt")
            if not m or m[1] not in ("unit", "dims", "dims_rev"):
                continue
            rest = [t for t in toks[i + m[2]:] if t.text not in (":", "/")]
            if rest and m[1] == "dims":
                rest = rest[1:]
            if rest and rest[0].readable() and canonical_unit(rest[0].text.rstrip(".,;:")):
                units.add(canonical_unit(rest[0].text.rstrip(".,;:")))
    return units


def _po_ref(seg, j):
    """A receipt's purchase-order reference, keyed like provenance.purchase_order_key.

    "PO 9999", "4500 9999" or "9999 rev2" are keyed as a whole. A reference
    that cannot be keyed (unreadable, no digit, too many words) is kept with
    key None: it refuses the receipt quantity rather than being ignored.
    """
    from folio.provenance import purchase_order_key

    toks = seg[j:]
    if not toks:
        return []
    text = " ".join(t.text for t in toks).rstrip(".,;:")
    key = None
    if len(toks) <= 4 and all(t.readable() for t in toks) and re.search(r"\d", text) \
            and re.fullmatch(r"[A-Za-z0-9#./ -]+", text):
        key = purchase_order_key(text)
    elif len(toks) == 1 and toks[0].readable() and re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", text) \
            and len(text) >= 4:
        # A single hyphenated identifier without digits ("PO-DIM-EUR-KG") is
        # still an identifier; it is compared with the packet's PO like any other.
        key = purchase_order_key(text)
    return [{"field": "po_ref", "value": text, "key": key, "start": toks[0].start, "end": toks[-1].end,
             "quote": " ".join(t.text for t in toks)}]


def _ref_item(seg, j, ctx):
    """A delivery-note / packing-slip number: a receipt identity only as a fallback."""
    toks = seg[j:]
    item = {"field": "receipt_ref", "value": toks[0].text, "start": toks[0].start, "end": toks[-1].end,
            "quote": ctx["text"][toks[0].start:toks[-1].end]}
    item["valid"] = len(toks) == 1 and toks[0].readable() and RECEIPT_ID.fullmatch(toks[0].text) is not None
    return item


def _unit_label(seg):
    """"Pallets received: 3", "Cartons counted 60": a unit word as the label."""
    from folio.layout import norm
    from folio.units import WEAK, canonical_unit

    if len(seg) >= 3 and norm(seg[1].text) in RECEIPT_COUNT_WORDS:
        unit = canonical_unit(seg[0].text)
        if unit and seg[0].text.casefold() not in WEAK:
            return unit, seg[0]
    return None


def _receipt_segment(seg, ctx, pending):
    """One column segment of a receipt page under folio-layout-v2.

    Returns (items, pending_label). Free text and date/time stamps are
    ignored as notes. A fragment that is neither a labelled field nor free
    text (a bare identifier, number or punctuation run) may be a wrapped
    continuation of an identity and fails closed.
    """
    from folio.layout import FILLER, _skip, is_prose, match_label, norm

    label = match_label(seg, 0, "receipt")
    if pending and pending[0] == "ignore":
        if not label:
            return [], None  # the value cell of an ignored label ("Invoice reference | INV-7")
        pending = None
    if pending and label and pending[1] == "rid" and label[1] == "rid":
        pending = None  # "GOODS RECEIVED NOTE | GRN No. | GR-5": a title beside the labelled id
    if pending and pending[0] == "po_ref":
        if not label:
            return _po_ref(seg, 0), None
        pending = None
    if pending and pending[0] in ("item_code", "item_desc"):
        if not label:
            return [{"field": "ident_" + pending[0], "value": " ".join(t.text for t in seg)}], None
        pending = None
    if pending:
        if label:
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        field, kind = pending
        if kind in ("dims", "dims_rev"):
            return _receipt_dims(seg, 0, kind, ctx), None
        if field == "receipt_ref":
            return [_ref_item(seg, 0, ctx)], None
        return _robust_value(seg, 0, field, kind, ctx) or [None], None
    if not label:
        by_unit = _unit_label(seg)
        if by_unit:
            unit, tok = by_unit
            j = _skip(seg, 2, FILLER | {":"})
            items = _robust_value(seg, j, "received_quantity", "qty", ctx)
            if items is None:
                raise ValueError("RECEIPT_QUANTITY_INVALID")
            items.append(_unit_item(unit, tok, tok.start, tok.start + len(tok.text), ctx))
            return items, None
        if is_prose(seg) or _date_line(seg):
            return [], None
        if len(seg) == 1 and seg[0].readable() and "po_refs" in ctx:
            from folio.provenance import purchase_order_key

            # A bare reference equal to the purchase order declared by the
            # invoice/PO ("PO-38012") is that reference, not an identity part.
            if purchase_order_key(seg[0].text.rstrip(".,;:")) in ctx["po_refs"]():
                return [], None
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    field, kind, n, _ = label
    if kind in ("dims", "dims_rev"):
        if _skip(seg, n, {":", "-", "="}) >= len(seg):
            return [], (field, kind)
        return _receipt_dims(seg, n, kind, ctx), None
    if field == "purchase_order":
        # The purchase order the receipt is for: checked against the invoice/PO.
        if _skip(seg, n, FILLER | {":"}) >= len(seg):
            return [], ("po_ref", kind)
        return _po_ref(seg, _skip(seg, n, FILLER | {":"})), None
    if field in ("item_code", "item_desc"):
        # The item the receipt is for ("Code | SR-10", "Material | Seal ring").
        j = _skip(seg, n, FILLER | {":"})
        if j >= len(seg):
            return [], (field, kind)
        return [{"field": "ident_" + field, "value": " ".join(t.text for t in seg[j:])}], None
    if kind not in ("qty", "rid", "unit", "currency"):
        # Totals, parties, items, PO/invoice references: never receipt authority.
        # A label alone in its cell also owns the value cell beside it.
        if kind in ("id", "text") and _skip(seg, n, FILLER | {":"}) >= len(seg):
            return [], ("ignore", kind)
        return [], None
    j = _skip(seg, n, FILLER | {":"})
    if j >= len(seg):
        return [], (field, kind)
    weak = n == 1 and norm(seg[0].text) in WEAK_LABELS
    if kind == "qty":
        try:
            items = _robust_value(seg, j, field, kind, ctx)
        except ValueError:
            if weak and is_prose(seg):
                return [], None  # "Received 2 pallets in good condition."
            raise
        if items is None and is_prose(seg[j:]):
            return [], None  # "Received in good condition."
        if items is None and weak and is_prose(seg):
            return [], None
        return items or [None], None
    if field == "receipt_ref":
        return [_ref_item(seg, j, ctx)], None
    if kind == "rid":
        tok, rest = seg[j], seg[j + 1 :]
        whole = re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}", tok.text)
        if (not whole or rest) and not re.search(r"\d", tok.text) and is_prose(seg[j:]):
            return [], None  # "Receipt of goods acknowledged."
        if not whole or rest:
            raise ValueError("RECEIPT_IDENTITY_INVALID")
    return _robust_value(seg, j, field, kind, ctx), None


def _receipt_dims(seg, n, kind, ctx):
    """ "Currency / unit: USD / BOX" on a receipt: both values must read."""
    from folio.layout import _skip

    j = _skip(seg, n, {":", "-", "="})
    toks = [t for t in seg[j:] if t.text != "/"]
    slashes = [t.text for t in seg[j:]].count("/")
    if not (len(toks) == 2 and slashes == 1 and seg[j].text != "/") and not (len(toks) == 1 and not slashes):
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    order = ("currency", "unit") if kind == "dims" else ("unit", "currency")
    items = []
    for name, tok in zip(order, toks):
        items.extend(_robust_value([tok], 0, name, name, ctx))
    return items


def _unit_item(unit, tok, start, end, ctx):
    quote = ctx["text"][start:end]
    item = {"field": "unit", "value": unit, "start": start, "end": end, "quote": quote}
    if quote != unit:
        # folio-unit-v1 kept for the historical each/ea spelling.
        if unit == "each" and quote.casefold() in ("each", "ea"):
            item["normalization"] = {"rule": "folio-unit-v1"}
        else:
            item["normalization"] = {"rule": "folio-unit-v2", "unit": unit}
    if tok.ocr:
        item["ocr_confidence"] = tok.conf
    return item


def _robust_value(seg, j, field, kind, ctx):
    """Typed receipt value(s) that fill the rest of the segment, or a raise.

    Quantities return a list (the quantity, plus its unit when printed after
    it), or None when no number is there at all.
    """
    from folio.layout import _skip, parse_qty
    from folio.money import read_mark
    from folio.units import canonical_unit

    j = _skip(seg, j, {":", "-", "="})
    errors = {
        "qty": "RECEIPT_QUANTITY_INVALID",
        "rid": "RECEIPT_IDENTITY_INVALID",
        "unit": "RECEIPT_UNIT_MISMATCH",
        "currency": "RECEIPT_CURRENCY_MISMATCH",
    }
    if j >= len(seg):
        raise ValueError(errors[kind])
    tok = seg[j]
    if not all(t.readable() for t in seg[j:]):
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    name = {"received_quantity": "quantity", "accepted_quantity": "accepted", "rejected_quantity": "rejected"}.get(field, field)
    item = {"field": name, "start": tok.start, "end": tok.end, "quote": tok.text}
    if tok.ocr:
        item["ocr_confidence"] = tok.conf
    if kind == "qty":
        got = parse_qty(seg, j, {**ctx, "role": "receipt"})
        if got is None:
            return None
        if got.next != len(seg):
            raise ValueError(errors[kind])
        item.update(start=got.start, end=got.end, quote=ctx["text"][got.start : got.end])
        item["value"] = got.value
        if got.normalization:
            item["normalization"] = got.normalization
        if Decimal(got.value) <= 0 and name != "rejected":
            raise ValueError(errors[kind])
        out = [item]
        if got.unit:
            unit, start, end, _, utok = got.unit
            out.append(_unit_item(unit, utok, start, end, ctx))
        return out
    if len(seg) != j + 1:
        raise ValueError(errors[kind])
    if kind == "rid":
        if not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}", tok.text):
            raise ValueError(errors[kind])
        item["value"] = tok.text
    elif kind == "unit":
        text = tok.text.rstrip(".,;:") if canonical_unit(tok.text.rstrip(".,;:")) else tok.text
        unit = canonical_unit(text)
        if unit is None:
            raise ValueError(errors[kind])
        return [_unit_item(unit, tok, tok.start, tok.start + len(text), ctx)]
    elif kind == "currency":
        mark = read_mark(tok.text)
        if mark is None or mark.code is None or len(tok.text) != 3:
            raise ValueError(errors[kind])
        item["value"] = mark.code
    return [item]


RECEIPT_ID = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}")


def _cell_id(toks, field, error):
    if len(toks) != 1 or not RECEIPT_ID.fullmatch(toks[0].text) or not toks[0].readable():
        raise ValueError(error)
    t = toks[0]
    return {"field": field, "value": t.text, "start": t.start, "end": t.end, "quote": t.text}


def _aligned_receipts(lines, ctx, identity):
    """Receipt tables under folio-header-row-v1 (heading row over value rows).

    Returns (table_line_indexes, page_items, rows). A table with a receipt
    column lists receipts: every row is its own receipt row (receipt, line,
    quantity, unit), summed later only as unique receipt/line keys. A table
    without one is an item table: one row, or the one row bound to the
    purchase-order line. Every quantity cell must read, else the page fails.
    """
    from folio.layout import _currency_of, _value_fills, aligned_tables, count_fill
    from folio.units import canonical_unit

    used, items, receipt_rows = set(), [], []
    for table in aligned_tables(lines, "receipt", ctx):
        kinds = {c["kind"] for c in table["cells"]}
        if not kinds & {"qty_received", "qty_accepted"}:
            continue
        used |= table["lines"]
        parsed = []
        for row in table["rows"]:
            if row is None:
                raise ValueError("RECEIPT_QUANTITY_INVALID")
            by = {}
            for cell, toks in row:
                by.setdefault(cell["kind"], []).append((cell, toks))
            found = {}
            # The row's unit (unit column, unit heading, or a unit printed in a
            # quantity cell) tells whether "3,600" counts whole items.
            hints = {canonical_unit(t[0].text) for c, t in by.get("unit", []) + by.get("unitcode", []) if len(t) == 1}
            hints |= {c["attr"][1] for c, _ in row if c["attr"] and c["attr"][0] == "unit"}
            for kind in ("qty_received", "qty_accepted", "qty_rejected"):
                for _, toks in by.get(kind, []):
                    first = _value_fills(toks, "qty", {**ctx, "role": "receipt"})
                    if first is not None and first.unit:
                        hints.add(first.unit[0])
            hint = next(iter(hints)) if len(hints) == 1 else None
            for kind, name in (("qty_received", "quantity"), ("qty_accepted", "accepted"), ("qty_rejected", "rejected")):
                hits = by.get(kind, [])
                if not hits:
                    continue
                if len(hits) != 1:
                    raise ValueError("RECEIPT_IDENTITY_REQUIRED")
                cell, toks = hits[0]
                if not all(t.readable() for t in toks):
                    raise ValueError("RECEIPT_QUANTITY_INVALID")
                got = _value_fills(toks, "qty", {**ctx, "role": "receipt"}) or count_fill(toks, ctx, hint)
                if got is None or (Decimal(got.value) <= 0 and name != "rejected"):
                    raise ValueError("RECEIPT_QUANTITY_INVALID")
                item = {"field": name, "value": got.value, "start": got.start, "end": got.end,
                        "quote": ctx["text"][got.start:got.end]}
                if got.normalization:
                    item["normalization"] = got.normalization
                if toks[0].ocr:
                    item["ocr_confidence"] = toks[0].conf
                found[name] = [item]
                if got.unit:
                    unit, start, end, _, utok = got.unit
                    found.setdefault("unit", []).append(_unit_item(unit, utok, start, end, ctx))
                if cell["attr"] and cell["attr"][0] == "unit":
                    t = cell["seg"][-1]
                    word = re.search(r"[^\W_][\w²³]*", t.text)
                    found.setdefault("unit", []).append(
                        _unit_item(cell["attr"][1], t, t.start + word.start(), t.start + word.end(), ctx))
            if "quantity" not in found and "accepted" not in found:
                continue
            for cell, toks in by.get("unit", []) + by.get("unitcode", []):
                t = toks[0]
                unit = canonical_unit(t.text) if len(toks) == 1 else None
                if unit is None or not t.readable():
                    raise ValueError("RECEIPT_UNIT_MISMATCH")
                if cell["kind"] == "unitcode" and unit != cell["attr"]:
                    raise ValueError("RECEIPT_UNIT_MISMATCH")
                found.setdefault("unit", []).append(_unit_item(unit, t, t.start, t.start + len(t.text), ctx))
            for cell, toks in by.get("currency", []) + by.get("currcode", []):
                t = toks[0]
                ok, mark = _currency_of(t.text) if len(toks) == 1 else (False, None)
                if not ok or mark.code is None or len(t.text) != 3 or not t.readable():
                    raise ValueError("RECEIPT_CURRENCY_MISMATCH")
                if cell["kind"] == "currcode" and mark.code != cell["attr"]:
                    raise ValueError("RECEIPT_CURRENCY_MISMATCH")
                found.setdefault("currency", []).append(
                    {"field": "currency", "value": mark.code, "start": t.start, "end": t.end, "quote": t.text})
            for _, toks in by.get("po", []):
                found.setdefault("po_ref", []).extend(_po_ref(toks, 0))
            if by.get("line") and by.get("po_line"):
                raise ValueError("RECEIPT_IDENTITY_REQUIRED")
            for kind, field in (("receipt", "receipt_id"), ("line", "line_id"), ("po_line", "line_id")):
                hits = by.get(kind, [])
                if len(hits) > 1:
                    raise ValueError("RECEIPT_IDENTITY_REQUIRED")
                if hits:
                    found[field] = [_cell_id(hits[0][1], field, "RECEIPT_IDENTITY_INVALID")]
                    if kind == "line":
                        # RV-01 refinement 2026-11-04: a row number headed just
                        # "Line"/"Ln"/"#" in a receipt table is receipt-local
                        # numbering, never a purchase-order line reference.
                        found[field][0]["line_scope"] = "receipt-local"
            ident = {}
            codes, descs = by.get("code", []), by.get("desc", [])
            if len(codes) == 1 and len(codes[0][1]) == 1 and re.search(r"\d", codes[0][1][0].text):
                ident["code"] = codes[0][1][0].text.rstrip(".,;:")
            if len(descs) == 1 and all(t.readable() for t in descs[0][1]):
                ident["desc"] = " ".join(t.text for t in descs[0][1])
            if ident:
                found["ident"] = [{"field": "ident", **ident}]
            # A return, reversal or cancellation row is not a receipt of goods.
            if any(
                RETURN_WORDS.search(" ".join(t.text for t in toks))
                for cell, toks in row if not cell["kind"].startswith("qty")
            ):
                raise ValueError("RECEIPT_QUANTITY_INVALID")
            parsed.append((ident, found))
        if not parsed:
            continue
        # Local row numbers: a row is tied to the purchase-order line only by
        # its own item (code/description) matching that line's item. Rows of
        # one receipt with distinct local numbers are distinct items otherwise
        # (refused by receipt_manifest, never summed).
        from folio.provenance import _same_item

        ref = identity()
        for ident, f in parsed:
            for cite in f.get("line_id", []):
                if cite.get("line_scope") == "receipt-local":
                    cite["item_bound"] = bool(ref and ident and _same_item(ident, ref))
        idents = {tuple(sorted(i.items())) for i, _ in parsed}
        if len(parsed) > 1 and (len(idents) > 1 or "receipt" not in kinds):
            # Several items: only the row(s) of the purchase-order line count.
            from folio.provenance import _same_item

            ref = identity()
            hits = [(i, f) for i, f in parsed if ref and i and _same_item(i, ref)]
            if "receipt" in kinds:
                # A receipt list is summed whole or not at all: a row that cannot
                # be shown to be this line's (blank or other item text) is never
                # silently dropped from the received quantity.
                if len(hits) != len(parsed):
                    raise ValueError("RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED")
            elif len(hits) != 1:
                raise ValueError("RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED")
            parsed = hits
        if any(item_conflicts(ident, ref) for ident, _ in parsed):
            # A receipt row used here names another item than the purchase-order line.
            raise ValueError("RECEIPT_ITEM_MISMATCH")
        if "receipt" in kinds:
            receipt_rows.extend(f for _, f in parsed)
        else:
            for name, values in parsed[0][1].items():
                items.extend(values)
    return used, items, receipt_rows


def _receipt_tables(lines, ctx, identity, skip=()):
    """Receipt item tables (delivery notes, warehouse logs) on one page.

    Returns (table_line_indexes, items). Every quantity row must read; with
    several rows, exactly one must match the packet's purchase-order line
    identity (folio-line-binding-v1), else the page fails closed.
    """
    from folio.layout import _row_identity, _value_fills, header_columns, table_rows
    from folio.units import canonical_unit, heading_unit

    used, items = set(skip), []
    for index, line in enumerate(lines):
        if index in used:
            continue
        cols = header_columns(line)
        if not cols or not any(c["kind"] in ("qty_received", "qty_accepted") for c in cols):
            continue
        rows = table_rows(lines, index, cols, "receipt")
        used |= set(range(index, index + 1 + len(rows)))
        parsed = []
        for row in rows:
            entry = []
            for kind, name in (("qty_received", "received_quantity"), ("qty_accepted", "accepted_quantity")):
                if kind not in row:
                    continue
                toks = row[kind]
                if not toks or not all(t.readable() for t in toks):
                    raise ValueError("RECEIPT_QUANTITY_INVALID")
                got = _value_fills(toks, "qty", {**ctx, "role": "receipt"})
                if got is None or Decimal(got.value) <= 0:
                    raise ValueError("RECEIPT_QUANTITY_INVALID")
                item = {"field": "quantity" if name == "received_quantity" else "accepted",
                        "value": got.value, "start": got.start, "end": got.end,
                        "quote": ctx["text"][got.start:got.end]}
                if got.normalization:
                    item["normalization"] = got.normalization
                if toks[0].ocr:
                    item["ocr_confidence"] = toks[0].conf
                entry.append(item)
                if got.unit:
                    unit, start, end, _, utok = got.unit
                    entry.append(_unit_item(unit, utok, start, end, ctx))
            if not entry:
                continue
            utoks = row.get("unit")
            if utoks:
                if len(utoks) != 1 or not canonical_unit(utoks[0].text) or not utoks[0].readable():
                    raise ValueError("RECEIPT_UNIT_MISMATCH")
                t = utoks[0]
                entry.append(_unit_item(canonical_unit(t.text), t, t.start, t.start + len(t.text), ctx))
            for c in cols:
                if c.get("unit") and c["kind"] in ("qty_received", "qty_accepted"):
                    unit, t = c["unit"]
                    inner = re.search(r"[(\[]([^()\[\]]+)[)\]]", t.text)
                    if heading_unit(t.text):
                        entry.append(_unit_item(unit, t, t.start + inner.start(1), t.start + inner.end(1), ctx))
            rtoks = row.get("receipt")
            if rtoks:
                if len(rtoks) != 1 or not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}", rtoks[0].text) or not rtoks[0].readable():
                    raise ValueError("RECEIPT_IDENTITY_INVALID")
                t = rtoks[0]
                entry.append({"field": "receipt_id", "value": t.text, "start": t.start, "end": t.end, "quote": t.text})
            ctoks = row.get("currency")
            if ctoks:
                from folio.money import read_mark

                if len(ctoks) != 1 or not ctoks[0].readable():
                    raise ValueError("RECEIPT_CURRENCY_MISMATCH")
                t = ctoks[0]
                mark = read_mark(t.text)
                if mark is None or mark.code is None or len(t.text) != 3:
                    raise ValueError("RECEIPT_CURRENCY_MISMATCH")
                entry.append({"field": "currency", "value": mark.code, "start": t.start,
                              "end": t.start + len(t.text), "quote": t.text})
            code, desc = _row_identity(row)
            parsed.append(({"code": code, "desc": desc}, entry))
        if len(parsed) == 1:
            items.extend(parsed[0][1])
        elif parsed:
            from folio.provenance import _same_item

            ref = identity()
            hits = [e for ident, e in parsed if ref and _same_item(ident, ref)]
            if len(hits) != 1:
                raise ValueError("RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED")
            items.extend(hits[0])
    return used, items


def robust_receipt_page(source, page, receipt_count, identity=lambda: None, po_refs=lambda: set(), packet_decimal=None):
    """folio-layout-v2 receipt reading for pages the strict v1 grammar refused.

    Same authority rules as v1: one receipt identity, one received quantity,
    whole identifier fields, a unit, and a line identity when several receipt
    files exist. Differences: fields are read per physical line and column
    segment; free text and date/time stamps are notes; a receipt-id label
    alone on a line is a title when the identity is labelled elsewhere;
    numbers are locale-aware; any unit of the pinned folio-unit-v2 table and
    any pinned ISO 4217 currency is read (agreement across documents is
    checked by the extractor, never assumed); an accepted quantity from an
    inspection is the supported quantity when it is consistent with the
    received and rejected quantities; receipt item tables are bound to the
    purchase-order line; OCR pages are readable when every token on a field
    line clears the confidence floor. Returns rows ([] when the page declares
    no received quantity).
    """
    from folio.layout import NOISE_MAX_CONFIDENCE, is_ocr, page_decimal, page_lines

    lines = page_lines(source, page)
    if lines is None or (is_ocr(source, page) and not lines):
        raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
    ctx = {
        "source": source,
        "page": page,
        "text": page.get("text") or "",
        "decimal": page_decimal(source),
        "role": "receipt",
        "po_refs": po_refs,
        "identity": identity,
        "packet_decimal": packet_decimal,
    }
    declared = _declared_units(lines)
    if len(declared) == 1:
        ctx["count_unit"] = declared.pop()
    found = {}
    aligned_lines, aligned_items, receipt_rows = _aligned_receipts(lines, ctx, identity)
    table_lines, table_items = _receipt_tables(lines, ctx, identity, aligned_lines)
    for item in aligned_items + table_items:
        found.setdefault(item["field"], []).append(item)
    titles = 0
    for index, line in enumerate(lines):
        if index in table_lines:
            continue
        # Isolated low-confidence OCR fragments are scanner noise, unless they
        # share a physical line with a field, which is then unreadable.
        noise = [
            seg
            for seg in line
            if all(t.ocr and (t.conf is None or t.conf < NOISE_MAX_CONFIDENCE) for t in seg)
        ]
        pending, items, pending_seg = None, [], None
        for seg in line:
            if any(seg is n for n in noise):
                continue
            got, pending = _receipt_segment(seg, ctx, pending)
            pending_seg = seg if pending else None
            items.extend(got)
        if pending and pending[0] not in ("ignore", "po_ref", "item_code", "item_desc"):
            if pending[1] == "rid" and len(line) == 1 and pending_seg is line[0]:
                titles += 1  # "DELIVERY NOTE": valid only if the id is labelled elsewhere
            else:
                raise ValueError("RECEIPT_IDENTITY_INVALID")
        if any(item is None for item in items):
            raise ValueError("RECEIPT_QUANTITY_INVALID")
        if items and noise:
            raise ValueError("RECEIPT_IDENTITY_BOUNDARY_UNSUPPORTED")
        for item in items:
            found.setdefault(item["field"], []).append(item)
    # A receipt for another purchase order is no support for this invoice; a
    # purchase-order reference that cannot be keyed is no support either.
    pos = found.pop("po_ref", []) + [i for r in receipt_rows for i in r.pop("po_ref", [])]
    known = po_refs()
    if any(i["key"] is None or (known and i["key"] not in known) for i in pos):
        raise ValueError("RECEIPT_PURCHASE_ORDER_MISMATCH")
    page_ident = {
        key: found.pop("ident_item_" + key)[0]["value"]
        for key in ("code", "desc") if found.get("ident_item_" + key)
    }
    if item_conflicts(page_ident, identity()):
        raise ValueError("RECEIPT_ITEM_MISMATCH")  # a receipt for another item
    if page_ident and "ident" not in found:
        found["ident"] = [{"field": "ident", **page_ident}]
    refs = found.pop("receipt_ref", [])
    if refs and "receipt_id" not in found and not any("receipt_id" in r for r in receipt_rows):
        # No receipt number of its own: the delivery-note number identifies it.
        if len({r["value"] for r in refs}) != 1 or not all(r["valid"] for r in refs):
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        found["receipt_id"] = [{**refs[0], "field": "receipt_id", "identity_from": "delivery-note"}]
    elif refs and found.get("receipt_id") and len({r["value"] for r in refs}) == 1:
        # The receipt names the delivery note it books in: a document identified
        # by that delivery-note number is the same delivery (deduplicated later).
        found["receipt_id"] = [{**found["receipt_id"][0], "delivery_note": refs[0]["value"]}]
    if receipt_rows:
        # A receipt-list table: each row is one receipt row; a page-level
        # quantity beside it would be a second, competing quantity.
        if any(found.get(name) for name in ("quantity", "accepted", "rejected")):
            raise ValueError("RECEIPT_IDENTITY_REQUIRED")
        out = []
        for row in receipt_rows:
            merged = {name: list(values) for name, values in found.items()}
            for name, values in row.items():
                if name in ("receipt_id", "line_id") and merged.get(name):
                    if {v["value"] for v in merged[name]} != {values[0]["value"]}:
                        raise ValueError("RECEIPT_IDENTITY_REQUIRED")
                    merged[name] = list(values)
                else:
                    merged.setdefault(name, []).extend(values)
            out.extend(_robust_row(merged, receipt_count, source, page, ctx, titles))
        if len({r["unit"] for r in out}) > 1:
            raise ValueError("RECEIPT_UNIT_MISMATCH")
        if len({r["currency"] for r in out if r["currency"] is not None}) > 1:
            raise ValueError("RECEIPT_CURRENCY_MISMATCH")
        return out
    return _robust_row(found, receipt_count, source, page, ctx, titles)


def _robust_row(found, receipt_count, source, page, ctx, titles):
    if not found.get("quantity") and not found.get("accepted"):
        if found.get("rejected"):
            raise ValueError("RECEIPT_QUANTITY_INVALID")
        return []
    for name in ("quantity", "accepted", "rejected", "receipt_id", "line_id"):
        if len(found.get(name, [])) > 1:
            raise ValueError("RECEIPT_IDENTITY_REQUIRED")
    if "receipt_id" not in found:
        raise ValueError("RECEIPT_IDENTITY_REQUIRED")
    if titles and "receipt_id" not in found:
        raise ValueError("RECEIPT_IDENTITY_INVALID")
    units = found.get("unit", [])
    if not units or len({u["value"] for u in units}) != 1:
        raise ValueError("RECEIPT_UNIT_MISMATCH")
    if len({c["value"] for c in found.get("currency", [])}) > 1:
        raise ValueError("RECEIPT_CURRENCY_MISMATCH")
    currency_value = found["currency"][0]["value"] if found.get("currency") else None
    currency, currency_status = _receipt_currency(currency_value, ctx["text"])
    if "line_id" not in found and receipt_count > 1:
        # Several receipt files need a line each, unless the receipt's own item
        # (code/description cells) is shown to be the purchase-order line's item.
        from folio.provenance import _same_item

        ref = ctx["identity"]() if "identity" in ctx else None
        idents = found.get("ident") or []
        if not (ref and idents and all(_same_item(i, ref) for i in idents)):
            raise ValueError("RECEIPT_LINE_REQUIRED")
    # Inspection: the accepted quantity is the supported quantity, only when
    # it is consistent with what was received and rejected; a rejection
    # without an accepted quantity is never subtracted by Folio.
    quantity = found["quantity"][0] if found.get("quantity") else None
    if found.get("accepted"):
        accepted = found["accepted"][0]
        a = Decimal(accepted["value"])
        if quantity is not None and a > Decimal(quantity["value"]):
            raise ValueError("RECEIPT_ACCEPTANCE_INCONSISTENT")
        if found.get("rejected"):
            r = Decimal(found["rejected"][0]["value"])
            if quantity is None or a + r != Decimal(quantity["value"]):
                raise ValueError("RECEIPT_ACCEPTANCE_INCONSISTENT")
        quantity = accepted
    elif found.get("rejected") and Decimal(found["rejected"][0]["value"]) > 0:
        raise ValueError("RECEIPT_ACCEPTANCE_INCONSISTENT")
    receipt_id = found["receipt_id"][0]["value"]
    line_id = found["line_id"][0]["value"] if "line_id" in found else "1"
    base = {
        "source_id": source["id"],
        "source_sha256": source["sha256"],
        "source_version": source.get("version", 1),
        "text_artifact_sha256": hashlib.sha256(ctx["text"].encode()).hexdigest(),
        "page": page["number"],
    }

    def cite(item):
        out = {**base, "start": item["start"], "end": item["end"], "quote": item["quote"]}
        for key in ("normalization", "ocr_confidence", "line_scope", "item_bound", "delivery_note", "identity_from"):
            if key in item:
                out[key] = item[key]
        out["reader"] = "folio-layout-v2"
        return out

    evidence = cite(quantity)
    field_evidence = {"quantity": [deepcopy(evidence)]}
    for name in ("receipt_id", "line_id", "unit", "currency"):
        if name in found:
            field_evidence[name] = [cite(found[name][0])]
        elif name == "line_id":
            field_evidence[name] = [
                {
                    "kind": "policy-default",
                    "policy": "receipt-allocation-v1",
                    "value": "1",
                    "reason": "omitted in supported historical labelled layout",
                }
            ]
        elif name == "currency" and currency is not None:
            raise ValueError("RECEIPT_CURRENCY_EVIDENCE_INVALID")
    return [
        {
            "key": receipt_id + ":" + line_id,
            "receipt_id": receipt_id,
            "line_id": line_id,
            "quantity": decimal_text(quantity["value"]),
            "unit": units[0]["value"],
            "currency": currency,
            "currency_status": currency_status,
            "evidence": [evidence],
            "field_evidence": field_evidence,
        }
    ]


LINE_DECLARATION = r"\bLine(?:\s+(?:no\.?|number|#))?:?\s+([A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)[.,;:]?(?!\S)"


# Strict v1 cell rejections that only reflect v1's closed vocabulary (exact
# table header, each/USD only); folio-layout-v2 may read such a source.
# Every other v1 cell rejection (identity, formula, quantity) stands.
CELL_VOCABULARY_ERRORS = {
    "RECEIPT_TABLE_HEADER_INVALID",
    "RECEIPT_UNIT_MISMATCH",
}


PO_LINE_DECLARATION = r"\bP\.?O\.?\s+(?:line|ln)\.?(?:\s+(?:no\.?|number|#))?:?\s+([A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)[.,;:]?(?!\S)"


RETURN_WORDS = re.compile(
    r"\b(?:return(?:ed)?(?: delivery| to vendor| to supplier)?|reversal|reversed|cancel(?:led|lation)?|storno|"
    r"credit(?:ed)?|rücklieferung|retour)\b",
    re.I,
)


def _item_keys(ident):
    from folio.provenance import _desc_key

    return {_desc_key(v) for v in (ident or {}).values() if v and _desc_key(v)}


def item_conflicts(ident, ref):
    """True when a receipt names another item than the purchase-order line.

    Item codes, when both sides print one, must be equal (normalized).
    Otherwise some code/description of one side must contain one of the
    other ("HEX BOLT M8X40" in "Hex bolt M8x40 zinc"); nothing in common is
    a different item.
    """
    from folio.provenance import _desc_key

    ident, ref = ident or {}, ref or {}
    if ident.get("code") and ref.get("code"):
        return _desc_key(ident["code"]) != _desc_key(ref["code"])
    a, b = _item_keys(ident), _item_keys(ref)
    return bool(a and b) and not any(x in y or y in x for x in a for y in b if min(len(x), len(y)) >= 3)


UNPOSTED = re.compile(
    r"\b(?:revers(?:al|ed|es|ing)|cancel(?:s|led|ed|lation)?|void(?:ed)?|storno|draft|not\s+posted|unposted|"
    # "Return to supplier" is a return document; "6 tiles ... returned to
    # supplier" explains rejected goods on a real receipt and is not.
    r"pro[-\s]?forma|return\s+to\s+(?:supplier|vendor|sender)|return\s+delivery|returns?\s+note|"
    r"goods\s+return|rücklieferung|retour|annul(?:ée|ation|led))\b",
    re.I,
)
MOVEMENT_LABEL = re.compile(r"\b(?:movement(?:\s+(?:type|code))?|mvt(?:\s+type)?|transaction\s+type|bwart)\b", re.I)
# SAP MM movement 101 is the plain goods receipt for a purchase order; every
# other movement (102 reversal, 122 return, 161 return PO, ...) is refused.
GOODS_RECEIPT_MOVEMENTS = {"101"}


def _reversal_or_unposted(source):
    """The refusal code when a receipt document is not a plain posted receipt."""
    from folio.layout import page_lines

    text = " ".join(page.get("text") or "" for page in source.get("pages", []))
    if UNPOSTED.search(text):
        return "RECEIPT_REVERSAL_OR_UNPOSTED"
    if not MOVEMENT_LABEL.search(text):
        return None
    # A movement-type label or column: every code printed under or beside it
    # must be a plain goods receipt.
    for page in source.get("pages", []):
        lines = page_lines(source, page) or []
        for li, line in enumerate(lines):
            for si, seg in enumerate(line):
                if not MOVEMENT_LABEL.fullmatch(" ".join(t.text for t in seg).rstrip(":").strip()):
                    continue
                if len(line) >= 3:  # a column heading: the cells below it
                    values = [ln[si] for ln in lines[li + 1:] if len(ln) == len(line)]
                else:  # a label: the value beside it
                    values = line[si + 1 : si + 2]
                codes = {" ".join(t.text for t in s) for s in values}
                if not codes or not codes <= GOODS_RECEIPT_MOVEMENTS:
                    return "RECEIPT_REVERSAL_OR_UNPOSTED"
    return None


COPY_OF = re.compile(
    r"\b(?:reprint|copy|duplicate|replacement|replaces)\s+(?:of\s+)?(?:(?:goods\s+)?receipt\s+(?:no\.?\s+)?)?"
    r"([A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)",
    re.I,
)


def _copy_of_other_receipt(sources, rows, errors):
    """A document that says it is a reprint/copy/replacement of receipt X but
    carries another receipt number supports nothing; a copy of itself is the
    same receipt (merged by receipt/line key)."""
    for source in sources:
        text = " ".join(page.get("text") or "" for page in source.get("pages", []))
        refs = {m.group(1) for m in COPY_OF.finditer(text)}
        if not refs:
            continue
        mine = [k for k, r in rows.items() if any(e.get("source_id") == source["id"] for e in r["evidence"])]
        if refs <= {rows[k]["receipt_id"] for k in mine}:
            continue
        errors.append("RECEIPT_COPY_IDENTITY_MISMATCH")
        for key in mine:
            if all(e.get("source_id") == source["id"] for e in rows[key]["evidence"]):
                del rows[key]


def _same_delivery(rows, errors):
    """A receipt that names the delivery note it books in, and another document
    identified only by that delivery-note number, record one delivery: the
    second row is dropped (equal quantity) or the delivery conflicts."""
    cited = {}
    for key, row in rows.items():
        for c in row.get("field_evidence", {}).get("receipt_id", []):
            if c.get("delivery_note"):
                cited.setdefault(c["delivery_note"], []).append(row)
    for key in list(rows):
        row = rows[key]
        if not any(c.get("identity_from") == "delivery-note" for c in row.get("field_evidence", {}).get("receipt_id", [])):
            continue
        booked = cited.get(row["receipt_id"], [])
        if not booked:
            # A supplier delivery note beside a goods receipt that does not
            # cite it may be the same delivery: never summed, refused.
            if any(
                other is not row and not any(
                    c.get("identity_from") == "delivery-note"
                    for c in other.get("field_evidence", {}).get("receipt_id", [])
                )
                for other in rows.values()
            ):
                errors.append("RECEIPT_IDENTITY_CONFLICT")
            continue
        if any(b["quantity"] != row["quantity"] or b["unit"] != row["unit"] for b in booked):
            errors.append("RECEIPT_IDENTITY_CONFLICT")
        else:
            del rows[key]


def _local_line(row):
    cites = row.get("field_evidence", {}).get("line_id") or []
    return bool(cites) and all(c.get("line_scope") == "receipt-local" for c in cites)


def _receipt_list_table(source):
    """True when a cell source carries a heading-row table of received quantities."""
    from folio.layout import aligned_tables, page_decimal, page_lines

    page = source["pages"][0]
    lines = page_lines(source, page)
    if not lines:
        return False
    ctx = {"source": source, "page": page, "text": page.get("text") or "", "decimal": page_decimal(source), "role": "receipt"}
    return any(
        {c["kind"] for c in t["cells"]} & {"qty_received", "qty_accepted"}
        for t in aligned_tables(lines, "receipt", ctx)
    )


def receipt_manifest(sources):
    rows = {}
    errors = []
    receipt_sources = [s for s in sources if s.get("document_role") == "receipt"]
    memo = []

    def packet():
        if not memo:
            from folio.provenance import packet_context

            memo.append(packet_context(sources))
        return memo[0]

    def identity():
        return packet()["identity"]

    def po_refs():
        return packet()["po_refs"]

    from folio.numbers import infer_decimal

    # The packet's decimal mark, only when its documents prove one and none
    # proves the other (folio-number-v2 separator evidence over every page).
    decimal = infer_decimal([
        m.group() for s in sources for page in s.get("pages", [])
        for m in re.finditer(r"\d[\d.,]*\d|\d", page.get("text") or "")
    ])

    for source in receipt_sources:
        refused = _reversal_or_unposted(source)
        if refused:
            # A cancellation, reversal, return, void, draft or unposted receipt
            # (or a goods movement other than a plain receipt) undoes or never
            # booked a receipt: on every path it supports no quantity.
            errors.append(refused)
            continue
        if source.get("kind") in {"csv", "xlsx"} or "cells" in source:
            try:
                table = table_receipts(source)
                if table is None and _receipt_list_table(source):
                    # Labelled cells above a heading-row receipt table: the
                    # folio-layout-v2 reader owns the whole sheet.
                    raise ValueError("RECEIPT_TABLE_HEADER_INVALID")
                if table is None:
                    labelled = labelled_receipt(source, len(receipt_sources))
                    table = [labelled] if labelled is not None else []
            except ValueError as error:
                if str(error) not in CELL_VOCABULARY_ERRORS:
                    errors.append(str(error))
                    continue
                try:
                    table = robust_receipt_page(source, source["pages"][0], len(receipt_sources), identity, po_refs, decimal)
                except ValueError:
                    table = []
                if not table:
                    errors.append(str(error))
                    continue
            if table is not None:
                for row in table:
                    merge_receipt(rows, errors, row)
                continue
        for page in source["pages"]:
            try:
                row = v1_page(source, page, len(receipt_sources))
                v1_error = None
            except ValueError as error:
                row, v1_error = None, str(error)
            if row is not None:
                merge_receipt(rows, errors, row)
                continue
            # folio-layout-v2 reading only when the strict v1 grammar could
            # not read the page; its own failures never mask a v1 result.
            try:
                robust = robust_receipt_page(source, page, len(receipt_sources), identity, po_refs, decimal)
            except ValueError:
                robust = []
            if robust:
                for item in robust:
                    merge_receipt(rows, errors, item)
            elif v1_error:
                errors.append(v1_error)
    _copy_of_other_receipt(receipt_sources, rows, errors)
    _same_delivery(rows, errors)
    # Receipt-local row numbers (a header-row table's "Line"/"Ln"/"#" column)
    # are not purchase-order line references (RV-01 refinement 2026-11-04).
    line_ids = {row["line_id"] for row in rows.values() if not _local_line(row)}
    if len(line_ids) > 1:
        errors.append("RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED")
    # ... but rows of one receipt under distinct local numbers are distinct
    # items unless each is tied to the purchase-order line by its own item.
    local = {}
    for row in rows.values():
        if _local_line(row):
            local.setdefault(row["receipt_id"], []).append(row)
    for group in local.values():
        if len({r["line_id"] for r in group}) > 1 and not all(
            all(c.get("item_bound") for c in r["field_evidence"]["line_id"]) for r in group
        ):
            errors.append("RECEIPT_MULTIPLE_PO_LINES_UNSUPPORTED")
    if len({row["unit"] for row in rows.values()}) > 1:
        errors.append("RECEIPT_UNIT_MISMATCH")  # quantities in different units never add up
    declared_currencies = {row["currency"] for row in rows.values() if row.get("currency") is not None}
    if len(declared_currencies) > 1:
        errors.append("RECEIPT_CURRENCY_MISMATCH")
    # A line declaration is a "Line" label followed by an identifier carrying
    # a digit; prose such as "tax applied per line" declares nothing.
    # RV-01 refinement 2026-11-04: invoice line numbers are vendor-local. Only
    # the purchase order's declared lines, and lines an invoice explicitly
    # labels as PO lines ("PO line 2"), bind: every purchase-order line a
    # receipt references must be one of them.
    declared_lines = {
        match.group(1)
        for source in sources
        if source.get("document_role") in ("invoice", "purchase_order")
        for page in source["pages"]
        for match in re.finditer(
            LINE_DECLARATION if source["document_role"] == "purchase_order" else PO_LINE_DECLARATION,
            page["text"], re.I,
        )
    }
    if declared_lines and line_ids and not line_ids <= declared_lines:
        errors.append("RECEIPT_PO_LINE_BINDING_MISMATCH")
    # When the invoice is bound to one purchase-order line, a receipt's
    # explicit line references must name exactly that line.
    explicit = {
        row["line_id"] for row in rows.values()
        if not _local_line(row) and not any(
            c.get("kind") == "policy-default" for c in row.get("field_evidence", {}).get("line_id", [])
        )
    }
    if receipt_sources and explicit and packet().get("po_line") and explicit != {packet()["po_line"]}:
        errors.append("RECEIPT_PO_LINE_BINDING_MISMATCH")
    return {
        "policy": "receipt-allocation-v1",
        "rows": list(rows.values()),
        "errors": sorted(set(errors)),
        "total": decimal_text(
            sum((Decimal(r["quantity"]) for r in rows.values()), Decimal(0))
        ),
    }
