"""Round-7 generality fixtures (author-created, CC0, fictional companies).

Written for the round-7 extractor work, independently of eval/packets.py, of
the round-5 robustness fixtures and of every sealed set (none was read). The
packets vary suppliers, currencies (0-, 2- and 3-decimal), units, receipt
layouts (thermal slips, delivery notes, inspection notes, warehouse logs,
tallies), multi-line tables, locales, scans and CSV dialects. Labels come
from the authoring parameters below, never from product code.

    python tests/fixtures/generality/build_fixtures.py   # writes set/ + labels.json

The set directory has the shape of an eval set: prepare it with the real
container parser (eval/prepare_cases.py) and run it with eval/run_eval.py.

Label conventions (``expect``):
* a value: the extractor must produce exactly this value;
* None: the field must stay unresolved (missing or conflicting);
* fields listed in ``ocr_or_unresolved`` may also stay unresolved (an honest
  refusal on a scan) but a wrong value is always a failure;
* ``conflict``: that field must be reported as conflicting.
"""

import hashlib
import io
import json
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
SET = HERE / "set"
BUYER = "LINDQVIST & OKAFOR ARCHITECTS"
FONT = "C:/Windows/Fonts/arial.ttf"
NBSP = "\u00a0"
MINOR = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KRW": 0, "KWD": 3, "BHD": 3, "SEK": 2, "CAD": 2, "CHF": 2}


# ------------------------------------------------------------------ writers

def pdf(rows, *, size=11, page=(612, 792), ttf=False):
    """rows: (x, y_from_top, text) placed exactly; invariant bytes.

    ttf=True embeds Arial (needed for glyphs outside WinAnsi, e.g. the won sign).
    """
    from reportlab.pdfgen.canvas import Canvas

    out = io.BytesIO()
    c = Canvas(out, pagesize=page, invariant=1)
    c.setTitle("Folio round-7 generality fixture")
    if ttf:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        pdfmetrics.registerFont(TTFont("FixtureArial", FONT))
        c.setFont("FixtureArial", size)
    else:
        c.setFont("Helvetica", size)
    for x, y, text in rows:
        c.drawString(x, page[1] - y, text)
    c.save()
    return out.getvalue()


def lines(texts, *, x=60, top=60, step=24):
    return [(x, top + i * step, t) for i, t in enumerate(texts)]


def table(header, rows, *, xs, top, step=22):
    """Header and rows placed in fixed x columns."""
    out = [(x, top, h) for x, h in zip(xs, header)]
    for r, row in enumerate(rows, 1):
        out += [(x, top + r * step, str(v)) for x, v in zip(xs, row) if str(v)]
    return out


def scan(texts, *, seed, angle=0.0, noise=0.0, blur=0.0, scale=1.0, page=(1224, 1584), size=26):
    """Image-only PDF of rendered text (no text layer): forces the OCR path."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    rnd = random.Random(seed)
    w, h = int(page[0] * scale), int(page[1] * scale)
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, int(size * scale))
    for i, t in enumerate(texts):
        if isinstance(t, tuple):
            x, t = t
        else:
            x = 90
        draw.text((int(x * scale), int((90 + i * 52) * scale)), t, fill=0, font=font)
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    px = img.load()
    for _ in range(int(w * h * noise / 1000)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = rnd.choice((0, 90, 170))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    out = io.BytesIO()
    c = Canvas(out, pagesize=(page[0] / 2, page[1] / 2), invariant=1)
    c.drawImage(ImageReader(img), 0, 0, page[0] / 2, page[1] / 2)
    c.save()
    return out.getvalue()


def csv_bytes(rows, delimiter=","):
    import csv

    text = io.StringIO(newline="")
    csv.writer(text, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    return text.getvalue().encode()


def xlsx_bytes(rows):
    from openpyxl import Workbook

    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def payable(invoice_key, supplier, face, currency="USD", recognized="0"):
    ident = hashlib.sha256((invoice_key + supplier).encode()).hexdigest()
    zero = "0" if MINOR[currency] == 0 else "0." + "0" * MINOR[currency]
    return {
        "id": f"{ident[:8]}-{ident[8:12]}-{ident[12:16]}-{ident[16:20]}-{ident[20:32]}",
        "revision": 4,
        "supplier": supplier,
        "invoice_key": invoice_key,
        "invoice_face_amount": face,
        "recognized_quantity": recognized,
        "net_amount": zero,
        "tax_amount": zero,
        "gross_amount": zero,
        "status": "unmatched",
        "bank_fingerprint": "synthetic-bank-unchanged",
        "due_date": "2026-12-18",
        "currency": currency,
    }


def gross(qty, price, rate, currency="USD"):
    """Arithmetic policy v1: net and tax half-up to currency minor units."""
    q = Decimal(1).scaleb(-MINOR[currency])
    net = (Decimal(qty) * Decimal(price)).quantize(q, ROUND_HALF_UP)
    tax = (net * Decimal(rate)).quantize(q, ROUND_HALF_UP)
    return str(net + tax)


def en(amount, places=2):
    return f"{Decimal(amount):,.{places}f}"


def de(amount, places=2):
    return en(amount, places).replace(",", "_").replace(".", ",").replace("_", ".")


def sv(amount, places=2):
    return en(amount, places).replace(",", NBSP).replace(".", ",")


# ------------------------------------------------------------------ packets

def build():
    P = []

    def add(**kw):
        P.append(kw)

    # ---------------------------------------------------------------- units / receipts

    # G01 thermal goods-in slip (narrow page, glued labels, dot leaders), PCS
    face = gross(240, "3.15", "0.06")
    add(
        id="G01-thermal-slip-pcs", slice="receipt_layout", meta={"receipt": "thermal slip", "unit": "pcs"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["HALVARD FASTENERS INC", "INVOICE", "Invoice No: HF-30418", "Customer PO: PO-77120",
                                                  "Quantity: 240 PCS", "Unit price: USD 3.15", "Sales tax: 6%", f"Total due: USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "PURCHASE ORDER PO-77120", "Vendor: Halvard Fasteners, Inc.", "Item: Hex bolt M8x40 zinc",
                                                               "Order qty: 240 pcs", "Unit cost: USD 3.15"]))),
            ("receipt.pdf", "receipt", pdf(lines(["L&O ARCHITECTS - DOCK 2", "GOODS IN SLIP", "GRN#:50418", "PO#:PO-77120", "ITEM: HEX BOLT M8X40",
                                                  "QTY RCVD....236 PCS", "CHECKED BY: J. MBEKI", "2026-10-12 14:03"], x=12, top=30, step=16),
                                           size=8, page=(226, 300))),
        ],
        payable=payable("HF-30418", "Halvard Fasteners Inc", face),
        expect={"invoice": "HF-30418", "invoice_quantity": "240", "ordered_quantity": "240", "received_quantity": "236",
                "unit_price": "3.15", "tax_rate": "0.06", "invoice_face_amount": face, "currency": "USD", "unit": "each", "supplier_check": "match"},
        allowed=[{"mode": "partial", "quantity": "236"}],
    )

    # G02 supplier delivery note with an item table, unit boxes
    face = gross(18, "11.40", "0.20", "GBP")
    dn = lines(["MARLOWE PACKAGING LTD", "DELIVERY NOTE", "Delivery note no. DN-55120", "Your order: PO-41877",
                "Delivery date: 14 Oct 2026"]) + table(
        ["Item code", "Description", "Ordered", "Delivered", "Unit"],
        [["MP-220", "Double-wall carton", "20", "18", "boxes"]], xs=[60, 140, 330, 400, 480], top=200) + [
        (60, 280, "Received in good order. Two boxes to follow next week.")]
    add(
        id="G02-delivery-note-boxes", slice="receipt_layout", meta={"receipt": "delivery note table", "unit": "boxes", "currency": "GBP"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["MARLOWE PACKAGING LTD", "Invoice MPL-7731", "Purchase order PO-41877", "Item code MP-220",
                                                  "Quantity 18 boxes", "Price per box GBP 11.40", "VAT 20%", f"Invoice total GBP {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-41877", "Supplier Marlowe Packaging Ltd", "Item code MP-220",
                                                               "Ordered quantity 20 boxes", "Unit price GBP 11.40"]))),
            ("delivery-note.pdf", "receipt", pdf(dn)),
        ],
        payable=payable("MPL-7731", "Marlowe Packaging Ltd", face, "GBP"),
        expect={"invoice_quantity": "18", "ordered_quantity": "20", "received_quantity": "18", "unit_price": "11.40", "tax_rate": "0.2",
                "invoice_face_amount": face, "currency": "GBP", "unit": "box", "supplier_check": "match"},
        allowed=[{"mode": "partial", "quantity": "18"}],
    )

    # G03 weighed goods in kg (fractional quantity)
    face = gross("412.5", "3.80", "0")
    add(
        id="G03-kg-fractional", slice="units", meta={"unit": "kg"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["CASTELLAN GRAIN COOPERATIVE", "Invoice CGC-9902", "PO number PO-30219", "Quantity 412.5 kg",
                                                  "Unit price USD 3.80 per kg", "Tax rate 0%", f"Amount due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-30219", "Supplier Castellan Grain Cooperative",
                                                               "Ordered quantity 450 kg", "Unit price USD 3.80"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Weighbridge receipt", "Goods receipt GR-WB-8821", "Purchase order PO-30219",
                                                  "Quantity received: 412.5 kg", "Gross 1,020.0 kg / tare 607.5 kg recorded by scale 3"]))),
        ],
        payable=payable("CGC-9902", "Castellan Grain Cooperative", face),
        expect={"invoice_quantity": "412.5", "ordered_quantity": "450", "received_quantity": "412.5", "unit_price": "3.80",
                "invoice_face_amount": face, "unit": "kg", "currency": "USD"},
        allowed=[{"mode": "partial", "quantity": "412.5"}],
    )

    # G04 inspection note: received, accepted and rejected quantities
    face = gross(300, "7.95", "0.05")
    add(
        id="G04-inspection-accepted", slice="receipt_layout", meta={"receipt": "inspection note", "unit": "units"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["PELLARIN CERAMICS SRL", "Invoice PC-2026-118", "Purchase order PO-61833", "Qty billed: 300 units",
                                                  "Unit price: USD 7.95", "Tax: 5%", f"Grand total: USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-61833", "Supplier: Pellarin Ceramics S.R.L.",
                                                               "Quantity ordered: 300 units", "Unit price: USD 7.95"]))),
            ("inspection.pdf", "receipt", pdf(lines([BUYER, "INCOMING INSPECTION NOTE", "Inspection report no. IR-7781", "PO: PO-61833",
                                                     "Quantity received: 300", "Accepted quantity: 294", "Rejected quantity: 6",
                                                     "UoM: units", "Six tiles chipped in transit; returned to supplier."]))),
        ],
        payable=payable("PC-2026-118", "Pellarin Ceramics SRL", face),
        expect={"invoice_quantity": "300", "ordered_quantity": "300", "received_quantity": "294", "unit": "each", "invoice_face_amount": face},
        allowed=[{"mode": "partial", "quantity": "294"}],
    )

    # G05 warehouse receiving log: several items, one matches the PO item
    face = gross(600, "1.12", "0")
    log = lines([BUYER, "WAREHOUSE RECEIVING LOG", "Log no. RL-2026-1182", "Purchase order PO-60331"]) + table(
        ["SKU", "Description", "Qty received", "UoM"],
        [["SKU-1180", "Oat drink 1L", "480", "pcs"], ["SKU-1181", "Soy drink 1L", "540", "pcs"], ["SKU-1190", "Rice drink 1L", "120", "pcs"]],
        xs=[60, 150, 330, 430], top=170)
    add(
        id="G05-warehouse-log-multi-item", slice="receipt_layout", meta={"receipt": "warehouse log, 3 items", "binding": "SKU"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["NORDBY DAIRY ALTERNATIVES AS", "Invoice ND-55102", "PO: PO-60331", "SKU: SKU-1181",
                                                  "Quantity 600 pcs", "Unit price USD 1.12", "Tax rate 0%", f"Total USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-60331", "Supplier Nordby Dairy Alternatives AS",
                                                               "SKU SKU-1181", "Ordered quantity 600 pcs", "Unit price USD 1.12"]))),
            ("receiving-log.pdf", "receipt", pdf(log)),
        ],
        payable=payable("ND-55102", "Nordby Dairy Alternatives AS", face),
        expect={"invoice_quantity": "600", "ordered_quantity": "600", "received_quantity": "540", "unit": "each"},
        allowed=[{"mode": "partial", "quantity": "540"}],
    )

    # G06 tally sheet with a counted total in cartons
    face = gross(64, "22.00", "0.0825")
    add(
        id="G06-tally-cartons", slice="receipt_layout", meta={"receipt": "tally sheet", "unit": "cartons"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["OSTERWALD PAPER MILLS", "Invoice OPM-4410", "Purchase order PO-81402", "Qty 64 ctns",
                                                  "Unit price USD 22.00", "Sales tax 8.25%", f"Balance due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-81402", "Supplier Osterwald Paper Mills",
                                                               "Order quantity 64 cartons", "Unit price USD 22.00"]))),
            ("tally.pdf", "receipt", pdf(lines([BUYER, "TALLY SHEET", "Tally no. TS-0931", "PO-81402 - copy paper A4",
                                                "Pallet 1: 32 cartons, pallet 2: 28 cartons", "Total counted: 60 cartons", "Counted by night shift."]))),
        ],
        payable=payable("OPM-4410", "Osterwald Paper Mills", face),
        expect={"invoice_quantity": "64", "ordered_quantity": "64", "received_quantity": "60", "unit": "carton", "invoice_face_amount": face},
        allowed=[{"mode": "partial", "quantity": "60"}],
    )

    # G07 unit mismatch: ordered in boxes, invoiced in each
    face = gross(240, "0.85", "0")
    add(
        id="G07-unit-mismatch-box-each", slice="units", meta={"conflict": "unit"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["BRISCOE LAB SUPPLIES", "Invoice BLS-2209", "Purchase order PO-52260", "Quantity 240 each",
                                                  "Unit price USD 0.85", "Tax rate 0%", f"Invoice total USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-52260", "Supplier Briscoe Lab Supplies",
                                                               "Ordered quantity 20 boxes", "Unit price USD 10.20"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-5226", "Purchase order PO-52260", "Received quantity 20", "Unit box"]))),
        ],
        payable=payable("BLS-2209", "Briscoe Lab Supplies", face),
        expect={"unit": None, "conflict": "unit"},
        allowed=[{"mode": "escalate"}],
    )

    # G08 unit conflict (dozen vs each) while the receipt is missing: escalate wins
    face = gross(10, "30.00", "0")
    add(
        id="G08-unit-conflict-and-missing", slice="decision_order", meta={"conflict": "unit", "missing": "receipt"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["FERNHILL CATERING GOODS", "Invoice FCG-118", "Purchase order PO-14450", "Quantity 10 dozen",
                                                  "Unit price USD 30.00", "Tax rate 0%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-14450", "Supplier Fernhill Catering Goods",
                                                               "Ordered quantity 120 each", "Unit price USD 2.50"]))),
        ],
        payable=payable("FCG-118", "Fernhill Catering Goods", face),
        expect={"received_quantity": None, "unit": None, "conflict": "unit"},
        allowed=[{"mode": "escalate"}],
    )

    # G09 cable in metres; grouped PO quantity resolved by the PO's own decimals
    face = gross(860, "2.45", "0.07")
    add(
        id="G09-metres", slice="units", meta={"unit": "m"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["TRAVERSE CABLE WORKS", "Invoice TCW-6620", "PO PO-70114", "Quantity 860 m",
                                                  "Unit price USD 2.45", "Tax 7%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-70114", "Supplier Traverse Cable Works",
                                                               "Ordered quantity 1,000 m", "Unit price USD 2.45"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-TC-301", "Purchase order PO-70114", "Received qty: 860 m",
                                                  "Reel R-17 measured on arrival."]))),
        ],
        payable=payable("TCW-6620", "Traverse Cable Works", face),
        expect={"invoice_quantity": "860", "ordered_quantity": "1000", "received_quantity": "860", "unit": "m", "invoice_face_amount": face},
        allowed=[{"mode": "partial", "quantity": "860"}],
    )

    # G10 pairs; invoice single-row table with a UoM column
    face = gross(500, "0.42", "0")
    inv = lines(["QUILLON SAFETY PRODUCTS", "Invoice QSP-30071", "Purchase order PO-90233"]) + table(
        ["Description", "Qty", "UoM", "Unit price", "Amount"], [["Nitrile gloves L", "500", "pr", "0.42", "210.00"]],
        xs=[60, 240, 300, 360, 460], top=140) + [(360, 220, "Tax rate 0%"), (360, 244, f"Total due USD {en(face)}")]
    add(
        id="G10-pairs-table", slice="units", meta={"unit": "pairs", "layout": "one-row table with UoM column"},
        files=[
            ("invoice.pdf", "invoice", pdf(inv)),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-90233", "Supplier Quillon Safety Products",
                                                               "Qty ordered: 500 pairs", "Unit price: USD 0.42"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-QS-77", "PO PO-90233", "Received quantity 480", "Unit: pairs"]))),
        ],
        payable=payable("QSP-30071", "Quillon Safety Products", face),
        expect={"invoice_quantity": "500", "ordered_quantity": "500", "received_quantity": "480", "unit": "pair", "unit_price": "0.42", "invoice_face_amount": face},
        allowed=[{"mode": "partial", "quantity": "480"}],
    )

    # ---------------------------------------------------------------- currencies

    # G11 EUR, German labels, suffix euro sign, Stk
    face = gross(75, "18.40", "0.19", "EUR")
    add(
        id="G11-eur-de-suffix", slice="currency", meta={"currency": "EUR", "locale": "de"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["HEIDKAMP BÜROTECHNIK GMBH", "Rechnung", "Rechnungsnummer RE-2026-4471", "Bestellnummer PO-24410",
                                                  "Menge 75 Stk", "Einzelpreis 18,40 €", "MwSt 19 %", f"Gesamtbetrag {de(face)} €"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-24410", "Supplier Heidkamp Bürotechnik GmbH",
                                                               "Ordered quantity 75 pcs", "Unit price EUR 18.40"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-HB-12", "Purchase order PO-24410", "Received quantity 70", "Unit pcs"]))),
        ],
        payable=payable("RE-2026-4471", "Heidkamp Bürotechnik GmbH", face, "EUR"),
        expect={"invoice_quantity": "75", "ordered_quantity": "75", "received_quantity": "70", "unit_price": "18.40", "tax_rate": "0.19",
                "invoice_face_amount": face, "currency": "EUR", "unit": "each"},
        allowed=[{"mode": "partial", "quantity": "70"}],
    )

    # G12 JPY: zero minor units, grouped thousands
    face = gross(1200, "980", "0.10", "JPY")
    add(
        id="G12-jpy-zero-decimals", slice="currency", meta={"currency": "JPY", "minor_units": 0},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["KIRISHIMA PRECISION K.K.", "TAX INVOICE", "Invoice No. KP-23017", "PO No. PO-55802",
                                                  "Quantity 1,200 pcs", "Unit price JPY 980", "Consumption tax 10%", f"Total JPY {en(face, 0)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-55802", "Supplier Kirishima Precision KK",
                                                               "Ordered quantity 1,200 pcs", "Unit price JPY 980", f"Order value JPY {en(1176000, 0)}"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-KP-55", "Purchase order PO-55802", "Received quantity 1150", "Unit pcs"]))),
        ],
        payable=payable("KP-23017", "Kirishima Precision K.K.", face, "JPY"),
        expect={"invoice_quantity": "1200", "ordered_quantity": "1200", "received_quantity": "1150", "unit_price": "980",
                "invoice_face_amount": face, "currency": "JPY", "tax_rate": "0.1"},
        allowed=[{"mode": "partial", "quantity": "1150"}],
    )

    # G13 KWD: three minor units
    face = gross(40, "56.250", "0", "KWD")
    add(
        id="G13-kwd-three-decimals", slice="currency", meta={"currency": "KWD", "minor_units": 3},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["AL-SAFWA ELECTRICAL TRADING CO.", "Invoice ASE-7720", "Purchase order PO-33061", "Quantity 40 units",
                                                  "Unit price KWD 56.250", "Tax rate 0%", f"Invoice total KWD {en(face, 3)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-33061", "Supplier Al-Safwa Electrical Trading Co.",
                                                               "Ordered quantity 40 units", "Unit price KWD 56.250"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-AS-4", "Purchase order PO-33061", "Received quantity 38", "Unit units"]))),
        ],
        payable=payable("ASE-7720", "Al-Safwa Electrical Trading Co.", face, "KWD"),
        expect={"invoice_quantity": "40", "ordered_quantity": "40", "received_quantity": "38", "unit_price": "56.250",
                "invoice_face_amount": face, "currency": "KWD", "unit": "each"},
        allowed=[{"mode": "partial", "quantity": "38"}],
    )

    # G14 GBP pound sign plus a currency declaration
    face = gross(20, "45.25", "0.20", "GBP")
    add(
        id="G14-gbp-symbol", slice="currency", meta={"currency": "GBP"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["WEXCOMBE GLASSWORKS LTD", "Invoice WG-0915", "Your PO: PO-48802", "Currency: GBP",
                                                  "Qty: 20", "Unit price: £45.25", "VAT: 20%", f"Invoice total: £{en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-48802", "Supplier: Wexcombe Glassworks Limited",
                                                               "Ordered quantity: 20", "Unit price: £45.25"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-WG-2", "Purchase order PO-48802", "Received quantity 20", "Unit each"]))),
        ],
        payable=payable("WG-0915", "Wexcombe Glassworks Ltd", face, "GBP"),
        expect={"invoice_quantity": "20", "ordered_quantity": "20", "received_quantity": "20", "unit_price": "45.25",
                "invoice_face_amount": face, "currency": "GBP"},
        allowed=[{"mode": "partial", "quantity": "20"}],
    )

    # G15 EUR documents, USD payable: currency mismatch escalates
    face = gross(30, "64.00", "0.21", "EUR")
    add(
        id="G15-eur-docs-usd-payable", slice="currency", meta={"conflict": "payable currency"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["VAN RIJN MEUBELEN B.V.", "Invoice VRM-3301", "Purchase order PO-19022", "Quantity 30",
                                                  "Unit price EUR 64.00", "VAT 21%", f"Total EUR {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-19022", "Supplier Van Rijn Meubelen BV",
                                                               "Ordered quantity 30", "Unit price EUR 64.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-VR-3", "Purchase order PO-19022", "Received quantity 30", "Unit each"]))),
        ],
        payable=payable("VRM-3301", "Van Rijn Meubelen B.V.", "2323.20", "USD"),
        expect={"currency": "EUR", "invoice_face_amount": face},
        allowed=[{"mode": "escalate"}],
    )

    # G16 CHF invoice, EUR purchase order, receipt missing: escalate
    face = gross(12, "140.00", "0.081", "CHF")
    add(
        id="G16-chf-vs-eur-missing-receipt", slice="decision_order", meta={"conflict": "currency", "missing": "receipt"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["ZIMMERLI MESSTECHNIK AG", "Invoice ZM-88120", "Purchase order PO-27705", "Quantity 12",
                                                  "Unit price CHF 140.00", "VAT 8.1%", f"Total due CHF {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-27705", "Supplier Zimmerli Messtechnik AG",
                                                               "Ordered quantity 12", "Unit price EUR 149.00"]))),
        ],
        payable=payable("ZM-88120", "Zimmerli Messtechnik AG", face, "CHF"),
        expect={"received_quantity": None, "currency": None, "conflict": "currency"},
        allowed=[{"mode": "escalate"}],
    )

    # G17 KRW won sign, zero minor units
    face = gross(200, "15000", "0.10", "KRW")
    add(
        id="G17-krw-symbol", slice="currency", meta={"currency": "KRW", "minor_units": 0},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["HANBIT OPTICS CO., LTD.", "Invoice HO-2610-044", "PO number PO-66120", "Quantity 200 EA",
                                                  "Unit price ₩15,000", "VAT 10%", f"Total amount ₩{en(face, 0)}"]), ttf=True)),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-66120", "Supplier Hanbit Optics Co., Ltd.",
                                                               "Ordered quantity 200 EA", "Unit price KRW 15000"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-HO-8", "Purchase order PO-66120", "Received quantity 196", "Unit EA"]))),
        ],
        payable=payable("HO-2610-044", "Hanbit Optics Co., Ltd.", face, "KRW"),
        expect={"invoice_quantity": "200", "ordered_quantity": "200", "received_quantity": "196", "unit_price": "15000",
                "invoice_face_amount": face, "currency": "KRW"},
        allowed=[{"mode": "partial", "quantity": "196"}],
    )

    # G18 SEK, Swedish number style (no-break space grouping, decimal comma)
    face = gross(50, "200.00", "0.25", "SEK")
    add(
        id="G18-sek-nbsp-grouping", slice="currency", meta={"currency": "SEK", "locale": "sv"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["BERGSTRÖM TRÄVAROR AB", "Invoice BT-50021", "Purchase order PO-35512", "Quantity 50 st",
                                                  "Unit price SEK 200,00", "VAT 25%", f"Total due SEK {sv(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-35512", "Supplier Bergström Trävaror AB",
                                                               "Ordered quantity 50", "Unit price SEK 200,00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-BT-5", "Purchase order PO-35512", "Received quantity 50", "Unit each"]))),
        ],
        payable=payable("BT-50021", "Bergström Trävaror AB", face, "SEK"),
        expect={"invoice_quantity": "50", "received_quantity": "50", "unit_price": "200.00", "invoice_face_amount": face, "currency": "SEK"},
        allowed=[{"mode": "partial", "quantity": "50"}],
    )

    # G19 dollar sign resolved by an explicit CAD declaration
    face = gross(16, "87.50", "0.13", "CAD")
    add(
        id="G19-cad-dollar-declared", slice="currency", meta={"currency": "CAD", "symbol": "$"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["LAURENTIDE OUTDOOR SUPPLY", "Invoice LOS-1207", "PO: PO-80419", "All amounts in CAD",
                                                  "Currency: CAD", "Quantity: 16", "Unit price: $87.50", "HST: 13%", f"Amount due: ${en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-80419", "Supplier: Laurentide Outdoor Supply",
                                                               "Ordered quantity: 16", "Unit price: CAD 87.50"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-LO-16", "Purchase order PO-80419", "Received quantity 12", "Unit each"]))),
        ],
        payable=payable("LOS-1207", "Laurentide Outdoor Supply", face, "CAD"),
        expect={"invoice_quantity": "16", "received_quantity": "12", "unit_price": "87.50", "invoice_face_amount": face, "currency": "CAD"},
        allowed=[{"mode": "partial", "quantity": "12"}],
    )

    # G20 BHD three decimals, de-style decimal comma
    face = gross(9, "120.500", "0.10", "BHD")
    add(
        id="G20-bhd-decimal-comma", slice="currency", meta={"currency": "BHD", "minor_units": 3, "locale": "decimal comma"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["DILMUN MARINE SERVICES W.L.L.", "Invoice DMS-0412", "Purchase order PO-71230", "Quantity 9",
                                                  "Unit price BHD 120,500", "VAT 10%", f"Total BHD {de(face, 3)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-71230", "Supplier Dilmun Marine Services WLL",
                                                               "Ordered quantity 9", "Unit price BHD 120.500", "Order total BHD 1,084.500"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-DM-1", "Purchase order PO-71230", "Received quantity 9", "Unit each"]))),
        ],
        payable=payable("DMS-0412", "Dilmun Marine Services W.L.L.", face, "BHD"),
        expect={"invoice_quantity": "9", "received_quantity": "9", "unit_price": "120.500", "invoice_face_amount": face, "currency": "BHD"},
        allowed=[{"mode": "partial", "quantity": "9"}],
    )

    # ---------------------------------------------------------------- multi-line layouts

    # G21 multi-line invoice; one row matches the single PO line by item code
    rows = [["AC-100", "Acoustic panel grey", "40", "18.00", "720.00"],
            ["AC-140", "Acoustic panel oak", "25", "26.00", "650.00"],
            ["FR-1", "Freight", "1", "45.00", "45.00"]]
    net = Decimal("1415.00")
    face = str(net + (net * Decimal("0.08")).quantize(Decimal("0.01")))
    inv = lines(["SØNDERGAARD ACOUSTICS APS", "Invoice SA-6610", "Purchase order PO-42207"]) + table(
        ["Item code", "Description", "Qty", "Unit price", "Amount"], rows, xs=[60, 140, 320, 380, 470], top=140) + [
        (380, 250, "Subtotal"), (470, 250, "1,415.00"), (380, 274, "Tax rate 8%"), (380, 298, f"Total due USD {en(face)}")]
    po = lines([BUYER, "Purchase order PO-42207", "Supplier Søndergaard Acoustics ApS"]) + table(
        ["Item code", "Description", "Qty ordered", "Unit price"], [["AC-140", "Acoustic panel oak", "25", "26.00"]], xs=[60, 140, 320, 420], top=140)
    add(
        id="G21-multiline-invoice-one-match", slice="multi_line", meta={"invoice_rows": 3, "binding": "item code"},
        files=[
            ("invoice.pdf", "invoice", pdf(inv)),
            ("purchase-order.pdf", "purchase_order", pdf(po)),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-SA-40", "Purchase order PO-42207", "Received quantity 24", "Unit each"]))),
        ],
        payable=payable("SA-6610", "Søndergaard Acoustics ApS", face),
        expect={"invoice_quantity": "25", "ordered_quantity": "25", "received_quantity": "24", "unit_price": "26.00", "invoice_face_amount": face},
        allowed=[{"mode": "partial", "quantity": "24"}],
    )

    # G22 multi-line purchase order; the single-line invoice names one item
    face = gross(150, "4.20", "0.0625")
    po = lines([BUYER, "Purchase order PO-58816", "Supplier Ridgeway Horticulture LLC"]) + table(
        ["SKU", "Description", "Quantity", "Unit price"],
        [["RH-01", "Terracotta pot 20 cm", "300", "2.10"], ["RH-07", "Glazed pot 30 cm", "150", "4.20"], ["RH-12", "Saucer 30 cm", "150", "0.90"]],
        xs=[60, 140, 330, 420], top=140)
    add(
        id="G22-multiline-po-one-match", slice="multi_line", meta={"po_rows": 3, "binding": "SKU"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["RIDGEWAY HORTICULTURE LLC", "Invoice RH-90044", "Purchase order PO-58816", "SKU: RH-07",
                                                  "Description: Glazed pot 30 cm", "Quantity: 150", "Unit price: USD 4.20", "Tax rate: 6.25%",
                                                  f"Total due: USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(po)),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-RH-22", "Purchase order PO-58816", "Received quantity 140", "Unit each"]))),
        ],
        payable=payable("RH-90044", "Ridgeway Horticulture LLC", face),
        expect={"invoice_quantity": "150", "ordered_quantity": "150", "received_quantity": "140", "unit_price": "4.20", "invoice_face_amount": face},
        allowed=[{"mode": "partial", "quantity": "140"}],
    )

    # G23 two items on both invoice and PO: no single line, never summed
    inv = lines(["CORVINA LIGHTING SPA", "Invoice CL-3381", "Purchase order PO-29934"]) + table(
        ["Item code", "Description", "Qty", "Unit price", "Amount"],
        [["CL-A1", "Pendant lamp", "10", "95.00", "950.00"], ["CL-B4", "Wall lamp", "20", "40.00", "800.00"]],
        xs=[60, 140, 320, 380, 470], top=140) + [(380, 230, "Tax rate 0%"), (380, 254, "Total due USD 1,750.00")]
    po = lines([BUYER, "Purchase order PO-29934", "Supplier Corvina Lighting SpA"]) + table(
        ["Item code", "Description", "Qty", "Unit price"],
        [["CL-A1", "Pendant lamp", "10", "95.00"], ["CL-B4", "Wall lamp", "20", "40.00"]], xs=[60, 140, 320, 420], top=140)
    add(
        id="G23-two-shared-lines", slice="multi_line", meta={"rows": 2, "binding": "ambiguous"},
        files=[
            ("invoice.pdf", "invoice", pdf(inv)),
            ("purchase-order.pdf", "purchase_order", pdf(po)),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-CL-9", "Purchase order PO-29934", "Received quantity 30", "Unit each"]))),
        ],
        payable=payable("CL-3381", "Corvina Lighting SpA", "1750.00"),
        expect={"invoice_quantity": None, "ordered_quantity": None},
        allowed=[{"mode": "escalate"}, {"mode": "hold"}],
    )

    # G24 unit in the quantity column heading, description binding
    face = gross(36, "12.75", "0.05")
    inv = lines(["PALOMAR BINDERY", "Invoice PB-7781", "Purchase order PO-63370"]) + table(
        ["Description", "Qty (pcs)", "Rate", "Amount"], [["Linen notebook A5", "36", "12.75", "459.00"]], xs=[60, 260, 340, 440], top=140) + [
        (340, 200, "Tax 5%"), (340, 224, f"Amount due USD {en(face)}")]
    add(
        id="G24-unit-in-heading", slice="multi_line", meta={"layout": "unit in column heading"},
        files=[
            ("invoice.pdf", "invoice", pdf(inv)),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-63370", "Supplier Palomar Bindery",
                                                               "Ordered quantity 36 pcs", "Unit price USD 12.75"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-PB-6", "Purchase order PO-63370", "Received quantity 36", "Unit pcs"]))),
        ],
        payable=payable("PB-7781", "Palomar Bindery", face),
        expect={"invoice_quantity": "36", "ordered_quantity": "36", "received_quantity": "36", "unit_price": "12.75", "invoice_face_amount": face, "unit": "each"},
        allowed=[{"mode": "partial", "quantity": "36"}],
    )

    # ---------------------------------------------------------------- CSV dialects

    # G25 semicolon CSV invoice with decimal commas
    face = gross(60, "12.40", "0.21", "EUR")
    add(
        id="G25-csv-semicolon-decimal-comma", slice="csv_dialect", meta={"dialect": "semicolon", "locale": "decimal comma"},
        files=[
            ("invoice.csv", "invoice", csv_bytes([["Supplier", "Vermeulen Drukwerk BV"], ["Invoice", "VD-26-0098"], ["Purchase order", "PO-37741"],
                                                  ["Quantity", "60"], ["Unit price", "EUR 12,40"], ["VAT rate", "21%"], ["Total", f"EUR {de(face)}"]], ";")),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-37741", "Supplier Vermeulen Drukwerk B.V.",
                                                               "Ordered quantity 60", "Unit price EUR 12.40"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-VD-60", "Purchase order PO-37741", "Received quantity 55", "Unit each"]))),
        ],
        payable=payable("VD-26-0098", "Vermeulen Drukwerk BV", face, "EUR"),
        expect={"invoice_quantity": "60", "unit_price": "12.40", "invoice_face_amount": face, "currency": "EUR", "received_quantity": "55", "tax_rate": "0.21"},
        allowed=[{"mode": "partial", "quantity": "55"}],
    )

    # G26 tab-separated receipt table
    face = gross(90, "5.60", "0")
    add(
        id="G26-tsv-receipt-table", slice="csv_dialect", meta={"dialect": "tab", "receipt": "table"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["ASHGROVE CANDLE COMPANY", "Invoice ACC-4402", "Purchase order PO-12276", "Quantity 90",
                                                  "Unit price USD 5.60", "Tax rate 0%", f"Invoice total USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-12276", "Supplier Ashgrove Candle Company",
                                                               "Ordered quantity 90", "Unit price USD 5.60"]))),
            ("receipt.csv", "receipt", csv_bytes([["GRN", "Purchase order", "Description", "Qty received", "UoM"],
                                                  ["GRN-AC-0090", "PO-12276", "Soy candle, cedar", "84", "each"]], "\t")),
        ],
        payable=payable("ACC-4402", "Ashgrove Candle Company", face),
        expect={"received_quantity": "84", "invoice_quantity": "90", "ordered_quantity": "90", "unit": "each"},
        allowed=[{"mode": "partial", "quantity": "84"}],
    )

    # G27 pipe-delimited purchase order
    face = gross(150, "7.10", "0.0475")
    add(
        id="G27-pipe-po", slice="csv_dialect", meta={"dialect": "pipe"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["KESTREL JANITORIAL SUPPLY", "Invoice KJS-1150", "Purchase order PO-44718", "Quantity 150",
                                                  "Unit price USD 7.10", "Tax rate 4.75%", f"Total due USD {en(face)}"]))),
            ("purchase-order.csv", "purchase_order", csv_bytes([["Purchase order", "PO-44718"], ["Supplier", "Kestrel Janitorial Supply"],
                                                               ["Ordered quantity", "150"], ["Unit price", "USD 7.10"]], "|")),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-KJ-15", "Purchase order PO-44718", "Received quantity 150", "Unit each"]))),
        ],
        payable=payable("KJS-1150", "Kestrel Janitorial Supply", face),
        expect={"ordered_quantity": "150", "supplier": "Kestrel Janitorial Supply", "invoice_quantity": "150", "received_quantity": "150"},
        allowed=[{"mode": "partial", "quantity": "150"}],
    )

    # G28 ambiguous dialect: every row splits on both ';' and ',' -> refused
    face = gross(10, "9.00", "0")
    raw = "\r\n".join(["Supplier;Tamsin Craft, Ltd", "Invoice;TC-118, page 1", "Purchase order;PO-55410, rev A",
                       "Quantity;10, boxed", "Total;USD 90.00, net"]).encode() + b"\r\n"
    add(
        id="G28-csv-ambiguous-dialect", slice="csv_dialect", meta={"dialect": "ambiguous ; and ,"},
        files=[
            ("invoice.csv", "invoice", raw),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-55410", "Supplier Tamsin Craft Ltd",
                                                               "Ordered quantity 10", "Unit price USD 9.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-TC-1", "Purchase order PO-55410", "Received quantity 10", "Unit each"]))),
        ],
        payable=payable("TC-118", "Tamsin Craft Ltd", face),
        expect={"invoice_quantity": None},
        allowed=[{"mode": "hold"}, {"mode": "escalate"}],
    )

    # G29 XLSX receipt with a header table in pcs
    face = gross(48, "15.00", "0.08")
    add(
        id="G29-xlsx-receipt-pcs", slice="csv_dialect", meta={"format": "xlsx receipt table", "unit": "pcs"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["MERIDALE GARDEN TOOLS", "Invoice MGT-208", "Purchase order PO-60917", "Quantity 48 pcs",
                                                  "Unit price USD 15.00", "Tax rate 8%", f"Invoice total USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-60917", "Supplier Meridale Garden Tools",
                                                               "Ordered quantity 48 pcs", "Unit price USD 15.00"]))),
            ("receipt.xlsx", "receipt", xlsx_bytes([["Goods receipt", "Item", "Received qty", "Unit"], ["GR-MG-48", "Pruning shears", "46", "pcs"]])),
        ],
        payable=payable("MGT-208", "Meridale Garden Tools", face),
        expect={"received_quantity": "46", "unit": "each"},
        allowed=[{"mode": "partial", "quantity": "46"}],
    )

    # ---------------------------------------------------------------- scans

    # G30 scanned thermal receipt
    face = gross(88, "6.25", "0")
    add(
        id="G30-scan-thermal-receipt", slice="scan", meta={"scan": "receipt", "unit": "pcs"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["THORNBURY HARDWARE", "Invoice TH-7713", "Purchase order PO-31907", "Quantity 88 pcs",
                                                  "Unit price USD 6.25", "Tax rate 0%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-31907", "Supplier Thornbury Hardware",
                                                               "Ordered quantity 88 pcs", "Unit price USD 6.25"]))),
            ("receipt-scan.pdf", "receipt", scan(["GOODS IN SLIP", "GRN 50777", "PO PO-31907", "QTY RECEIVED 88 PCS", "Checked by dock team"],
                                                 seed=71, angle=0.4, noise=0.4, blur=0.3)),
        ],
        payable=payable("TH-7713", "Thornbury Hardware", face),
        expect={"received_quantity": "88", "ocr_or_unresolved": ["received_quantity"]},
        allowed=[{"mode": "partial", "quantity": "88"}, {"mode": "hold"}],
    )

    # G31 scanned EUR invoice with suffix currency
    face = gross(120, "9.50", "0.20", "EUR")
    add(
        id="G31-scan-eur-invoice", slice="scan", meta={"scan": "invoice", "currency": "EUR"},
        files=[
            ("invoice-scan.pdf", "invoice", scan(["ATELIER DUVAL SARL", "Invoice AD-60117", "Purchase order PO-28830", "Quantity 120",
                                                   "Unit price 9.50 EUR", "VAT 20%", f"Total due {en(face)} EUR"], seed=5, angle=0.3, noise=0.3)),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-28830", "Supplier Atelier Duval SARL",
                                                               "Ordered quantity 120", "Unit price EUR 9.50"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-AD-12", "Purchase order PO-28830", "Received quantity 110", "Unit each"]))),
        ],
        payable=payable("AD-60117", "Atelier Duval SARL", face, "EUR"),
        expect={"invoice_quantity": "120", "unit_price": "9.50", "invoice_face_amount": face, "currency": "EUR",
                "ocr_or_unresolved": ["invoice_quantity", "unit_price", "invoice_face_amount", "currency", "tax_rate"]},
        allowed=[{"mode": "partial", "quantity": "110"}, {"mode": "hold"}, {"mode": "escalate"}],
    )

    # G32 scanned invoice whose quantity sits beside a stray small number
    face = gross(120, "48.00", "0.08")
    add(
        id="G32-scan-quantity-trap", slice="scan", meta={"scan": "invoice", "trap": "payment terms 7 days, line 7"},
        files=[
            ("invoice-scan.pdf", "invoice", scan(["ELDERFIELD MACHINE PARTS", "Invoice EMP-4471", "Purchase order PO-91150", "Payment terms 7 days",
                                                   "Line 7   Quantity 120", "Unit price USD 48.00", "Tax rate 8%", f"Invoice total USD {en(face)}"],
                                                  seed=19, angle=0.5, noise=0.5, blur=0.4)),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-91150", "Supplier Elderfield Machine Parts",
                                                               "Line 7", "Ordered quantity 120", "Unit price USD 48.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-EM-77", "Purchase order PO-91150", "Line 7", "Received quantity 100", "Unit each"]))),
        ],
        payable=payable("EMP-4471", "Elderfield Machine Parts", face),
        expect={"invoice_quantity": "120", "invoice_face_amount": face,
                "ocr_or_unresolved": ["invoice_quantity", "unit_price", "invoice_face_amount", "tax_rate", "invoice", "currency"]},
        allowed=[{"mode": "partial", "quantity": "100"}, {"mode": "hold"}, {"mode": "escalate"}],
    )

    # ---------------------------------------------------------------- precision traps

    # G33 net, VAT, total, paid and balance lines that all differ
    face = gross(55, "19.90", "0.10")
    net = en(Decimal(55) * Decimal("19.90"))
    tax = en((Decimal(55) * Decimal("19.90") * Decimal("0.10")).quantize(Decimal("0.01")))
    add(
        id="G33-totals-ladder", slice="precision", meta={"trap": "subtotal/tax/total/paid/balance"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["GRANITE PEAK OUTFITTERS", "Invoice GPO-3305", "Purchase order PO-70081", "Quantity 55",
                                                  "Unit price USD 19.90", f"Subtotal USD {net}", f"Tax amount USD {tax}", "Tax rate 10%",
                                                  f"Invoice total USD {en(face)}", "Amount paid USD 0.00", f"Balance due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-70081", "Supplier Granite Peak Outfitters",
                                                               "Ordered quantity 55", "Unit price USD 19.90"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-GP-55", "Purchase order PO-70081", "Received quantity 50", "Unit each"]))),
        ],
        payable=payable("GPO-3305", "Granite Peak Outfitters", face),
        expect={"invoice_face_amount": face, "unit_price": "19.90", "tax_rate": "0.1", "invoice_quantity": "55"},
        allowed=[{"mode": "partial", "quantity": "50"}],
    )

    # G34 the only "Total" is the pre-tax subtotal; the payable figure is unlabelled
    add(
        id="G34-total-is-subtotal", slice="precision", meta={"trap": "TOTAL before VAT, unlabelled payable"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["BLACKWATER SIGNS", "Invoice BWS-910", "Purchase order PO-33190", "Quantity 100",
                                                  "Unit price USD 48.00", "TOTAL USD 4,800.00", "VAT 8%", "VAT USD 384.00", "TO PAY USD 5,184.00"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-33190", "Supplier Blackwater Signs",
                                                               "Ordered quantity 100", "Unit price USD 48.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-BW-1", "Purchase order PO-33190", "Received quantity 100", "Unit each"]))),
        ],
        payable=payable("BWS-910", "Blackwater Signs", "5184.00"),
        expect={"invoice_face_amount": None},
        allowed=[{"mode": "escalate"}, {"mode": "hold"}],
    )

    # G35 legitimate freight line: face differs from qty x price x tax, still readable
    face = str(Decimal(gross(40, "25.00", "0.05")) + Decimal("35.00"))
    add(
        id="G35-freight-line", slice="precision", meta={"trap": "freight changes face"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["HOLLINS POTTERY SUPPLY", "Invoice HPS-552", "Purchase order PO-20983", "Quantity 40",
                                                  "Unit price USD 25.00", "Tax rate 5%", "Freight USD 35.00", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-20983", "Supplier Hollins Pottery Supply",
                                                               "Ordered quantity 40", "Unit price USD 25.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-HP-4", "Purchase order PO-20983", "Received quantity 36", "Unit each"]))),
        ],
        payable=payable("HPS-552", "Hollins Pottery Supply", face),
        expect={"invoice_face_amount": face, "invoice_quantity": "40"},
        allowed=[{"mode": "partial", "quantity": "36"}],
    )

    # ---------------------------------------------------------------- decision order / controls

    # G36 explicit issuer differs from the PO supplier, and the receipt is missing
    face = gross(30, "15.00", "0")
    add(
        id="G36-supplier-conflict-missing-receipt", slice="decision_order", meta={"conflict": "supplier", "missing": "receipt"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["INVOICE", "Supplier: Quayside Trading Partners", "Invoice QTP-1010", "Purchase order PO-47730",
                                                  "Quantity 30", "Unit price USD 15.00", "Tax rate 0%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-47730", "Supplier Harbourline Provisions",
                                                               "Ordered quantity 30", "Unit price USD 15.00"]))),
        ],
        payable=payable("QTP-1010", "Harbourline Provisions", face),
        expect={"received_quantity": None, "conflict": "supplier"},
        allowed=[{"mode": "escalate"}],
    )

    # G37 receipt for another purchase order: missing support, hold
    face = gross(70, "3.30", "0")
    add(
        id="G37-receipt-other-po", slice="decision_order", meta={"missing": "receipt for this PO"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["WILLOWMERE STATIONERS", "Invoice WS-2280", "Purchase order PO-66001", "Quantity 70",
                                                  "Unit price USD 3.30", "Tax rate 0%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-66001", "Supplier Willowmere Stationers",
                                                               "Ordered quantity 70", "Unit price USD 3.30"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-WS-9", "Purchase order PO-66019", "Received quantity 70", "Unit each"]))),
        ],
        payable=payable("WS-2280", "Willowmere Stationers", face),
        expect={"conflict": "purchase_order"},
        allowed=[{"mode": "hold"}, {"mode": "escalate"}],
    )

    # G38 control: ordinary USD packet with units on every document
    face = gross(25, "120.00", "0.0875")
    add(
        id="G38-control-units-everywhere", slice="units", meta={"control": True},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["CALDERA OFFICE SEATING", "Invoice COS-7001", "Purchase order PO-10770", "Quantity 25 units",
                                                  "Unit price USD 120.00", "Tax rate 8.75%", f"Invoice total USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-10770", "Supplier Caldera Office Seating",
                                                               "Ordered quantity 25 units", "Unit price USD 120.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-CO-25", "Purchase order PO-10770", "Received quantity 25", "Unit each"]))),
        ],
        payable=payable("COS-7001", "Caldera Office Seating", face, recognized="25"),
        expect={"invoice_quantity": "25", "ordered_quantity": "25", "received_quantity": "25", "unit": "each", "currency": "USD", "invoice_face_amount": face},
        allowed=[{"mode": "no_change"}],
    )

    # G39 pallets, with the unit inside the receipt label
    face = gross(4, "310.00", "0")
    add(
        id="G39-pallets", slice="units", meta={"unit": "pallets"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["STAVANGER BLOCKWORKS", "Invoice SB-1904", "Purchase order PO-38012", "Quantity 4 pallets",
                                                  "Unit price USD 310.00", "Tax rate 0%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-38012", "Supplier Stavanger Blockworks",
                                                               "Ordered quantity 4 pallets", "Unit price USD 310.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Delivery receipt", "Receipt no. DR-38012-1", "PO-38012", "Pallets received: 3",
                                                  "One pallet damaged, refused at gate."]))),
        ],
        payable=payable("SB-1904", "Stavanger Blockworks", face),
        expect={"invoice_quantity": "4", "ordered_quantity": "4", "received_quantity": "3", "unit": "pallet"},
        allowed=[{"mode": "partial", "quantity": "3"}],
    )

    # G40 receipt in kg against an invoice in each: unit conflict escalates
    face = gross(50, "4.00", "0")
    add(
        id="G40-receipt-kg-invoice-each", slice="units", meta={"conflict": "unit"},
        files=[
            ("invoice.pdf", "invoice", pdf(lines(["PRAIRIE SEED HOUSE", "Invoice PSH-610", "Purchase order PO-25075", "Quantity 50",
                                                  "Unit price USD 4.00", "Tax rate 0%", f"Total due USD {en(face)}"]))),
            ("purchase-order.pdf", "purchase_order", pdf(lines([BUYER, "Purchase order PO-25075", "Supplier Prairie Seed House",
                                                               "Ordered quantity 50 bags", "Unit price USD 4.00"]))),
            ("receipt.pdf", "receipt", pdf(lines([BUYER, "Goods receipt GR-PS-50", "Purchase order PO-25075", "Received quantity 1250", "Unit kg"]))),
        ],
        payable=payable("PSH-610", "Prairie Seed House", face),
        expect={"unit": None, "conflict": "unit"},
        allowed=[{"mode": "escalate"}],
    )
    return P


def main():
    packets = build()
    (SET / "packets").mkdir(parents=True, exist_ok=True)
    manifest = {
        "set": "generality-r7",
        "license": "CC0-1.0",
        "authorship": "Author-created synthetic packets for Folio round 7 (tests/fixtures/generality/build_fixtures.py); fictional companies; independent of eval/packets.py and of every sealed set",
        "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tasks": [],
    }
    labels = {"set": "generality-r7", "tasks": {}}
    for p in packets:
        folder = SET / "packets" / p["id"]
        folder.mkdir(parents=True, exist_ok=True)
        entry = {"task_id": p["id"], "slice": p["slice"], "files": []}
        for name, role, raw in p["files"]:
            (folder / name).write_bytes(raw)
            entry["files"].append({"name": name, "document_role": role, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        (folder / "payable.json").write_text(json.dumps(p["payable"], indent=2))
        manifest["tasks"].append(entry)
        labels["tasks"][p["id"]] = {"slice": p["slice"], "allowed": p["allowed"], "meta": p["meta"], "expect": p["expect"]}
    (SET / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (HERE / "labels.json").write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"packets": len(packets)}))


if __name__ == "__main__":
    main()
