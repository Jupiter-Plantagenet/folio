"""Host-side validation of untrusted parser-container output (defence in depth).

The container is the only place hostile bytes are parsed, but its JSON is
still untrusted data: a parser bug or exploit must not be able to plant a
span whose quote is not the cited text, a CSV byte range that points at other
bytes, a box outside the page, or a string PostgreSQL cannot store. Anything
inconsistent rejects the whole file (fail closed); nothing is repaired.
"""

import hashlib
import math

from folio.intake_limits import (
    MAX_PAGES_PER_FILE,
    MAX_PARSER_OUTPUT_BYTES,
    MAX_XLSX_CELL_TEXT_BYTES,
    MAX_XLSX_CELLS,
    MAX_XLSX_PROJECTED_TEXT_BYTES,
    xlsx_projection_within_budget,
)

KINDS = {"digital-pdf", "ocr-or-mixed-pdf", "csv", "xlsx"}
CSV_CODECS = {"utf-8", "utf-16-le", "utf-16-be"}
MAX_WORDS = 200000
MAX_BOXES = 2000


def _fail(reason):
    raise ValueError("PARSER_OUTPUT_INVALID:" + reason)


def _strings(value, depth=0):
    if depth > 12:
        _fail("NESTING")
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, depth + 1)


def _number(v):
    return type(v) in (int, float) and math.isfinite(v)


def csv_literal(quote):
    return quote[1:-1].replace('""', '"') if quote.startswith('"') else quote


def _xlsx_bounds(parsed):
    cells = parsed.get("cells")
    if not isinstance(cells, list) or len(cells) > MAX_XLSX_CELLS:
        _fail("XLSX_CELL_LIMIT")
    value_bytes = 0
    metadata_bytes = 0
    for cell in cells:
        if not isinstance(cell, dict):
            _fail("XLSX_CELL_SHAPE")
        for key, value in cell.items():
            if key == "value" and value is not None and not isinstance(
                value, (str, int, float, bool)
            ):
                _fail("XLSX_CELL_VALUE")
            if isinstance(value, str):
                if len(value) > MAX_XLSX_CELL_TEXT_BYTES:
                    _fail("XLSX_CELL_TEXT_LIMIT")
                size = len(value.encode("utf-8"))
                if size > MAX_XLSX_CELL_TEXT_BYTES:
                    _fail("XLSX_CELL_TEXT_LIMIT")
                if key == "value":
                    value_bytes += size
                else:
                    metadata_bytes += size
    if not xlsx_projection_within_budget(
        len(cells), value_bytes, metadata_bytes
    ):
        _fail("XLSX_PROJECTION_LIMIT")

    pages = parsed.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES_PER_FILE:
        _fail("PAGES")
    all_page_text_bytes = 0
    all_quote_bytes = 0
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("text"), str):
            _fail("PAGE_SHAPE")
        if len(page["text"]) > MAX_XLSX_PROJECTED_TEXT_BYTES:
            _fail("XLSX_PROJECTION_LIMIT")
        text_size = len(page["text"].encode("utf-8"))
        all_page_text_bytes += text_size
        if all_page_text_bytes > MAX_XLSX_PROJECTED_TEXT_BYTES:
            _fail("XLSX_PROJECTION_LIMIT")
        words = page.get("words")
        if not isinstance(words, list) or len(words) > MAX_XLSX_CELLS:
            _fail("XLSX_PROJECTION_LIMIT")
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("quote"), str):
                _fail("WORD_SHAPE")
            if len(word["quote"]) > MAX_XLSX_CELL_TEXT_BYTES:
                _fail("XLSX_CELL_TEXT_LIMIT")
            size = len(word["quote"].encode("utf-8"))
            if size > MAX_XLSX_CELL_TEXT_BYTES:
                _fail("XLSX_CELL_TEXT_LIMIT")
            all_quote_bytes += size
            if all_quote_bytes > MAX_XLSX_PROJECTED_TEXT_BYTES:
                _fail("XLSX_PROJECTION_LIMIT")


def validate(parsed, raw):
    if not isinstance(parsed, dict) or parsed.get("kind") not in KINDS:
        _fail("KIND")
    if parsed["kind"] == "xlsx":
        _xlsx_bounds(parsed)
    string_bytes = 0
    for text in _strings(parsed):
        if parsed["kind"] == "xlsx":
            if len(text) > MAX_PARSER_OUTPUT_BYTES - string_bytes:
                _fail("OUTPUT_LIMIT")
            string_bytes += len(text.encode("utf-8"))
            if string_bytes > MAX_PARSER_OUTPUT_BYTES:
                _fail("OUTPUT_LIMIT")
        if "\x00" in text:
            _fail("NUL")  # PostgreSQL text/jsonb cannot store U+0000
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            _fail("SURROGATE")
    pages = parsed.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES_PER_FILE:
        _fail("PAGES")
    words_total = 0
    tabular = parsed["kind"] in ("csv", "xlsx")
    for page in pages:
        text = page.get("text")
        words = page.get("words")
        if not isinstance(text, str) or not isinstance(words, list):
            _fail("PAGE_SHAPE")
        if page.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest():
            _fail("TEXT_SHA256")
        width, height = page.get("width"), page.get("height")
        if not tabular and not (_number(width) and _number(height) and width > 0 and height > 0):
            _fail("PAGE_SIZE")
        words_total += len(words)
        if words_total > MAX_WORDS:
            _fail("WORD_LIMIT")
        previous_end = 0
        for word in words:
            start, end, quote = word.get("start"), word.get("end"), word.get("quote")
            if (
                type(start) is not int
                or type(end) is not int
                or not previous_end <= start < end <= len(text)
                or text[start:end] != quote
            ):
                _fail("SPAN")
            previous_end = end
            boxes = word.get("boxes")
            if not isinstance(boxes, list) or len(boxes) > MAX_BOXES:
                _fail("BOXES")
            if tabular and boxes:
                _fail("TABULAR_BOX")  # no geometry may be implied for cells
            if not tabular and not boxes:
                _fail("MISSING_BOX")
            for box in boxes:
                if (
                    not isinstance(box, list)
                    or len(box) != 4
                    or not all(_number(v) for v in box)
                    or box[0] > box[2]
                    or box[1] > box[3]
                    or box[0] < -0.5
                    or box[1] < -0.5
                    or box[2] > width + 0.5
                    or box[3] > height + 0.5
                ):
                    _fail("BOX_GEOMETRY")
    if parsed["kind"] == "csv":
        codec = (parsed.get("csv") or {}).get("encoding", "utf-8")
        if codec not in CSV_CODECS:
            _fail("CSV_ENCODING")
        for cell in parsed.get("cells", []):
            bs, be = cell.get("byte_start"), cell.get("byte_end")
            if type(bs) is not int or type(be) is not int or not 0 <= bs <= be <= len(raw):
                _fail("CSV_BYTE_RANGE")
            try:
                decoded = raw[bs:be].decode(codec)
            except UnicodeDecodeError:
                _fail("CSV_BYTE_RANGE")
            if decoded != cell.get("quote") or csv_literal(decoded) != cell.get("value"):
                _fail("CSV_CELL_LITERAL")
    if parsed["kind"] == "xlsx":
        seen = set()
        for cell in parsed["cells"]:
            key = (cell.get("sheet_id", cell.get("sheet")), cell.get("coordinate"))
            if key in seen:
                _fail("XLSX_CELL_DUPLICATE")
            seen.add(key)
    return parsed
