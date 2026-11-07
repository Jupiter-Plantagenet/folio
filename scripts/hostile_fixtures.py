"""Deterministic hostile parser/provenance fixtures (DOC-T-PROVENANCE, DOC-T-PARSER).

Every byte is authored here: PDFs are written object by object (no PDF library),
CSVs by an explicit serializer, XLSX packages entry by entry. Ground truth is
recorded at generation time:

* PDF text: every glyph's em box (origin, advance width, font descent/size) in
  PDF user space, mapped through the page /Rotate and effective CropBox to the
  displayed crop frame (top-left origin, points). Text drawn outside the
  CropBox is recorded as hidden and must never be extracted.
* Raster (OCR) text: each word's tight ink box measured from its own raster,
  mapped through the image placement matrix, page rotation and crop.
* CSV: each field's raw byte range under its declared encoding.
* XLSX: sheet names/IDs and raw cell/formula/cached values.

Usage: python scripts/hostile_fixtures.py [--check]
--check regenerates in memory and fails if any committed byte differs.
"""

import hashlib
import io
import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures/hostile"
GENERATOR = "scripts/hostile_fixtures.py"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

# ----------------------------------------------------------------- fonts

def _helvetica_widths():
    from reportlab.pdfbase import pdfmetrics

    return list(pdfmetrics.getFont("Helvetica").widths)  # WinAnsi, 256 codes


HELV = _helvetica_widths()
DESCENT = -207  # Helvetica AFM descent, also declared in the custom descriptors

# Hostile glyph table: unicode scalar -> (font, code, width). FH/FM are
# non-standard fonts so their /Widths and /ToUnicode are authoritative.
HOSTILE = {
    "\u200b": ("FH", 0x01, 0),  # ZERO WIDTH SPACE
    "\u200d": ("FH", 0x02, 0),  # ZERO WIDTH JOINER
    "\ufeff": ("FH", 0x03, 0),  # ZERO WIDTH NO-BREAK SPACE / BOM
    "\u202e": ("FH", 0x04, 0),  # RIGHT-TO-LEFT OVERRIDE
    "\u2066": ("FH", 0x05, 0),  # LEFT-TO-RIGHT ISOLATE
    "\u2067": ("FH", 0x06, 0),  # RIGHT-TO-LEFT ISOLATE
    "\u2068": ("FH", 0x07, 0),  # FIRST STRONG ISOLATE
    "\u2069": ("FH", 0x08, 0),  # POP DIRECTIONAL ISOLATE
    "\u202c": ("FH", 0x09, 0),  # POP DIRECTIONAL FORMATTING
    "\u0301": ("FH", 0xB4, 0),  # COMBINING ACUTE ACCENT (acute glyph, zero advance)
    "\u0406": ("FH", 0x49, HELV[0x49]),  # CYRILLIC CAPITAL I (looks like I)
    "\u0410": ("FH", 0x41, HELV[0x41]),  # CYRILLIC CAPITAL A
    "\u0435": ("FH", 0x65, HELV[0x65]),  # CYRILLIC SMALL IE (looks like e)
    "\u043e": ("FH", 0x6F, HELV[0x6F]),  # CYRILLIC SMALL O
    "\u0660": ("FH", 0x30, HELV[0x30]),  # ARABIC-INDIC DIGIT ZERO (drawn as 0)
    "\u0662": ("FH", 0x32, HELV[0x32]),  # ARABIC-INDIC DIGIT TWO (drawn as 2)
    "\u0668": ("FH", 0x38, HELV[0x38]),  # ARABIC-INDIC DIGIT EIGHT (drawn as 8)
    "\ufb01": ("FH", 0x1E, HELV[0x66] + HELV[0x69]),  # LATIN SMALL LIGATURE FI
    "\U0001f4b5": ("FM", 0x24, 1000),  # BANKNOTE WITH DOLLAR SIGN (supplementary)
}
for _i in range(26):  # MATHEMATICAL BOLD A-Z / a-z (supplementary plane)
    HOSTILE[chr(0x1D400 + _i)] = ("FM", 0x41 + _i, HELV[0x41 + _i])
    HOSTILE[chr(0x1D41A + _i)] = ("FM", 0x61 + _i, HELV[0x61 + _i])
# Code points that cannot be stored or encoded downstream; ToUnicode emits them
# raw (lone surrogate, NUL). Kept out of HOSTILE string encoding on purpose.
RAW_TOUNICODE = {"FH": {0x0E: "D800", 0x0F: "0000"}}


def _tounicode(font):
    entries = [
        (code, ch.encode("utf-16-be").hex().upper())
        for ch, (f, code, _) in HOSTILE.items()
        if f == font
    ]
    entries += sorted(RAW_TOUNICODE.get(font, {}).items())
    body = "\n".join(f"<{code:02X}> <{dst}>" for code, dst in sorted(entries))
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        f"/CMapName /Folio-{font}-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        f"{len(entries)} beginbfchar\n{body}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode()


def _font_widths(font):
    widths = [500] * 256
    for _ch, (f, code, width) in HOSTILE.items():
        if f == font:
            widths[code] = width
    return widths


def font_width(font, code):
    return HELV[code] if font == "F1" else _font_widths(font)[code]


def encode_runs(text):
    """Split text into (font, codes, chars) runs; ASCII uses standard Helvetica."""
    runs = []
    for ch in text:
        if " " <= ch <= "~":
            font, code = "F1", ord(ch)
        elif ch in HOSTILE:
            font, code, _ = HOSTILE[ch]
        elif len(ch.encode("cp1252", errors="ignore")) == 1:
            font, code = "F1", ch.encode("cp1252")[0]  # WinAnsi Latin-1 letters
        else:
            raise ValueError(f"no glyph for U+{ord(ch):04X}")
        if runs and runs[-1][0] == font:
            runs[-1][1].append(code)
            runs[-1][2].append(ch)
        else:
            runs.append((font, [code], [ch]))
    return runs


# ----------------------------------------------------------------- PDF writer

class Pdf:
    def __init__(self):
        self.objects = []

    def reserve(self):
        self.objects.append(None)
        return len(self.objects)

    def add(self, body):
        self.objects.append(body)
        return len(self.objects)

    def set(self, number, body):
        self.objects[number - 1] = body

    def stream(self, data, extra=b"", compress=True):
        if compress:
            data = zlib.compress(data, 9)
            extra += b" /Filter /FlateDecode"
        return self.add(
            b"<< /Length %d%s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"
        )

    def build(self, root, trailer_extra=b""):
        out = io.BytesIO()
        out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(self.objects, 1):
            offsets.append(out.tell())
            out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
        xref = out.tell()
        out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(self.objects) + 1))
        for off in offsets:
            out.write(b"%010d 00000 n \n" % off)
        out.write(
            b"trailer\n<< /Size %d /Root %d 0 R%s >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(self.objects) + 1, root, trailer_extra, xref)
        )
        return out.getvalue()


def _num(v):
    text = f"{v:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def _box(box):
    return ("[" + " ".join(_num(v) for v in box) + "]").encode()


def page_affine(rotate, crop):
    """User space -> displayed crop frame (top-left origin, points).

    Returns (a, b, c, d, e, f) with X = a*x + c*y + e, Y = b*x + d*y + f,
    and the displayed (width, height)."""
    cx0, cy0, cx1, cy1 = crop
    w, h = cx1 - cx0, cy1 - cy0
    if rotate == 0:
        return (1, 0, 0, -1, -cx0, cy1), (w, h)
    if rotate == 90:
        return (0, 1, 1, 0, -cy0, -cx0), (h, w)
    if rotate == 180:
        return (-1, 0, 0, 1, cx1, -cy0), (w, h)
    if rotate == 270:
        return (0, -1, -1, 0, cy1, cx1), (h, w)
    raise ValueError(rotate)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def map_box(m, x0, y0, x1, y1):
    pts = [apply(m, x, y) for x in (x0, x1) for y in (y0, y1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def inside(crop, x, y):
    return crop[0] <= x <= crop[2] and crop[1] <= y <= crop[3]


class Page:
    def __init__(self, media, crop=None, rotate=0):
        self.media, self.crop, self.rotate = media, crop, rotate
        self.ops = []
        self.chars = []  # (unicode, user box)
        self.images = {}
        self.raster_words = []  # (text, user box)

    def text(self, x, y, size, text):
        ops = [b"BT 1 0 0 1 %s %s Tm" % (_num(x).encode(), _num(y).encode())]
        pen = x
        for font, codes, chars in encode_runs(text):
            ops.append(
                b"/%s %s Tf <%s> Tj"
                % (font.encode(), _num(size).encode(), bytes(codes).hex().upper().encode())
            )
            for code, ch in zip(codes, chars):
                adv = font_width(font, code) * size / 1000
                bottom = y + DESCENT * size / 1000
                self.chars.append((ch, [pen, bottom, pen + adv, bottom + size]))
                pen += adv
        self.chars.append(("\n", None))
        ops.append(b"ET")
        self.ops.append(b" ".join(ops))

    def raw_codes(self, x, y, size, font, codes):
        """Draw codes without ground truth (lone surrogate / NUL mappings)."""
        self.ops.append(
            b"BT 1 0 0 1 %s %s Tm /%s %s Tf <%s> Tj ET"
            % (_num(x).encode(), _num(y).encode(), font.encode(), _num(size).encode(), bytes(codes).hex().upper().encode())
        )

    def image(self, name, gray, matrix, words):
        """gray: PIL 'L' image; matrix maps the image unit square to user space."""
        self.images[name] = gray
        self.ops.append(
            b"q " + b" ".join(_num(v).encode() for v in matrix) + b" cm /%s Do Q" % name.encode()
        )
        a, b, c, d, e, f = matrix
        wi, hi = gray.size
        for text, (px0, py0, px1, py1) in words:
            corners = []
            for px, py in ((px0, py0), (px1, py0), (px0, py1), (px1, py1)):
                u, v = px / wi, 1 - py / hi
                corners.append((a * u + c * v + e, b * u + d * v + f))
            xs, ys = [p[0] for p in corners], [p[1] for p in corners]
            self.raster_words.append((text, [min(xs), min(ys), max(xs), max(ys)]))

    def effective_crop(self):
        if self.crop is None:
            return list(self.media)
        m, c = self.media, self.crop
        return [max(m[0], c[0]), max(m[1], c[1]), min(m[2], c[2]), min(m[3], c[3])]

    def truth(self):
        crop = self.effective_crop()
        m, (w, h) = page_affine(self.rotate, crop)
        visible, hidden = [], []
        for ch, box in self.chars:
            if box is None:  # end of a drawn line: a word boundary, not a glyph
                visible.append({"char": " ", "box": None})
                continue
            x0, y0, x1, y1 = box
            target = visible if inside(crop, (x0 + x1) / 2, (y0 + y1) / 2) else hidden
            target.append({"char": ch, "box": [round(v, 4) for v in map_box(m, x0, y0, x1, y1)]})
        words, current = [], None
        for item in visible:
            if item["char"] == " ":
                current = None
                continue
            if current is None:
                current = {"text": "", "chars": []}
                words.append(current)
            current["text"] += item["char"]
            current["chars"].append(item)
        raster = []
        for text, (x0, y0, x1, y1) in self.raster_words:
            entry = {"text": text, "box": [round(v, 4) for v in map_box(m, x0, y0, x1, y1)]}
            (raster if inside(crop, (x0 + x1) / 2, (y0 + y1) / 2) else hidden).append(entry)
        return {
            "frame": "displayed CropBox, top-left origin, PDF points (folio-crop-frame-v1)",
            "rotate": self.rotate,
            "mediabox": list(self.media),
            "cropbox": crop,
            "user_to_frame": list(m),
            "width": w,
            "height": h,
            "words": [
                {"text": wd["text"], "boxes": [c["box"] for c in wd["chars"]], "chars": [c["char"] for c in wd["chars"]]}
                for wd in words
            ],
            "ocr_words": raster,
            "hidden": hidden,
        }


def build_pdf(pages, extra_catalog=b"", trailer_extra=b""):
    pdf = Pdf()
    catalog = pdf.reserve()
    tree = pdf.reserve()
    f1 = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    fonts = {"F1": f1}
    for name, base in (("FH", b"FolioHostileSans"), ("FM", b"FolioMathSans")):
        desc = pdf.add(
            b"<< /Type /FontDescriptor /FontName /%s /Flags 32 /FontBBox [-166 -225 1000 931]"
            b" /ItalicAngle 0 /Ascent 718 /Descent %d /CapHeight 718 /StemV 88 >>" % (base, DESCENT)
        )
        cmap = pdf.stream(_tounicode(name))
        widths = b" ".join(b"%d" % w for w in _font_widths(name))
        fonts[name] = pdf.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /FirstChar 0 /LastChar 255"
            b" /Widths [%s] /Encoding /WinAnsiEncoding /FontDescriptor %d 0 R /ToUnicode %d 0 R >>"
            % (base, widths, desc, cmap)
        )
    font_dict = b"<< " + b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in fonts.items()) + b" >>"
    kids = []
    for page in pages:
        xobjects = []
        for name, gray in page.images.items():
            ref = pdf.stream(
                gray.tobytes(),
                b" /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 8"
                % gray.size,
            )
            xobjects.append(b"/%s %d 0 R" % (name.encode(), ref))
        content = pdf.stream(b"\n".join(page.ops) + b"\n")
        resources = b"<< /Font " + font_dict
        if xobjects:
            resources += b" /XObject << " + b" ".join(xobjects) + b" >>"
        resources += b" >>"
        body = b"<< /Type /Page /Parent %d 0 R /MediaBox %s" % (tree, _box(page.media))
        if page.crop is not None:
            body += b" /CropBox " + _box(page.crop)
        if page.rotate:
            body += b" /Rotate %d" % page.rotate
        body += b" /Resources " + resources + b" /Contents %d 0 R >>" % content
        kids.append(pdf.add(body))
    pdf.set(
        tree,
        b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids),
    )
    pdf.set(catalog, b"<< /Type /Catalog /Pages %d 0 R%s >>" % (tree, extra_catalog))
    return pdf.build(catalog, trailer_extra)


# ----------------------------------------------------------------- PDF fixtures

GEOM_LINES = [
    (14, "Invoice INV-1048"),
    (12, "Purchase order PO-0732"),
    (12, "Quantity 120"),
    (12, "Unit price USD 48.00"),
    (10, "Invoice face total USD 6220.80"),
]


def geometry_page(rotate, crop, origin=(0, 0)):
    ox, oy = origin
    media = [ox, oy, ox + 420, oy + 320]
    page = Page(media, None if crop is None else [ox + crop[0], oy + crop[1], ox + crop[2], oy + crop[3]], rotate)
    y = oy + 255
    for size, line in GEOM_LINES:
        page.text(ox + 70, y, size, line)
        y -= 28
    if crop is not None:
        # Hidden injection below the CropBox: present in the content stream,
        # invisible in any viewer. It must never become extracted evidence.
        page.text(ox + 70, oy + 12, 10, "Quantity 999 hidden outside cropbox")
    return page


def _raster(lines, size_px, font_px=44):
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=font_px)  # Aileron Regular (CC0) bundled with Pillow
    image = Image.new("L", size_px, 255)
    draw = ImageDraw.Draw(image)
    words = []
    for (x, y), line in lines:
        draw.text((x, y), line, font=font, fill=0)
        pen = x
        space = draw.textlength(" ", font=font)
        for word in line.split(" "):
            probe = Image.new("L", size_px, 255)
            ImageDraw.Draw(probe).text((pen, y), word, font=font, fill=0)
            box = probe.point(lambda v: 255 - v).getbbox()  # tight ink box
            words.append((word, box))
            pen += draw.textlength(word, font=font) + space
    return image, words


OCR_LINES = [
    ((200, 160), "Goods receipt GR-208"),
    ((200, 240), "Purchase order PO-0732"),
    ((200, 320), "Received quantity 100"),
    ((200, 400), "Unit each"),
]


def ocr_page(rotate, crop, orientation):
    """Whole-media raster (4 px/pt). orientation: 'upright' = display upright."""
    media = [0, 0, 420, 320]
    lines = list(OCR_LINES)
    if crop is not None:
        lines.append(((200, 1200), "Received quantity 999 hidden"))
    if orientation == "upright" and rotate in (90, 270):
        size = (1280, 1680)  # display frame is portrait
    else:
        size = (1680, 1280)
    image, words = _raster(lines, size)
    W, H = 420, 320
    if orientation == "upright":
        matrix = {
            0: (W, 0, 0, H, 0, 0),
            90: (0, H, -W, 0, W, 0),  # image right -> user up; image up -> user left
            180: (-W, 0, 0, -H, W, H),
            270: (0, -H, W, 0, 0, H),
        }[rotate]
    else:
        matrix = (W, 0, 0, H, 0, 0)  # upright in user space, sideways when rotated
    page = Page(media, crop, rotate)
    page.image("Im1", image, matrix, words)
    return page


def hostile_invoice(lines):
    page = Page([0, 0, 612, 400])
    y = 360
    for line in lines:
        page.text(60, y, 12, line)
        y -= 24
    return page


INVOICE_BASE = [
    "FOLIO AUTHOR-CREATED SYNTHETIC FIXTURE",
    "ASTER SUPPLY CO.",
    "Invoice INV-1048",
    "Purchase order PO-0732",
    "Quantity 120",
    "Unit price USD 48.00",
    "Tax rate 8%",
    "Invoice face total USD 6220.80",
]


def invoice_variant(**replace):
    lines = list(INVOICE_BASE)
    for index, line in replace.items():
        lines[int(index[1:])] = line
    return lines


def text_pages(count, label):
    pages = []
    for n in range(1, count + 1):
        page = Page([0, 0, 300, 200])
        page.text(30, 150, 12, f"{label} page {n}")
        pages.append(page)
    return pages


def form_bomb(depth=34):
    """Each Form XObject draws the next one twice: 2**depth no-op leaf draws."""
    pdf = Pdf()
    catalog = pdf.reserve()
    tree = pdf.reserve()
    leaf = pdf.stream(b"q Q\n", b" /Type /XObject /Subtype /Form /BBox [0 0 1 1]")
    prev = leaf
    for _ in range(depth):
        prev = pdf.stream(
            b"/X Do /X Do\n",
            b" /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /X %d 0 R >> >>" % prev,
        )
    content = pdf.stream(b"BT /F1 12 Tf 1 0 0 1 20 100 Tm (Form bomb) Tj ET\n/X Do\n")
    f1 = pdf.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page = pdf.add(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 %d 0 R >> /XObject << /X %d 0 R >> >> /Contents %d 0 R >>"
        % (tree, f1, prev, content)
    )
    pdf.set(tree, b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page)
    pdf.set(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % tree)
    return pdf.build(catalog)


def flate_bomb(megabytes=600):
    """One content stream inflating to `megabytes` MiB of spaces."""
    comp = zlib.compressobj(9)
    chunk = b" " * (1 << 20)
    data = b"".join(comp.compress(chunk) for _ in range(megabytes)) + comp.flush()
    pdf = Pdf()
    catalog = pdf.reserve()
    tree = pdf.reserve()
    content = pdf.add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
    page = pdf.add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 200 200] /Contents %d 0 R >>" % (tree, content))
    pdf.set(tree, b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page)
    pdf.set(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % tree)
    return pdf.build(catalog)


def deep_nesting(depth=50000):
    pdf = Pdf()
    catalog = pdf.reserve()
    tree = pdf.reserve()
    junk = pdf.add(b"[" * depth + b"]" * depth)
    page = pdf.add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 200 200] /Junk %d 0 R >>" % (tree, junk))
    pdf.set(tree, b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page)
    pdf.set(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % tree)
    return pdf.build(catalog)


# ----------------------------------------------------------------- CSV

CSV_ROWS = [
    ["supplier", "goods receipt", "line", "received quantity", "unit", "notes"],
    ["Société Générale Fournitures", "GR-208", "1", "100", "each", 'Zoë said "ok"\nsecond line'],
    ["Müller & Søn ApS", "GR-209", "2", "20", "each", "plain, with comma"],
]
CSV_UNICODE_ROWS = [
    ["supplier", "goods receipt", "line", "received quantity", "unit", "notes"],
    ["\U0001d400ster Supply Co.", "GR-208", "1", "1\u200b00", "each", "Cafe\u0301 \u202eevil\u202c \U0001f4b5"],
    ["\u0410ster Supply Co.", "GR-2\u200d09", "2", "\u0662\u0660", "each", "\u2066isolate\u2069"],
]


def serialize_csv(rows, encoding, bom, newline="\r\n", literal_quotes=False):
    """Return (bytes, truth cells with raw byte ranges).

    literal_quotes leaves a '"' that is not at a field start unquoted, which
    csv.reader (default dialect) keeps as a literal character."""
    text = ""
    spans = []
    for r, row in enumerate(rows, 1):
        for c, value in enumerate(row, 1):
            if c > 1:
                text += ","
            raw = value
            specials = ",\r\n" if literal_quotes and not value.startswith('"') else ',"\r\n'
            if any(ch in value for ch in specials) or value != value.strip():
                raw = '"' + value.replace('"', '""') + '"'
            spans.append((r, c, value, len(text), len(text) + len(raw)))
            text += raw
        text += newline
    codec = {"utf-8": "utf-8", "utf-16-le": "utf-16-le", "utf-16-be": "utf-16-be", "cp1252": "cp1252"}[encoding]
    prefix = {"utf-8": b"\xef\xbb\xbf", "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(encoding, b"") if bom else b""
    data = prefix + text.encode(codec)
    cells = []
    for r, c, value, s, e in spans:
        bs = len(prefix) + len(text[:s].encode(codec))
        be = len(prefix) + len(text[:e].encode(codec))
        cells.append({"row": r, "column": c, "value": value, "quote": text[s:e], "byte_start": bs, "byte_end": be})
    return data, cells


# ----------------------------------------------------------------- XLSX

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xml_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def sheet_xml(cells, prolog=""):
    """cells: list of (ref, kind, value, formula, cached)."""
    rows = {}
    for ref, kind, value, formula, cached in cells:
        row = int("".join(ch for ch in ref if ch.isdigit()))
        if kind == "s":
            node = f'<c r="{ref}" t="inlineStr"><is><t>{_xml_escape(value)}</t></is></c>'
        elif kind == "f":
            cache = "" if cached is None else f"<v>{_xml_escape(str(cached))}</v>"
            t = ' t="str"' if isinstance(cached, str) else ""
            node = f'<c r="{ref}"{t}><f>{_xml_escape(formula)}</f>{cache}</c>'
        else:
            node = f'<c r="{ref}"><v>{value}</v></c>'
        rows.setdefault(row, []).append(node)
    body = "".join(f'<row r="{r}">{"".join(nodes)}</row>' for r, nodes in sorted(rows.items()))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + prolog
        + f'<worksheet xmlns="{NS}" xmlns:r="{RNS}"><sheetData>{body}</sheetData></worksheet>'
    ).encode()


def build_xlsx(sheets, extra_entries=(), workbook_rels_extra="", content_extra="", sheet_rels=None):
    """sheets: list of (name, sheetId, xml bytes)."""
    entries = []
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    entries.append((
        "[Content_Types].xml",
        (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + overrides + content_extra + "</Types>"
        ).encode(),
    ))
    entries.append((
        "_rels/.rels",
        (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ).encode(),
    ))
    sheet_nodes = "".join(
        f'<sheet name="{_xml_escape(name)}" sheetId="{sid}" r:id="rId{i}"/>' for i, (name, sid, _) in enumerate(sheets, 1)
    )
    entries.append((
        "xl/workbook.xml",
        (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="{NS}" xmlns:r="{RNS}"><sheets>{sheet_nodes}</sheets></workbook>'
        ).encode(),
    ))
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    entries.append((
        "xl/_rels/workbook.xml.rels",
        (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + rels + workbook_rels_extra + "</Relationships>"
        ).encode(),
    ))
    for i, (_, _, xml) in enumerate(sheets, 1):
        entries.append((f"xl/worksheets/sheet{i}.xml", xml))
    for path, xml in (sheet_rels or {}).items():
        entries.append((path, xml))
    entries.extend(extra_entries)
    return zip_bytes(entries)


def zip_bytes(entries, compress=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = compress
            info.external_attr = 0o600 << 16
            info.create_system = 0  # host-independent bytes (Unix Python defaults to 3)
            archive.writestr(info, data, compresslevel=9)
    return buf.getvalue()


def lie_about_size(raw, member, claimed):
    """Rewrite the local and central uncompressed size of one member."""
    data = bytearray(raw)
    name = member.encode()
    pos = 0
    while True:
        pos = data.find(b"PK\x03\x04", pos)
        if pos < 0:
            break
        nlen = struct.unpack_from("<H", data, pos + 26)[0]
        if bytes(data[pos + 30 : pos + 30 + nlen]) == name:
            struct.pack_into("<I", data, pos + 22, claimed)
        pos += 4
    pos = 0
    while True:
        pos = data.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        nlen = struct.unpack_from("<H", data, pos + 28)[0]
        if bytes(data[pos + 46 : pos + 46 + nlen]) == name:
            struct.pack_into("<I", data, pos + 24, claimed)
        pos += 4
    return bytes(data)


def receipt_cells():
    return [
        ("A1", "s", "Goods receipt", None, None), ("B1", "s", "GR-208", None, None),
        ("A2", "s", "Received quantity", None, None), ("B2", "n", "100", None, None),
        ("A3", "s", "Unit", None, None), ("B3", "s", "each", None, None),
    ]


def xlsx_truth(sheets):
    return {"sheets": [{"name": n, "sheetId": s} for n, s, _ in sheets]}


# ----------------------------------------------------------------- catalogue

def catalogue():
    """Yield (name, bytes, metadata)."""

    # PROVENANCE: digital geometry (rotation / crop / mediabox origin)
    for rotate in (0, 90, 180, 270):
        page = geometry_page(rotate, None)
        yield f"geom-r{rotate}.pdf", build_pdf([page]), {
            "suite": "provenance", "case": "pdf-rotate", "truth": page.truth(),
            "expect": "words in reading order; boxes IoU>=0.95 in displayed crop frame",
        }
        page = geometry_page(rotate, (50, 40, 380, 290))
        yield f"geom-crop-r{rotate}.pdf", build_pdf([page]), {
            "suite": "provenance", "case": "pdf-rotate-crop", "truth": page.truth(),
            "expect": "crop-frame boxes; hidden text outside CropBox never extracted",
        }
    page = geometry_page(90, (50, 40, 380, 290), origin=(100, 50))
    yield "geom-media-offset-crop-r90.pdf", build_pdf([page]), {
        "suite": "provenance", "case": "pdf-mediabox-origin-crop-rotate", "truth": page.truth(),
        "expect": "non-zero MediaBox origin handled; crop-frame boxes",
    }

    # PROVENANCE: raster pages needing OCR
    for name, rotate, crop, orientation in (
        ("ocr-r0.pdf", 0, None, "upright"),
        ("ocr-crop-r0.pdf", 0, [30, 50, 400, 300], "upright"),
        ("ocr-r90-upright.pdf", 90, None, "upright"),
        ("ocr-r180-crop.pdf", 180, [30, 10, 400, 270], "upright"),
        ("ocr-r270-upright.pdf", 270, None, "upright"),
        ("ocr-r90-sideways.pdf", 90, None, "sideways"),
    ):
        page = ocr_page(rotate, crop, orientation)
        yield name, build_pdf([page]), {
            "suite": "provenance", "case": f"ocr-{orientation}", "truth": page.truth(),
            "expect": "OCR boxes in displayed crop frame; nothing outside crop; sideways text never invents boxes",
        }

    # PROVENANCE: hostile Unicode in otherwise valid invoices
    variants = {
        "uni-inv-control.pdf": ({}, "baseline: identical layout, plain ASCII"),
        "uni-inv-homoglyph-id.pdf": ({"L2": "Invoice \u0406NV-1048"}, "Cyrillic I in invoice id"),
        "uni-inv-zw-id-suffix.pdf": ({"L2": "Invoice INV-1048\u200b7"}, "ZWSP hides trailing digit"),
        "uni-inv-zw-id-inner.pdf": ({"L2": "Invoice INV-10\u200d48"}, "ZWJ inside invoice id"),
        "uni-inv-bidi-id.pdf": ({"L2": "Invoice INV-\u202e8401\u202c"}, "RLO reverses displayed id"),
        "uni-inv-zw-amounts.pdf": (
            {"L4": "Quantity 1\ufeff20", "L7": "Invoice face total USD 6220.\u200b80"},
            "zero-width inside quantity and face amount",
        ),
        "uni-inv-isolate-qty.pdf": ({"L4": "Quantity \u2066120\u2069"}, "bidi isolates around quantity"),
        "uni-inv-arabic-digits.pdf": (
            {"L4": "Quantity 1\u0662\u0660", "L6": "Tax rate \u0668%"},
            "mixed-script digits",
        ),
        "uni-inv-mathbold-issuer.pdf": (
            {"L1": "Supplier \U0001d400ster Supply Co."},
            "math-bold (NFKC-foldable) issuer name",
        ),
        "uni-inv-cyrillic-issuer.pdf": ({"L1": "Supplier \u0410ster Supply Co."}, "Cyrillic A issuer"),
        "uni-inv-ligature-issuer.pdf": ({"L1": "Supplier Aster Supply Co. \ufb01nance"}, "fi ligature"),
    }
    for name, (replace, why) in variants.items():
        page = hostile_invoice(invoice_variant(**replace))
        yield name, build_pdf([page]), {
            "suite": "provenance", "case": "unicode-invoice", "description": why,
            "document_role": "invoice", "truth": page.truth(),
        }
    page = hostile_invoice([
        "Memo Ame\u0301lie Cafe\u0301 Zo\u00eb",
        "Ref \U0001d400\U0001d401\U0001d402 \U0001f4b5 \ufb01nance",
        "Bidi \u2067abc\u2069 \u202eXYZ\u202c \u2068q\u2069",
        "Zero a\u200bb c\u200dd \ufeffe",
        "Homoglyph \u0410\u0435\u043e \u0406NV",
    ])
    yield "uni-combining-supplementary.pdf", build_pdf([page]), {
        "suite": "provenance", "case": "unicode-preservation", "truth": page.truth(),
        "expect": "exact scalar preservation; offsets are Unicode-scalar offsets",
    }
    page = hostile_invoice(["Lone surrogate follows"])
    page.raw_codes(60, 300, 12, "FH", [0x0E, 0x0E])
    yield "uni-lone-surrogate.pdf", build_pdf([page]), {
        "suite": "provenance", "case": "unicode-lone-surrogate",
        "expect": "rejected or replaced visibly; never an unencodable string",
    }
    page = hostile_invoice(["Invoice INV-1048", "NUL follows"])
    page.raw_codes(60, 300, 12, "FH", [0x0F])
    yield "uni-nul.pdf", build_pdf([page]), {
        "suite": "provenance", "case": "unicode-nul",
        "expect": "rejected before storage (PostgreSQL text cannot hold U+0000)",
    }

    # PROVENANCE: CSV encodings
    for name, rows, encoding, bom, newline in (
        ("csv-utf8.csv", CSV_ROWS, "utf-8", False, "\r\n"),
        ("csv-utf8-bom.csv", CSV_ROWS, "utf-8", True, "\r\n"),
        ("csv-utf16le-bom.csv", CSV_ROWS, "utf-16-le", True, "\r\n"),
        ("csv-utf16be-bom.csv", CSV_ROWS, "utf-16-be", True, "\n"),
        ("csv-cp1252.csv", CSV_ROWS, "cp1252", False, "\r\n"),
        ("csv-utf8-hostile-unicode.csv", CSV_UNICODE_ROWS, "utf-8", False, "\n"),
    ):
        data, cells = serialize_csv(rows, encoding, bom, newline)
        yield name, data, {
            "suite": "provenance", "case": "csv-encoding",
            "truth": {"encoding": encoding, "bom": bom, "cells": cells},
        }
    data, cells = serialize_csv(
        [["item", "quantity", "note"], ["Aster cable", "100", "ok"], ['Copper 5" pipe', "20", "rush, priority"]],
        "utf-8", False, literal_quotes=True,
    )
    assert data == b'item,quantity,note\r\nAster cable,100,ok\r\nCopper 5" pipe,20,"rush, priority"\r\n'
    yield "csv-inch-mark.csv", data, {
        "suite": "provenance", "case": "csv-unquoted-quote",
        "truth": {"encoding": "utf-8", "bom": False, "cells": cells},
        "expect": "csv.reader keeps the inch mark literal; byte ranges must match each cell",
    }

    # PROVENANCE: XLSX duplicate sheets
    for name, names in (
        ("xlsx-dup-sheet-exact.xlsx", ["Receipts", "Receipts"]),
        ("xlsx-dup-sheet-case.xlsx", ["Receipts", "RECEIPTS"]),
        ("xlsx-dup-sheet-nfd.xlsx", ["Caf\u00e9", "Cafe\u0301"]),
    ):
        sheets = [
            (names[0], 1, sheet_xml(receipt_cells())),
            (names[1], 2, sheet_xml([("A1", "s", "Received quantity", None, None), ("B1", "n", "999", None, None)])),
        ]
        yield name, build_xlsx(sheets), {
            "suite": "provenance", "case": "xlsx-duplicate-sheet", "truth": xlsx_truth(sheets),
            "expect": "rejected or disambiguated by sheetId; never merged or renamed",
        }
    sheets = [("Receipt", 1, sheet_xml(receipt_cells() + [
        ("A4", "s", "Cached formula", None, None), ("B4", "f", None, "60+40", 100),
        ("A5", "s", "Uncached formula", None, None), ("B5", "f", None, "SUM(B2:B2)", None),
        ("A6", "s", "Text formula", None, None), ("B6", "f", None, 'CONCATENATE("1","00")', "100"),
    ]))]
    yield "xlsx-formulas.xlsx", build_xlsx(sheets), {
        "suite": "provenance", "case": "xlsx-formula-cache", "truth": xlsx_truth(sheets),
        "expect": "formula text and cached value separate; absent cache stays None; never evaluated",
    }

    # PARSER: malformed / limits / hostile containers
    good = build_pdf([geometry_page(0, None)])
    yield "pdf-truncated.pdf", good[: len(good) // 2], {"suite": "parser", "case": "malformed-pdf"}
    yield "pdf-garbage-xref.pdf", good.replace(b"xref\n", b"xref\nGARBAGE\x00\xff\n", 1), {
        "suite": "parser", "case": "malformed-pdf",
    }
    yield "pdf-no-header.pdf", good[9:], {"suite": "parser", "case": "malformed-pdf"}
    yield "pdf-50-pages.pdf", build_pdf(text_pages(50, "Boundary")), {"suite": "parser", "case": "page-limit-boundary"}
    yield "pdf-51-pages.pdf", build_pdf(text_pages(51, "Over limit")), {"suite": "parser", "case": "page-limit"}
    yield "pdf-flate-bomb.pdf", flate_bomb(), {"suite": "parser", "case": "decompression-bomb-pdf"}
    yield "pdf-form-bomb.pdf", form_bomb(), {"suite": "parser", "case": "cpu-exhaustion"}
    yield "pdf-deep-nesting.pdf", deep_nesting(), {"suite": "parser", "case": "recursion"}
    js = build_pdf(
        [hostile_invoice(["Invoice INV-1048"])],
        extra_catalog=b" /OpenAction << /S /JavaScript /JS (app.launchURL\\('http://127.0.0.1:8101/'\\);) >>"
        b" /AA << /WC << /S /Launch /F (cmd.exe) >> >>",
    )
    yield "pdf-js-launch.pdf", js, {"suite": "parser", "case": "active-content", "expect": "parsed as data only"}
    ocr11 = [Page([0, 0, 300, 200]) for _ in range(10)]
    for p in ocr11:
        p.text(20, 100, 12, "Digital page")
    scan = ocr_page(0, None, "upright")
    yield "pdf-ocr-page-11.pdf", build_pdf(ocr11 + [scan]), {"suite": "parser", "case": "ocr-page-limit"}
    big = Page([0, 0, 5000, 5000])
    big.image("Im1", _raster([((40, 40), "Huge page")], (400, 400))[0], (5000, 0, 0, 5000, 0, 0), [])
    yield "pdf-ocr-huge-page.pdf", build_pdf([big]), {"suite": "parser", "case": "ocr-page-size-limit"}

    bomb_xml = b'<?xml version="1.0"?><worksheet xmlns="' + NS.encode() + b'"><sheetData>' + b" " * (400 << 20) + b"</sheetData></worksheet>"
    sheets = [("Receipt", 1, sheet_xml(receipt_cells()))]
    yield "xlsx-zip-bomb.xlsx", build_xlsx(sheets, extra_entries=[("xl/worksheets/sheet9.xml", bomb_xml)]), {
        "suite": "parser", "case": "decompression-bomb-xlsx",
    }
    honest = build_xlsx([("Receipt", 1, bomb_xml)])
    yield "xlsx-zip-bomb-lying-size.xlsx", lie_about_size(honest, "xl/worksheets/sheet1.xml", 4096), {
        "suite": "parser", "case": "decompression-bomb-xlsx-lying-header",
    }
    yield "xlsx-many-entries.xlsx", build_xlsx(sheets, extra_entries=[(f"xl/media/x{i}.xml", b"<x/>") for i in range(300)]), {
        "suite": "parser", "case": "zip-entry-limit",
    }
    yield "xlsx-path-traversal.xlsx", build_xlsx(sheets, extra_entries=[("../../evil.xml", b"<x/>")]), {
        "suite": "parser", "case": "zip-path-traversal",
    }
    yield "xlsx-absolute-path.xlsx", build_xlsx(sheets, extra_entries=[("/etc/evil.xml", b"<x/>")]), {
        "suite": "parser", "case": "zip-absolute-path",
    }
    dde_sheets = [("Receipt", 1, sheet_xml([
        ("A1", "s", "Received quantity", None, None),
        ("B1", "f", None, "cmd|' /C calc'!A0", 100),
        ("A2", "s", "Lookup", None, None),
        ("B2", "f", None, 'WEBSERVICE("http://127.0.0.1:8101/api/v1/health")', "100"),
        ("A3", "s", "Link", None, None),
        ("B3", "f", None, 'HYPERLINK("file:///C:/Windows/win.ini","open")', "open"),
        ("A4", "s", "Remote", None, None),
        ("B4", "f", None, "[1]Sheet1!A1", 100),
    ]))]
    yield "xlsx-dde-formulas.xlsx", build_xlsx(dde_sheets), {
        "suite": "parser", "case": "formula-dde", "expect": "formula text returned, never evaluated; requires review",
    }
    ext_link = (
        b'<?xml version="1.0" encoding="UTF-8"?><externalLink xmlns="' + NS.encode()
        + b'"><ddeLink ddeService="cmd" ddeTopic="/C calc"><ddeItems><ddeItem name="A0"/></ddeItems></ddeLink></externalLink>'
    )
    yield "xlsx-external-link.xlsx", build_xlsx(
        dde_sheets,
        extra_entries=[
            ("xl/externalLinks/externalLink1.xml", ext_link),
            ("xl/externalLinks/_rels/externalLink1.xml.rels", (
                b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" '
                b'Target="file:///C:/Windows/win.ini" TargetMode="External"/></Relationships>'
            )),
        ],
        workbook_rels_extra='<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" Target="externalLinks/externalLink1.xml"/>',
    ), {"suite": "parser", "case": "external-link-dde"}
    yield "xlsx-external-hyperlink-rel.xlsx", build_xlsx(
        sheets,
        sheet_rels={"xl/worksheets/_rels/sheet1.xml.rels": (
            b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            b'Target="http://127.0.0.1:8101/exfiltrate" TargetMode="External"/></Relationships>'
        )},
    ), {"suite": "parser", "case": "external-relationship", "expect": "never fetched (network none)"}
    yield "xlsx-vba.xlsx", build_xlsx(sheets, extra_entries=[("xl/vbaProject.bin", b"\xd0\xcf\x11\xe0" + b"\x00" * 60)]), {
        "suite": "parser", "case": "macro-container",
    }
    xxe = sheet_xml(receipt_cells(), prolog='<!DOCTYPE worksheet [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n')
    yield "xlsx-xxe.xlsx", build_xlsx([("Receipt", 1, xxe)]), {"suite": "parser", "case": "xml-external-entity"}
    lots = [(f"S{i}", i, sheet_xml([("A1", "s", "x", None, None)])) for i in range(1, 22)]
    yield "xlsx-21-sheets.xlsx", build_xlsx(lots), {"suite": "parser", "case": "sheet-limit"}

    big_csv = "item,quantity\n" + "".join(f"row{i},1\n" for i in range(1001))
    yield "csv-1002-rows.csv", big_csv.encode(), {"suite": "parser", "case": "csv-row-limit"}
    yield "csv-huge-cell.csv", b'note\n"' + b"A" * 20000 + b'"\n', {"suite": "parser", "case": "csv-cell-limit"}
    yield "csv-nul.csv", b"item,quantity\nA\x00,1\n", {"suite": "parser", "case": "csv-nul"}
    yield "csv-formula-injection.csv", b"item,quantity\n=cmd|' /C calc'!A0,100\n+SUM(1;2),20\n@HYPERLINK(\"http://x\"),3\n", {
        "suite": "parser", "case": "csv-formula-injection", "expect": "literal text only",
    }


def generate():
    files = {}
    meta = {}
    for name, data, info in catalogue():
        files[name] = data
        meta[name] = info
    return files, meta


def main(argv):
    check = "--check" in argv
    files, meta = generate()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "truth").mkdir(exist_ok=True)
    manifest = {
        "generator": GENERATOR,
        "generator_sha256": hashlib.sha256((ROOT / GENERATOR).read_bytes()).hexdigest(),
        "authorship": "Independently authored synthetic hostile test assets, generated deterministically by the generator above; not private documents and not copied from any third party",
        "license": "CC0-1.0",
        "rights": "All bytes self-authored. Fonts: none embedded; PDFs reference Helvetica by name and two descriptor-only fonts. Raster text uses Pillow's bundled Aileron Regular (CC0). No EICAR or real malware.",
        "files": [],
    }
    mismatches = []
    for name in sorted(files):
        data = files[name]
        info = dict(meta[name])
        entry = {
            "name": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "suite": info.pop("suite"),
            "case": info.pop("case"),
        }
        truth = info.pop("truth", None)
        entry.update(info)
        if truth is not None:
            truth_bytes = (json.dumps(truth, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
            entry["truth"] = f"truth/{name}.json"
            entry["truth_sha256"] = hashlib.sha256(truth_bytes).hexdigest()
            if check:
                if (OUT / entry["truth"]).read_bytes() != truth_bytes:
                    mismatches.append(entry["truth"])
            else:
                (OUT / entry["truth"]).write_bytes(truth_bytes)
        if check:
            if not (OUT / name).exists() or hashlib.sha256((OUT / name).read_bytes()).hexdigest() != entry["sha256"]:
                mismatches.append(name)
        else:
            (OUT / name).write_bytes(data)
        manifest["files"].append(entry)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if check:
        current = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
        current.pop("generator_sha256", None)
        fresh = json.loads(text)
        fresh.pop("generator_sha256", None)
        if current != fresh:
            mismatches.append("manifest.json")
        print(json.dumps({"checked": len(files), "mismatches": mismatches}))
        return 1 if mismatches else 0
    (OUT / "manifest.json").write_text(text, encoding="utf-8")
    print(json.dumps({"written": len(files), "dir": str(OUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
