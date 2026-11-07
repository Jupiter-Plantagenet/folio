"""Digital PDF character geometry and optional isolated English OCR; never fabricate text/boxes.

Geometry (folio-crop-frame-v1): every box is [x0, top, x1, bottom] in PDF
points in the page as displayed, i.e. the effective CropBox (CropBox clipped
to the MediaBox) after /Rotate, with the origin at its top-left corner.
`user_to_frame` is the exact affine from PDF user space into that frame, so
any box can be mapped back. Text is read in unrotated user space (the order
it was authored in). Glyphs whose centre lies outside the CropBox are
invisible to a reader, so they are never extracted; their count is reported.
"""

import hashlib
import io
import json
import sys

import pdfplumber

from folio.intake_limits import MAX_FILE_BYTES, MAX_PAGES_PER_FILE

FRAME = "folio-crop-frame-v1: displayed CropBox after /Rotate, top-left origin, PDF points"


def _rect(value):
    x0, y0, x1, y1 = (float(v) for v in value)
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def frame_affine(rotate, crop):
    """User space -> displayed crop frame: X = a*x + c*y + e, Y = b*x + d*y + f."""
    cx0, cy0, cx1, cy1 = crop
    return {
        0: (1, 0, 0, -1, -cx0, cy1),
        90: (0, 1, 1, 0, -cy0, -cx0),
        180: (-1, 0, 0, 1, cx1, -cy0),
        270: (0, -1, -1, 0, cy1, cx1),
    }[rotate]


def _invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * e + ic * f), -(ib * e + id_ * f))


def _map_box(m, x0, y0, x1, y1):
    a, b, c, d, e, f = m
    xs = [a * x + c * y + e for x in (x0, x1) for y in (y0, y1)]
    ys = [b * x + d * y + f for x in (x0, x1) for y in (y0, y1)]
    return [min(xs), min(ys), max(xs), max(ys)]


def page_geometry(page_obj):
    rotate = page_obj.rotate  # pdfminer: inherited, int, modulo 360
    if rotate % 90:
        raise ValueError("PAGE_ROTATION_INVALID")
    media = _rect(page_obj.mediabox)
    crop = _rect(page_obj.cropbox)
    crop = [
        max(media[0], crop[0]),
        max(media[1], crop[1]),
        min(media[2], crop[2]),
        min(media[3], crop[3]),
    ]
    if crop[2] - crop[0] < 1 or crop[3] - crop[1] < 1:
        raise ValueError("PAGE_GEOMETRY_INVALID")
    width, height = crop[2] - crop[0], crop[3] - crop[1]
    if rotate in (90, 270):
        width, height = height, width
    return rotate, media, crop, width, height


def parse_pdf(raw):
    if len(raw) > MAX_FILE_BYTES or not raw.startswith(b"%PDF-"):
        raise ValueError("FILE_REJECTED")
    from pdfminer.pdfpage import PDFPage
    from pdfplumber.page import Page

    result = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "kind": "digital-pdf",
        "pages": [],
    }
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfplumber.utils.exceptions import PdfminerException

    try:
        opened = pdfplumber.open(io.BytesIO(raw))
    except PDFPasswordIncorrect as error:
        raise ValueError("ENCRYPTED_REJECTED") from error
    except PdfminerException as error:
        # pdfplumber wraps pdfminer errors; only a password failure is reclassified.
        if error.args and isinstance(error.args[0], PDFPasswordIncorrect):
            raise ValueError("ENCRYPTED_REJECTED") from error
        raise
    with opened as doc:
        # Even an empty-user-password PDF is encrypted; refuse rather than decrypt.
        if getattr(doc.doc, "encryption", None):
            raise ValueError("ENCRYPTED_REJECTED")
        page_objs = []
        for page_obj in PDFPage.create_pages(doc.doc):
            page_objs.append(page_obj)
            if len(page_objs) > MAX_PAGES_PER_FILE:
                raise ValueError("PAGE_LIMIT")
        for n, page_obj in enumerate(page_objs, 1):
            rotate, media, crop, width, height = page_geometry(page_obj)
            # Read text unrotated; geometry is mapped into the displayed frame.
            page_obj.attrs = {**page_obj.attrs, "Rotate": 0}
            page_obj.rotate = 0
            page = Page(doc, page_obj, page_number=n)
            to_frame = frame_affine(rotate, crop)
            # pdfminer y is relative to the MediaBox origin; pdfplumber x is absolute.
            oy = media[1]

            def user_box(ch, oy=oy):
                return ch["x0"], ch["y0"] + oy, ch["x1"], ch["y1"] + oy

            def visible(obj, crop=crop, user_box=user_box):
                if obj.get("object_type") != "char":
                    return True
                x0, y0, x1, y1 = user_box(obj)
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                return crop[0] <= cx <= crop[2] and crop[1] <= cy <= crop[3]

            all_chars = page.chars
            hidden = sum(1 for ch in all_chars if not visible(ch))
            shown = page.filter(visible) if hidden else page
            words = shown.extract_words(return_chars=True, expand_ligatures=False)
            text = ""
            spans = []
            for word in words:
                if text:
                    text += " "
                start = len(text)
                text += word["text"]
                spans.append(
                    {
                        "quote": word["text"],
                        "start": start,
                        "end": len(text),
                        "boxes": [
                            _map_box(to_frame, *user_box(char)) for char in word["chars"]
                        ],
                    }
                )
            geometry = {
                "number": n,
                "width": width,
                "height": height,
                "rotation": rotate,
                "mediabox": media,
                "cropbox": crop,
                "user_to_frame": list(to_frame),
                "coordinate_frame": FRAME,
                "hidden_chars_outside_cropbox": hidden,
            }
            if not text:
                if all_chars:
                    # Text exists but none of it is visible: never OCR around it.
                    raise ValueError("PDF_TEXT_OUTSIDE_CROPBOX")
                result["kind"] = "ocr-or-mixed-pdf"
                result["pages"].append(
                    {**geometry, **ocr_page(raw, n, width, height, to_frame)}
                )
                continue
            result["pages"].append(
                {
                    **geometry,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "words": spans,
                    "text_kind": "immutable parser word-order text; spaces inserted, not original content-stream offsets",
                }
            )
    return result


def ocr_page(raw, n, width, height, to_frame):
    try:
        import pypdfium2
        import pytesseract
    except ImportError as error:
        raise ValueError("OCR_RUNTIME_UNAVAILABLE") from error
    if n > MAX_PAGES_PER_FILE or width > 2000 or height > 2000:
        raise ValueError("OCR_PAGE_LIMIT")
    # Tesseract is most accurate near 300 dpi; scans are commonly 200-300 dpi.
    # Large pages render lower so the raster stays inside the pixel limit.
    dpi = max(72, min(300, int(72 * (15_500_000 / (width * height)) ** 0.5)))
    document = pypdfium2.PdfDocument(raw)
    try:
        # pdfium renders the displayed page: effective CropBox after /Rotate.
        bitmap = document[n - 1].render(scale=dpi / 72, may_draw_forms=False)
        image = bitmap.to_pil().convert("L")
    finally:
        document.close()
    from PIL import ImageFilter, ImageOps

    # Speckle removal and contrast stretch only; geometry is unchanged.
    image = ImageOps.autocontrast(image.filter(ImageFilter.MedianFilter(3)), cutoff=1)
    if image.width * image.height > 16000000:
        raise ValueError("OCR_PIXEL_LIMIT")
    if (
        abs(image.width - width * dpi / 72) > 2
        or abs(image.height - height * dpi / 72) > 2
    ):
        raise ValueError("OCR_GEOMETRY_MISMATCH")
    sx, sy = width / image.width, height / image.height
    data = pytesseract.image_to_data(
        image,
        lang="eng",
        config="--psm 6",
        output_type=pytesseract.Output.DICT,
        timeout=40,
    )
    text = ""
    spans = []
    for index, value in enumerate(data["text"]):
        if not value.strip():
            continue
        if text:
            text += " "
        start = len(text)
        text += value
        x, y, w, h = [data[k][index] for k in ["left", "top", "width", "height"]]
        ids = [data[k][index] for k in ("block_num", "par_num", "line_num", "word_num")]
        spans.append(
            {
                "quote": value,
                "start": start,
                "end": len(text),
                "boxes": [[x * sx, y * sy, (x + w) * sx, (y + h) * sy]],
                "pixel_box": [x, y, x + w, y + h],
                "ocr_word_id": ".".join(str(v) for v in [n, *ids]),
                "reading_order": len(spans),
                "geometry_kind": "ocr-word-box",
                "confidence": float(data["conf"][index]),
                "requires_review": True,
            }
        )
    if not text:
        raise ValueError("OCR_NO_SUPPORTED_TEXT")
    return {
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "words": spans,
        "text_kind": "ocr-word-order-v1",
        "ocr_engine": "tesseract-eng",
        "render_dpi": dpi,
        "raster": {
            "sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            "digest_of": "8-bit grayscale pixel rows, top to bottom",
            "width": image.width,
            "height": image.height,
            "dpi": dpi,
        },
        "raster_to_frame": [sx, 0, 0, sy, 0, 0],
        "frame_to_user": list(_invert(to_frame)),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(parse_pdf(open(sys.argv[1], "rb").read()), ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        raise SystemExit(2)
