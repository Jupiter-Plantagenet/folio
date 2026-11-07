"""Locale-aware decimal reading (folio-number-v2); never guesses an ambiguous token.

A printed number is converted to the canonical fixed-point text used by every
Folio field ("1234.50"), or rejected. The decision inputs (decimal separator and
whether OCR digit repair was applied) are recorded beside the quote so any
consumer can recompute the same value from the cited literal.
"""

import re

RULE = "folio-number-v2"
# Grouping characters other than "." and ",": apostrophe (de-CH) and the
# no-break / narrow / thin spaces used by fr/de typesetting inside one token.
_GROUP_SPACES = "'’   "
# Common OCR glyph confusions, applied ONLY inside a token that already
# contains a real digit and otherwise consists of number characters.
_OCR_DIGITS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5"})
_NUMBERISH = re.compile(r"[0-9.,'’   ]+")
_CANON = re.compile(r"(0|[1-9]\d{0,11})(\.\d{1,6})?")


class NumberError(ValueError):
    pass


def ocr_repair(token):
    """Map OCR letter/digit confusions within a numeric token, else None."""
    if not any(ch.isdigit() for ch in token):
        return None
    repaired = token.translate(_OCR_DIGITS)
    if repaired == token or not _NUMBERISH.fullmatch(repaired):
        return None
    letters = sum(1 for a, b in zip(token, repaired) if a != b)
    real = sum(1 for ch in token if ch.isdigit())
    # A word with a stray digit ("S5O") is not a number: real digits must be
    # at least as many as repaired glyphs.
    if real < letters:
        return None
    return repaired


def _canonical(integral, fraction):
    integral = integral.lstrip("0") or "0"
    text = integral + ("." + fraction if fraction else "")
    if not _CANON.fullmatch(text):
        raise NumberError("NUMBER_RANGE")
    return text


def separator_evidence(token):
    """Return the decimal separator a token proves on its own, or None."""
    t = token.strip()
    if not re.fullmatch(r"\d[\d.,]*\d|\d", t):
        return None
    dots, commas = t.count("."), t.count(",")
    if dots and commas:
        return "." if t.rfind(".") > t.rfind(",") else ","
    for sep, count in ((".", dots), (",", commas)):
        if count == 1:
            tail = t.rsplit(sep, 1)[1]
            if len(tail) != 3 or t.split(sep)[0] == "0":
                return sep
            return None
        if count > 1 and re.fullmatch(r"\d{1,3}(" + re.escape(sep) + r"\d{3})+", t):
            return "," if sep == "." else "."
    return None


def infer_decimal(tokens):
    """Document decimal separator from unambiguous tokens; None if absent/mixed."""
    seen = {s for s in (separator_evidence(t) for t in tokens) if s}
    return seen.pop() if len(seen) == 1 else None


def parse_number(token, decimal=None, ocr=False, spaces=False):
    """Canonical decimal text for one printed token.

    ``decimal`` is the document decimal separator when known. A lone separator
    followed by exactly three digits is ambiguous without it (1,234 vs 1.234)
    and is rejected rather than guessed.
    """
    if not isinstance(token, str) or not token or len(token) > 40:
        raise NumberError("NUMBER_INVALID")
    t = token.strip()
    if spaces:
        # Plain-space thousands groups ("12 500,00"), only on explicit request
        # (folio-layout-v2 money with a currency mark); same exact-3 rule.
        if not re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", t):
            raise NumberError("NUMBER_INVALID")
        t = t.replace(" ", "\u00a0")
    if ocr:
        repaired = ocr_repair(t)
        if repaired is not None:
            t = repaired
    if not t or not t[0].isdigit() or not t[-1].isdigit():
        raise NumberError("NUMBER_INVALID")
    # Space/apostrophe grouping is accepted only in exact 3-digit groups.
    if any(ch in t for ch in _GROUP_SPACES):
        m = re.fullmatch(r"(\d{1,3}(?:[" + _GROUP_SPACES + r"]\d{3})+)([.,]\d+)?", t)
        if not m:
            raise NumberError("NUMBER_INVALID")
        integral = re.sub("[" + _GROUP_SPACES + "]", "", m.group(1))
        fraction = m.group(2)[1:] if m.group(2) else ""
        return _canonical(integral, fraction)
    if not re.fullmatch(r"[\d.,]+", t):
        raise NumberError("NUMBER_INVALID")
    dots, commas = t.count("."), t.count(",")
    if not dots and not commas:
        return _canonical(t, "")
    if dots and commas:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        group = "," if dec == "." else "."
        if t.count(dec) != 1:
            raise NumberError("NUMBER_INVALID")
        integral, fraction = t.split(dec)
        if not re.fullmatch(r"\d{1,3}(" + re.escape(group) + r"\d{3})*", integral):
            raise NumberError("NUMBER_INVALID")
        return _canonical(integral.replace(group, ""), fraction)
    sep = "." if dots else ","
    parts = t.split(sep)
    if len(parts) > 2:
        if all(len(p) == 3 for p in parts[1:]) and 1 <= len(parts[0]) <= 3:
            if decimal == sep:
                raise NumberError("NUMBER_INVALID")
            return _canonical("".join(parts), "")
        # OCR may print a grouping comma as a period ("1.333.20"): only when
        # every earlier group is a 3-digit group and the last has two digits.
        if (
            ocr
            and len(parts[-1]) == 2
            and 1 <= len(parts[0]) <= 3
            and all(len(p) == 3 for p in parts[1:-1])
        ):
            return _canonical("".join(parts[:-1]), parts[-1])
        raise NumberError("NUMBER_INVALID")
    integral, fraction = parts
    if not integral or not fraction:
        raise NumberError("NUMBER_INVALID")
    if len(fraction) != 3 or integral == "0":
        return _canonical(integral, fraction)
    if decimal == sep:
        return _canonical(integral, fraction)
    if decimal is not None and len(integral) <= 3:
        return _canonical(integral + fraction, "")
    raise NumberError("NUMBER_AMBIGUOUS")


def read_number(token, decimal=None, ocr=False):
    """(canonical, normalization-record-or-None) for a cited literal."""
    value = parse_number(token, decimal=decimal, ocr=ocr)
    if value == token:
        return value, None
    return value, {"rule": RULE, "decimal": decimal, "ocr": bool(ocr)}


def recompute(quote, normalization):
    """Recompute a recorded normalization; raises on any mismatch of rule."""
    if not isinstance(normalization, dict) or normalization.get("rule") != RULE:
        raise NumberError("NORMALIZATION_INVALID")
    decimal = normalization.get("decimal")
    if decimal not in (None, ".", ","):
        raise NumberError("NORMALIZATION_INVALID")
    return parse_number(
        quote,
        decimal=decimal,
        ocr=normalization.get("ocr") is True,
        spaces=normalization.get("space_groups") is True,
    )
