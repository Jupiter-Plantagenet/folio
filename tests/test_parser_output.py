import hashlib

import pytest


def _xlsx_output(cell_count):
    cells = [
        {"sheet": "Sheet", "sheet_id": "1", "coordinate": f"A{row}", "value": "v"}
        for row in range(1, cell_count + 1)
    ]
    text = ""
    return {
        "kind": "xlsx",
        "sha256": "a" * 64,
        "cells": cells,
        "pages": [
            {
                "number": 1,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "words": [],
                "width": None,
                "height": None,
            }
        ],
    }


def test_host_parser_output_accepts_exact_workbook_cell_quota():
    from folio.intake_limits import MAX_XLSX_CELLS
    from folio.parser_output import validate

    parsed = _xlsx_output(MAX_XLSX_CELLS)
    assert validate(parsed, b"authored input") is parsed


def test_host_parser_output_rejects_one_cell_over_quota():
    from folio.intake_limits import MAX_XLSX_CELLS
    from folio.parser_output import validate

    parsed = _xlsx_output(MAX_XLSX_CELLS + 1)
    with pytest.raises(ValueError, match="PARSER_OUTPUT_INVALID:XLSX_CELL_LIMIT"):
        validate(parsed, b"authored input")


def test_host_parser_output_rejects_oversize_cell_projection():
    from folio.intake_limits import MAX_XLSX_CELL_TEXT_BYTES
    from folio.parser_output import validate

    parsed = _xlsx_output(1)
    parsed["cells"][0]["value"] = "x" * (MAX_XLSX_CELL_TEXT_BYTES + 1)
    with pytest.raises(
        ValueError, match="PARSER_OUTPUT_INVALID:XLSX_CELL_TEXT_LIMIT"
    ):
        validate(parsed, b"authored input")


def test_host_parser_output_rejects_cumulative_projection_before_string_walk():
    from folio.parser_output import validate

    parsed = _xlsx_output(5_000)
    repeated = "x" * 1024
    for cell in parsed["cells"]:
        cell["value"] = repeated
    with pytest.raises(ValueError, match="PARSER_OUTPUT_INVALID:XLSX_PROJECTION_LIMIT"):
        validate(parsed, b"authored input")
