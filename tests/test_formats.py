import io
import zipfile

import pytest


def _column_name(number):
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _minimal_sparse_xlsx(
    coordinates,
    *,
    shared_value=None,
    shared_values=None,
    shared_indices=None,
    shared_xml_override=None,
):
    from xml.sax.saxutils import escape

    from openpyxl.utils.cell import coordinate_to_tuple

    if shared_values is None and shared_value is not None:
        shared_values = [shared_value]

    rows = {}
    for coordinate in coordinates:
        row, column = coordinate_to_tuple(coordinate)
        rows.setdefault(row, []).append((coordinate, column))
    max_row = max(rows)
    max_column = max(column for row in rows.values() for _, column in row)
    sheet_rows = []
    for row_number, row_cells in sorted(rows.items()):
        if shared_values is None:
            cells = "".join(
                f'<c r="{coordinate}" t="inlineStr"><is><t>v</t></is></c>'
                for coordinate, _ in sorted(row_cells, key=lambda item: item[1])
            )
        else:
            cells = "".join(
                f'<c r="{coordinate}" t="s"><v>'
                f'{(shared_indices or {}).get(coordinate, 0)}</v></c>'
                for coordinate, _ in sorted(row_cells, key=lambda item: item[1])
            )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{_column_name(max_column)}{max_row}"/>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    book = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    shared_rel = (
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        if shared_values is not None
        else ""
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        f"{shared_rel}</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        + (
            '<Override PartName="/xl/sharedStrings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            if shared_values is not None
            else ""
        )
        + '</Types>'
    )
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", book)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        if shared_values is not None:
            archive.writestr(
                "xl/sharedStrings.xml",
                shared_xml_override
                or (
                    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    f'count="{len(shared_values)}" uniqueCount="{len(shared_values)}">'
                    + "".join(
                        f"<si><t>{escape(value)}</t></si>" for value in shared_values
                    )
                    + "</sst>"
                ),
                compress_type=zipfile.ZIP_STORED,
            )
    return result.getvalue()


def test_csv_multiline_cells_keep_logical_row_column_and_original_utf8():
    from folio.formats import parse_document

    raw = 'supplier,notes,quantity\r\n"Aster, Inc.","first\nsecond",100\r\n'.encode()
    out = parse_document(raw, "receipt.csv")
    cell = next(x for x in out["cells"] if x["row"] == 2 and x["column"] == 2)
    assert cell["value"] == "first\nsecond"
    assert cell["quote"] == '"first\nsecond"'
    assert raw[cell["byte_start"] : cell["byte_end"]].decode() == cell["quote"]
    assert cell["provenance_kind"] == "csv-raw-field"


def test_twenty_mib_pdf_is_accepted_at_the_declared_file_boundary():
    from reportlab.pdfgen.canvas import Canvas

    from folio.formats import parse_document
    from folio.intake_limits import MAX_FILE_BYTES

    buf = io.BytesIO()
    pdf = Canvas(buf, pagesize=(200, 200))
    pdf.drawString(30, 150, "Authored 20 MiB intake boundary")
    pdf.save()
    raw = buf.getvalue()
    raw += b"\n%" + b"x" * (MAX_FILE_BYTES - len(raw) - 2)
    assert len(raw) == MAX_FILE_BYTES
    parsed = parse_document(raw, "boundary.pdf")
    assert len(parsed["pages"]) == 1
    assert parsed["sha256"]


def test_file_over_twenty_mib_is_rejected_before_pdf_parser(monkeypatch):
    from folio import parser
    from folio.formats import parse_document
    from folio.intake_limits import MAX_FILE_BYTES

    monkeypatch.setattr(
        parser, "parse_pdf", lambda _raw: pytest.fail("oversize reached parser")
    )
    with pytest.raises(ValueError, match="FILE_REJECTED"):
        parse_document(b"%PDF-" + b"0" * MAX_FILE_BYTES, "oversize.pdf")


def test_xlsx_cell_and_packet_page_quotas_match_original_limits():
    from folio.intake_limits import (
        MAX_XLSX_CELLS,
        MAX_XLSX_DECOMPRESSED_BYTES,
        source_within_packet_limits,
        xlsx_cell_count_within_limit,
        xlsx_decompressed_bytes_within_limit,
    )

    assert xlsx_cell_count_within_limit(MAX_XLSX_CELLS)
    assert not xlsx_cell_count_within_limit(MAX_XLSX_CELLS + 1)
    assert xlsx_decompressed_bytes_within_limit(MAX_XLSX_DECOMPRESSED_BYTES)
    assert not xlsx_decompressed_bytes_within_limit(MAX_XLSX_DECOMPRESSED_BYTES + 1)
    assert source_within_packet_limits(0, 150, 50)
    assert not source_within_packet_limits(0, 150, 51)
    assert source_within_packet_limits(9, 150, 50)
    assert not source_within_packet_limits(10, 150, 1)
    assert not source_within_packet_limits(9, 151, 50)


def test_single_sheet_accepts_exactly_one_hundred_thousand_cells_with_provenance():
    from folio.formats import parse_document

    coords = [
        f"{_column_name(column)}{row}"
        for row in range(1, 1001)
        for column in range(1, 101)
    ]
    parsed = parse_document(_minimal_sparse_xlsx(coords), "100k.xlsx")
    assert len(parsed["cells"]) == 100_000
    first, last = parsed["cells"][0], parsed["cells"][-1]
    assert (first["coordinate"], first["row"], first["column"]) == ("A1", 1, 1)
    assert (last["coordinate"], last["row"], last["column"]) == ("CV1000", 1000, 100)
    assert first["value"] == last["value"] == "v"
    assert first["provenance_kind"] == last["provenance_kind"] == "xlsx-cell"
    assert first["raw_type"] == last["raw_type"] == "inlineStr"


def test_sparse_excel_max_coordinate_does_not_allocate_a_dense_rectangle(monkeypatch):
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    from folio.formats import parse_document

    monkeypatch.setattr(
        ReadOnlyWorksheet,
        "iter_rows",
        lambda *_args, **_kwargs: pytest.fail("dense worksheet iteration used"),
    )
    parsed = parse_document(
        _minimal_sparse_xlsx(["XFD1048576"]), "sparse-max-coordinate.xlsx"
    )
    assert len(parsed["cells"]) == 1
    cell = parsed["cells"][0]
    assert (cell["coordinate"], cell["row"], cell["column"]) == (
        "XFD1048576",
        1_048_576,
        16_384,
    )
    assert cell["value"] == "v" and cell["provenance_kind"] == "xlsx-cell"


def test_single_sheet_rejects_one_cell_over_the_workbook_total():
    from folio.formats import parse_document

    coords = [
        f"{_column_name(column)}{row}"
        for row in range(1, 2527)
        for column in range(1, 100)
    ][:250_001]
    assert len(coords) == 250_001
    with pytest.raises(ValueError, match="CELL_LIMIT"):
        parse_document(_minimal_sparse_xlsx(coords), "over-100k.xlsx")


def test_xlsx_sheet_page_limit_accepts_twenty_one_and_fifty_rejects_fifty_one():
    from openpyxl import Workbook

    from folio.formats import parse_document

    def workbook_bytes(sheet_count):
        book = Workbook()
        book.remove(book.active)
        for index in range(sheet_count):
            book.create_sheet(f"Sheet-{index + 1}")["A1"] = f"value-{index + 1}"
        output = io.BytesIO()
        book.save(output)
        return output.getvalue()

    for sheet_count in (21, 50):
        parsed = parse_document(workbook_bytes(sheet_count), f"{sheet_count}-sheets.xlsx")
        assert len(parsed["pages"]) == sheet_count
        assert parsed["pages"][0]["words"][0]["quote"] == "value-1"
        assert parsed["pages"][-1]["words"][0]["quote"] == f"value-{sheet_count}"
    with pytest.raises(ValueError, match="PAGE_LIMIT"):
        parse_document(workbook_bytes(51), "51-sheets.xlsx")


def test_xlsx_shared_string_fanout_is_bounded_before_cell_projection():
    from folio.formats import parse_document

    coordinates = [f"A{row}" for row in range(1, 10_001)]
    raw = _minimal_sparse_xlsx(coordinates, shared_value="x" * 1024)
    with pytest.raises(ValueError, match="PARSER_OUTPUT_LIMIT"):
        parse_document(raw, "shared-string-fanout.xlsx")


def test_xlsx_oversize_single_shared_string_fails_before_projected_cell_creation():
    from folio.formats import parse_document
    from folio.intake_limits import MAX_XLSX_CELL_TEXT_BYTES

    raw = _minimal_sparse_xlsx(
        ["A1", "A2"], shared_value="x" * (MAX_XLSX_CELL_TEXT_BYTES + 1)
    )
    with pytest.raises(ValueError, match="XLSX_CELL_TEXT_LIMIT"):
        parse_document(raw, "oversize-cell-text.xlsx")


def test_xlsx_large_unreferenced_shared_string_table_is_streamed_not_materialized(
    monkeypatch,
):
    from openpyxl.reader.excel import ExcelReader

    from folio.formats import parse_document

    monkeypatch.setattr(
        ExcelReader,
        "read_strings",
        lambda *_args: pytest.fail("openpyxl eagerly materialized the SST"),
    )
    unused_count = 200_000
    values = ["unreferenced" for _ in range(unused_count)] + ["used value"]
    raw = _minimal_sparse_xlsx(
        ["A1"], shared_values=values, shared_indices={"A1": unused_count}
    )
    parsed = parse_document(raw, "large-unreferenced-sst.xlsx")
    assert parsed["cells"][0]["value"] == "used value"
    assert parsed["cells"][0]["provenance_kind"] == "xlsx-cell"


def test_xlsx_referenced_oversize_sst_entry_is_rejected_during_streaming(monkeypatch):
    from openpyxl.reader.excel import ExcelReader

    from folio.formats import parse_document
    from folio.intake_limits import MAX_XLSX_CELL_TEXT_BYTES

    monkeypatch.setattr(
        ExcelReader,
        "read_workbook",
        lambda *_args: pytest.fail("oversized SST entry reached workbook loading"),
    )
    raw = _minimal_sparse_xlsx(
        ["A1"], shared_value="x" * (MAX_XLSX_CELL_TEXT_BYTES + 1)
    )
    with pytest.raises(ValueError, match="XLSX_CELL_TEXT_LIMIT"):
        parse_document(raw, "streamed-oversize-sst.xlsx")


def test_xlsx_one_hundred_thousand_unique_shared_strings_keep_provenance():
    from folio.formats import parse_document

    coordinates = [
        f"{_column_name(column)}{row}"
        for row in range(1, 1001)
        for column in range(1, 101)
    ]
    shared_values = [f"value-{index:06d}" for index in range(100_000)]
    shared_indices = {coordinate: index for index, coordinate in enumerate(coordinates)}
    parsed = parse_document(
        _minimal_sparse_xlsx(
            coordinates,
            shared_values=shared_values,
            shared_indices=shared_indices,
        ),
        "100k-unique-sst.xlsx",
    )
    assert len(parsed["cells"]) == 100_000
    first, last = parsed["cells"][0], parsed["cells"][-1]
    assert first["value"] == "value-000000"
    assert last["value"] == "value-099999"
    assert first["provenance_kind"] == last["provenance_kind"] == "xlsx-cell"


def test_xlsx_foreign_si_does_not_shift_canonical_shared_string_indices():
    from folio.formats import parse_document

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    foreign = "urn:attacker-extension"
    shared_xml = (
        f'<sst xmlns="{main}" xmlns:e="{foreign}">'
        '<e:si><e:t>foreign text</e:t></e:si>'
        '<si><t>canonical index zero</t></si></sst>'
    )
    parsed = parse_document(
        _minimal_sparse_xlsx(
            ["A1"],
            shared_values=["canonical index zero"],
            shared_xml_override=shared_xml,
        ),
        "foreign-si.xlsx",
    )
    cell = parsed["cells"][0]
    assert cell["value"] == "canonical index zero"
    assert cell["provenance_kind"] == "xlsx-cell"


def test_xlsx_foreign_t_and_rph_collisions_fail_closed():
    from folio.formats import parse_document

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    foreign = "urn:attacker-extension"
    bodies = (
        '<si><e:t>foreign text</e:t><t>canonical text</t></si>',
        '<si><e:rPh><t>foreign phonetic text</t></e:rPh>'
        '<t>canonical text</t></si>',
    )
    for body in bodies:
        shared_xml = f'<sst xmlns="{main}" xmlns:e="{foreign}">{body}</sst>'
        raw = _minimal_sparse_xlsx(
            ["A1"],
            shared_values=["canonical text"],
            shared_xml_override=shared_xml,
        )
        with pytest.raises(ValueError, match="XLSX_XML_REJECTED"):
            parse_document(raw, "foreign-shared-string-tags.xlsx")


def test_xlsx_prefixed_spreadsheetml_rich_text_keeps_canonical_provenance():
    from folio.formats import parse_document

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared_xml = (
        f'<s:sst xmlns:s="{main}"><s:si><s:r><s:t>prefixed rich text</s:t>'
        "</s:r></s:si></s:sst>"
    )
    parsed = parse_document(
        _minimal_sparse_xlsx(
            ["A1"],
            shared_values=["prefixed rich text"],
            shared_xml_override=shared_xml,
        ),
        "prefixed-spreadsheetml.xlsx",
    )
    cell = parsed["cells"][0]
    assert cell["value"] == "prefixed rich text"
    assert cell["provenance_kind"] == "xlsx-cell"


def test_bounded_json_writer_refuses_output_before_writing_past_quota():
    from folio.formats import BoundedJsonWriter

    destination = io.StringIO()
    writer = BoundedJsonWriter(destination, max_bytes=8)
    assert writer.write("1234") == 4
    with pytest.raises(ValueError, match="PARSER_OUTPUT_LIMIT"):
        writer.write("56789")
    assert destination.getvalue() == "1234"


def test_xlsx_formula_has_literal_and_cached_value_without_evaluation():
    from openpyxl import Workbook

    from folio.formats import parse_document

    book = Workbook()
    book.active["A1"] = "Invoice"
    book.active["B1"] = "=2+2"
    buf = io.BytesIO()
    book.save(buf)
    out = parse_document(buf.getvalue(), "invoice.xlsx")
    cell = next(x for x in out["cells"] if x["coordinate"] == "B1")
    assert cell["formula"] == "=2+2"
    assert cell["cached_value"] is None
    assert cell["value"] is None and cell["requires_review"] is True
    assert cell["sheet"] == "Sheet"


def test_zip_bomb_and_path_traversal_are_rejected_before_xml_parser():
    import zipfile

    from folio.formats import parse_document

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("../escape", b"x" * 1000000)
    with pytest.raises(ValueError, match="XLSX_CONTAINER_REJECTED"):
        parse_document(buf.getvalue(), "bad.xlsx")


def test_cached_formula_does_not_silently_become_an_accepted_required_field():
    import zipfile
    from xml.etree import ElementTree as ET

    from openpyxl import Workbook

    from folio.formats import parse_document
    from folio.provenance import extract_packet

    book = Workbook()
    book.active["A1"] = "Received quantity"
    book.active["B1"] = "=60+40"
    original = io.BytesIO()
    book.save(original)
    out = io.BytesIO()
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(out, "w") as dst:
        for member in src.infolist():
            data = src.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                tree = ET.fromstring(data)
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                tree.find("s:sheetData/s:row/s:c[@r='B1']/s:v", ns).text = "100"
                data = ET.tostring(tree)
            dst.writestr(member, data)
    source = {
        **parse_document(out.getvalue(), "cached.xlsx"),
        "id": "authored-cached-formula",
        "document_role": "receipt",
    }
    assert source["cells"][1]["cached_value"] == 100
    extracted = extract_packet([source])
    assert "received_quantity" not in extracted["values"]
    assert "received_quantity" in extracted["missing"]
