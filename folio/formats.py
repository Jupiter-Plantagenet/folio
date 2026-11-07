"""Bounded parsing with literal CSV bytes and XLSX cell/formula provenance.
Call only in the isolated parser for untrusted inputs. Never evaluate formulas.
"""

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from pathlib import PurePosixPath
from xml.parsers import expat

from folio.intake_limits import (
    MAX_FILE_BYTES,
    MAX_PAGES_PER_FILE,
    MAX_PARSER_ERROR_BYTES,
    MAX_PARSER_OUTPUT_BYTES,
    MAX_XLSX_CELL_TEXT_BYTES,
    MAX_XLSX_CELLS,
    MAX_XLSX_COLUMNS,
    MAX_XLSX_PROJECTED_TEXT_BYTES,
    MAX_XLSX_ROWS,
    xlsx_decompressed_bytes_within_limit,
    xlsx_projection_within_budget,
)

MAX_BYTES = MAX_FILE_BYTES
CSV_DIALECT = {
    "delimiter": ",",
    "quotechar": '"',
    "doublequote": True,
    "strict": True,
    "line_terminators": ["\r\n", "\n", "\r"],
}
# Delimiters a CSV file may use (folio-csv-dialect-v1). Detection is by
# structure only; anything not unambiguous is refused, never guessed.
CSV_DELIMITERS = (",", ";", "\t", "|")
_DECIMAL_COMMA_FIELD = re.compile(r"[^\d,]*\d[\d.' \u00a0]*,\d+[^\d,]*")


def csv_delimiter(text):
    """The one delimiter of the allowlist that structures this CSV, or a refusal.

    Each allowed delimiter is lexed with csv.reader rules (quotes respected);
    a delimiter is viable when it splits at least one record. One viable
    delimiter wins; otherwise the one splitting strictly the most records
    wins. On a tie, a comma whose every occurrence (under the other
    delimiter) sits inside a decimal-comma number ("12,40", "EUR 1.234,50")
    is a decimal mark, not a delimiter. Any other tie is ambiguous: refused.
    A single-column file keeps the comma dialect.
    """
    splits, lexed = {}, {}
    for delim in CSV_DELIMITERS:
        try:
            rows = list(
                csv.reader(io.StringIO(text, newline=""), delimiter=delim, strict=True)
            )
        except csv.Error:
            continue
        lexed[delim] = rows
        count = sum(1 for r in rows if len(r) >= 2)
        if count:
            splits[delim] = count
    if not splits:
        return ","
    best = max(splits.values())
    tied = [d for d, n in splits.items() if n == best]
    if len(tied) == 2 and "," in tied:
        other = next(d for d in tied if d != ",")
        fields = [f for r in lexed[other] for f in r if "," in f]
        if fields and all(_DECIMAL_COMMA_FIELD.fullmatch(f) for f in fields):
            return other
    if len(tied) != 1:
        raise ValueError("CSV_DIALECT_AMBIGUOUS")
    return tied[0]


# A byte-order mark is the only encoding declaration a CSV file can carry.
# Anything else must be strict UTF-8; legacy code pages are refused, never guessed.
CSV_BOMS = [
    (b"\xff\xfe\x00\x00", None),
    (b"\x00\x00\xfe\xff", None),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]


def _no_dtd(xml):
    # No DTD/entity processing, including UTF-16 spellings of these tokens.
    flat = xml.replace(b"\x00", b"")
    if b"<!DOCTYPE" in flat or b"<!ENTITY" in flat:
        raise ValueError("XLSX_XML_REJECTED")
    return xml


def _scan_no_dtd(stream):
    """Reject DTD/entity declarations while streaming a potentially large XML part."""
    tail = b""
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        flat = (tail + chunk).replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in flat or b"<!ENTITY" in flat:
            raise ValueError("XLSX_XML_REJECTED")
        tail = flat[-16:]


def xlsx_lexical_cells(raw, worksheet_path, remaining_cell_budget):
    """Stream original OOXML cells before numeric decoding loses digits."""
    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        path = worksheet_path.lstrip("/")
        info = archive.getinfo(path)
        if not xlsx_decompressed_bytes_within_limit(info.file_size):
            raise ValueError("XLSX_CONTAINER_REJECTED")
        with archive.open(path) as scan_stream:
            _scan_no_dtd(scan_stream)
        with archive.open(path) as stream:
            cells = {}
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            try:
                for _, node in ET.iterparse(stream, events=("end",)):
                    if node.tag == ns + "c":
                        coordinate = node.get("r")
                        if coordinate in cells:
                            raise ValueError("XLSX_CELL_DUPLICATE")
                        if len(cells) >= remaining_cell_budget:
                            raise ValueError("CELL_LIMIT")
                        value = node.find(ns + "v")
                        formula = node.find(ns + "f")
                        raw_type = node.get("t", "n")
                        cells[coordinate] = {
                            "raw_type": raw_type,
                            "raw_value": value.text if value is not None else None,
                            "numeric_lexical": value.text
                            if value is not None and raw_type == "n"
                            else None,
                            "raw_formula": formula.text if formula is not None else None,
                            "has_formula": formula is not None,
                        }
                        node.clear()
                    elif node.tag.endswith("}row"):
                        node.clear()
            except ET.ParseError as error:
                raise ValueError("XLSX_XML_REJECTED") from error
        return cells


def _sparse_xlsx_cells(workbook, sheet, *, data_only):
    """Yield only authored OOXML cells without filling missing rows or columns.

    openpyxl's public read-only ``iter_rows`` deliberately synthesizes missing
    rows and cells. A hostile sparse reference near Excel's coordinate ceiling
    would therefore allocate or iterate over an enormous empty rectangle. Its
    bounded streaming row parser yields only the cell elements present in XML.
    """
    from openpyxl.cell.read_only import ReadOnlyCell
    from openpyxl.worksheet._reader import WorkSheetParser

    with sheet._get_source() as source:
        parser = WorkSheetParser(
            source,
            sheet._shared_strings,
            data_only=data_only,
            epoch=workbook.epoch,
            date_formats=workbook._date_formats,
            timedelta_formats=workbook._timedelta_formats,
        )
        for _, row_cells in parser.parse():
            for cell in row_cells:
                yield ReadOnlyCell(sheet, **cell)


class _ReferencedSharedStrings:
    """Sparse shared-string lookup that retains only referenced, bounded text."""

    def __init__(self, values):
        self._values = values

    def __getitem__(self, index):
        try:
            return self._values[index]
        except (KeyError, TypeError) as error:
            raise ValueError("XLSX_SHARED_STRING_INDEX") from error


def _bounded_shared_strings(raw, referenced_indices, strings_path):
    """Stream the SST, keeping only cell-referenced values within parser budgets.

    Openpyxl 3.1.5's default reader builds a Python list containing every
    ``<si>`` before it reads a worksheet. Here the XML is streamed first, and
    unreferenced strings are discarded as character callbacks arrive. This
    avoids object amplification from large unreferenced tables while retaining
    ordinary shared-string indexing and rich-text run concatenation.
    """
    if not strings_path:
        if referenced_indices:
            raise ValueError("XLSX_SHARED_STRING_INDEX")
        return _ReferencedSharedStrings({})
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if strings_path not in archive.namelist():
            if referenced_indices:
                raise ValueError("XLSX_SHARED_STRING_INDEX")
            return _ReferencedSharedStrings({})
        info = archive.getinfo(strings_path)
        if not xlsx_decompressed_bytes_within_limit(info.file_size):
            raise ValueError("XLSX_CONTAINER_REJECTED")
        with archive.open(info) as scan_stream:
            _scan_no_dtd(scan_stream)
        values = {}
        parser = expat.ParserCreate(namespace_separator="}")
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        string_index = -1
        in_string = False
        in_text = False
        pieces = None
        value_bytes = 0
        retained_bytes = 0
        depth = 0
        string_depth = None
        phonetic_depth = None
        root_seen = False
        ignored_depth = 0
        element_stack = []
        main_namespace = (
            "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        )

        def split_qname(name):
            if "}" not in name:
                return None, name
            namespace, local = name.split("}", 1)
            return namespace, local

        def start(name, _attrs):
            nonlocal string_index, in_string, in_text
            nonlocal pieces, value_bytes
            nonlocal depth, string_depth, phonetic_depth, root_seen
            nonlocal ignored_depth
            namespace, local = split_qname(name)
            parent = element_stack[-1][1] if element_stack else None
            depth += 1
            if ignored_depth:
                ignored_depth += 1
                element_stack.append((namespace, local))
                return
            if not root_seen:
                if depth != 1 or namespace != main_namespace or local != "sst":
                    raise ValueError("XLSX_XML_REJECTED")
                root_seen = True
                element_stack.append((namespace, local))
                return
            if in_string:
                if namespace != main_namespace:
                    raise ValueError("XLSX_XML_REJECTED")
                if local == "rPh":
                    if parent != "si":
                        raise ValueError("XLSX_XML_REJECTED")
                    phonetic_depth = depth
                    ignored_depth = 1
                elif local == "rPr" or local == "phoneticPr":
                    if (local == "rPr" and parent != "r") or (
                        local == "phoneticPr" and parent != "si"
                    ):
                        raise ValueError("XLSX_XML_REJECTED")
                    ignored_depth = 1
                elif local == "t":
                    if parent not in ("si", "r") or in_text:
                        raise ValueError("XLSX_XML_REJECTED")
                    in_text = True
                elif local == "r":
                    if parent != "si":
                        raise ValueError("XLSX_XML_REJECTED")
                else:
                    raise ValueError("XLSX_XML_REJECTED")
                element_stack.append((namespace, local))
                return
            if depth == 2 and namespace == main_namespace and local == "si":
                string_index += 1
                in_string = True
                string_depth = depth
                in_text = False
                phonetic_depth = None
                value_bytes = 0
                pieces = [] if string_index in referenced_indices else None
            element_stack.append((namespace, local))

        def end(name):
            nonlocal in_string, in_text, pieces
            nonlocal retained_bytes
            nonlocal depth, string_depth, phonetic_depth
            nonlocal ignored_depth
            namespace, local = split_qname(name)
            if ignored_depth:
                if depth == phonetic_depth:
                    phonetic_depth = None
                ignored_depth -= 1
                element_stack.pop()
                depth -= 1
                return
            if namespace == main_namespace and local == "t":
                in_text = False
            elif (
                namespace == main_namespace
                and local == "si"
                and in_string
                and string_depth == depth
            ):
                if pieces is not None:
                    if retained_bytes + value_bytes > MAX_XLSX_PROJECTED_TEXT_BYTES:
                        raise ValueError("PARSER_OUTPUT_LIMIT")
                    values[string_index] = "".join(pieces).replace("x005F_", "")
                    retained_bytes += value_bytes
                in_string = False
                pieces = None
                string_depth = None
            element_stack.pop()
            depth -= 1

        def characters(text):
            nonlocal value_bytes
            if in_text and pieces is not None:
                value_bytes += len(text.encode("utf-8"))
                if value_bytes > MAX_XLSX_CELL_TEXT_BYTES:
                    raise ValueError("XLSX_CELL_TEXT_LIMIT")
                if retained_bytes + value_bytes > MAX_XLSX_PROJECTED_TEXT_BYTES:
                    raise ValueError("PARSER_OUTPUT_LIMIT")
                pieces.append(text)

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = characters
        try:
            with archive.open(info) as stream:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    parser.Parse(chunk, False)
                parser.Parse(b"", True)
        except expat.ExpatError as error:
            raise ValueError("XLSX_XML_REJECTED") from error
        if not root_seen or in_string or not referenced_indices.issubset(values):
            raise ValueError("XLSX_SHARED_STRING_INDEX")
    return _ReferencedSharedStrings(values)


def _load_xlsx_with_shared_strings(raw, declared):
    """Pre-scan cell references, then load openpyxl without eager SST materialization."""
    lexical_by_sheet = {}
    referenced_indices = set()
    cell_budget = MAX_XLSX_CELLS
    for meta in declared:
        lexical = xlsx_lexical_cells(raw, meta["part"], cell_budget)
        cell_budget -= len(lexical)
        for item in lexical.values():
            if item["raw_type"] == "s":
                try:
                    index = int(item["raw_value"])
                except (TypeError, ValueError) as error:
                    raise ValueError("XLSX_SHARED_STRING_INDEX") from error
                if index < 0:
                    raise ValueError("XLSX_SHARED_STRING_INDEX")
                referenced_indices.add(index)
        lexical_by_sheet[meta["name"]] = lexical

    from openpyxl.reader.excel import ExcelReader
    from openpyxl.styles.stylesheet import apply_stylesheet
    from openpyxl.xml.constants import SHARED_STRINGS

    reader = ExcelReader(
        io.BytesIO(raw), read_only=True, data_only=False, keep_links=False
    )
    reader.read_manifest()
    try:
        reader.read_manifest()
        shared_string_part = reader.package.find(SHARED_STRINGS)
        strings_path = (
            shared_string_part.PartName.lstrip("/") if shared_string_part else None
        )
        reader.shared_strings = _bounded_shared_strings(
            raw, referenced_indices, strings_path
        )
        reader.read_workbook()
        reader.read_properties()
        reader.read_custom()
        reader.read_theme()
        apply_stylesheet(reader.archive, reader.wb)
        reader.read_worksheets()
        reader.parser.assign_names()
    except BaseException:
        reader.archive.close()
        raise
    return reader.wb, lexical_by_sheet


def xlsx_sheets(raw):
    """Workbook sheet list (name, sheetId, part) read lexically, duplicates refused.

    Excel compares sheet names case-insensitively; canonically equivalent
    Unicode spellings are the same name too. openpyxl would silently rename a
    duplicate ("Receipts1"), inventing a sheet that is not in the file, so a
    workbook with duplicate names or sheet IDs is rejected before it loads.
    """
    from xml.etree import ElementTree as ET

    main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    pkg = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for path in ("xl/workbook.xml", "xl/_rels/workbook.xml.rels"):
            if archive.getinfo(path).file_size > 1024 * 1024:
                raise ValueError("XLSX_CONTAINER_REJECTED")
        book = ET.fromstring(_no_dtd(archive.read("xl/workbook.xml")))
        rels = ET.fromstring(_no_dtd(archive.read("xl/_rels/workbook.xml.rels")))
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(pkg + "Relationship")}
    sheets, names, ids = [], set(), set()
    for node in book.iter(main + "sheet"):
        name, sheet_id = node.get("name") or "", node.get("sheetId")
        key = unicodedata.normalize("NFC", name).casefold()
        if not name or key in names or sheet_id in ids:
            raise ValueError("XLSX_SHEET_NAME_DUPLICATE")
        names.add(key)
        ids.add(sheet_id)
        target = targets.get(node.get(rel + "id")) or ""
        part = target.lstrip("/") if target.startswith("/") else "xl/" + target
        sheets.append({"name": name, "sheet_id": sheet_id, "part": part})
    return sheets


def csv_cells(raw):
    """Decode by BOM or strict UTF-8, then lex with csv.reader semantics.

    Returns (cells, encoding, bom). Every cell carries the raw field text
    (`quote`, including any quoting) and its exact byte range in `raw`.
    """
    encoding, bom = "utf-8", b""
    for mark, codec in CSV_BOMS:
        if raw.startswith(mark):
            if codec is None:
                raise ValueError("CSV_ENCODING_UNSUPPORTED")
            encoding, bom = codec, mark
            break
    try:
        text = raw[len(bom) :].decode(encoding, errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("CSV_ENCODING_UNSUPPORTED") from error
    if "\x00" in text:
        raise ValueError("CSV_REJECTED")
    delim = csv_delimiter(text)
    values = list(
        csv.reader(io.StringIO(text, newline=""), delimiter=delim, strict=True)
    )
    if not values or len(values) > 1000 or any(len(r) > 50 for r in values):
        raise ValueError("CSV_LIMIT")
    # Lexer: a quote is special only at the start of a field (csv.reader rule).
    spans = []
    row, col, i, n = 1, 0, 0, len(text)
    start, state = 0, "start"

    def close(end):
        nonlocal col
        col += 1
        spans.append((row, col, start, end))

    while i < n:
        ch = text[i]
        if state == "quoted":
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    i += 2
                    continue
                state = "closed"
            i += 1
            continue
        if state == "start" and ch == '"':
            state = "quoted"
            i += 1
            continue
        if ch == delim:
            close(i)
            start, state = i + 1, "start"
        elif ch in "\r\n":
            if col or state != "start":
                close(i)
            i += 2 if ch == "\r" and i + 1 < n and text[i + 1] == "\n" else 1
            row, col, start, state = row + 1, 0, i, "start"
            continue
        elif state == "closed":
            raise ValueError("CSV_REJECTED")
        else:
            state = "field"
        i += 1
    if state == "quoted":
        raise ValueError("CSV_REJECTED")
    if col or state != "start":
        close(n)
    flat = [
        (r, c, value)
        for r, record in enumerate(values, 1)
        for c, value in enumerate(record, 1)
    ]
    if len(flat) != len(spans):
        raise ValueError("CSV_LEXICAL_MISMATCH")
    # Byte offsets of every character boundary under the declared encoding.
    offsets = [len(bom)]
    for ch in text:
        offsets.append(offsets[-1] + len(ch.encode(encoding)))
    cells = []
    for (r, c, value), (sr, sc, s, e) in zip(flat, spans):
        quote = text[s:e]
        literal = quote[1:-1].replace('""', '"') if quote.startswith('"') else quote
        if (r, c) != (sr, sc) or literal != value:
            raise ValueError("CSV_LEXICAL_MISMATCH")
        if len(value) > 10000:
            raise ValueError("CELL_LIMIT")
        cells.append(
            {
                "row": r,
                "column": c,
                "value": value,
                "quote": quote,
                "byte_start": offsets[s],
                "byte_end": offsets[e],
                "encoding": encoding,
                "provenance_kind": "csv-raw-field",
            }
        )
    return cells, encoding, bool(bom), delim


OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")


def parse_document(raw, name):
    if not raw or len(raw) > MAX_BYTES or "/" in name or "\\" in name:
        raise ValueError("FILE_REJECTED")
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "xlsx" and raw.startswith(OLE2_MAGIC):
        # Password-protected OOXML is an OLE2 compound file wrapping EncryptedPackage.
        raise ValueError("ENCRYPTED_REJECTED")
    sha = hashlib.sha256(raw).hexdigest()
    extra = {}
    if ext == "pdf":
        from folio.parser import parse_pdf

        return {**parse_pdf(raw), "parser_version": "folio-formats-v2"}
    if ext == "csv":
        cells, encoding, bom, delim = csv_cells(raw)
        extra["csv"] = {
            "encoding": encoding,
            "bom": bom,
            "dialect": {
                **CSV_DIALECT,
                "delimiter": delim,
                "detection": "folio-csv-dialect-v1",
            },
        }
    elif ext == "xlsx":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > 250
                or not xlsx_decompressed_bytes_within_limit(
                    sum(x.file_size for x in entries)
                )
            ):
                raise ValueError("XLSX_CONTAINER_REJECTED")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or not xlsx_decompressed_bytes_within_limit(entry.file_size)
                    or entry.file_size > max(1, entry.compress_size) * 100
                    or "externalLinks/" in entry.filename
                    or "vbaProject" in entry.filename
                ):
                    raise ValueError("XLSX_CONTAINER_REJECTED")
        declared = xlsx_sheets(raw)
        if not 1 <= len(declared) <= MAX_PAGES_PER_FILE:
            raise ValueError("PAGE_LIMIT")
        book, lexical_by_sheet = _load_xlsx_with_shared_strings(raw, declared)
        cells = []
        cells_by_sheet = {sheet["name"]: [] for sheet in declared}
        workbook_cell_count = 0
        projected_value_bytes = 0
        projected_metadata_bytes = 0
        try:
            if [s.title for s in book] != [d["name"] for d in declared]:
                raise ValueError("XLSX_SHEET_NAME_MISMATCH")
            for sheet, meta in zip(book, declared):
                lexical = lexical_by_sheet[sheet.title]
                # Limits come from the cells actually present, not from the
                # (optional, attacker-written) <dimension> element.
                from openpyxl.utils.cell import coordinate_to_tuple

                for ref in lexical:
                    try:
                        row_number, column_number = coordinate_to_tuple(ref or "")
                    except (TypeError, ValueError) as error:
                        raise ValueError("SHEET_LIMIT") from error
                    if (
                        row_number < 1
                        or column_number < 1
                        or row_number > MAX_XLSX_ROWS
                        or column_number > MAX_XLSX_COLUMNS
                    ):
                        raise ValueError("SHEET_LIMIT")
                cached_cells = iter(_sparse_xlsx_cells(book, sheet, data_only=True))
                parsed_cell_count = 0
                seen_coordinates = set()
                for cell in _sparse_xlsx_cells(book, sheet, data_only=False):
                    parsed_cell_count += 1
                    if cell.coordinate in seen_coordinates:
                        raise ValueError("XLSX_CELL_DUPLICATE")
                    seen_coordinates.add(cell.coordinate)
                    literal = lexical.get(cell.coordinate)
                    if literal is None:
                        raise ValueError("XLSX_CELL_MISMATCH")
                    cached_cell = next(cached_cells, None)
                    if (
                        cached_cell is None
                        or (cached_cell.row, cached_cell.column)
                        != (cell.row, cell.column)
                    ):
                        raise ValueError("XLSX_CELL_MISMATCH")
                    if cell.value is None and not literal["has_formula"]:
                        continue
                    formula = cell.value if cell.data_type == "f" else None
                    value = (
                        cached_cell.value
                        if formula
                        else literal["numeric_lexical"]
                        if literal["raw_type"] == "n"
                        else cell.value
                    )
                    if value is not None and not isinstance(
                        value, (str, int, float, bool)
                    ):
                        value = value.isoformat()
                    display_text = "" if value is None else str(value)
                    if len(display_text) > MAX_XLSX_CELL_TEXT_BYTES:
                        raise ValueError("XLSX_CELL_TEXT_LIMIT")
                    display_bytes = len(display_text.encode("utf-8"))
                    if display_bytes > MAX_XLSX_CELL_TEXT_BYTES:
                        raise ValueError("XLSX_CELL_TEXT_LIMIT")
                    metadata_text = (
                        sheet.title,
                        str(meta["sheet_id"]),
                        cell.coordinate,
                        str(cell.row),
                        str(cell.column),
                        "" if formula is None else str(formula),
                        "" if literal["raw_type"] is None else str(literal["raw_type"]),
                        "" if literal["raw_value"] is None else str(literal["raw_value"]),
                        ""
                        if literal["numeric_lexical"] is None
                        else str(literal["numeric_lexical"]),
                        "" if literal["raw_formula"] is None else str(literal["raw_formula"]),
                        "" if cell.number_format is None else str(cell.number_format),
                    )
                    metadata_bytes = 0
                    for part in metadata_text:
                        if len(part) > MAX_XLSX_CELL_TEXT_BYTES:
                            raise ValueError("XLSX_CELL_TEXT_LIMIT")
                        part_bytes = len(part.encode("utf-8"))
                        if part_bytes > MAX_XLSX_CELL_TEXT_BYTES:
                            raise ValueError("XLSX_CELL_TEXT_LIMIT")
                        metadata_bytes += part_bytes
                    projected_value_bytes += display_bytes
                    projected_metadata_bytes += metadata_bytes
                    projected_cell_count = workbook_cell_count + parsed_cell_count
                    if not xlsx_projection_within_budget(
                        projected_cell_count,
                        projected_value_bytes,
                        projected_metadata_bytes,
                    ):
                        raise ValueError("PARSER_OUTPUT_LIMIT")
                    output_cell = {
                            "sheet": sheet.title,
                            "sheet_id": meta["sheet_id"],
                            "coordinate": cell.coordinate,
                            "row": cell.row,
                            "column": cell.column,
                            "value": value,
                            "formula": formula,
                            "cached_value": value if formula else None,
                            **literal,
                            "requires_review": bool(formula)
                            or literal["has_formula"],
                            "provenance_kind": "xlsx-cell",
                            "number_format": cell.number_format,
                        }
                    cells.append(output_cell)
                    cells_by_sheet[sheet.title].append(output_cell)
                if (
                    parsed_cell_count != len(lexical)
                    or next(cached_cells, None) is not None
                ):
                    raise ValueError("XLSX_CELL_MISMATCH")
                workbook_cell_count += parsed_cell_count
        finally:
            book.close()
    else:
        raise ValueError("FORMAT_UNSUPPORTED")
    # Canonical projection for inspection only; cell provenance is authoritative.
    def project_page(page_cells, number):
        words = []
        parts = []
        text_length = 0
        for cell in page_cells:
            value = cell.get("value")
            if value is None or value == "":
                continue
            value_text = str(value)
            if ext == "xlsx" and len(value_text.encode("utf-8")) > MAX_XLSX_CELL_TEXT_BYTES:
                raise ValueError("XLSX_CELL_TEXT_LIMIT")
            if parts:
                parts.append(" ")
                text_length += 1
            start = text_length
            text_length += len(value_text)
            if ext == "xlsx" and text_length > MAX_XLSX_PROJECTED_TEXT_BYTES:
                raise ValueError("PARSER_OUTPUT_LIMIT")
            parts.append(value_text)
            words.append(
                {
                    "quote": value_text,
                    "start": start,
                    "end": text_length,
                    "boxes": [],
                    "cell": {k: v for k, v in cell.items() if k not in ("value",)},
                }
            )
        text = "".join(parts)
        return {
            "number": number,
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "words": words,
            "width": None,
            "height": None,
            "text_kind": "derived cell projection; no PDF geometry implied",
        }

    if ext == "xlsx":
        pages = [
            project_page(cells_by_sheet[meta["name"]], index)
            for index, meta in enumerate(declared, 1)
        ]
    else:
        pages = [project_page(cells, 1)]
    return {
        "sha256": sha,
        "kind": ext,
        "parser_version": "folio-formats-v2",
        **extra,
        "cells": cells,
        "pages": pages,
    }


class BoundedJsonWriter:
    """Write JSON text only while its encoded size remains under a hard cap."""

    def __init__(self, stream, max_bytes):
        self.stream = stream
        self.max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, text):
        remaining = self.max_bytes - self.bytes_written
        if len(text) > remaining:
            raise ValueError("PARSER_OUTPUT_LIMIT")
        byte_count = len(text.encode("utf-8"))
        if byte_count > remaining:
            raise ValueError("PARSER_OUTPUT_LIMIT")
        self.stream.write(text)
        self.bytes_written += byte_count
        return len(text)


if __name__ == "__main__":
    import base64
    import sys

    try:
        request = json.load(sys.stdin)
        raw = base64.b64decode(request["base64"], validate=True)
        writer = BoundedJsonWriter(
            sys.stdout, MAX_PARSER_OUTPUT_BYTES - MAX_PARSER_ERROR_BYTES
        )
        json.dump(
            parse_document(raw, request["name"]),
            writer,
            ensure_ascii=False,
            allow_nan=False,
        )
        writer.write("\n")
    except Exception as error:
        sys.stdout.write(
            json.dumps({"error": type(error).__name__ + ":" + str(error)[:200]})
            + "\n"
        )
        raise SystemExit(2)
