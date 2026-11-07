"""folio-number-v2: locale-aware, OCR-repairing, never guessing."""

import pytest

from folio.numbers import (
    NumberError,
    infer_decimal,
    ocr_repair,
    parse_number,
    read_number,
    recompute,
)


@pytest.mark.parametrize(
    "token,decimal,want",
    [
        ("120", None, "120"),
        ("48.00", None, "48.00"),
        ("4,25", None, "4.25"),
        ("1,234.50", None, "1234.50"),
        ("1.234,50", None, "1234.50"),
        ("1'234.50", None, "1234.50"),
        ("1 234,50", None, "1234.50"),
        ("1,234,567", None, "1234567"),
        ("1.234.567", None, "1234567"),
        ("0,125", None, "0.125"),
        ("1,200", ".", "1200"),
        ("1.200", ",", "1200"),
        ("1,200", ",", "1.200"),
        ("1.200", ".", "1.200"),
        ("007", None, "7"),
    ],
)
def test_locale_numbers(token, decimal, want):
    assert parse_number(token, decimal=decimal) == want


@pytest.mark.parametrize(
    "token,decimal",
    [
        ("1,200", None),  # en 1200 or de 1.2: never guessed
        ("1.200", None),
        ("1,2,3", None),
        ("1.234.56", None),  # digital text: no OCR repair
        ("12a", None),
        ("-5", None),
        ("", None),
        ("1e5", None),
        ("NaN", None),
        ("1 234", None),  # ordinary space is not a grouping glyph inside one token
        ("1,234,567", ","),  # grouping contradicts the document decimal
        ("1" * 13, None),  # beyond the 12 integral digit policy
    ],
)
def test_ambiguous_or_invalid_is_rejected(token, decimal):
    with pytest.raises(NumberError):
        parse_number(token, decimal=decimal)


def test_ocr_repairs_only_inside_numeric_tokens():
    assert ocr_repair("1O5") == "105"
    assert ocr_repair("l2") == "12"
    assert ocr_repair("S0") == "50"
    assert ocr_repair("SOLD") is None  # no digit: a word, not a number
    assert ocr_repair("S5O") is None  # more glyph repairs than real digits
    assert ocr_repair("each") is None
    assert parse_number("1O5", ocr=True) == "105"
    with pytest.raises(NumberError):
        parse_number("1O5")  # digital text is never "repaired"
    # comma/period confusion: grouping comma printed as a period
    assert parse_number("1.333.20", ocr=True) == "1333.20"


def test_document_decimal_inference():
    assert infer_decimal(["4.25", "1,200"]) == "."
    assert infer_decimal(["2,50", "1.200"]) == ","
    assert infer_decimal(["1,200"]) is None
    assert infer_decimal(["4.25", "2,50"]) is None  # mixed evidence: unknown


def test_normalization_record_recomputes_exactly():
    value, record = read_number("1.234,50", decimal=",")
    assert value == "1234.50" and record["decimal"] == ","
    assert recompute("1.234,50", record) == "1234.50"
    assert read_number("120") == ("120", None)
    with pytest.raises(NumberError):
        recompute("1.234,50", {**record, "rule": "other"})
    with pytest.raises(NumberError):
        recompute("1,200", {"rule": "folio-number-v2", "decimal": None, "ocr": False})
