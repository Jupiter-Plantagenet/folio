"""Versioned field evidence; extracted, human, computed and lookup remain distinct."""

import hashlib
import re
from decimal import Decimal

FIELDS = {
    "invoice",
    "supplier",
    "invoice_quantity",
    "ordered_quantity",
    "received_quantity",
    "unit_price",
    "tax_rate",
    "invoice_face_amount",
    "currency",
    "unit",
}
NUMERIC = {
    "invoice_quantity",
    "ordered_quantity",
    "received_quantity",
    "unit_price",
    "tax_rate",
    "invoice_face_amount",
}


UNSAFE_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def unsafe_characters(value):
    import unicodedata

    return [ch for ch in value if unicodedata.category(ch) in UNSAFE_CATEGORIES]


def compatibility_folded(value):
    """True when NFKC changes the text beyond canonical (NFC) equivalence."""
    import unicodedata

    return isinstance(value, str) and unicodedata.normalize(
        "NFKC", value
    ) != unicodedata.normalize("NFC", value)


def validate_value(field, value):
    if (
        field not in FIELDS
        or not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
    ):
        raise ValueError("FIELD_VALUE_INVALID")
    if unsafe_characters(value):
        # Zero-width, bidi, control, surrogate, private-use or unassigned code
        # points can hide or reorder what a reviewer sees: never a clean value.
        raise ValueError("FIELD_VALUE_UNSAFE_CHARACTERS")
    if field in NUMERIC:
        # ASCII digits only: "1٢٠" or "１２０" is not silently the number 120.
        if not re.fullmatch(r"(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?", value):
            raise ValueError("DECIMAL_VALUE_INVALID")
        scale = len(value.partition(".")[2])
        # Face amounts are at currency scale; the pinned table's largest minor
        # unit is 3 (KWD, BHD, ...). The currency-specific scale is checked
        # where the currency is known (folio.money.scale_ok).
        if (field == "unit_price" and scale > 4) or (
            field == "invoice_face_amount" and scale > 3
        ):
            raise ValueError("DECIMAL_SCALE_INVALID")
        if field == "tax_rate" and Decimal(value) > 1:
            raise ValueError("TAX_RATE_INVALID")
    if field == "currency":
        from folio.money import MINOR_UNITS

        if value not in MINOR_UNITS:
            raise ValueError("CURRENCY_UNSUPPORTED")
    if field == "unit":
        from folio.units import UNITS

        if value not in UNITS:
            raise ValueError("UNIT_UNSUPPORTED")
    return value


def validate_citation(sources, citation):
    try:
        source = next(s for s in sources if s["id"] == citation["source_id"])
        page = next(p for p in source["pages"] if p["number"] == citation["page"])
        start, end = citation["start"], citation["end"]
        source_version = source.get("version", 1)
        text_hash = hashlib.sha256(page["text"].encode("utf-8")).hexdigest()
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(page["text"])
            or type(source_version) is not int
            or source_version < 1
            or type(citation.get("source_version")) is not int
            or citation["source_version"] != source_version
            or citation.get("text_artifact_sha256") != text_hash
        ):
            raise ValueError("CITATION_INVALID")
        if (
            page["text"][start:end] != citation["quote"]
            or source["sha256"] != citation["source_sha256"]
        ):
            raise ValueError("CITATION_INVALID")
        if "cell" in citation and not any(
            citation["cell"] == {k: v for k, v in cell.items() if k != "value"}
            and str(cell.get("value")) == citation["quote"]
            for cell in source.get("cells", [])
        ):
            raise ValueError("CITATION_INVALID")
        return {
            k: citation[k]
            for k in [
                "source_id",
                "source_sha256",
                "page",
                "start",
                "end",
                "quote",
                "source_version",
                "text_artifact_sha256",
                "cell",
            ]
            if k in citation
        }
    except (StopIteration, KeyError, TypeError) as error:
        raise ValueError("CITATION_INVALID") from error


def extract_packet(sources):
    patterns = {
        # The identifier must end at whitespace/punctuation: "INV-1048<ZWSP>7"
        # or "INV-1048/7" is not the identifier INV-1048.
        "invoice": (
            "invoice",
            r"Invoice\s+(INV-[A-Za-z0-9-]+)(?=$|\s|[.,;:)\]](?:\s|$))",
        ),
        "supplier": ("purchase_order", r"Supplier\s+(.+?)\s+Ordered quantity"),
        "invoice_quantity": ("invoice", r"Quantity\s+(\S+)"),
        "ordered_quantity": ("purchase_order", r"Ordered quantity\s+(\S+)"),
        "received_quantity": ("receipt", r"Received quantity\s+(\S+)"),
        "unit_price": ("invoice", r"Unit price USD\s+(\S+)"),
        # Not "Withholding tax 10%": a tax withheld from payment is no tax rate.
        "tax_rate": ("invoice", r"(?<!withholding )Tax(?: rate)?\s+(\d+)%"),
        "invoice_face_amount": (
            "invoice",
            # \b: "Subtotal USD 4,800.00" is not "Total USD".
            r"\b(?:Face total|Invoice face total|Total) USD\s+(\S+)",
        ),
        "currency": ("invoice", r"\b(USD)\b"),
        "unit": ("receipt", r"Unit\s+(each)"),
    }
    values = {}
    provenance = {}
    conflicts = []
    unreadable = {}
    readings = {}
    for field, (role, pattern) in patterns.items():
        candidates = []
        for source in sources:
            if source.get("document_role") != role:
                continue
            if field in {"received_quantity", "unit"} and (
                source.get("kind") in {"csv", "xlsx"} or "cells" in source
            ):
                # Only validated complete receipt cells can supply these fields.
                # Scalar text-prefix extraction must not reintroduce authority.
                continue
            for page in source["pages"]:
                review = [
                    (word["start"], word["end"]) for word in page.get("words", [])
                    if word.get("cell", {}).get("requires_review")
                ]
                # ASCII classes/case folding: "ıNV" (dotless i), K (Kelvin) or
                # Arabic-Indic digits never satisfy an ASCII label pattern.
                for match in re.finditer(
                    pattern, page["text"], re.IGNORECASE | re.ASCII
                ):
                    literal = match.group(1)
                    if any(end > match.start(1) and start < match.end(1) for start, end in review):
                        continue
                    value = (
                        str(Decimal(literal) / 100) if field == "tax_rate" else literal
                    )
                    try:
                        validate_value(field, value)
                    except ValueError:
                        conflicts.append(field)
                        unreadable.setdefault(field, []).append(
                            (source["id"], page["number"], match.start(1), match.end(1), literal)
                        )
                        continue
                    candidates.append(
                        {
                            "kind": "computed" if field == "tax_rate" else "extracted",
                            "value": value,
                            "version": 1,
                            "derivation": "percent / 100"
                            if field == "tax_rate"
                            else None,
                            "source": {
                                "source_id": source["id"],
                                "source_sha256": source["sha256"],
                                "source_version": source.get("version", 1),
                                "page": page["number"],
                                "text_artifact_sha256": hashlib.sha256(
                                    page["text"].encode("utf-8")
                                ).hexdigest(),
                                "start": match.start(1),
                                "end": match.end(1),
                                "quote": literal,
                            },
                        }
                    )
        readings[field] = candidates
        if candidates and len({v["value"] for v in candidates}) == 1:
            values[field] = candidates[0]["value"]
            provenance[field] = candidates
        elif candidates:
            conflicts.append(field)
    from folio.receipts import receipt_manifest

    manifest = receipt_manifest(sources)
    # Table headers are labels, not scalar values in the flattened projection
    # ("Currency GR-A" is not a declaration). Complete table currency cells
    # were checked before their source-linked rows could enter the manifest.
    table_source_ids = {
        evidence["source_id"]
        for row in manifest["rows"]
        for evidence in row["evidence"]
        if "cell" in evidence
    }
    del table_source_ids  # declarations are read per segment by folio-layout-v2

    if manifest["rows"] or manifest["errors"]:
        conflicts = [f for f in conflicts if f != "received_quantity"]
        if manifest["errors"] and manifest["errors"] != ["RECEIPT_IDENTITY_REQUIRED"]:
            values.pop("received_quantity", None)
            provenance.pop("received_quantity", None)
            conflicts.append("received_quantity")
        elif manifest["rows"] and not manifest["errors"]:
            values["received_quantity"] = manifest["total"]
            unit_sources = [
                item
                for row in manifest["rows"]
                for item in row.get("field_evidence", {}).get("unit", [])
            ]
            row_units = {row["unit"] for row in manifest["rows"]}
            if unit_sources and len(row_units) == 1:
                unit = row_units.pop()
                values["unit"] = unit
                provenance["unit"] = [
                    {
                        "kind": "computed",
                        "value": unit,
                        "version": 1,
                        "derivation": f"all validated receipt rows use {unit}; receipt-allocation-v1",
                        "inputs": [{"value": unit, "sources": unit_sources}],
                        "source": unit_sources[0],
                    }
                ]
            provenance["received_quantity"] = [
                {
                    "kind": "computed",
                    "value": manifest["total"],
                    "version": 1,
                    "derivation": "sum unique receipt/line quantities; receipt-allocation-v1",
                    "inputs": manifest["rows"],
                    "source": manifest["rows"][0]["evidence"][0],
                }
            ]
    # Purchase-order identity must agree across every document that declares
    # one; a receipt for another PO is not support for this invoice.
    declared_po = {
        match.group(1).upper()
        for source in sources
        for page in source["pages"]
        for match in re.finditer(
            r"(?:Purchase order|PO number:?|Customer PO:?)\s+(PO-[A-Za-z0-9-]+)",
            page["text"],
            re.IGNORECASE,
        )
    }
    layout = read_layout(sources)
    currency = packet_currency(layout, readings, manifest)
    layout["currency"] = currency["code"]
    if any(
        DEDUCTION.search(page.get("text") or "")
        for source in sources if source.get("document_role") == "invoice"
        for page in source.get("pages", [])
    ):
        # A deposit, payment, credit or brought-forward balance makes a
        # "due/payable/pay" amount a balance, not the invoice face: only a
        # total-type label ("Invoice total") can still give the face.
        key = ("invoice_face_amount", "invoice")
        layout["fields"][key] = [i for i in layout["fields"].get(key, []) if not i.get("due_label")]
    for field in LAYOUT_FIELDS:
        merge_layout_field(field, layout, values, provenance, conflicts, unreadable, readings)
    receipt_supplier(layout, values, provenance, conflicts)
    declared_po |= {po.upper() for po in layout["purchase_orders"]}
    if (
        len({purchase_order_key(po) for po in declared_po}) > 1
        and "purchase_order" not in conflicts
    ):
        conflicts.append("purchase_order")
    if "purchase_order" in conflicts and "received_quantity" in values:
        # Documents naming different purchase orders: no receipt supports this invoice.
        values.pop("received_quantity", None)
        provenance.pop("received_quantity", None)
        conflicts.append("received_quantity")
    apply_currency(currency, values, provenance, conflicts)
    apply_units(layout, manifest, values, provenance, conflicts)
    if any(
        CREDIT_NOTE.search(page.get("text") or "")
        for source in sources if source.get("document_role") == "invoice"
        for page in source.get("pages", [])
    ):
        # A credit note uploaded as an invoice has no positive payable face.
        values.pop("invoice_face_amount", None)
        provenance.pop("invoice_face_amount", None)
        if "invoice_face_amount" not in conflicts:
            conflicts.append("invoice_face_amount")
    arithmetic = invoice_consistency(layout, values, provenance, conflicts, sources)
    confirm_tax_rate(layout, values, provenance, readings, arithmetic)
    if arithmetic.get("status") != "consistent":
        # Owner decision (round 6), confirm-or-refuse: the four money fields
        # are reported only together, on a single-line invoice whose
        # arithmetic is exactly consistent; otherwise none is (readings that
        # conflicted stay conflicts, the others are missing).
        for name in MONEY_FIELDS:
            values.pop(name, None)
            provenance.pop(name, None)
    issuer = supplier_identity(values.get("supplier"), layout)
    if issuer["conflict"] and "supplier" not in conflicts:
        conflicts.append("supplier")
    conflicts = list(dict.fromkeys(conflicts))
    return {
        "receipt_manifest": manifest,
        "values": values,
        "field_provenance": provenance,
        "missing": sorted(FIELDS - set(values)),
        "conflicts": conflicts,
        "invoice_supplier": issuer["invoice_supplier"],
        "supplier_check": issuer["check"],
        "currency_check": currency["check"],
        "line_binding": layout["line_binding"],
        "invoice_arithmetic": arithmetic,
        "extractor": "folio-labelled-text-patterns-v1 + folio-layout-v2 (folio-header-row-v1, folio-line-binding-v1, folio-currency-v1, folio-unit-v2, folio-invoice-consistency-v1); unreadable or ambiguous fields stay missing/conflicting for human correction",
    }


# ------------------------------------------------------------- layout v2

DEDUCTION = re.compile(
    r"\b(?:deposits?|amount paid|paid to date|already paid|paid on account|less paid|payments? received|"
    r"less payments?|credit notes?|credit memo|credit applied|less credit|credited|advance payment|"
    r"payment in advance|advance received|prepayment|prepaid|balance brought forward|balance b/f|"
    r"balance forward|previous balance|on account|retention|retainage|anzahlung|acompte)\b",
    re.I,
)

TAX_WORDS = re.compile(r"\b(?:tax|vat|gst|hst|pst|qst|mwst|ust|btw|tva|iva|levy)\b", re.I)
MONEY_FIELDS = ("invoice_quantity", "unit_price", "tax_rate", "invoice_face_amount")
CREDIT_NOTE = re.compile(r"\b(?:credit\s+(?:note|memo|invoice)|gutschrift|avoir|nota\s+de\s+cr[ée]dito)\b", re.I)

LAYOUT_FIELDS = {
    "invoice": "invoice",
    "supplier": "purchase_order",
    "invoice_quantity": "invoice",
    "ordered_quantity": "purchase_order",
    "unit_price": "invoice",
    "tax_rate": "invoice",
    "invoice_face_amount": "invoice",
}


def purchase_order_key(value):
    """Comparable PO identity: "PO-4981", "po 4981" and "4981" are one order.

    A leading PO prefix and separators next to letters are dropped; a
    separator between two digit groups is kept ("8801-1" is not "88011").
    """
    text = re.sub(r"^\s*(?:P\.?\s?O\.?|PURCHASE\s+ORDER)(?:\s*(?:NO\.?|NUMBER|#))?(?=[\s:#./-]*\d)[\s:#./-]*",
                  "", str(value).upper())
    tokens = re.findall(r"[A-Z0-9]+", text)
    key = tokens[0] if tokens else ""
    for prev, tok in zip(tokens, tokens[1:]):
        key += ("-" if prev[-1].isdigit() and tok[0].isdigit() else "") + tok
    return key


def read_layout(sources):
    """folio-layout-v2 candidates for every source (data only, never rules)."""
    from folio.layout import (
        buyer_names,
        invoice_issuer,
        normalize_supplier,
        page_lines,
        read_page,
    )

    out = {
        "buyers": set(),
        "fields": {},
        "conflicts": set(),
        "unreadable": {},
        "purchase_orders": set(),
        "purchase_orders_by_role": {"invoice": set(), "purchase_order": set(), "receipt": set()},
        "marks": [],
        "currency_unreadable": [],
        "units": [],
        "items": {"invoice": [], "purchase_order": [], "receipt": []},
        "tables": {"invoice": [], "purchase_order": [], "receipt": []},
        "issuer_explicit": [],
        "issuer_letterhead": [],
    }
    for source in sources:
        role = source.get("document_role")
        if role not in ("invoice", "purchase_order", "receipt"):
            continue
        for index, page in enumerate(source.get("pages", [])):
            lines = page_lines(source, page)
            if not lines:
                continue
            found, table_conflicts, tables = read_page(source, page, role, lines)
            out["conflicts"] |= {(f, role) for f in table_conflicts}
            for table in tables:
                table["source"] = source
                out["tables"][role].append(table)
            for item in found:
                if item.get("unreadable"):
                    if item["field"] == "currency_declared":
                        out["currency_unreadable"].append(item)
                        continue
                    out["unreadable"].setdefault((item["field"], role), []).append(
                        (item["source_id"], item["page"], item["start"], item["end"])
                    )
                    continue
                if item["field"] == "purchase_order":
                    out["purchase_orders"].add(item["value"])
                    out["purchase_orders_by_role"][role].add(item["value"])
                    continue
                if item["field"] in ("item_code", "item_desc"):
                    out["items"][role].append(item)
                    continue
                if item["field"] == "currency_declared":
                    out["marks"].append({"code": item["value"], "role": role, "item": item,
                                         "readable": item.get("readable", True)})
                    continue
                if item["field"] == "unit_declared":
                    if role != "receipt":
                        out["units"].append({"value": item["value"], "role": role, "item": item})
                    continue
                if role != "receipt":
                    for mark in item.get("currency_marks", []):
                        out["marks"].append({**mark, "role": role, "item": item})
                out["fields"].setdefault((item["field"], role), []).append(item)
                if item["field"] == "buyer":
                    out["buyers"].add(normalize_supplier(item["value"]))
            out["buyers"] |= {normalize_supplier(n) for n in buyer_names(lines)}
            if role == "invoice" and index == 0:
                explicit, letterhead = invoice_issuer(source, page, lines)
                # An issuer label in its own cell/column ("Supplier | Acme Ltd")
                # is read by the page reader as label-beside-value.
                seen = {(i["start"], i["end"]) for i in explicit}
                explicit += [
                    i for i in found
                    if i["field"] == "invoice_supplier" and not i.get("unreadable")
                    and (i["start"], i["end"]) not in seen
                ]
                out["issuer_explicit"] += explicit
                out["issuer_letterhead"] += letterhead
    bind_lines(out)
    return out


# ------------------------------------------------------------ line binding

def _code_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _desc_key(value):
    import unicodedata

    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", str(value)).casefold())


def _same_item(a, b):
    """True/False when two item identities can be compared, else None."""
    if a.get("code") and b.get("code"):
        return _code_key(a["code"]) == _code_key(b["code"])
    if a.get("desc") and b.get("desc"):
        return _desc_key(a["desc"]) == _desc_key(b["desc"])
    return None


def _document_identity(layout, role):
    """Item identity of a single-line document (key-value or one-row table)."""
    items = layout["items"][role]
    codes = {_code_key(i["value"]): i["value"] for i in items if i["field"] == "item_code"}
    descs = {_desc_key(i["value"]): i["value"] for i in items if i["field"] == "item_desc"}
    one_row = [t["rows"][0] for t in layout["tables"][role] if len(t["rows"]) == 1]
    for row in one_row:
        if row["code"]:
            codes.setdefault(_code_key(row["code"]), row["code"])
        if row["desc"]:
            descs.setdefault(_desc_key(row["desc"]), row["desc"])
    if len(codes) > 1 or len(descs) > 1:
        return None  # several items named: not a single-line identity
    ident = {}
    if codes:
        ident["code"] = next(iter(codes.values()))
    if descs:
        ident["desc"] = next(iter(descs.values()))
    return ident or None


def bind_lines(layout):
    """folio-line-binding-v1: pick the purchase-order line in multi-row tables.

    A multi-row invoice or purchase-order table contributes the one row whose
    item identity (code; else description) matches the item named by every
    single-line document of the packet, or, when both documents list several
    rows, the one row pair that matches. No match, several matches or no
    comparable identity keep the old outcome: the table's fields conflict.
    The bound identity is also what receipt tables are matched against.
    """
    roles = ("invoice", "purchase_order")
    singles = {r: _document_identity(layout, r) for r in roles}
    multi = {r: [t for t in layout["tables"][r] if len(t["rows"]) > 1] for r in roles}
    binding = {"policy": "folio-line-binding-v1", "status": "single_line", "identity": None}
    bound_rows = {}
    for role in roles:
        if not multi[role]:
            continue
        if len(multi[role]) > 1:
            bound_rows[role] = None
            continue
        rows = multi[role][0]["rows"]
        other = [r for r in roles if r != role]
        refs = [singles[r] for r in other if not multi[r] and singles[r]]
        if refs:
            hits = [row for row in rows if all(_same_item(row, ref) for ref in refs)]
        else:
            others = [t for r in other for t in multi[r]]
            hits = []
            if len(others) == 1:
                for row in rows:
                    pair = [o for o in others[0]["rows"] if _same_item(row, o)]
                    if len(pair) == 1:
                        hits.append(row)
        bound_rows[role] = hits[0] if len(hits) == 1 else None
    for role, row in bound_rows.items():
        table = multi[role][0] if len(multi[role]) == 1 else None
        fields = table["fields"] if table else (["invoice_quantity", "unit_price"] if role == "invoice" else ["ordered_quantity"])
        if row is None:
            binding["status"] = "unbound"
            for field in fields:
                layout["conflicts"].add((field, role))
            continue
        binding["status"] = "bound" if binding["status"] != "unbound" else "unbound"
        binding["identity"] = {k: row[k] for k in ("code", "desc") if row.get(k)}
        if role == "purchase_order" and row.get("line"):
            layout["bound_po_line"] = row["line"]  # the PO line this invoice is for
        for item in row["fields"]:
            item["method"] = "table-row-bound"
            layout["fields"].setdefault((item["field"], role), []).append(item)
        for field in row["bad"]:
            layout["conflicts"].add((field, role))
    if binding["identity"] is None:
        idents = [singles[r] for r in roles if singles[r]]
        if idents:
            merged = {}
            for ident in idents:
                for k, v in ident.items():
                    merged.setdefault(k, v)
            binding["identity"] = merged
    layout["line_binding"] = binding
    return binding


def line_identity(sources):
    """Item identity of this packet's purchase-order line, for receipt tables."""
    return packet_context(sources)["identity"]


def packet_context(sources):
    """Line identity and the PO identities declared by the invoice and PO."""
    layout = read_layout(sources)
    pos = layout["purchase_orders_by_role"]["invoice"] | layout["purchase_orders_by_role"]["purchase_order"]
    return {
        "identity": layout["line_binding"]["identity"],
        "po_refs": {purchase_order_key(p) for p in pos},
        "po_line": layout.get("bound_po_line"),
    }


# ------------------------------------------------------------ currency / units

def packet_currency(layout, readings, manifest):
    """folio-currency-v1: one explicit currency for the whole packet, or none.

    Marks come from money values (codes, symbols), "Currency:" declarations on
    any document, the v1 "USD" literal and receipt currency fields with a
    citation. Two codes, or a family symbol ("$", "¥") incompatible with the
    code, is a currency conflict. A declaration naming no pinned currency is
    unreadable and also a conflict. Families alone decide nothing.
    """
    from folio.money import Mark, resolve

    marks, cites = [], []
    for m in layout["marks"]:
        if not m.get("readable", True):
            continue
        if "code" in m:
            marks.append(Mark(m.get("quote", m["code"]), code=m["code"]))
        else:
            marks.append(Mark(m.get("quote", ""), family=frozenset(m["family"])))
        cites.append(m)
    for entry in readings.get("currency", []):
        marks.append(Mark(entry["value"], code=entry["value"]))
    for row in manifest.get("rows", []):
        for item in row.get("field_evidence", {}).get("currency", []):
            if item.get("kind") != "policy-default":
                marks.append(Mark(item.get("quote", ""), code=row["currency"]))
    code, problem = resolve(marks)
    if layout["currency_unreadable"]:
        code, problem = None, "conflict"
    check = {"policy": "folio-currency-v1 / iso4217-folio-pin-2026-10",
             "status": "conflict" if problem else ("determined" if code else "not_determined"),
             "codes": sorted({m.code for m in marks if m.code}),
             "families": sorted({",".join(sorted(m.family)) for m in marks if not m.code})}
    if code:
        check["code"] = code
    sources = []
    for m in cites:
        if m.get("code") == code:
            item = m["item"]
            if m.get("quote") is not None and "start" in m:
                sources.append({"source_id": item["source_id"], "source_sha256": item["source_sha256"],
                                "source_version": item["source_version"], "page": item["page"],
                                "text_artifact_sha256": item["text_artifact_sha256"],
                                "start": m["start"], "end": m["end"], "quote": m["quote"]})
            else:
                sources.append({"source_id": item["source_id"], "source_sha256": item["source_sha256"],
                                "source_version": item["source_version"], "page": item["page"],
                                "text_artifact_sha256": item["text_artifact_sha256"],
                                "start": item["start"], "end": item["end"], "quote": item["quote"]})
    return {"code": code, "problem": problem, "check": check, "sources": sources, "v1": readings.get("currency", [])}


def apply_currency(currency, values, provenance, conflicts):
    code = currency["code"]
    if currency["problem"]:
        values.pop("currency", None)
        provenance.pop("currency", None)
        if "currency" not in conflicts:
            conflicts.append("currency")
        return
    if code is None:
        return
    values["currency"] = code
    entries = list(currency["v1"]) + [
        {"kind": "extracted", "value": code, "version": 1, "derivation": None,
         "reader": "folio-currency-v1", "source": src}
        for src in currency["sources"]
        if not any(e["source"]["start"] == src["start"] and e["source"]["source_id"] == src["source_id"] for e in currency["v1"])
    ]
    for entry in entries:
        if entry["source"]["quote"] != code:
            entry["normalization"] = {"rule": "folio-currency-v1", "code": code}
    if entries:
        provenance["currency"] = entries
    if "currency" in conflicts:
        conflicts.remove("currency")
    from folio.money import scale_ok

    face = values.get("invoice_face_amount")
    if face is not None and not scale_ok(face, code):
        # An amount at currency scale cannot carry more decimals than the
        # currency's minor unit (JPY 0, USD 2, KWD 3).
        values.pop("invoice_face_amount", None)
        provenance.pop("invoice_face_amount", None)
        conflicts.append("invoice_face_amount")


def apply_units(layout, manifest, values, provenance, conflicts):
    """folio-unit-v2: invoice, purchase-order and receipt units must agree.

    Units printed with the used quantities (or declared by a unit label) on
    the invoice and purchase order are compared with the units of the
    validated receipt rows. Different canonical units are a unit conflict
    (no conversion factor is ever applied); one unit is the packet unit.
    """
    seen = []
    for field, role in (("invoice_quantity", "invoice"), ("ordered_quantity", "purchase_order")):
        for item in layout["fields"].get((field, role), []):
            unit = item.get("unit")
            if unit and unit.get("readable", True):
                seen.append((unit["value"], role, item, unit))
    for u in layout["units"]:
        seen.append((u["value"], u["role"], u["item"], None))
    if "unit" in values:
        seen.append((values["unit"], "receipt", None, None))
    elif manifest.get("rows") and not manifest.get("errors"):
        for row in manifest["rows"]:
            seen.append((row["unit"], "receipt", None, None))
    units = {u[0] for u in seen}
    if len(units) > 1:
        values.pop("unit", None)
        provenance.pop("unit", None)
        if "unit" not in conflicts:
            conflicts.append("unit")
        received_units = {u[0] for u in seen if u[1] == "receipt"}
        ordered_units = {u[0] for u in seen if u[1] == "purchase_order"} or {u[0] for u in seen if u[1] == "invoice"}
        if received_units and (received_units != ordered_units) and "received_quantity" in values:
            # Received in another unit than ordered/invoiced (120 EA vs 10 dozen):
            # no factor is applied, so the received quantity supports nothing.
            values.pop("received_quantity", None)
            provenance.pop("received_quantity", None)
            if "received_quantity" not in conflicts:
                conflicts.append("received_quantity")
        return
    if not units or "unit" in values or "unit" in conflicts:
        return
    unit = units.pop()
    entries = []
    for _, _, item, info in seen:
        if item is None:
            continue
        src = info or {"start": item["start"], "end": item["end"], "quote": item["quote"]}
        entry = {"kind": "extracted", "value": unit, "version": 1, "derivation": None,
                 "reader": "folio-unit-v2",
                 "source": {"source_id": item["source_id"], "source_sha256": item["source_sha256"],
                            "source_version": item["source_version"], "page": item["page"],
                            "text_artifact_sha256": item["text_artifact_sha256"],
                            "start": src["start"], "end": src["end"], "quote": src["quote"]}}
        if src["quote"] != unit:
            entry["normalization"] = {"rule": "folio-unit-v2", "unit": unit}
        entries.append(entry)
    if entries:
        values["unit"] = unit
        provenance["unit"] = entries


def confirm_tax_rate(layout, values, provenance, readings, arithmetic):
    """A rate read only under a bare "Tax"/"VAT" heading must reproduce the face.

    Such a heading may head a tax amount; the number is a rate only when
    round(qty x price) + round(net x rate) equals the printed invoice total
    (folio-invoice-consistency-v1 arithmetic). Otherwise the rate is left
    missing; the arithmetic never chooses or repairs a value.
    """
    # The same holds for a quantity read under "Quantity per" (it may head a
    # pack size). Runs after invoice_consistency: an inconsistent packet has
    # already refused every reading involved, so dropping an unconfirmed
    # reading here can never switch the guard off.
    consistent = arithmetic.get("status") == "consistent"
    for field in ("invoice_quantity", "tax_rate"):
        if field not in values:
            continue
        items = [i for i in layout["fields"].get((field, "invoice"), []) if i["value"] == values[field]]
        if readings.get(field) or not items or not all(i.get("confirm") for i in items):
            continue
        if consistent:
            for entry in provenance.get(field, []):
                entry["derivation"] = ((entry.get("derivation") or "") + "; confirmed by folio-invoice-consistency-v1").lstrip("; ")
            continue
        values.pop(field, None)
        provenance.pop(field, None)


def invoice_consistency(layout, values, provenance, conflicts, sources):
    """folio-invoice-consistency-v1: refuse face/quantity readings that cannot be right.

    For a single-line invoice with quantity, unit price and face amount read
    (and a tax rate, or no tax line at all), gross = round(qty x price) +
    round(net x rate) at the currency's minor unit (arithmetic policy v1),
    plus printed freight/carriage charges (untaxed or taxed) as the only other
    exact conventions. A printed tax amount must equal round(net x rate).
    When nothing reproduces the face, all four readings become conflicts:
    nothing tells which one is wrong. Arithmetic only ever refuses a reading;
    it never chooses or repairs one.
    """
    from decimal import ROUND_HALF_UP, Decimal

    from folio.money import minor_units

    names = ["invoice_quantity", "unit_price", "tax_rate", "invoice_face_amount"]
    result = {"policy": "folio-invoice-consistency-v1", "status": "not_checked"}
    if any(n not in values for n in ("invoice_quantity", "unit_price", "invoice_face_amount")):
        return result
    if any(len(t["rows"]) > 1 for t in layout["tables"]["invoice"]):
        result["status"] = "not_checked_multi_line"
        return result
    if "tax_rate" in values:
        rate = Decimal(values["tax_rate"])
    elif "tax_rate" in conflicts or any(
        TAX_WORDS.search(page.get("text") or "")
        for s in sources if s.get("document_role") == "invoice" for page in s.get("pages", [])
    ):
        rate = None  # tax is printed but its rate is not known
    else:
        rate = Decimal(0)  # no tax line anywhere on the invoice
    face = Decimal(values["invoice_face_amount"])
    places = minor_units(values.get("currency") or "")
    if places is None:
        places = max(2, -face.as_tuple().exponent)
    q = Decimal(1).scaleb(-places)

    def amounts(field):
        seen = {}
        for item in layout["fields"].get((field, "invoice"), []):
            seen.setdefault((item["source_id"], item["page"], item["start"]), Decimal(item["value"]))
        return list(seen.values())

    printed_tax = set(amounts("tax_amount"))
    charges = sum(amounts("charge_amount"), Decimal(0))
    net = (Decimal(values["invoice_quantity"]) * Decimal(values["unit_price"])).quantize(q, ROUND_HALF_UP)
    if rate is None:
        # Tax is printed but its rate is unknown: the face goes out with
        # quantity and price only when one printed tax amount makes it exact.
        if len(printed_tax) == 1 and face in (net + t + c for t in printed_tax for c in {Decimal(0), charges}):
            result.update(net=str(net), tax=str(next(iter(printed_tax))), face=str(face),
                          status="consistent_tax_amount")
            return result
        result["status"] = "face_unconfirmed_tax_unknown"
        values.pop("invoice_face_amount", None)
        provenance.pop("invoice_face_amount", None)
        if "invoice_face_amount" not in conflicts:
            conflicts.append("invoice_face_amount")
        return result
    # Exact conventions only, never a tolerance: goods + tax, with printed
    # charges (freight, carriage, ...) added untaxed or taxed.
    conventions = [(net, (net * rate).quantize(q, ROUND_HALF_UP), Decimal(0))]
    if charges:
        conventions.append((net, (net * rate).quantize(q, ROUND_HALF_UP), charges))
        conventions.append((net, ((net + charges) * rate).quantize(q, ROUND_HALF_UP), charges))
    tax = conventions[0][1]
    result.update(net=str(net), tax=str(tax), gross=str(net + tax), face=str(face))
    for c_net, c_tax, c_charges in conventions:
        if face == c_net + c_tax + c_charges and printed_tax <= {c_tax}:
            result.update(tax=str(c_tax), gross=str(face), status="consistent")
            return result
    # Quantity x price + tax does not reproduce the total (or a printed tax
    # amount disagrees): every reading involved is refused, never chosen.
    refused = [n for n in names if n in values]
    result["status"] = "inconsistent_refused"
    result["refused"] = refused
    for name in refused:
        values.pop(name, None)
        provenance.pop(name, None)
        if name not in conflicts:
            conflicts.append(name)
    return result


def _layout_entry(item):
    entry = {
        "kind": "computed" if item.get("derivation") else "extracted",
        "value": item["value"],
        "version": 1,
        "derivation": item.get("derivation"),
        "reader": "folio-layout-v2:" + item["method"],
        "source": {
            "source_id": item["source_id"],
            "source_sha256": item["source_sha256"],
            "source_version": item["source_version"],
            "page": item["page"],
            "text_artifact_sha256": item["text_artifact_sha256"],
            "start": item["start"],
            "end": item["end"],
            "quote": item["quote"],
        },
    }
    for key in ("normalization", "ocr_confidence"):
        if key in item:
            entry[key] = item[key]
    return entry


def _currency_fits(item, code):
    marks = item.get("currency_marks") or []
    if not marks:
        return True
    if code is None:
        return False  # a priced value whose currency is undetermined
    return all(m.get("code") == code if "code" in m else code in m.get("family", ()) for m in marks)


def _valid(field, value):
    try:
        validate_value(field, value)
    except ValueError:
        return False
    return True


def merge_layout_field(field, layout, values, provenance, conflicts, unreadable, readings):
    """Combine the v1 and v2 readings of one field without guessing.

    * v2 adds nothing: the v1 result is left exactly as it was.
    * A v1 literal that failed validation stays a conflict unless v2 read the
      same cited span (e.g. "1,200" or OCR "1O0") into a valid value.
    * A v1 multi-word value whose span strictly contains a v2 value (a label
      pattern that ran past a column boundary) yields to the bounded reading.
    * Two distinct readable values are a conflict; so is a multi-row table
      (multi-line documents are outside the single-line policy).
    """
    role = LAYOUT_FIELDS[field]
    v2 = [
        item
        for item in layout["fields"].get((field, role), [])
        if _valid(field, item["value"])
        # An amount printed in another currency than the packet currency is
        # never a field value (no cross-currency arithmetic).
        and _currency_fits(item, layout.get("currency"))
    ]
    table_conflict = (field, role) in layout["conflicts"]
    vetoes = layout["unreadable"].get((field, role), [])
    if not v2 and not table_conflict and not vetoes:
        return

    def where(entry):
        s = entry["source"] if "source" in entry else entry
        return (s["source_id"], s["page"], s["start"], s["end"])

    def overlap(a, b):
        return a[0] == b[0] and a[1] == b[1] and a[2] < b[3] and b[2] < a[3]

    v2_at = [where(i) for i in v2]
    v1 = [
        e
        # every valid v1 reading, including those of an already conflicting field
        for e in readings.get(field, [])
        if not any(
            overlap(where(e), w)
            and where(e)[2] <= w[2]
            and w[3] <= where(e)[3]
            and where(e)[3] - where(e)[2] > w[3] - w[2]
            for w in v2_at
        )
    ]
    # A v1 literal without any digit is a label running into the next word
    # (stacked or tabular layouts), not an unreadable printed value: it yields
    # to a valid bounded reading. Literals with digits need a same-span repair.
    bad = [
        u[:4]
        for u in unreadable.get(field, [])
        if not any(overlap(u, w) for w in v2_at)
        and (field not in NUMERIC or re.search(r"\d", u[4]) or not v2)
    ]
    # A printed value that v2 saw but could not read safely (ambiguous
    # separator, split digit group, low OCR confidence) vetoes any other
    # reading of the same token.
    vetoed = [
        u for u in vetoes
        if not any(overlap(u, w) for w in v2_at)
        and any(overlap(u, where(e)) for e in v1)
    ]
    if vetoed:
        v1 = [e for e in v1 if not any(overlap(where(e), u) for u in vetoed)]
        bad += vetoed
    distinct = {e["value"] for e in v1} | {i["value"] for i in v2}
    while field in conflicts:
        conflicts.remove(field)
    values.pop(field, None)
    provenance.pop(field, None)
    if bad or table_conflict or len(distinct) != 1:
        conflicts.append(field)
        return
    values[field] = distinct.pop()
    provenance[field] = v1 or [_layout_entry(i) for i in v2]


def receipt_supplier(layout, values, provenance, conflicts):
    """A goods receipt's labelled supplier ("Supplier: Acme Ltd").

    It must agree with the purchase-order supplier when both are printed
    (else a supplier conflict); without a purchase-order supplier it is the
    packet supplier when every receipt label names the same company. The
    invoice is never a source here: its issuer is what the supplier checks.
    """
    from folio.layout import normalize_supplier

    named = [i for i in layout["fields"].get(("supplier", "receipt"), []) if _valid("supplier", i["value"])]
    keys = {normalize_supplier(i["value"]) for i in named}
    if not named or "supplier" in conflicts:
        return
    if "supplier" in values:
        if keys != {normalize_supplier(values["supplier"])}:
            values.pop("supplier", None)
            provenance.pop("supplier", None)
            conflicts.append("supplier")
        return
    if len(keys) == 1:
        values["supplier"] = named[0]["value"]
        provenance["supplier"] = [_layout_entry(i) for i in named]


LEGAL_NOTE = "case, punctuation and legal-form suffixes ignored (folio-supplier-identity-v1)"


def supplier_identity(po_supplier, layout):
    """Deterministic invoice-issuer vs purchase-order supplier comparison.

    Each invoice document names its issuer by explicit issuer labels
    ("Supplier:", "From:"), which decide on their own for that document, or
    else by its letterhead region: a conflict needs a name-like letterhead and
    no letterhead candidate naming the purchase-order supplier. Every
    invoice document must agree (an attachment cannot vouch for an invoice
    printed by another company). When nothing identifies the issuer the
    result is "not_determined": never a pass and never a conflict.
    """
    from folio.layout import normalize_supplier

    explicit = layout["issuer_explicit"]
    letterhead = layout["issuer_letterhead"]
    target = normalize_supplier(po_supplier) if po_supplier else None
    # NFKC folds math-bold, fullwidth or ligature look-alikes into the plain
    # name; such a fold may show a difference but can never prove a match.
    folded = compatibility_folded(po_supplier) or any(
        compatibility_folded(i["value"]) for i in explicit + letterhead
    )
    result = {
        "invoice_supplier": None,
        "conflict": False,
        "check": {"status": "not_determined", "rule": LEGAL_NOTE},
    }
    docs = list(dict.fromkeys(i["source_id"] for i in explicit + letterhead))
    if not docs:
        return result
    issuers, states = [], []
    for doc in docs:
        labelled = [i for i in explicit if i["source_id"] == doc]
        heads = [i for i in letterhead if i["source_id"] == doc]
        if labelled:
            names = {normalize_supplier(i["value"]) for i in labelled}
            # Two different labelled issuers name no single issuer.
            issuers.append(labelled[0] if len(names) == 1 else None)
            if len(names) > 1 or (target is not None and target not in names):
                states.append(("conflict", "explicit-label", None))
            else:
                states.append(("match" if target is not None else "not_determined", "explicit-label", None))
            continue
        # A name printed under/beside a bill-to, ship-to, sold-to or customer
        # heading on any document is the buyer, never the issuer.
        heads = [i for i in heads if normalize_supplier(i["value"]) not in layout.get("buyers", ())]
        if not heads:
            issuers.append(None)
            states.append(("not_determined", "letterhead-buyer-only", None))
            continue
        hit = next((i for i in heads if target is not None and normalize_supplier(i["value"]) == target), None)
        if hit:
            issuers.append(hit)
            states.append(("match", "letterhead", None))
            continue
        if target is not None and any(_short_form(normalize_supplier(i["value"]), target) for i in heads):
            # A trade-name short form ("Bellhaven" for "Bellhaven Textiles SARL")
            # neither proves nor refutes the supplier: not determined.
            issuers.append(None)
            states.append(("not_determined", "letterhead-short-form", None))
            continue
        issuers.append(_letterhead_issuer(heads))
        if target is not None and any(
            i.get("ocr") and _within_ocr_error(normalize_supplier(i["value"]), target) for i in heads
        ):
            # An OCR'd letterhead within OCR error distance of the PO supplier
            # cannot establish a different company: neither match nor conflict.
            states.append(("ocr-near", "letterhead-ocr-near-match", None))
        elif target is not None:
            states.append(("conflict", "letterhead", [i["value"] for i in heads]))
        else:
            states.append(("not_determined", "letterhead", None))
    if all(issuers) and len({normalize_supplier(i["value"]) for i in issuers}) == 1:
        result["invoice_supplier"] = _issuer_value(issuers[0])
    conflict = next((s for s in states if s[0] == "conflict"), None)
    if conflict:
        result["conflict"] = True
        result["check"].update(status="conflict", method=conflict[1])
        if conflict[2] is not None:
            result["check"]["letterhead"] = conflict[2]
        return result
    near = next((s for s in states if s[0] == "ocr-near"), None)
    if near:
        result["check"].update(status="not_determined", method=near[1])
        result["invoice_supplier"] = None  # not established; never compared later
        return result
    result["check"]["method"] = states[0][1]
    if all(s[0] == "match" for s in states):
        result["check"]["status"] = "match"
        if folded:
            result["check"].update(status="not_determined", unicode="compatibility-fold")
    return result


LEGAL_FORMS = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "plc", "pty", "pte", "llp",
    "lp", "oy", "oyj", "ab", "kk", "kft", "kg",
}


def _short_form(a, b):
    """One normalized name is the leading words of the other ("bellhaven" / "bellhaven textiles")."""
    wa, wb = a.split(), b.split()
    short, long = (wa, wb) if len(wa) < len(wb) else (wb, wa)
    return bool(short) and len(short) < len(long) and long[: len(short)] == short


def _letterhead_issuer(candidates):
    """The one issuer name of a document's letterhead, else None.

    Department and document-title lines beside a company letterhead are not
    issuers: among several candidates, the one name carrying a legal form
    ("Acme Tools Ltd") is.
    """
    from folio.layout import normalize_supplier

    if any(i.get("short_form") for i in candidates):
        return None  # a short/logo form names no legal entity on its own
    if len(candidates) == 1:
        return candidates[0]
    legal = [i for i in candidates if _legal_form(i["value"])]
    return legal[0] if len({normalize_supplier(i["value"]) for i in legal}) == 1 else None


def _legal_form(name):
    words = re.sub(r"[^\w\s]", " ", str(name).casefold()).split()
    return len(words) >= 2 and words[-1] in LEGAL_FORMS


def _within_ocr_error(a, b):
    """Edit distance <= 2 and <= 15% of the longer name (glyph-level OCR noise)."""
    if a == b:
        return True
    limit = min(2, max(1, int(0.15 * max(len(a), len(b)))))
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1] <= limit


def _issuer_value(item):
    return {
        "value": item["value"],
        "method": item["method"],
        "source": {
            "source_id": item["source_id"],
            "source_sha256": item["source_sha256"],
            "source_version": item["source_version"],
            "page": item["page"],
            "text_artifact_sha256": item["text_artifact_sha256"],
            "start": item["start"],
            "end": item["end"],
            "quote": item["quote"],
        },
    }
