"""folio-layout-v2: line/segment/column reading of untrusted document text.

Rules are general, never per template:

* Physical lines come from word geometry (PDF text boxes or OCR word boxes),
  spreadsheet rows, or explicit newlines. Words split into segments at
  column-sized horizontal gaps, so two-column and boxed layouts keep each
  label beside its own value.
* A field is read only as LABEL [separator] VALUE, where LABEL is one of a
  closed synonym set and VALUE has the field's type (quantity, money,
  percent, identifier, name). A label alone in its segment pairs with the value
  in the next segment of the same line, or directly below it.
* Table headers (two or more column-header synonyms, no numbers) assign the
  words of the rows below to columns by x-position.
* Numbers are read with folio.numbers (locale-aware, OCR digit repair only for
  OCR tokens, ambiguous tokens rejected). OCR value tokens below a confidence
  floor are unreadable: confidence can only refuse a reading, never grant one.
* Any other text is data that is ignored. Nothing here can change a rule.
"""

import hashlib
import re
from functools import lru_cache
from statistics import median

from folio.numbers import NumberError, infer_decimal, parse_number, read_number

READER = "folio-layout-v2"
OCR_MIN_CONFIDENCE = 80.0
LETTERHEAD_MIN_CONFIDENCE = 85.0
NOISE_MAX_CONFIDENCE = 60.0
ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}")


class Tok:
    __slots__ = ("text", "start", "end", "x0", "x1", "top", "bottom", "conf", "ocr", "review", "fmt")

    def __init__(self, text, start, end, x0, x1, top, bottom, conf=None, ocr=False, review=False, fmt=None):
        self.text, self.start, self.end = text, start, end
        self.x0, self.x1, self.top, self.bottom = x0, x1, top, bottom
        self.conf, self.ocr, self.review = conf, ocr, review
        self.fmt = fmt  # a spreadsheet cell's number format ("0%", "0.00"), else None

    def readable(self, floor=OCR_MIN_CONFIDENCE):
        if self.review:
            return False
        return not self.ocr or (self.conf is not None and self.conf >= floor)


@lru_cache(maxsize=65536)
def norm(text):
    raw = text.casefold().strip()
    t = raw.strip("()[]{}\"'")
    t = t.rstrip(":;,")
    if len(t) > 1:
        t = t.rstrip(".")
    if len(t) > 1 and t[-1] in "#№" and t[:-1].isalpha():
        t = t[:-1].rstrip(".")  # "GRN#", "PO#", "No.#" read as the word itself
    return t or raw  # a bare separator (":") stays itself


LEADER = re.compile(r"[.·…_]{2,}|[-=]{3,}")


def _split_token(tok):
    """Split one printed word at a glued label boundary or a dot leader.

    "Qty:150" -> "Qty:" "150"; "GRN#:50418" -> "GRN#:" "50418";
    "RCVD....236" -> "RCVD" "236" (the leader itself is dropped). Only a
    leading alphabetic label ending in ':' or '#' is split off, so times
    ("14:03"), URLs and identifiers ("PO-4/7") stay whole.
    """
    parts = []
    text, base = tok.text, tok.start
    pos = 0
    for m in LEADER.finditer(text):
        if m.start() > pos:
            parts.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        parts.append((pos, len(text)))
    out = []
    for a, b in parts:
        piece = text[a:b]
        m = re.fullmatch(r"([^\W\d_][^\W\d_.]*\.?[#№]?:|[^\W\d_]+[#№]:?)(\S+)", piece)
        cuts = [(a, a + m.end(1)), (a + m.end(1), b)] if m else [(a, b)]
        for s, e in cuts:
            out.append((s, e))
    if len(out) == 1 and out[0] == (0, len(text)):
        return [tok]
    width = max(1, len(text))
    toks = []
    for s, e in out:
        x0 = tok.x0 + (tok.x1 - tok.x0) * s / width
        x1 = tok.x0 + (tok.x1 - tok.x0) * e / width
        toks.append(Tok(text[s:e], base + s, base + e, x0, x1, tok.top, tok.bottom, tok.conf, tok.ocr, tok.review))
    return toks


def _pipe_pieces(tok):
    """ "11|EA|13.25" -> "11" BAR "EA" BAR "13.25": a printed '|' is a column rule."""
    if "|" not in tok.text:
        return [tok]
    out, text = [], tok.text
    width = max(1, len(text))
    for m in re.finditer(r"[^|]+|\|", text):
        s, e = m.start(), m.end()
        x0 = tok.x0 + (tok.x1 - tok.x0) * s / width
        x1 = tok.x0 + (tok.x1 - tok.x0) * e / width
        out.append(Tok(m.group(), tok.start + s, tok.start + e, x0, x1, tok.top, tok.bottom, tok.conf, tok.ocr, tok.review))
    return out


def refine(lines, pipes=True):
    """Apply _split_token to every word; pure leader words are not content.

    On printed text (not spreadsheet cells) a '|' rule separates columns:
    the segment is split there and the rule itself is not content.
    """
    if not lines:
        return lines
    out = []
    for line in lines:
        segs = []
        for seg in line:
            toks = [t for tok in seg for t in _split_token(tok) if not LEADER.fullmatch(t.text)]
            if pipes:
                part = []
                for t in (p for tok in toks for p in _pipe_pieces(tok)):
                    if t.text == "|":
                        if part:
                            segs.append(part)
                        part = []
                    else:
                        part.append(t)
                toks = part
            if toks:
                segs.append(toks)
        if segs:
            out.append(segs)
    return out


def is_ocr(source, page):
    return source.get("kind") == "ocr-or-mixed-pdf" or bool(page.get("ocr_engine"))


# ---------------------------------------------------------------- lines

def _geometry_lines(words, text, ocr):
    toks = []
    for w in words:
        boxes = w.get("boxes") or []
        start, end, quote = w.get("start"), w.get("end"), w.get("quote")
        if (
            not boxes
            or type(start) is not int
            or type(end) is not int
            or text[start:end] != quote
        ):
            return None
        conf = w.get("confidence") if ocr else None
        tok = Tok(
            quote, start, end,
            min(b[0] for b in boxes), max(b[2] for b in boxes),
            min(b[1] for b in boxes), max(b[3] for b in boxes),
            conf=float(conf) if isinstance(conf, (int, float)) else None,
            ocr=ocr,
        )
        # Scanner specks: low-confidence tokens with no letter or digit.
        if ocr and (tok.conf is None or tok.conf < NOISE_MAX_CONFIDENCE) and not any(
            ch.isalnum() for ch in quote
        ):
            continue
        toks.append(tok)
    if not toks:
        return []
    lines = []
    for tok in sorted(toks, key=lambda t: (t.x0, t.top)):
        best, best_overlap = None, 0.0
        for line in lines:
            last = line[-1]
            if last.x1 > tok.x0 + 1.0:
                continue
            overlap = min(last.bottom, tok.bottom) - max(last.top, tok.top)
            height = max(1e-6, min(last.bottom - last.top, tok.bottom - tok.top))
            ratio = overlap / height
            if ratio >= 0.5 and ratio > best_overlap:
                best, best_overlap = line, ratio
        if best is None:
            lines.append([tok])
        else:
            best.append(tok)
    lines.sort(key=lambda line: median(t.top for t in line))
    gap = max(12.0, 1.5 * median(t.bottom - t.top for t in toks))
    out = []
    for line in lines:
        segments = [[line[0]]]
        for prev, tok in zip(line, line[1:]):
            if tok.x0 - prev.x1 > gap:
                segments.append([tok])
            else:
                segments[-1].append(tok)
        out.append(segments)
    return out


def _cell_lines(page):
    text = page.get("text", "")
    rows = {}
    for w in page.get("words", []):
        cell = w.get("cell") or {}
        if "row" not in cell or "column" not in cell:
            return None
        if text[w["start"] : w["end"]] != w["quote"]:
            return None
        rows.setdefault((str(cell.get("sheet")), cell["row"]), []).append((cell, w))
    out = []
    for (_, row), items in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        segments = []
        for cell, w in sorted(items, key=lambda cw: cw[0]["column"]):
            col = cell["column"]
            seg = [
                Tok(
                    m.group(), w["start"] + m.start(), w["start"] + m.end(),
                    col * 100.0, col * 100.0 + 90.0, row * 20.0, row * 20.0 + 10.0,
                    review=bool(cell.get("requires_review")),
                    fmt=cell.get("number_format") if isinstance(cell.get("number_format"), str) else None,
                )
                for m in re.finditer(r"\S+", w["quote"])
            ]
            if seg:
                segments.append(seg)
        if segments:
            out.append(segments)
    return out


def _text_lines(text):
    out = []
    for n, line in enumerate(re.finditer(r"[^\r\n]+", text)):
        segments = []
        for run in re.finditer(r"\S+(?: \S+)*", line.group()):
            base = line.start() + run.start()
            seg = [
                Tok(
                    m.group(), base + m.start(), base + m.end(),
                    (run.start() + m.start()) * 6.0, (run.start() + m.end()) * 6.0,
                    n * 20.0, n * 20.0 + 10.0,
                )
                for m in re.finditer(r"\S+", run.group())
            ]
            segments.append(seg)
        if segments:
            out.append(segments)
    return out


def page_lines(source, page):
    """Lines -> segments -> tokens, or None when no supported boundary exists."""
    words = page.get("words") or []
    text = page.get("text") or ""
    if "cells" in source or source.get("kind") in ("csv", "xlsx"):
        return refine(_cell_lines(page), pipes=False)
    ocr = is_ocr(source, page)
    if words and all(w.get("boxes") for w in words):
        return refine(_geometry_lines(words, text, ocr))
    if "\n" in text and not ocr:
        return refine(_text_lines(text))
    return None


def page_decimal(source):
    """Decimal separator proven by this source's own unambiguous numbers."""
    tokens = []
    for page in source.get("pages", []):
        for m in re.finditer(r"\d[\d.,]*\d|\d", page.get("text") or ""):
            tokens.append(m.group())
    return infer_decimal(tokens)


# ---------------------------------------------------------------- labels

ALL = frozenset({"invoice", "purchase_order", "receipt"})
INVOICE_ID_LABELS = [
    "invoice", "invoice no", "invoice no.", "invoice number", "invoice #",
    "invoice id", "inv no", "inv. no.", "inv #", "rechnungsnummer",
    "rechnung nr", "facture n°", "factura no", "invoice reference", "invoice ref",
    "invoice ref no", "invoice num", "inv ref", "inv reference", "inv number",
    "bill no", "bill number", "bill #", "bill ref", "invoice code", "billing reference", "billing ref",
    "statement no", "statement number", "invoice / statement no", "factuurnummer", "rechnungsnr", "numéro de facture",
]
INVOICE_ID = re.compile(r"(?:INV|INVOICE|BILL)[-#/._]?[A-Za-z0-9][A-Za-z0-9_./-]*", re.I)


def _entries(spec):
    out = []
    for phrases, field, kind, roles, colon_only in spec:
        for phrase in phrases:
            words = tuple(norm(w) for w in phrase.split())
            out.append((words, field, kind, frozenset(roles), colon_only))
            if len(words) > 1 and all(re.fullmatch(r"[^\W\d_]+", w) for w in words):
                # snake_case keys of exported records: "units_billed", "sum_payable"
                out.append((("_".join(words),), field, kind, frozenset(roles), colon_only))
    out.sort(key=lambda e: -len(e[0]))
    return out


LABELS = _entries(
    [
        # Totals that are NOT the invoice face amount; consumed, never read.
        (
            [
                "subtotal", "sub total", "sub-total", "net total", "net amount",
                "total tax", "tax total", "tax amount", "total quantity",
                "total qty", "total excl tax", "total excl. tax", "total excluding tax",
                "total before tax", "total net", "amount paid", "paid to date",
                "balance forward", "previous balance", "exchange rate",
                "interest rate", "discount", "shipping", "freight",
                "invoice subtotal", "vat amount", "gst amount", "sales tax amount",
                "po total", "order total", "purchase order total",
                "carriage", "handling", "delivery charge", "freight & handling", "shipping & handling",
                "freight and handling", "shipping and handling",
                "merchandise", "goods value", "goods total", "net value", "total ex vat", "total ex gst",
                "total excl vat", "total excl gst", "total excl. vat", "total before vat", "nettobetrag",
                "net total", "total net amount", "line total", "montant ht", "total ht",
                # Quantities and percentages that are not the invoiced ones.
                "minimum order quantity", "minimum order qty", "min order qty", "min. order qty", "moq",
                "minimum quantity", "order minimum", "pack quantity", "pack size", "units per pallet",
                "units per box", "units per carton", "quantity per carton", "quantity per box",
                "quantity per pack", "qty per carton", "qty per box", "qty per pack", "quantity backordered",
                "qty backordered", "backordered", "back order", "remaining on order", "quantity remaining",
                "previous invoice quantity", "previously invoiced", "withholding tax", "withholding",
                "wht", "tax withheld", "retention", "early payment discount", "settlement discount",
                "discount %", "cash discount", "quantity discount",
            ],
            None, "neg", ALL, False,
        ),
        (
            [
                "invoice face total", "face total", "invoice total", "total due",
                "amount due", "balance due", "grand total", "total amount",
                "total amount due", "total payable", "amount payable",
                "total incl tax", "total incl. tax", "total including tax", "total",
                "gesamtbetrag", "rechnungsbetrag", "endbetrag", "montant total",
                "total ttc", "importe total", "total incl vat", "total incl. vat", "total including vat",
                "total incl gst", "total inc gst", "total incl. gst", "total including gst", "total inc vat",
                "total incl hst", "total gst incl", "total vat incl", "amount now due", "amount to pay",
                "total to pay", "please pay this amount", "pay this amount", "invoice amount",
                "total invoice amount", "balance payable", "net amount due", "amount due now",
                "factuurbedrag", "te betalen", "totaal", "zu zahlen", "à payer", "net à payer", "sum payable",
                "sum due", "total sum", "amount payable now",
            ],
            "invoice_face_amount", "money", {"invoice"}, False,
        ),
        (
            [
                "unit price", "price per unit", "unit cost", "price each", "price",
                "rate", "unit rate", "einzelpreis", "stückpreis", "prix unitaire",
                "precio unitario", "charge per unit", "unit charge", "price per item", "net unit price",
                "rate per unit",
            ],
            "unit_price", "money", {"invoice"}, False,
        ),
        (
            [
                "unit price", "price per unit", "unit cost", "price each", "price",
                "einzelpreis", "prix unitaire", "precio unitario",
            ],
            "po_unit_price", "money", {"purchase_order"}, False,
        ),
        (
            [
                "tax rate", "tax", "sales tax", "sales tax rate", "vat", "vat rate",
                "gst", "hst", "pst", "qst", "mwst", "ust", "tva", "iva", "btw",
            ],
            "tax_rate", "percent", {"invoice"}, False,
        ),
        # Labels that name the percentage themselves: the "%" may be omitted.
        (
            [
                "tax %", "tax rate %", "vat %", "gst %", "sales tax %", "tax percent", "tax pct",
                "vat percent", "vat pct", "gst percent", "levy percent", "levy %", "tax rate percent",
            ],
            "tax_rate", "pct", {"invoice"}, False,
        ),
        (
            [
                "quantity", "qty", "qty billed", "quantity billed", "billed qty",
                "billed quantity", "qty invoiced", "quantity invoiced",
                "invoiced quantity", "invoiced qty", "units", "no of units",
                "no. of units", "number of units", "menge", "anzahl", "quantité",
                "quantite", "qté", "cantidad", "units billed", "units supplied", "qty supplied",
                "quantity supplied", "supplied", "units shipped", "qty shipped", "quantity shipped",
            ],
            "invoice_quantity", "qty", {"invoice"}, False,
        ),
        (
            [
                "ordered quantity", "ordered qty", "order quantity", "order qty",
                "qty ordered", "quantity ordered", "quantity", "qty", "bestellmenge",
                "menge", "quantité commandée", "cantidad pedida", "cantidad",
            ],
            "ordered_quantity", "qty", {"purchase_order"}, False,
        ),
        (
            [
                "supplier", "vendor", "seller", "supplier name", "vendor name",
                "lieferant", "fournisseur", "proveedor", "approved vendor", "approved supplier",
                "preferred vendor", "preferred supplier", "vendor / supplier", "awarded to",
            ],
            "supplier", "text", {"purchase_order", "receipt"}, False,
        ),
        (
            [
                "supplier", "vendor", "seller", "supplier name", "vendor name",
                "issued by", "bill from", "sold by", "lieferant", "fournisseur",
                "proveedor", "issuer", "invoice issuer",
            ],
            "invoice_supplier", "text", {"invoice"}, False,
        ),
        (["from"], "invoice_supplier", "text", {"invoice"}, True),
        # Party identifiers are not party names ("Vendor ID | V938588").
        (
            [
                "supplier id", "vendor id", "supplier no", "vendor no", "supplier number", "vendor number",
                "supplier code", "vendor code", "vendor tax id", "supplier tax id", "supplier vat", "vendor vat",
                "supplier account", "vendor account", "issuer reg", "issuer tax id", "customer id", "customer no",
                "customer number", "customer code", "client no",
            ],
            "party_ref", "id", ALL, False,
        ),
        (
            [
                "bill to", "ship to", "sold to", "deliver to", "invoice to",
                "buyer", "attn", "attention", "rechnungsempfänger",
            ],
            "buyer", "text", ALL, False,
        ),
        (["customer", "client"], "buyer", "text", ALL, True),
        (["recipient", "invoice recipient", "consignee"], "buyer", "text", ALL, False),
        # An explicit number label only: a bare "Invoice" is usually the
        # document title beside an address, postcode or reference.
        (INVOICE_ID_LABELS[1:], "invoice", "invno", {"invoice"}, False),
        # References to another invoice (credit notes, corrections).
        (
            [
                "original invoice", "original invoice no", "original invoice number", "credit note for invoice",
                "replaces invoice", "replaced invoice", "related invoice", "corrected invoice", "reference invoice",
                "against invoice", "cancels invoice", "previous invoice", "previous invoice no",
            ],
            "invoice_ref", "id", {"invoice"}, False,
        ),
        (["invoice"], "invoice_title", "id", {"invoice"}, False),
        # The same labels on a purchase order or receipt cite the invoice;
        # they are read (and ignored) so that the cited value is not data.
        (INVOICE_ID_LABELS, "invoice_ref", "id", {"purchase_order", "receipt"}, False),
        # Generic reference labels name the invoice only when the identifier
        # itself is invoice-prefixed ("Reference: INV-1048", not "Ref PO-77").
        (
            [
                "reference", "ref", "ref no", "reference no", "reference number",
                "document no", "document number", "doc no", "document ref",
            ],
            "invoice", "invref", {"invoice"}, False,
        ),
        (
            [
                "purchase order", "purchase order no", "purchase order number",
                "purchase order #", "po", "po no", "po number", "po #", "p.o.",
                "p.o. no", "customer po", "your po", "buyer po", "bestellnummer",
                "po ref", "po reference", "purchase order ref", "purchase order reference",
            ],
            "purchase_order", "id", ALL, False,
        ),
        # "Currency / unit: USD / BOX": one declaration of both dimensions.
        (
            ["currency / unit", "currency/unit", "currency unit", "currency and unit", "currency & unit"],
            "dims", "dims", ALL, False,
        ),
        (["unit / currency", "unit/currency", "unit and currency", "unit & currency"], "dims", "dims_rev", ALL, False),
        (
            [
                "received quantity", "received qty", "quantity received",
                "qty received", "delivered quantity", "delivered qty",
                "quantity delivered", "qty delivered", "qty rcvd", "rcvd qty",
                "qty recd", "recd qty", "quantity in", "qty in", "total received",
                "total counted", "quantity counted", "counted qty", "qty counted",
                "count", "received", "delivered", "counted", "units counted", "units received",
                "units delivered",
                "gelieferte menge", "eingangsmenge", "quantité reçue", "cantidad recibida",
            ],
            "received_quantity", "qty", {"receipt"}, False,
        ),
        (
            [
                "accepted quantity", "accepted qty", "quantity accepted", "qty accepted",
                "accepted", "good qty", "qty ok", "units accepted", "units passed qc", "passed qc",
                "qty passed", "qty passed qc",
            ],
            "accepted_quantity", "qty", {"receipt"}, False,
        ),
        (
            [
                "rejected quantity", "rejected qty", "quantity rejected", "qty rejected",
                "rejected", "damaged qty", "returned qty", "short qty", "qty short",
                "damaged / returned", "damaged", "returned", "units held / rejected", "held / rejected",
                "units rejected", "rejected / held", "qty damaged", "damaged / rejected",
                "returned to vendor", "returned to supplier", "quarantined", "qty on hold", "on hold",
                "failed qc", "non-conforming", "nonconforming",
            ],
            "rejected_quantity", "qty", {"receipt"}, False,
        ),
        (
            [
                "goods receipt", "receipt", "goods received note", "grn",
                "receipt no", "receipt number", "goods receipt no",
                "goods receipt number", "receipt #", "grn no", "grn number",
                "gr no", "gr number", "goods in no",
                "inspection report", "inspection report no", "inspection no",
                "receiving report", "receiving report no", "tally no", "tally sheet no",
                "log no", "receiving log no", "delivery receipt", "delivery receipt no",
                "receipt ref", "wareneingang", "record no", "record number",
                "goods received note no", "goods received note number", "receiving no",
                "goods receipt note", "goods receipt note no", "goods receipt note number",
                "inspection record no", "wareneingang nr",
            ],
            "receipt_id", "rid", {"receipt"}, False,
        ),
        # The supplier's delivery-note number identifies the receipt only when
        # the receipt carries no receipt number of its own.
        (
            [
                "delivery note", "delivery note no", "delivery note number", "dn no", "dn",
                "packing slip", "packing slip no", "supplier delivery note", "vendor delivery note",
                "supplier dn", "vendor dn", "lieferschein", "lieferschein nr",
            ],
            "receipt_ref", "rid", {"receipt"}, False,
        ),
        (["line", "line no", "line number", "po line", "line #", "po ln", "order line"], "line_id", "rid", {"receipt"}, False),
        # Receipt dates and item fields: never receipt authority.
        (
            [
                "date", "receipt date", "received date", "date received", "delivery date", "grn date",
                "inspected", "inspection date", "date of receipt", "received on", "receiving date",
                "delivered on", "date inspected",
            ],
            "date", "text", {"receipt"}, False,
        ),
        (["code", "material code"], "item_code", "id", {"receipt"}, False),
        (["material"], "item_desc", "text", {"receipt"}, False),
        # On a receipt, the order it answers is the buyer's purchase order.
        (["order no", "order number", "order ref", "order reference"], "purchase_order", "id", {"receipt"}, False),
        (["unit", "uom", "unit of measure", "einheit"], "unit", "unit", ALL, False),
        (["currency", "währung", "devise", "moneda"], "currency", "currency", ALL, False),
        (
            [
                "item code", "sku", "part no", "part number", "article no", "art no",
                "art. no", "product code", "item no", "item number", "catalog no",
                "cat no", "artikelnummer", "art.-nr", "référence",
            ],
            "item_code", "id", ALL, False,
        ),
        (["description", "item description", "item", "product", "article"], "item_desc", "text", ALL, False),
    ]
)

LABEL_INDEX = {}
for _entry in LABELS:
    LABEL_INDEX.setdefault(_entry[0][0], []).append(_entry)

FILLER = {"no", "nr", "number", "#", "n°", "-", "=", ":", "num"}
QTY_UNITS = {"ea", "each", "pcs", "pc", "units", "unit", "stk", "pieces", "x"}
CURRENCY = {
    "usd": "USD", "us$": "USD", "eur": "EUR", "€": "EUR", "gbp": "GBP", "£": "GBP",
    "chf": "CHF", "cad": "CAD", "jpy": "JPY", "¥": "JPY", "aud": "AUD", "krw": "KRW",
    "₩": "KRW", "$": None,
}
PRICE_OPERATORS = {"@", "at", "x", "à", "×"}
PROSE_LINKS = {"is", "was", "are", "charged", "at", "now"}
NOT_A_PRICE_CURRENCY = {
    "bank", "banking", "remit", "remittance", "payment", "payments", "fee", "fees", "charges", "equivalent",
    "converted", "conversion", "approx", "approximately", "account", "abroad", "iban", "exchange", "fx",
    "indicative", "reference",
}
AMOUNT_HEADING_WORDS = {
    "price", "amount", "total", "cost", "value", "rate", "net", "gross", "qty", "quantity", "extended", "ext",
}
TAX_AMOUNT_LABELS = {"tax amount", "vat amount", "gst amount", "sales tax amount", "total tax", "tax total"}
CHARGE_LABELS = {
    "shipping", "freight", "carriage", "handling", "delivery charge", "freight & handling",
    "shipping & handling", "freight and handling", "shipping and handling",
}
SECOND_PRICE_WORDS = {
    "list", "discount", "discounted", "reduced", "less", "instead", "special", "promotional",
    "rebate", "net", "gross", "originally", "previously", "was", "regular", "standard",
}
CURRENCY_NAMES = {
    "euro": "EUR", "euros": "EUR", "dollars": None, "yen": "JPY", "won": "KRW",
    "sterling": "GBP", "francs": None,
}
CURRENCY_NAMES = {k: v for k, v in CURRENCY_NAMES.items() if v}


def match_label(seg, i, role):
    if role in ("invoice", "purchase_order") and i + 2 < len(seg) and norm(seg[i].text) in ("price", "rate", "cost") \
            and norm(seg[i + 1].text) == "per":
        from folio.units import canonical_unit

        if canonical_unit(seg[i + 2].text):
            # "Price per box", "Rate per kg": a unit price for that unit.
            colon = seg[i + 2].text.rstrip().endswith(":")
            return ("unit_price" if role == "invoice" else "po_unit_price"), "money", 3, colon
    if i >= len(seg):
        return None
    # Labels indexed by their first word, longest first (same order as LABELS).
    for words, field, kind, roles, colon_only in LABEL_INDEX.get(norm(seg[i].text), ()):
        if role not in roles:
            continue
        n = len(words)
        if i + n > len(seg):
            continue
        if all(norm(seg[i + k].text) == words[k] for k in range(1, n)):
            colon = seg[i + n - 1].text.rstrip().endswith(":")
            if colon_only and not colon:
                continue
            return field, kind, n, colon
    return None


FIELD_WORDS = {
    "receipt", "goods", "grn", "line", "received", "delivered", "quantity",
    "qty", "unit", "uom", "currency", "purchase", "po", "invoice", "total",
    "amount", "price",
}


QUOTES = "\"'“”‘’«»„(["


def is_prose(tokens):
    """Free text: starts with a letter and has a 3+ letter alphabetic word.

    Text that starts like a field and carries a digit ("Qty 35") must read as
    a sentence (three such words) before it is treated as free text.
    """
    # Leading quote/bracket glyphs (often OCR artefacts) do not change that a
    # line is text; "/B", "-2" or "_A" fragments are not free text.
    if not tokens or not tokens[0].text.lstrip(QUOTES)[:1].isalpha():
        return False
    words = sum(1 for t in tokens if re.fullmatch(r"[^\W\d_]{3,}[.,;:!?]?", t.text))
    if norm(tokens[0].text) in FIELD_WORDS and any(
        re.search(r"\d", t.text) for t in tokens
    ):
        return words >= 3
    return words >= 1


# ---------------------------------------------------------------- values

class Value:
    __slots__ = ("value", "start", "end", "next", "normalization", "conf", "currency", "derivation", "unit", "tok")

    def __init__(self, value, tok, start, end, nxt, normalization=None, currency=None, derivation=None):
        self.value, self.start, self.end, self.next = value, start, end, nxt
        self.unit, self.tok = None, tok
        self.normalization, self.currency, self.derivation = normalization, currency, derivation
        self.conf = tok.conf if tok is not None and tok.ocr else None


def _skip(seg, j, words=FILLER):
    while j < len(seg) and norm(seg[j].text) in words:
        j += 1
    return j


def _number_core(tok):
    """(core text, offset) of a token's numeric part, trailing punctuation removed."""
    m = re.fullmatch(r"(.*?)[,;:.]?", tok.text)
    core = m.group(1) if m else tok.text
    return core, 0


def _split_group_follows(seg, j):
    """A following bare 3-digit group ("2 400") makes the number ambiguous."""
    return j < len(seg) and re.fullmatch(r"\d{3}(?:[.,]\d+)?[,;.]?", seg[j].text) is not None


def _read(tok, core, offset, ctx):
    if not tok.readable():
        return None
    try:
        value, normalization = read_number(core, decimal=ctx["decimal"], ocr=tok.ocr)
    except NumberError:
        return None
    return value, normalization, tok.start + offset, tok.start + offset + len(core)


GLUED_UNIT = re.compile(r"(\d[\d.,]*)([^\W\d_][^\W\d_²³]{0,9}[23²³]?\.?)")


def parse_qty(seg, j, ctx):
    from folio.units import canonical_unit

    j = _skip(seg, j, {":", "-", "="})
    if j >= len(seg):
        return None
    tok = seg[j]
    core, offset = _number_core(tok)
    glued = None
    m = GLUED_UNIT.fullmatch(tok.text)
    if m and canonical_unit(m.group(2)) and not tok.ocr:
        # "412.5kg", "240PCS": number and unit printed as one word.
        core, glued = m.group(1), (tok.start + m.start(2), tok.start + m.end(2), m.group(2))
    got = _read(tok, core, offset, ctx)
    nxt = j + 1
    if not got:
        hint = canonical_unit(glued[2]) if glued else (
            canonical_unit(seg[nxt].text.rstrip(".,;:")) if nxt < len(seg) and seg[nxt].readable() else None)
        hint = hint or ctx.get("count_unit")  # the one unit a receipt page declares
        got = _count_read(tok, core, offset, ctx, hint)
    if not got:
        return None
    value, normalization, start, end = got
    if _split_group_follows(seg, nxt):
        return None
    v = Value(value, tok, start, end, nxt, normalization)
    if glued:
        v.unit = (canonical_unit(glued[2]), glued[0], glued[1], glued[2], tok)
    elif nxt < len(seg) and not match_label(seg, nxt, ctx.get("role", "invoice")):
        unit = canonical_unit(seg[nxt].text)
        if unit and seg[nxt].readable():
            u = seg[nxt]
            text = u.text.rstrip(".,;:") if canonical_unit(u.text.rstrip(".,;:")) == unit else u.text
            v.unit = (unit, u.start, u.start + len(text), text, u)
            v.next = nxt + 1
    return v


# Units that count whole things: "5,350 EA" cannot be five and a third items,
# so a lone 3-digit group after its separator is a thousands group.
COUNT_UNITS = frozenset({"each", "box", "carton", "case", "pack", "set", "kit"})


def _count_read(tok, core, offset, ctx, unit):
    """Read an ambiguous "d,ddd" quantity whose unit counts whole items.

    Only a token rejected as ambiguous, with exactly one separator followed by
    three digits, and only when the packet's own numbers prove that separator
    is the grouping one (ctx["packet_decimal"] is the other mark and no
    document of the packet uses it as a decimal mark), is re-read; the
    normalization records the decimal so the reading recomputes exactly.
    Commonly fractional units (dozen, roll, drum, pallet, ...) never qualify.
    """
    m = re.fullmatch(r"\d{1,3}([.,])\d{3}", core)
    if unit not in COUNT_UNITS or not m or ctx.get("decimal") is not None:
        return None
    decimal = "." if m.group(1) == "," else ","
    if ctx.get("packet_decimal") != decimal:
        return None
    got = _read(tok, core, offset, {**ctx, "decimal": decimal})
    if got and got[1] is not None:
        got = (got[0], {**got[1], "grouping": "count-unit"}, got[2], got[3])
    return got


def count_fill(toks, ctx, unit):
    """A lone table quantity cell ("3,600") whose row/column unit counts items."""
    if len(toks) != 1:
        return None
    core, offset = _number_core(toks[0])
    got = _count_read(toks[0], core, offset, ctx, unit)
    if not got:
        return None
    value, normalization, start, end = got
    return Value(value, toks[0], start, end, 1, normalization)


def _currency_of(text):
    from folio.money import read_mark

    mark = read_mark(text.strip())
    if mark is None and norm(text) in CURRENCY:
        mark = read_mark(norm(text).upper() if CURRENCY[norm(text)] else "$")
    return (True, mark) if mark else (False, None)


def _mark_item(mark, tok, start, end):
    """Currency mark with its exact printed span (provenance for currency)."""
    return {"mark": mark, "start": start, "end": end, "tok": tok}


def parse_money(seg, j, ctx):
    from folio.money import split_mark

    j = _skip(seg, j, {":", "-", "=", "@"})
    marks = []
    # Up to two leading marks: "Amount due (GBP) GBP 1,330.03".
    while j < len(seg) and len(marks) < 2:
        found, mark = _currency_of(seg[j].text)
        if not found:
            break
        text = seg[j].text.rstrip(":;,")
        lead = len(text) - len(text.lstrip("(["))
        marks.append(_mark_item(mark, seg[j], seg[j].start + lead, seg[j].start + len(text.rstrip(")]"))))
        j += 1
    if j >= len(seg):
        return None
    tok = seg[j]
    text = tok.text
    offset = 0
    glued = split_mark(re.sub(r"[,;:]$", "", text))
    if glued:
        mark, number, at = glued
        if at:  # prefix mark: "€12,50", "USD1,200"
            marks.append(_mark_item(mark, tok, tok.start, tok.start + at))
            offset, text = at, number
        else:  # suffix mark: "12,50€"
            marks.append(_mark_item(mark, tok, tok.start + len(number), tok.start + len(number) + len(mark.text)))
            text = number
    core = re.sub(r"[,;:.]$", "", text)
    if not tok.readable():
        return None
    try:
        value, normalization = read_number(core, decimal=ctx["decimal"], ocr=tok.ocr)
    except NumberError:
        return None
    nxt = j + 1
    if _split_group_follows(seg, nxt):
        joined = _space_grouped(seg, j, core, offset, marks, ctx)
        if joined is None:
            return None
        value, normalization, end_tok, core = joined
        nxt = end_tok + 1
    if nxt < len(seg):
        found, mark = _currency_of(seg[nxt].text)
        if found:
            marks.append(_mark_item(mark, seg[nxt], seg[nxt].start, seg[nxt].start + len(seg[nxt].text.rstrip(":;,."))))
            nxt += 1
    if nxt < len(seg) and norm(seg[nxt].text) in {"ea", "each", "/ea", "per", "/unit", "/pc", "/pcs"}:
        nxt += 1
        if norm(seg[nxt - 1].text) == "per" and nxt < len(seg):
            from folio.units import canonical_unit

            if canonical_unit(seg[nxt].text):
                nxt += 1
    start = tok.start + offset
    v = Value(value, tok, start, start + len(core), nxt, normalization, marks or None)
    return v


def _space_grouped(seg, j, core, offset, marks, ctx):
    """ "SEK 12 500,00": an amount whose thousands groups were split into words.

    Accepted only for money with an explicit currency mark, a lead group of
    1-3 plain digits, following words of exactly 3 digits (the last may carry
    the document's decimal separator and fraction), on digital text, with
    exactly one space between the words. Quantities are never joined ("2 400"
    stays unreadable).
    """
    tok = seg[j]
    if not marks or tok.ocr or not re.fullmatch(r"\d{1,3}", core) or offset + len(core) != len(tok.text):
        return None
    k = j + 1
    groups = []
    while k < len(seg) and re.fullmatch(r"\d{3}([.,]\d+)?[,;]?", seg[k].text):
        groups.append(seg[k])
        if re.search(r"[.,]\d", seg[k].text):
            break
        k += 1
    if not groups:
        return None
    prev = tok
    for g in groups:
        if g.ocr or ctx["text"][prev.end : g.start] != " ":
            return None
        prev = g
    last = groups[-1]
    after = seg.index(last) + 1
    if after < len(seg) and re.match(r"\d", seg[after].text):
        return None  # more digits follow: not one amount
    end = last.end - (1 if last.text[-1] in ",;" else 0)
    quote = ctx["text"][tok.start + offset : end]
    try:
        value = parse_number(quote, decimal=ctx["decimal"], spaces=True)
    except NumberError:
        return None
    return value, {"rule": "folio-number-v2", "decimal": ctx["decimal"], "ocr": False, "space_groups": True}, seg.index(last), quote


def parse_percent(seg, j, ctx):
    from decimal import Decimal

    j = _skip(seg, j, {":", "-", "=", "@"})
    if j >= len(seg):
        return None
    tok = seg[j]
    m = re.fullmatch(r"(\()?(\d[\d.,]*)\s?%\)?[,;.:]?", tok.text)  # "8%", "(6.25%)", "7%)"
    nxt = j + 1
    lead = 0
    if m:
        core, lead = m.group(2), len(m.group(1) or "")
    elif nxt < len(seg) and (seg[nxt].text in ("%", "%,", "%.") or norm(seg[nxt].text) in ("percent", "per cent", "pct", "prozent", "pourcent")):
        core = re.sub(r"[,;:]$", "", tok.text)
        nxt += 1
    else:
        return None
    if not tok.readable():
        return None
    try:
        value, normalization = read_number(core, decimal=ctx["decimal"], ocr=tok.ocr)
    except NumberError:
        return None
    rate = Decimal(value) / 100
    return Value(
        format(rate, "f"), tok, tok.start + lead, tok.start + lead + len(core), nxt, normalization,
        derivation="percent / 100",
    )


def parse_id(seg, j, ctx, need_digit=True):
    j = _skip(seg, j)
    if j >= len(seg):
        return None
    tok = seg[j]
    core = re.sub(r"[,;:.)]+$", "", tok.text)
    if not ID_TOKEN.fullmatch(core) or (need_digit and not re.search(r"\d", core)):
        return None
    if not tok.readable():
        return None
    return Value(core, tok, tok.start, tok.start + len(core), j + 1)


def parse_text(seg, j, ctx):
    j = _skip(seg, j, {":", "-", "="})
    end_at = len(seg)
    # A name ends where another labelled field with a readable value starts
    # ("Supplier Northfield Cables Ordered quantity 50"); label words inside
    # a name ("Total Logistics") are not followed by a value and stay.
    for k in range(j + 1, len(seg)):
        m = match_label(seg, k, ctx.get("role", "invoice"))
        if m and m[1] in PARSERS and m[1] != "text" and PARSERS[m[1]](seg, k + m[2], ctx):
            end_at = k
            break
    toks = seg[j:end_at]
    if not toks or len(toks) > 12 or not any(re.search(r"[^\W\d_]", t.text) for t in toks):
        return None
    if not all(t.readable() for t in toks):
        return None
    text = ctx["text"]
    start, end = toks[0].start, toks[-1].end
    value = text[start:end]
    if value != " ".join(t.text for t in toks):
        return None
    return Value(value, toks[0], start, end, end_at)


def parse_pct(seg, j, ctx):
    """A value under a label that itself says percent ("Tax %", "levy_percent"): "8" is 8 %."""
    got = parse_percent(seg, j, ctx)
    if got:
        return got
    j = _skip(seg, j, {":", "-", "=", "@"})
    if j >= len(seg) or (j + 1 < len(seg) and _currency_of(seg[j + 1].text)[0]):
        return None
    got = _rate_value([seg[j]], ctx)
    if got:
        got.next = j + 1
    return got


OTHER_DOCUMENT_ID = re.compile(r"(?:P\.?O|PUR|GRN?|DN|DEL|RCV|REC|SO|ORD|LS|WE|QUO|QT|CN)[-#/.]?\d", re.I)


def parse_invno(seg, j, ctx):
    """An invoice number under an explicit number label; never a purchase-order,
    receipt or delivery identifier ("PO-8801", "GR-5", "DN-1")."""
    got = parse_id(seg, j, ctx)
    if got and OTHER_DOCUMENT_ID.match(got.value):
        return None
    return got


def parse_invtitle(seg, j, ctx):
    got = parse_id(seg, j, ctx)
    if got and (re.search(r"[A-Za-z]", got.value) or re.fullmatch(r"\d{4,}", got.value)):
        return got  # "INVOICE 7" (a title beside a page or unit number) is no identifier
    return None


def parse_invref(seg, j, ctx):
    got = parse_id(seg, j, ctx)
    return got if got and INVOICE_ID.fullmatch(got.value) else None


PARSERS = {
    "qty": parse_qty,
    "money": parse_money,
    "percent": parse_percent,
    "pct": parse_pct,
    "id": parse_id,
    "invref": parse_invref,
    "invtitle": parse_invtitle,
    "invno": parse_invno,
    "text": parse_text,
}


def candidate(field, v, ctx, method):
    page_text = ctx["page"].get("text") or ""
    item = {
        "field": field,
        "value": v.value,
        "source_id": ctx["source"]["id"],
        "source_sha256": ctx["source"]["sha256"],
        "source_version": ctx["source"].get("version", 1),
        "page": ctx["page"]["number"],
        "text_artifact_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
        "start": v.start,
        "end": v.end,
        "quote": ctx["text"][v.start : v.end],
        "method": method,
    }
    if v.normalization:
        item["normalization"] = v.normalization
    if v.conf is not None:
        item["ocr_confidence"] = v.conf
    if v.currency and field not in ("invoice_quantity", "ordered_quantity", "received_quantity"):
        marks = []
        for m in v.currency:
            tok = m["tok"]
            marks.append({
                **m["mark"].as_dict(),
                "start": m["start"], "end": m["end"], "quote": ctx["text"][m["start"]:m["end"]],
                "readable": tok.readable(),
            })
        item["currency_marks"] = marks
        codes = {m["code"] for m in marks if "code" in m}
        if len(codes) == 1:
            item["currency"] = codes.pop()
        elif len(codes) > 1:
            item["currency"] = "MIXED"
        else:
            item["currency"] = "FAMILY"
    if v.unit and field in ("invoice_quantity", "ordered_quantity", "received_quantity"):
        from folio.units import normalization

        unit, start, end, quote, tok = v.unit
        item["unit"] = {"value": unit, "start": start, "end": end, "quote": ctx["text"][start:end], "readable": tok.readable()}
        record = normalization(item["unit"]["quote"], unit)
        if record:
            item["unit"]["normalization"] = record
    if v.derivation:
        item["derivation"] = v.derivation
    return item


# ---------------------------------------------------------------- segments

def scan_segment(seg, role, ctx, start=0):
    """Read LABEL VALUE pairs in one segment.

    Returns (candidates, pending_label_or_None, label_only).
    pending is set when the segment ends in a label without a value;
    label_only is True when the segment consists of that label alone.
    """
    out = []
    i = start
    last_qty = False
    while i < len(seg):
        m = match_label(seg, i, role)
        if m and m[1] == "text" and i != 0:
            m = None  # names/parties are read only from the start of a segment
        if m and (m[1] == "rid" or (m[1] == "unit" and role == "receipt")):
            m = None  # receipt identity grammar lives in folio.receipts
        if m:
            field, kind, n, _ = m
            j = i + n
            if kind in ("dims", "dims_rev"):
                got = _dimension_pair(seg, j, kind, ctx)
                out.extend(got[0])
                i = got[1]
                continue
            if kind == "percent" and _amount_with_code(seg, j, ctx):
                # "Tax 2.79 USD": an amount in a currency is the tax amount, not a rate.
                got = _amount_with_code(seg, j, ctx)
                if role == "invoice":
                    out.append(candidate("tax_amount", got, ctx, "key-value"))
                i = got.next
                last_qty = False
                continue
            if kind == "unit":
                from folio.units import canonical_unit

                k = _skip(seg, j, {":", "-", "="})
                if k < len(seg) and canonical_unit(seg[k].text) and seg[k].readable():
                    tok = seg[k]
                    quote = tok.text.rstrip(".,;:") if canonical_unit(tok.text.rstrip(".,;:")) else tok.text
                    v = Value(canonical_unit(quote), tok, tok.start, tok.start + len(quote), k + 1)
                    out.append(candidate("unit_declared", v, ctx, "key-value"))
                    i = k + 1
                else:
                    i = j
                continue
            if kind == "neg":
                got = parse_money(seg, j, ctx) or parse_money(seg, _skip(seg, j, PROSE_LINKS), ctx)
                words = " ".join(norm(t.text) for t in seg[i:j])
                if got and role == "invoice":
                    # Amounts the invoice arithmetic checks against
                    # (folio-invoice-consistency-v1); never field values.
                    if words in TAX_AMOUNT_LABELS:
                        out.append(candidate("tax_amount", got, ctx, "key-value"))
                    elif words in CHARGE_LABELS and not any(
                        norm(t.text) in ("separately", "excluded", "invoiced") for t in seg[got.next:]
                    ):  # "Shipping: USD 40.00 (billed separately)" is not on this invoice
                        out.append(candidate("charge_amount", got, ctx, "key-value"))
                i = got.next if got else j
                last_qty = False
                continue
            if kind == "currency":
                k = _skip(seg, j, {":", "-"})
                if k < len(seg):
                    out.append(_declared_currency(seg[k], ctx))
                    i = k + 1
                else:
                    i = j
                continue
            if _skip(seg, j, FILLER | {":"}) >= len(seg):
                return out, (field, kind), i == start
            got = PARSERS[kind](seg, j, ctx)
            if not got and kind in ("money", "percent", "pct"):
                # Prose: "The amount now due is €576.13", "VAT charged at 13.5%".
                k = _skip(seg, j, PROSE_LINKS)
                got = PARSERS[kind](seg, k, ctx) if k != j else None
            if got and kind == "qty" and got.next < len(seg) and norm(seg[got.next].text) == "per":
                # "Units: 12 per box": a pack size, not the invoiced quantity.
                i = got.next
                last_qty = False
                continue
            if got:
                out.append(_due(candidate(field, got, ctx, "key-value"), seg[i : i + n]))
                i = got.next
                if field == "tax_rate" and role == "invoice":
                    # "Sales tax 8% 40.00", "VAT charged at 8% amounts to $40.00"
                    amount = parse_money(seg, _skip(seg, i, {"amounts", "to", "=", "of"}), ctx)
                    if amount:
                        out.append(candidate("tax_amount", amount, ctx, "key-value"))
                        i = amount.next
                last_qty = kind == "qty"
                continue
            if kind in ("qty", "money", "percent", "pct"):
                bad = _unreadable_value(seg, j, field, ctx)
                if bad:
                    out.append(bad)
            i = j
            last_qty = False
            continue
        if last_qty and role == "invoice" and norm(seg[i].text) in PRICE_OPERATORS:
            got = parse_money(seg, i + 1, ctx)
            if got:
                out.append(candidate("unit_price", got, ctx, "quantity-at-price"))
                i = got.next
                last_qty = False
                continue
        if role == "invoice" and norm(seg[i].text) == "at":
            # "at €2.82 per L": a price per unit, the unit printed after "per".
            got = parse_money(seg, i + 1, ctx)
            if got and got.currency and got.next >= 2 and norm(seg[got.next - 2].text) == "per":
                if any(norm(t.text) in SECOND_PRICE_WORDS for t in seg):
                    # "list price at $15.00 per box, discounted to $12.50": two prices.
                    out.append({**candidate("unit_price", got, ctx, "price-per-unit"), "unreadable": True,
                                "method": "unreadable"})
                else:
                    out.append(candidate("unit_price", got, ctx, "price-per-unit"))
                i = got.next
                continue
        if role == "invoice" and i == start:
            # "367.5 m × $1.60": quantity, unit, operator, priced amount.
            qty = parse_qty(seg, i, ctx)
            if qty and qty.unit and qty.next < len(seg) and norm(seg[qty.next].text) in PRICE_OPERATORS:
                price = parse_money(seg, qty.next + 1, ctx)
                if price and price.currency:
                    out.append(candidate("invoice_quantity", qty, ctx, "quantity-at-price"))
                    out.append(candidate("unit_price", price, ctx, "quantity-at-price"))
                    i = price.next
                    continue
        last_qty = False
        i += 1
    return out, None, False


DUE_WORDS = {"due", "payable", "pay", "balance", "remaining", "outstanding", "now", "zahlen", "payer", "betalen"}


def _due(item, label):
    """Mark a face amount read under a "due/payable/pay" label: such an amount
    is a balance when the invoice shows a deposit, payment or credit."""
    if item["field"] == "invoice_face_amount" and any(norm(t.text) in DUE_WORDS for t in label):
        item["due_label"] = True
    return item


def _declared_currency(tok, ctx):
    """A currency declaration value: a pinned code/name, or an unreadable declaration."""
    quote = tok.text.rstrip(".,;:")
    found, mark = _currency_of(quote)
    if found and mark.code is None:
        found = False  # "$" or "kr" alone never declares a currency
    named = CURRENCY_NAMES.get(quote.casefold())
    if found or named:
        code = mark.code if found else named
        item = candidate("currency_declared", Value(code, tok, tok.start, tok.start + len(quote), None), ctx, "key-value")
        item["readable"] = tok.readable()
        return item
    # A declaration that names no pinned currency: unreadable.
    return {"field": "currency_declared", "unreadable": True,
            "source_id": ctx["source"]["id"], "source_sha256": ctx["source"]["sha256"],
            "page": ctx["page"]["number"], "start": tok.start, "end": tok.end,
            "quote": tok.text, "method": "unreadable"}


def _declared_unit(tok, ctx, method="key-value"):
    from folio.units import canonical_unit

    quote = tok.text.rstrip(".,;:") if canonical_unit(tok.text.rstrip(".,;:")) else tok.text
    unit = canonical_unit(quote)
    if not unit or not tok.readable():
        return None
    return candidate("unit_declared", Value(unit, tok, tok.start, tok.start + len(quote), None), ctx, method)


def _dimension_pair(seg, j, kind, ctx):
    """ "Currency / unit: USD / BOX" (or "Unit / currency: BOX / USD").

    The first value is the first dimension, an optional '/' separates the
    second. Returns (declaration items, next index). A currency value that is
    no pinned currency stays an unreadable declaration (a conflict).
    """
    k = _skip(seg, j, {":", "-", "="})
    toks = []
    while k < len(seg) and len(toks) < 2:
        if seg[k].text == "/" and toks:
            k += 1
            continue
        toks.append(seg[k])
        k += 1
        if k < len(seg) and seg[k].text != "/":
            break
    order = ("currency", "unit") if kind == "dims" else ("unit", "currency")
    items = []
    for dim, tok in zip(order, toks):
        if dim == "currency":
            items.append(_declared_currency(tok, ctx))
        else:
            unit = _declared_unit(tok, ctx)
            if unit:
                items.append(unit)
    return items, k if toks else j


def _amount_with_code(seg, j, ctx):
    got = parse_money(seg, j, ctx)
    return got if got and got.currency else None


def _unreadable_value(seg, j, field, ctx):
    """A labelled numeric value that is present but cannot be read safely.

    Ambiguous separators, a split digit group ("2 400") or an OCR token
    below the confidence floor. Recorded so that no other reader's value
    for the same printed token can be accepted.
    """
    k = _skip(seg, j, {":", "-", "=", "@"})
    if k < len(seg) and _currency_of(seg[k].text)[0]:
        k += 1
    if k >= len(seg) or not re.search(r"\d", seg[k].text):
        return None
    tok = seg[k]
    return {
        "field": field,
        "unreadable": True,
        "source_id": ctx["source"]["id"],
        "source_sha256": ctx["source"]["sha256"],
        "page": ctx["page"]["number"],
        "start": tok.start,
        "end": tok.end,
        "quote": tok.text,
        "method": "unreadable",
    }


def _value_fills(seg, kind, ctx):
    if kind not in PARSERS:
        return None
    got = PARSERS[kind](seg, 0, ctx)
    if got and got.next == len(seg):
        return got
    return None


# ---------------------------------------------------------------- tables

COLUMNS = _entries(
    [
        (["item", "description", "item description", "product", "article", "goods", "service", "details", "beschreibung", "artikel", "désignation", "descripción"], "desc", "col", ALL, False),
        (["qty", "quantity", "units", "menge", "anzahl", "quantité", "cantidad"], "qty", "col", ALL, False),
        (["qty ordered", "ordered qty", "order qty", "ordered", "quantity ordered", "ordered quantity", "bestellmenge"], "qty_ordered", "col", ALL, False),
        (["qty billed", "billed qty", "qty invoiced", "invoiced qty", "invoiced", "quantity billed", "quantity invoiced"], "qty_billed", "col", ALL, False),
        (["qty received", "received qty", "received", "quantity received", "received quantity", "delivered", "qty delivered", "delivered qty"], "qty_received", "col", ALL, False),
        (["unit price", "price", "rate", "unit cost", "price each", "einzelpreis", "prix unitaire", "precio unitario"], "price", "col", ALL, False),
        (["amount", "total", "line total", "extended", "ext price", "net", "net amount", "betrag", "montant", "importe", "gesamt"], "amount", "col", ALL, False),
        (["unit", "uom", "unit of measure", "einheit"], "unit", "col", ALL, False),
        (["line", "line no", "ln", "pos"], "line", "col", ALL, False),
        (["receipt", "grn", "goods receipt", "receipt no", "receipt number"], "receipt", "col", ALL, False),
        (["currency", "curr", "ccy", "währung"], "currency", "col", ALL, False),
        (["po", "purchase order", "po number"], "po", "col", ALL, False),
        (["item code", "sku", "code", "part no", "part number", "article no", "art no", "product code", "item no", "artikelnummer", "ref"], "code", "col", ALL, False),
        (["accepted", "qty accepted", "accepted qty"], "qty_accepted", "col", ALL, False),
    ]
)
QTY_COLUMN_FIELD = {
    "invoice": {"qty": "invoice_quantity", "qty_billed": "invoice_quantity"},
    "purchase_order": {"qty": "ordered_quantity", "qty_ordered": "ordered_quantity"},
    "receipt": {"qty_received": "received_quantity"},
}
LINE_POLICY = "folio-line-binding-v1"


def header_columns(line):
    toks = [t for seg in line for t in seg]
    cols = []
    i = 0
    while i < len(toks):
        if re.search(r"\d", toks[i].text) and norm(toks[i].text) not in ("#",):
            return None
        hit = None
        for words, kind, _, _, _ in COLUMNS:
            n = len(words)
            if i + n <= len(toks) and all(norm(toks[i + k].text) == words[k] for k in range(n)):
                hit = (kind, n)
                break
        # A specific multi-word field label ("Tax rate", "Invoice total") is
        # one non-column heading, never split into generic column words.
        for words, _, _, _, _ in LABELS:
            n = len(words)
            if (
                n >= 2
                and (hit is None or n > hit[1])
                and i + n <= len(toks)
                and all(norm(toks[i + k].text) == words[k] for k in range(n))
            ):
                hit = ("other", n)
                break
        if hit:
            kind, n = hit
            cols.append({"kind": kind, "x0": toks[i].x0, "x1": toks[i + n - 1].x1})
            i += n
            continue
        from folio.units import heading_unit

        if cols and (norm(toks[i].text) in CURRENCY or _currency_of(toks[i].text.strip("()[]"))[0]):
            cols[-1]["x1"] = toks[i].x1
        elif cols and heading_unit(toks[i].text) and re.fullmatch(r"[(\[].*[)\]]", toks[i].text):
            # "Qty (pcs)": the unit printed in the heading belongs to its column.
            cols[-1]["x1"] = toks[i].x1
            cols[-1]["unit"] = (heading_unit(toks[i].text), toks[i])
        else:
            cols.append({"kind": "other", "x0": toks[i].x0, "x1": toks[i].x1})
        i += 1
    known = [c for c in cols if c["kind"] != "other"]
    if len(known) < 2 or not any(c["kind"].startswith("qty") or c["kind"] == "price" for c in known):
        return None
    cols.sort(key=lambda c: c["x0"])
    for a, b in zip(cols, cols[1:]):
        mid = (a["x1"] + b["x0"]) / 2
        a["right"], b["left"] = mid, mid
    cols[0]["left"], cols[-1]["right"] = float("-inf"), float("inf")
    return cols


def table_rows(lines, index, cols, role):
    """Assign the words of rows below a header to its columns by x-position."""
    rows = []
    for line in lines[index + 1 : index + 51]:
        toks = [t for seg in line for t in seg]
        if not any(re.search(r"\d", t.text) for t in toks):
            break
        first = match_label(line[0], 0, role)
        if first and first[1] in ("neg", "money", "percent"):
            break
        row = {}
        for t in toks:
            cx = (t.x0 + t.x1) / 2
            for c in cols:
                if c["left"] <= cx < c["right"]:
                    row.setdefault(id(c), (c, []))[1].append(t)
                    break
        out = {}
        for c, ts in row.values():
            # Two columns of one kind make that kind ambiguous (None).
            out[c["kind"]] = None if c["kind"] in out else ts
        rows.append(out)
    return rows


def _row_identity(row):
    """(code, description) printed in a table row's code/description columns."""
    code = desc = None
    toks = row.get("code")
    if toks and len(toks) == 1 and re.search(r"\d", toks[0].text) and ID_TOKEN.fullmatch(toks[0].text.rstrip(".,;:")):
        code = toks[0].text.rstrip(".,;:")
    toks = row.get("desc")
    if toks and all(t.readable() for t in toks):
        desc = " ".join(t.text for t in toks)
    return code, desc


def _row_unit(row, cols, got, ctx):
    """Units for a table quantity: printed after it, in a unit column or heading."""
    from folio.units import canonical_unit

    units = []
    if got is not None and got.unit:
        units.append(got.unit)
    toks = row.get("unit")
    if toks and len(toks) == 1 and canonical_unit(toks[0].text) and toks[0].readable():
        t = toks[0]
        units.append((canonical_unit(t.text), t.start, t.start + len(t.text), t.text, t))
    for c in cols:
        if c.get("unit") and c["kind"] in QTY_COLUMN_FIELD.get(ctx["role"], {}):
            unit, t = c["unit"]
            inner = re.search(r"[(\[]([^()\[\]]+)[)\]]", t.text)
            units.append((unit, t.start + inner.start(1), t.start + inner.end(1), inner.group(1), t))
    return units


def read_tables(lines, role, ctx, skip=()):
    """Typed values of x-aligned item tables.

    A table with one item row contributes its values directly. A table with
    several item rows is returned row by row with each row's item identity
    (code, description): which row (if any) is this packet's purchase-order
    line is decided across documents by folio-line-binding-v1, never by
    position, and rows are never summed.
    """
    from folio.units import normalization

    found, conflicts, tables = [], set(), []
    for index, line in enumerate(lines):
        cols = header_columns(line) if index not in skip else None
        if not cols:
            continue
        rows = table_rows(lines, index, cols, role)
        parsed = []
        for row in rows:
            entry = {"fields": [], "bad": set()}
            entry["code"], entry["desc"] = _row_identity(row)
            has_qty = False
            for kind, field in QTY_COLUMN_FIELD.get(role, {}).items():
                if kind not in row:
                    continue
                has_qty = True
                got = _value_fills(row[kind], "qty", ctx) if row[kind] else None
                if not got:
                    entry["bad"].add(field)
                    continue
                item = candidate(field, got, ctx, "table-column")
                units = _row_unit(row, cols, got, ctx)
                if len({u[0] for u in units}) > 1:
                    entry["bad"].add("unit")
                elif units and "unit" not in item:
                    unit, start, end, quote, tok = units[-1]
                    item["unit"] = {"value": unit, "start": start, "end": end,
                                    "quote": ctx["text"][start:end], "readable": tok.readable()}
                    record = normalization(item["unit"]["quote"], unit)
                    if record:
                        item["unit"]["normalization"] = record
                entry["fields"].append(item)
            if role == "invoice" and row.get("price"):
                got = _value_fills(row["price"], "money", ctx)
                if got:
                    entry["fields"].append(candidate("unit_price", got, ctx, "table-column"))
                else:
                    entry["bad"].add("unit_price")
            if has_qty:
                parsed.append(entry)
        if len(parsed) == 1:
            found.extend(parsed[0]["fields"])
            conflicts |= parsed[0]["bad"] - {"unit"}
            if "unit" in parsed[0]["bad"]:
                conflicts.add("unit")
        if parsed:
            fields = set(QTY_COLUMN_FIELD.get(role, {}).values())
            if role == "invoice":
                fields.add("unit_price")
            tables.append({"rows": parsed, "fields": sorted(fields)})
    return found, conflicts, tables


# ------------------------------------------------------- header-row tables

def _phrases(spec):
    return {" ".join(p.split()): kind for kind, phrases in spec for p in phrases}


HEADER_PHRASES = _phrases(
    [
        ("desc", ["item", "items", "description", "item description", "product", "article", "goods",
                  "service", "services", "details", "item / service", "product / service",
                  "goods / services", "description of goods", "beschreibung", "artikel",
                  "désignation", "descripción"]),
        ("line", ["line", "line no", "line #", "line number", "ln", "pos", "position", "line item", "#"]),
        ("po_line", ["po line", "po ln", "order line", "po line no", "po line #"]),
        ("code", ["item code", "sku", "code", "part no", "part number", "article no", "art no",
                  "product code", "item no", "item number", "catalog no", "artikelnummer"]),
        ("qty", ["qty", "quantity", "units", "no of units", "number of units", "menge", "anzahl",
                 "quantité", "cantidad"]),
        ("qty_invoiced", ["qty billed", "billed qty", "qty invoiced", "invoiced qty", "invoiced",
                          "quantity billed", "quantity invoiced", "invoiced quantity", "billed quantity",
                          "units shipped", "qty shipped", "quantity shipped", "shipped",
                          "qty supplied", "quantity supplied", "supplied", "menge geliefert", "aantal"]),
        # "Quantity per" may head a pack size: a quantity read under it needs the
        # invoice arithmetic to confirm it, like a bare "Tax" heading.
        ("qty_amb", ["quantity per", "qty per"]),
        ("qty_ordered", ["qty ordered", "ordered qty", "order qty", "ordered", "quantity ordered",
                         "ordered quantity", "order quantity", "bestellmenge"]),
        ("qty_received", ["qty received", "received qty", "received", "quantity received",
                          "received quantity", "delivered", "qty delivered", "delivered qty",
                          "delivered quantity", "quantity delivered", "qty rcvd", "rcvd qty",
                          "units counted", "counted", "qty counted", "units received"]),
        ("qty_accepted", ["accepted", "qty accepted", "accepted qty", "accepted quantity",
                          "quantity accepted", "good qty", "passed qc", "units passed qc", "units accepted"]),
        ("qty_rejected", ["rejected", "qty rejected", "rejected qty", "rejected quantity", "quantity rejected",
                          "failed qc", "qc failed", "non-conforming", "nonconforming", "non conforming",
                          "quarantined", "on hold", "qty on hold", "held", "damaged", "returned to vendor"]),
        ("price", ["unit price", "price", "rate", "unit cost", "price each", "unit rate", "price per unit",
                   "cost per unit", "einzelpreis", "prix unitaire", "precio unitario", "price ex gst",
                   "price ex vat", "price excl vat", "unit price ex vat", "unit price excl vat",
                   "unit price ex gst", "net price", "unit net price", "prijs", "stukprijs", "unit price net"]),
        ("tax_pct", ["tax %", "tax rate", "tax rate %", "vat %", "vat rate", "vat rate %", "gst %",
                     "gst rate", "sales tax %", "sales tax rate", "% tax", "tax pct"]),
        ("tax_amb", ["tax", "vat", "gst", "sales tax", "mwst"]),
        ("amount", ["amount", "total", "line total", "extended", "ext price", "extended price", "net",
                    "net amount", "line amount", "subtotal", "tax amount", "gross", "betrag", "montant",
                    "importe", "gesamt", "total amount"]),
        ("unit", ["unit", "uom", "unit of measure", "einheit"]),
        ("currency", ["currency", "curr", "ccy", "währung"]),
        ("receipt", ["receipt", "receipt no", "receipt number", "receipt id", "receipt #", "receipt ref",
                     "grn", "grn no", "grn number", "gr no", "goods receipt", "goods receipt no", "record no"]),
        # Headings of columns that are never fields (they still count as columns).
        ("ignore", ["list price", "disc %", "disc", "discount", "discount %", "batch", "lot", "lot no",
                    "delivery ref", "delivery note", "batch no", "weight", "origin", "hs code", "tariff code"]),
        ("po", ["po", "po no", "po number", "purchase order", "po ref", "po reference", "purchase order no", "order ref",
                "purchase order number", "customer po", "your order no"]),
        ("invoice", ["invoice no", "invoice number", "invoice #", "invoice id", "inv no", "invoice ref",
                     "invoice reference"]),
        ("date", ["date", "received date", "receipt date", "date received", "delivery date",
                  "delivered date", "order date", "due date", "ship date", "invoice date", "grn date",
                  "po date", "need by"]),
    ]
)
VALUE_KINDS = {"qty", "qty_amb", "qty_invoiced", "qty_ordered", "qty_received", "qty_accepted", "qty_rejected",
               "price", "tax_pct", "tax_amb"}
ROLE_QTY = {"invoice": ("qty", "qty_invoiced", "qty_amb"), "purchase_order": ("qty", "qty_ordered")}
ROLE_QTY_FIELD = {"invoice": "invoice_quantity", "purchase_order": "ordered_quantity"}


def _header_text(seg):
    # Case is kept: unit symbols are case-sensitive ("M" is not metre).
    t = " ".join(tok.text for tok in seg)
    t = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", " ", t)  # snake_case headings: qty_received
    t = re.sub(r"[()\[\]]", " ", t)
    t = re.sub(r"\s*/\s*", " / ", t)
    t = re.sub(r"[:.]+(?=\s|$)", "", t)
    return " ".join(t.split())


def _code_word(word):
    """('currency', code) or ('unit', unit) for a lone header/qualifier word."""
    from folio.units import canonical_unit

    if re.fullmatch(r"[A-Za-z]{3}", word):
        found, mark = _currency_of(word.casefold())
        if found and mark.code:
            return "currency", mark.code
    unit = canonical_unit(word)
    if unit and word.casefold() not in HEADER_PHRASES:
        return "unit", unit
    return None


def _header_kind(text):
    """(kind, attribute) of one header cell, or None for an unknown heading."""
    if text.casefold() in HEADER_PHRASES:
        return HEADER_PHRASES[text.casefold()], None
    words = text.split()
    if not words:
        return None
    if len(words) == 1:
        code = _code_word(words[0])
        if code:
            return ("currcode" if code[0] == "currency" else "unitcode"), code[1]
        return None
    head, last = " ".join(words[:-1]).casefold(), words[-1]
    kind = HEADER_PHRASES.get(head)
    if kind:
        if last == "%" and kind in ("tax_amb", "tax_pct"):
            return "tax_pct", None
        code = _code_word(last)
        if code and code[0] == "currency" and kind in ("price", "amount"):
            return kind, code  # "Unit price USD"
        if code and code[0] == "unit" and kind.startswith("qty"):
            return kind, code  # "Qty (pcs)"
        if last.casefold() in ("uom", "unit") and kind.startswith("qty"):
            return kind, None  # "Accepted UoM": the quantity cell prints its unit
    if " / " in text:
        kinds = {HEADER_PHRASES.get(p) for p in text.casefold().split(" / ")}
        if len(kinds) == 1 and None not in kinds:
            return kinds.pop(), None  # "Item / service"
    return None


def _header_cells(line):
    # Headings only route cells to columns and must still match the closed
    # vocabulary word for word; every value keeps the full OCR floor.
    cells = []
    for seg in line:
        if any(re.search(r"\d", t.text) for t in seg) or not all(t.readable(NOISE_MAX_CONFIDENCE) for t in seg):
            return None
        kind = _header_kind(_header_text(seg))
        cells.append({"kind": kind[0] if kind else "other", "attr": kind[1] if kind else None, "seg": seg})
    descs = [c for c in cells if c["kind"] == "desc"]
    if len(descs) == 2 and not any(c["kind"] == "code" for c in cells):
        # "Item | Description": the bare "Item" column holds the item code.
        bare = [c for c in descs if _header_text(c["seg"]).casefold() in ("item", "items", "article", "product")]
        if len(bare) == 1:
            bare[0]["kind"] = "code"
    phrases = [c for c in cells if c["kind"] not in ("other", "ignore", "currcode", "unitcode")]
    if len(cells) < 2 or len(phrases) < 2 or not any(c["kind"] in VALUE_KINDS for c in cells):
        return None
    return cells


def _expansions(cells, n):
    """Header layouts of n columns made by splitting some merged headings.

    Each heading that splits into known pieces may or may not be split (a
    printed "Qty UoM" can head one "10 pack" cell, or two cells); every
    subset of splits that yields n columns is a candidate layout.
    """
    from itertools import combinations

    split = [_expanded([c]) for c in cells]
    options = [i for i, pieces in enumerate(split) if len(pieces) > 1]
    need = n - len(cells)
    if need <= 0 or len(options) > 10:
        return []
    out = []
    for k in range(1, len(options) + 1):
        for chosen in combinations(options, k):
            if sum(len(split[i]) - 1 for i in chosen) != need:
                continue
            out.append([p for i, c in enumerate(cells) for p in (split[i] if i in chosen else [c])])
    return out


def _expanded(cells):
    """Header cells whose printed rule was lost: "Quantity EA", "Qty Rate" -> two columns."""
    out = []
    for cell in cells:
        seg = cell["seg"]
        pieces, i = [], 0
        while i < len(seg):
            for j in range(len(seg), i, -1):
                text = _header_text(seg[i:j])
                # pieces are whole headings or lone codes, never "phrase + code"
                kind = _header_kind(text) if text.casefold() in HEADER_PHRASES or len(text.split()) == 1 else None
                if kind:
                    pieces.append({"kind": kind[0], "attr": kind[1], "seg": seg[i:j], "split": True})
                    i = j
                    break
            else:
                pieces = None
                break
        if pieces and len(pieces) > 1 and all(p["attr"] is None or p["kind"] in ("currcode", "unitcode") for p in pieces):
            out.extend(pieces)
        else:
            out.append(cell)
    return out


def _column(seg):
    return int(seg[0].x0 // 100)


def _table_row(cells, line, role, cellular, ctx=None):
    """[(header cell, value tokens)] of a value row under a header, or None."""
    if not any(re.search(r"\d", t.text) for seg in line for t in seg):
        return None
    first = match_label(line[0], 0, role)
    if first and first[1] in ("neg", "money", "percent", "dims", "dims_rev"):
        return None
    if cellular:
        at = {_column(c["seg"]): c for c in cells}
        if len(at) != len(cells) or any(_column(seg) not in at for seg in line):
            return None
        # Every value heading needs its cell: "Net | 312.30 | USD" under
        # "Line | Qty | Rate | EA | Tax" is a totals line, not an item row.
        have = {_column(seg) for seg in line}
        if any(c["kind"] in VALUE_KINDS and col not in have for col, c in at.items()):
            return None
        row = [(at[_column(seg)], seg) for seg in line]
    else:
        layouts = [cells] if len(line) == len(cells) else _expansions(cells, len(line))
        rows = [list(zip(layout, line)) for layout in layouts]
        # A code split off a merged heading ("Quantity EA") must stand over
        # that very code, and every cell must have its heading's shape;
        # otherwise the columns are only counted to agree. Exactly one
        # layout may fit, else the row is not aligned.
        rows = [
            r for r in rows
            if all(_cell_fits(c, s) and _split_code_fits(c, s) for c, s in r)
            and (ctx is None or _row_arithmetic_fits(r, ctx))
        ]
        if len(rows) != 1:
            return None
        row = rows[0]
    if not any(c["kind"] in VALUE_KINDS for c, _ in row):
        return None
    return row


def _split_code_fits(cell, seg):
    if not (cell.get("split") and cell["kind"] in ("unitcode", "currcode")):
        return True
    word = seg[0].text.rstrip(".,;:") if len(seg) == 1 else None
    code = _code_word(word) if word else None
    return code is not None and code[1] == cell["attr"]


def _cell_fits(cell, seg):
    """A printed cell has the shape its heading promises.

    Quantities and prices carry a digit, descriptions a word, unit and
    currency columns one code. A stray or missing printed column shifts every
    later cell, and a shifted row then fails here instead of reading a line
    total as a unit price.
    """
    from folio.units import canonical_unit

    kind = cell["kind"]
    text = " ".join(t.text for t in seg)
    if kind.startswith("qty"):
        return QTY_CELL.fullmatch(text) is not None
    if kind in ("price", "amount"):
        return MONEY_CELL.fullmatch(text) is not None
    if kind in ("tax_pct", "tax_amb"):
        return PERCENT_CELL.fullmatch(text) is not None
    if kind == "ignore":
        heading = _header_text(cell["seg"]).casefold()
        if "price" in heading:
            return MONEY_CELL.fullmatch(text) is not None
        if "disc" in heading or "%" in heading:
            return PERCENT_CELL.fullmatch(text) is not None
        return True
    if kind == "desc":
        return re.search(r"[^\W\d_]{2}", text) is not None
    if kind in ("unit", "unitcode", "currency", "currcode"):
        word = seg[0].text.rstrip(".,;:") if len(seg) == 1 else ""
        return bool(canonical_unit(word) or canonical_unit(word.casefold()) or (_currency_of(word)[0] and len(word) == 3))
    return True


# Printed cell shapes: a quantity (optionally with its unit), a money amount
# (optionally with a currency code/symbol), a percentage.
QTY_CELL = re.compile(r"\d[\d.,' ]*(?: ?[^\W\d_][^\W\d_²³.]{0,11}\.?)?")
MONEY_CELL = re.compile(r"(?:[A-Za-z]{3} ?|[A-Z]{0,2}[$€£¥₹₩] ?)?-?\(?\d[\d.,' ]*\)?(?: ?(?:[A-Za-z]{3}|[$€£¥₹₩]))?")
PERCENT_CELL = re.compile(r"\(?\d{1,3}(?:[.,]\d+)? ?%?\)?|[0O]")


def _row_arithmetic_fits(row, ctx):
    """quantity x unit price = line amount, when all three read on a printed row.

    A row whose cells were shifted by one column reads a price under the
    quantity and the amount under the price; the product then fails. Rows
    whose numbers cannot be read are not judged here.
    """
    from decimal import Decimal

    by = {}
    for cell, seg in row:
        by.setdefault(cell["kind"], []).append(seg)
    qtys = [s for k in ("qty", "qty_invoiced", "qty_amb", "qty_ordered") for s in by.get(k, [])]
    if len(qtys) != 1 or len(by.get("price", [])) != 1 or len(by.get("amount", [])) != 1:
        return True
    for cell, seg in row:
        if cell["kind"] == "ignore" and "disc" in _header_text(cell["seg"]).casefold():
            # A non-zero discount leaves two prices (list and net) on the row.
            if re.sub(r"[0.,%() ]", "", " ".join(t.text for t in seg)):
                return False
    q = _value_fills(qtys[0], "qty", ctx)
    p = _value_fills(by["price"][0], "money", ctx)
    a = _value_fills(by["amount"][0], "money", ctx)
    if not (q and p and a):
        return True
    product = Decimal(q.value) * Decimal(p.value)
    amount = Decimal(a.value)
    return product == amount or product.quantize(Decimal(1).scaleb(min(-2, amount.as_tuple().exponent))) == amount


def _row_like(line, cells, role):
    """A printed line that reads as an item row although its columns do not
    line up with the headings ("15.15 | USD 10" under "Unit price | USD | Tax %")."""
    first = match_label(line[0], 0, role)
    if first:
        return False
    numbers = [t for seg in line for t in seg if re.search(r"\d", t.text) and not DATE_LIKE.fullmatch(t.text)]
    return len(numbers) >= sum(1 for c in cells if c["kind"] in VALUE_KINDS)


DATE_LIKE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[,;.]?")


def _continuation(line):
    """A wrapped header word ("service" under "Item /") before the first row."""
    return len(line) <= 2 and all(re.fullmatch(r"[^\W\d_]+|/", t.text) for seg in line for t in seg)


def aligned_tables(lines, role, ctx):
    """folio-header-row-v1: a header row of column names over value rows.

    Columns are cells (spreadsheets: the cell's column) or printed column
    segments ('|' rules or wide gaps: the n-th segment under the n-th
    heading, so the counts must agree). Each heading cell is matched as a
    whole against a closed vocabulary; a heading that is a currency or unit
    code names that dimension for its column. A row whose cells do not line
    up with the headings ends the table.
    """
    cellular = "cells" in ctx["source"] or ctx["source"].get("kind") in ("csv", "xlsx")
    tables, used = [], set()
    for index, line in enumerate(lines):
        if index in used:
            continue
        cells = _header_cells(line)
        if not cells:
            continue
        rows, k, skipped = [], index + 1, 0
        while k < len(lines) and len(rows) < 200:
            row = _table_row(cells, lines[k], role, cellular, ctx)
            if row is None:
                if not rows and skipped < 2 and _continuation(lines[k]):
                    skipped += 1
                    k += 1
                    continue
                if not cellular and _row_like(lines[k], cells, role):
                    rows.append(None)  # an item row that cannot be aligned: unreadable
                    k += 1
                break
            rows.append(row)
            k += 1
        if rows:
            span = set(range(index, k))
            used |= span
            tables.append({"header": index, "lines": span, "cells": cells, "rows": rows})
    return tables


def _rate_value(toks, ctx):
    """A tax-rate cell under a percent heading: "5", "5%", "7.5 %" -> 0.05."""
    from decimal import Decimal

    if len(toks) == 2 and toks[1].text == "%":
        toks = toks[:1]
    if len(toks) != 1:
        return None
    tok = toks[0]
    m = re.fullmatch(r"(\d[\d.,]*|[0-9OoIl]+)\s?%?", tok.text)
    if not m:
        return None
    got = _read(tok, m.group(1), 0, ctx)
    if not got:
        return None
    value, normalization, start, end = got
    if tok.fmt and "%" in tok.fmt and "%" not in tok.text:
        # A spreadsheet cell formatted as a percentage stores the fraction
        # itself (0.08 shown as "8%"): the value is already the rate.
        if not 0 <= Decimal(value) <= 1:
            return None
        return Value(format(Decimal(value).normalize(), "f"), tok, start, end, 1, normalization,
                     derivation="percent-formatted cell")
    if "%" not in tok.text and Decimal(value) < 1 and Decimal(value) != 0:
        return None  # "Tax %: 0.08": a fraction or 0.08 %? ambiguous, refused
    if not 0 <= Decimal(value) <= 100:
        return None
    return Value(format(Decimal(value) / 100, "f"), tok, start, end, 1, normalization, derivation="percent / 100")


def _unit_ref(unit, tok, start, end, ctx):
    from folio.units import normalization

    ref = {"value": unit, "start": start, "end": end, "quote": ctx["text"][start:end], "readable": tok.readable()}
    record = normalization(ref["quote"], unit)
    if record:
        ref["normalization"] = record
    return ref


def aligned_entries(table, role, ctx):
    """Typed rows of one header-row table: (entries, declarations, fields).

    Entries have read_tables' shape ({"fields", "bad", "code", "desc"}) so the
    single-row / folio-line-binding-v1 rules apply unchanged. Currency and
    unit codes printed in headings or code columns are declarations.
    """
    from folio.units import canonical_unit

    decls, fields = [], set()
    for cell in table["cells"]:
        tok = cell["seg"][-1]
        if cell["kind"] == "currcode" or (cell["attr"] and cell["attr"][0] == "currency"):
            decls.append(_declared_currency(tok, ctx))
        if cell["kind"] == "unitcode":
            item = _declared_unit(tok, ctx, "header-row-table")
            if item:
                decls.append(item)
    kinds = {c["kind"] for c in table["cells"]}
    offered = set()
    if role in ROLE_QTY and kinds & set(ROLE_QTY[role]):
        offered.add(ROLE_QTY_FIELD[role])
    if role == "invoice":
        offered |= {"unit_price"} if "price" in kinds else set()
        offered |= {"tax_rate"} if kinds & {"tax_pct", "tax_amb"} else set()
    entries = []
    for row in table["rows"]:
        if row is None:
            if offered:
                entries.append({"fields": [], "bad": set(offered), "code": None, "desc": None})
                fields |= offered
            continue
        by = {}
        for cell, toks in row:
            by.setdefault(cell["kind"], []).append((cell, toks))
        # Document references printed in every row ("po_number", "invoice_no")
        # are the document's own references, whatever row binds.
        for kind, field, parse in (("po", "purchase_order", parse_id), ("invoice", "invoice", parse_invno)):
            if kind == "invoice" and role != "invoice":
                continue
            for cell, toks in by.get(kind, []):
                got = parse(toks, 0, ctx)
                if got and got.next == len(toks):
                    decls.append(candidate(field, got, ctx, "header-row-table"))
        for cell, toks in by.get("currcode", []) + by.get("currency", []):
            # Every currency-column cell declares; one naming no pinned
            # currency ("XYZ", "US dollars") is unreadable, i.e. a conflict.
            item = _declared_currency(toks[0], ctx)
            if len(toks) > 1 and not item.get("unreadable"):
                item = {**_declared_currency(Tok("?", toks[0].start, toks[-1].end, 0, 0, 0, 0), ctx),
                        "quote": ctx["text"][toks[0].start:toks[-1].end]}
            decls.append(item)
        entry = {"fields": [], "bad": set(), "code": None, "desc": None}
        lines_ = by.get("line", []) + by.get("po_line", [])
        if len(lines_) == 1 and len(lines_[0][1]) == 1 and re.search(r"\d", lines_[0][1][0].text):
            entry["line"] = lines_[0][1][0].text.rstrip(".,;:")  # the row's own line number
        codes, descs = by.get("code", []), by.get("desc", [])
        if len(codes) == 1 and len(codes[0][1]) == 1 and re.search(r"\d", codes[0][1][0].text):
            entry["code"] = codes[0][1][0].text.rstrip(".,;:")
        if len(descs) == 1 and all(t.readable() for t in descs[0][1]):
            entry["desc"] = " ".join(t.text for t in descs[0][1])
        qtys = [ct for kind in ROLE_QTY.get(role, ()) for ct in by.get(kind, [])]
        if role in ROLE_QTY_FIELD and qtys:
            field = ROLE_QTY_FIELD[role]
            fields.add(field)
            got = _value_fills(qtys[0][1], "qty", ctx) if len(qtys) == 1 else None
            if got is None:
                entry["bad"].add(field)
            else:
                item = candidate(field, got, ctx, "header-row-table")
                if qtys[0][0]["kind"] == "qty_amb":
                    item["confirm"] = "invoice-arithmetic"
                units = []
                if got.unit:
                    units.append(_unit_ref(got.unit[0], got.unit[4], got.unit[1], got.unit[2], ctx))
                cell = qtys[0][0]
                if cell["attr"] and cell["attr"][0] == "unit":
                    t = cell["seg"][-1]
                    word = re.search(r"[^\W_][\w²³]*", t.text)
                    units.append(_unit_ref(cell["attr"][1], t, t.start + word.start(), t.start + word.end(), ctx))
                for c, toks in by.get("unit", []) + by.get("unitcode", []):
                    unit = canonical_unit(toks[0].text.rstrip(".,;:")) if len(toks) == 1 else None
                    if unit and toks[0].readable():
                        t = toks[0]
                        units.append(_unit_ref(unit, t, t.start, t.start + len(t.text.rstrip(".,;:")), ctx))
                    elif c["kind"] == "unit":
                        entry["bad"].add("unit")
                if len({u["value"] for u in units}) > 1:
                    entry["bad"].add("unit")
                elif units and "unit" not in item:
                    item["unit"] = units[-1]
                entry["fields"].append(item)
        if role == "invoice":
            for kinds, field in ((("price",), "unit_price"), (("tax_pct", "tax_amb"), "tax_rate")):
                hits = [ct for kind in kinds for ct in by.get(kind, [])]
                if not hits:
                    continue
                fields.add(field)
                if len(hits) != 1:
                    entry["bad"].add(field)
                    continue
                cell, toks = hits[0]
                got = _value_fills(toks, "money", ctx) if field == "unit_price" else _rate_value(toks, ctx)
                if got is None:
                    entry["bad"].add(field)
                    continue
                item = candidate(field, got, ctx, "header-row-table")
                if cell["kind"] == "tax_amb":
                    # A bare "Tax" heading may head an amount: the rate needs the
                    # invoice's own arithmetic to confirm it (folio-invoice-consistency-v1).
                    item["confirm"] = "invoice-arithmetic"
                entry["fields"].append(item)
        # Any other heading that is a field label ("Invoice total", "PO number")
        # reads the value cell below it, as a label above its value would.
        for cell, toks in by.get("other", []):
            # Words of the heading as printed ("invoice_total" reads "invoice total").
            words = [Tok(w, 0, 0, 0, 0, 0, 0) for w in _header_text(cell["seg"]).split()]
            m = match_label(words, 0, role)
            if not m or m[1] not in ("qty", "money", "percent", "pct", "id", "invref", "invtitle", "invno", "text") or _skip(words, m[2], FILLER | {":"}) < len(words):
                continue
            fields.add(m[0])
            got = _value_fills(toks, m[1], ctx)
            if got:
                entry["fields"].append(_due(candidate(m[0], got, ctx, "header-row-table"), cell["seg"]))
            elif m[1] in ("qty", "money", "percent", "pct"):
                entry["bad"].add(m[0])
        if qtys or entry["fields"]:
            entries.append(entry)
    return entries, decls, fields


# ---------------------------------------------------------------- documents

def _pair_dimension_declarations(lines, role, skip=()):
    """Join a standalone Unit/Currency label to its adjacent value segment.

    CSV/XLSX cells and spaced PDF columns carry exact label/value boundaries.
    Treating a label in one segment and its value in the next as unrelated
    silently loses explicit dimensions, so join only a label-only segment to
    the immediately following single-token segment. Table headings remain
    under the table reader.
    """
    paired = []
    for li, line in enumerate(lines):
        if header_columns(line) is not None or li in skip:
            paired.append(line)
            continue
        out, index = [], 0
        while index < len(line):
            segment = line[index]
            match = match_label(segment, 0, role) if segment else None
            if (
                index + 1 < len(line)
                and match
                and match[1] in ("unit", "currency", "dims", "dims_rev")
                and _skip(segment, match[2], {":", "-", "="}) >= len(segment)
                and (len(line[index + 1]) == 1 or (match[1].startswith("dims") and len(line[index + 1]) <= 3))
            ):
                out.append(segment + line[index + 1])
                index += 2
                continue
            tail = _label_tail(segment, role)
            nxt = line[index + 1] if index + 1 < len(line) else None
            if (
                tail
                and tail[1] in ("qty", "money", "percent", "pct", "id", "invref", "invtitle", "invno", "neg")
                and nxt is not None
                and re.search(r"\d", " ".join(t.text for t in nxt))
                # one value word, or a currency code beside the amount ("GBP 1,330.03")
                and (len(nxt) == 1 or (len(nxt) == 2 and any(_currency_of(t.text)[0] for t in nxt)))
                and not match_label(nxt, 0, role)
            ):
                # "Rate | 17.45 | USD", "Tax rate | 8 | percent": a label cell, its
                # value cell and a trailing currency/unit/percent qualifier cell.
                joined = segment + nxt
                index += 2
                if index + 1 == len(line) and len(line[index]) == 1 and _qualifier(line[index][0]):
                    joined = joined + line[index]
                    index += 1
                out.append(joined)
                continue
            if (
                role == "invoice" and nxt is not None and len(line) == index + 2 and _ends_with_rate(segment, role)
                and re.search(r"\d", " ".join(t.text for t in nxt))
                and (len(nxt) == 1 or (len(nxt) == 2 and any(_currency_of(t.text)[0] for t in nxt)))
            ):
                # "Sales tax 8% | 45.00": the printed tax amount beside its rate.
                out.append(segment + nxt)
                index += 2
                continue
            out.append(segment)
            index += 1
        paired.append(out)
    return paired


def _ends_with_rate(seg, role):
    for i in range(len(seg)):
        m = match_label(seg, i, role)
        if m and m[0] == "tax_rate":
            got = parse_percent(seg, i + m[2], {"decimal": None})
            if got is None:
                got = parse_percent(seg, _skip(seg, i + m[2], PROSE_LINKS), {"decimal": None})
            return got is not None and got.next == len(seg)
    return False


def _label_tail(seg, role):
    """The label that ends a segment with no value after it, if any.

    A currency code printed after the label ("Total USD", "Amount due (GBP)")
    still leaves the label waiting for its value cell.
    """
    for i in range(len(seg)):
        m = match_label(seg, i, role)
        if not m:
            continue
        k = _skip(seg, i + m[2], FILLER | {":"})
        if m[1] == "money":
            while k < len(seg) and _code_word(seg[k].text.strip("()[]:").casefold()) and \
                    _code_word(seg[k].text.strip("()[]:").casefold())[0] == "currency":
                k += 1
        if k >= len(seg):
            return m
    return None


def _qualifier(tok):
    from folio.units import canonical_unit

    text = tok.text.strip().rstrip(".,;:")
    return (
        text in ("%",)
        or norm(text) in ("percent", "per cent", "pct")
        or (_currency_of(text)[0] and _currency_of(text)[1].code is not None)
        or canonical_unit(text) is not None
    )


def read_page(source, page, role, lines=None):
    """All typed candidates on one page (plus table conflicts)."""
    lines = page_lines(source, page) if lines is None else lines
    if not lines:
        return [], set(), []
    ctx = {"source": source, "page": page, "text": page.get("text") or "", "decimal": page_decimal(source), "role": role}
    aligned = aligned_tables(lines, role, ctx)
    consumed = set().union(*(t["lines"] for t in aligned)) if aligned else set()
    lines = _pair_dimension_declarations(lines, role, consumed)
    found = []
    for li, line in enumerate(lines):
        if li in consumed:
            continue
        header = header_columns(line) is not None
        pending = None
        unpaired = []
        for seg in line:
            begin = 0
            if pending:
                field, kind = pending[0]
                # A label in the next column/segment is not the value of the
                # previous label (for example, CSV header cells "Supplier" |
                # "Ordered Quantity"). Leave it for scan_segment below; the
                # stacked-value path applies the same label boundary check.
                next_label = match_label(seg, 0, role)
                # A text value can legitimately begin with its field's label
                # word ("Supplier Technologies"). Keep that whole segment as
                # the pending value; a different field label ("Ordered
                # Quantity") still marks a boundary and must be scanned alone.
                next_is_label = bool(next_label and next_label[0] != field)
                got = (
                    PARSERS[kind](seg, 0, ctx)
                    if kind in PARSERS and not next_is_label
                    else None
                )
                if got and header and field.startswith("item_"):
                    got = None  # "Description | Qty": a heading beside a heading
                if got and kind == "text" and pending[1] and li + 1 < len(lines) and not _name_like(seg[: got.next]):
                    below = _stacked_value(pending[2], lines[li + 1], pending[0], role, ctx)
                    under = [t for s in lines[li + 1] for t in s if below and below.start <= t.start < below.end]
                    if under and _name_like(under):
                        # "Seller | Document data" over "Acme Ltd | ...": two column
                        # headings side by side; the party name sits below its label.
                        got = None
                if got:
                    found.append(_due(candidate(field, got, ctx, "label-beside-value"), pending[2]))
                    begin = got.next
                elif pending[1]:
                    unpaired.append((pending[2], pending[0]))
                pending = None
            items, tail, label_only = scan_segment(seg, role, ctx, begin)
            if header:
                # Column headings are not item names ("Description Qty Rate").
                items = [i for i in items if not i["field"].startswith("item_")]
            found.extend(items)
            if tail:
                pending = (tail, label_only, seg)
        if pending and pending[1]:
            unpaired.append((pending[2], pending[0]))
        # A label alone in its column segment may sit above its value (boxed
        # layouts). On a table header line, generic column headings ("Qty",
        # "Total") are read only by read_tables, column by column.
        if li + 1 < len(lines) and li + 1 not in consumed:
            for seg, tail in unpaired:
                if header and _is_column_heading(seg):
                    continue
                below = _stacked_value(seg, lines[li + 1], tail, role, ctx)
                if below:
                    found.append(_due(candidate(tail[0], below, ctx, "label-above-value"), seg))
    # A currency code printed in parentheses in an amount label or a price /
    # amount / quantity column heading ("Order total (CAD, excl. tax)",
    # "Price (USD)") declares the document's currency, on every document
    # role. Bank, remittance, fee or converted-amount notes declare nothing.
    for line in lines:
        if not any(t.text.startswith("(") for seg in line for t in seg):
            continue
        heading = header_columns(line) is not None or _header_cells(line) is not None
        for seg in line:
            words = {norm(t.text) for t in seg}
            label = match_label(seg, 0, role)
            amount_label = label is not None and label[1] in ("money", "neg")
            if words & NOT_A_PRICE_CURRENCY or not (amount_label or (heading and words & AMOUNT_HEADING_WORDS)):
                continue
            for tok in seg:
                m = re.fullmatch(r"\(([A-Z]{3})(?:\)|,)?[:.,]?", tok.text)
                if m and _currency_of(m.group(1))[0] and _currency_of(m.group(1))[1].code:
                    v = Value(_currency_of(m.group(1))[1].code, tok, tok.start + 1, tok.start + 4, None)
                    item = candidate("currency_declared", v, ctx, "label-currency")
                    item["readable"] = tok.readable()
                    found.append(item)
    table_found, conflicts, tables = read_tables(lines, role, ctx, consumed)
    found.extend(table_found)
    for table in aligned:
        entries, decls, fields = aligned_entries(table, role, ctx)
        found.extend(decls)
        if len(entries) == 1:
            found.extend(entries[0]["fields"])
            conflicts |= entries[0]["bad"]
        if entries:
            tables.append({"rows": entries, "fields": sorted(fields)})
    unique = {}
    for item in found:
        unique.setdefault((item["field"], item["start"], item["end"]), item)
    return list(unique.values()), conflicts, tables


def _is_column_heading(seg):
    words = tuple(norm(t.text) for t in seg)
    return any(words == entry[0] for entry in COLUMNS)


def _stacked_value(label_seg, next_line, pending, role, ctx):
    field, kind = pending
    x0, x1 = label_seg[0].x0, label_seg[-1].x1
    for seg in next_line:
        if seg[-1].x1 < x0 or seg[0].x0 > x1:
            continue
        if match_label(seg, 0, role):
            return None
        return _value_fills(seg, kind, ctx)
    return None


TITLE_WORDS = {
    "invoice", "tax", "commercial", "proforma", "pro-forma", "original", "copy",
    "duplicate", "rechnung", "facture", "factura", "statement", "bill",
}
CONNECTORS = {"&", "and", "of", "the", "de", "du", "la", "le", "und", "et", "y", "van", "von", "der", "+"}
# Field-label words: a segment made only of these is a label cell, never a company name.
FIELD_LABEL_WORDS = {
    "quantity", "qty", "total", "subtotal", "amount", "price", "unit", "units", "rate",
    "date", "due", "terms", "currency", "receipt", "received", "ordered", "line", "lines",
    "description", "item", "items", "number", "no", "ref", "reference", "balance", "net",
    "gross", "vat", "gst", "page",
}
# Document identifiers such as INV-RECEIPTS or PO-0732 are not issuer names.
DOCUMENT_ID = re.compile(r"(?:INV|PO|GR|SO|DN|CN|REC|ORD|BILL|RCPT)[-#/][\w-]+", re.I)


def _name_like(toks):
    if not 1 <= len(toks) <= 8:
        return False
    for t in toks:
        if re.search(r"\d|@|www\.|https?:", t.text, re.I) or t.text.endswith(":"):
            return False
        first = t.text[:1]
        if first.isalpha() and not first.isupper() and t.text.casefold() not in CONNECTORS:
            return False
    return any(len(re.sub(r"[^\w]", "", t.text)) >= 2 for t in toks)


BUYER_HEADINGS = {
    "bill to", "billed to", "bill-to", "ship to", "shipped to", "ship-to", "sold to", "deliver to",
    "delivered to", "delivery to", "invoice to", "invoiced to", "buyer", "customer", "consignee",
    "client", "attn", "attention", "purchaser",
}


def buyer_names(lines):
    """Names printed beside or directly below a buyer heading ("BILL TO", "Sold to: *").

    Used only to keep the buyer out of the invoice-issuer candidates, never as
    a field value.
    """
    names = []
    for li, line in enumerate(lines):
        for si, seg in enumerate(line):
            head = " ".join(norm(t.text) for t in seg if t.text not in ("*", ":", "-"))
            if head not in BUYER_HEADINGS:
                continue
            near = line[si + 1 : si + 2]
            if li + 1 < len(lines):
                below = [s for s in lines[li + 1] if s[0].x0 <= seg[-1].x1 and s[-1].x1 >= seg[0].x0]
                near += sorted(below, key=lambda s: abs(s[0].x0 - seg[0].x0))[:1]
            for s in near:
                words = [t for t in s if t.text not in ("*", ":")]
                if words and _name_like(words):
                    names.append(" ".join(t.text for t in words))
    return names


def invoice_issuer(source, page, lines=None):
    """Invoice-issuer names: explicit issuer labels, and letterhead candidates.

    Letterhead candidates are name-like segments that precede the first
    labelled field on the first page (document-title words removed).
    """
    lines = page_lines(source, page) if lines is None else lines
    explicit, letterhead = [], []
    if not lines:
        return explicit, letterhead
    ctx = {"source": source, "page": page, "text": page.get("text") or "", "decimal": page_decimal(source), "role": "invoice"}
    stopped = False
    for line in lines[:8]:
        for seg in line:
            items, _, _ = scan_segment(seg, "invoice", ctx)
            for item in items:
                if item["field"] == "invoice_supplier":
                    explicit.append(item)
            if items:
                stopped = True
                break
            if " ".join(norm(t.text) for t in seg if t.text not in ("*", ":", "-")) in BUYER_HEADINGS:
                continue  # "CUSTOMER", "SOLD TO:": a heading, never a name
            if match_label(seg, 0, "invoice") and not all(norm(t.text) in TITLE_WORDS for t in seg):
                # A field label ("Invoice #" with its value in the next cell)
                # ends the letterhead region as a read field would.
                stopped = True
                break
            words = [t for t in seg if norm(t.text) not in TITLE_WORDS]
            if not words:
                continue
            # The remaining words must be one contiguous run of the segment.
            first = seg.index(words[0])
            if seg[first : first + len(words)] != words:
                continue
            if all(norm(t.text) in FIELD_LABEL_WORDS for t in words) or (
                len(words) == 1 and DOCUMENT_ID.fullmatch(words[0].text)
            ):
                continue
            if not _name_like(words) or not all(
                t.readable(LETTERHEAD_MIN_CONFIDENCE) for t in words
            ):
                continue
            text = ctx["text"]
            value = text[words[0].start : words[-1].end]
            if value != " ".join(t.text for t in words):
                continue
            letterhead.append(
                {
                    "field": "invoice_supplier",
                    "value": value,
                    "source_id": source["id"],
                    "source_sha256": source["sha256"],
                    "source_version": source.get("version", 1),
                    "page": page["number"],
                    "text_artifact_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "start": words[0].start,
                    "end": words[-1].end,
                    "quote": value,
                    "method": "letterhead",
                    "ocr": any(t.ocr for t in words),
                }
            )
        if stopped:
            break
    # A logo/short name ("Lumen Ridge") whose longer form starts another
    # letterhead line ("Lumen Ridge Optics B.V. Kanaalweg 17") is not the
    # issuer's name: it is kept as a candidate but never reported alone.
    texts = [
        (" ".join(t.text for seg in line for t in seg), {t.start for seg in line for t in seg})
        for line in lines[:8]
    ]
    for item in letterhead:
        if any(item["start"] not in starts and t.startswith(item["value"] + " ") for t, starts in texts):
            item["short_form"] = True
    # Explicit issuer labels anywhere on the page also count.
    if not stopped or not explicit:
        for line in lines:
            for seg in line:
                for item in scan_segment(seg, "invoice", ctx)[0]:
                    if item["field"] == "invoice_supplier" and item not in explicit:
                        explicit.append(item)
    return explicit, letterhead


LEGAL_SUFFIXES = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "gmbh", "mbh", "ag", "kg", "kgaa", "ug", "eg", "se", "sa",
    "sas", "sarl", "srl", "spa", "bv", "nv", "plc", "pty", "pte", "pvt", "private",
    "oy", "oyj", "ab", "as", "asa", "aps", "kk", "llp", "lp", "sl", "sro", "kft",
    "zrt", "doo", "ou", "oü", "sp", "zoo", "and",
}


def normalize_supplier(name):
    """Comparable supplier identity: case, punctuation and legal suffix free."""
    import unicodedata

    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKC", name).casefold().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = s.split()
    merged = []
    for tok in tokens:  # dotted initialisms: "s a" -> "sa", "g m b h" -> "gmbh"
        if len(tok) == 1 and merged and merged[-1][1]:
            merged[-1] = (merged[-1][0] + tok, True)
        else:
            merged.append((tok, len(tok) == 1))
    tokens = [t for t, _ in merged]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or s.strip()
