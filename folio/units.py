"""Pinned unit-of-measure table (folio-unit-v2); no conversions, ever.

A printed unit word is mapped to one canonical unit, or to nothing. Spellings
of a plain count ("each", "ea", "pcs", "pieces", "units", "Stk") are one unit,
"each": they all count single items. Every other unit (kg, m, box, carton,
pair, dozen, ...) is its own canonical unit. Two different canonical units are
a mismatch: no factor (12 per dozen, 1000 g per kg, items per box) is applied,
because the factor is a business fact the documents do not prove.
"""

import re

POLICY = "folio-unit-v2"

_TABLE = {
    "each": [
        "each", "ea", "ea.", "pc", "pcs", "pce", "pces", "piece", "pieces", "unit", "units",
        "item", "items", "nos", "stk", "stk.", "stck", "stück", "stueck", "st", "st.",
        "un", "und", "ud", "uds", "unid", "unidad", "unidades", "unité", "unités", "pz", "pezzi",
    ],
    "dozen": ["dozen", "dozens", "doz", "dz"],
    "pair": ["pair", "pairs", "pr", "prs", "paar"],
    "box": ["box", "boxes", "bx", "karton"],
    "carton": ["carton", "cartons", "ctn", "ctns"],
    "case": ["case", "cases", "cs"],
    "pack": ["pack", "packs", "pk", "pkt", "pkts", "packet", "packets"],
    "pallet": ["pallet", "pallets", "plt", "plts"],
    "kit": ["kit", "kits"],
    "roll": ["roll", "rolls"],
    "set": ["set", "sets"],
    "bag": ["bag", "bags", "sack", "sacks"],
    "bottle": ["bottle", "bottles", "btl"],
    "drum": ["drum", "drums"],
    "bundle": ["bundle", "bundles"],
    "sheet": ["sheet", "sheets"],
    "ream": ["ream", "reams"],
    "kg": ["kg", "kgs", "kilogram", "kilograms", "kilo", "kilos"],
    "g": ["g", "gram", "grams", "gramm"],
    "t": ["tonne", "tonnes", "metric ton", "mt"],
    "lb": ["lb", "lbs", "pound", "pounds"],
    "oz": ["oz"],
    "m": ["m", "metre", "metres", "meter", "meters", "lfm"],
    "cm": ["cm"],
    "mm": ["mm"],
    "km": ["km"],
    "ft": ["ft", "feet", "foot"],
    "yd": ["yd", "yds", "yard", "yards"],
    "m2": ["m2", "m²", "sqm", "sq m"],
    "m3": ["m3", "m³", "cbm"],
    "l": ["l", "litre", "litres", "liter", "liters", "ltr", "ltrs"],
    "ml": ["ml"],
    "gal": ["gal", "gallon", "gallons"],
    "h": ["hour", "hours", "hr", "hrs", "h"],
    "day": ["day", "days"],
}

CANONICAL = {}
for _unit, _spellings in _TABLE.items():
    for _s in _spellings:
        CANONICAL[_s] = _unit
UNITS = frozenset(_TABLE)
# These spellings are symbols, not words. Their case is part of their
# identity (for example, M is not the metre symbol m). The single-letter L is
# retained as an explicit common domain alias for litre.
CASE_SENSITIVE_SYMBOLS = frozenset(
    {"m", "g", "t", "h", "l", "kg", "cm", "mm", "km", "m2", "m3", "m²", "m³", "ml"}
)
# Spellings that are also everyday words or single letters: accepted only in a
# unit position (right after a quantity, a unit label or a unit column),
# never as a label of their own.
WEAK = frozenset({"g", "h", "l", "m", "t", "st", "st.", "un", "cs", "set", "case", "day", "pound", "pounds", "foot", "item", "items", "unit", "units"})


def _key(text):
    t = text.casefold().strip()
    t = t.strip("()[]{}\"'")
    t = t.rstrip(":;,")
    return t


def canonical_unit(text):
    """Canonical unit for one printed unit word, or None (never guessed)."""
    if not isinstance(text, str) or not text or len(text) > 24:
        return None
    raw = text.strip().strip("()[]{}\"'").rstrip(":;,")
    t = raw.casefold()
    # Do not let case-folded symbols silently turn into another unit. The
    # explicit L -> litre alias is widely used in business documents.
    if t in CASE_SENSITIVE_SYMBOLS and raw != t:
        # All-capitals printing of a multi-letter symbol ("KG", "MM") is that
        # symbol; single letters and mixed case ("M", "Kg") stay unread.
        if raw == "L":
            return "l"
        if not (len(t) > 1 and raw == t.upper()):
            return None
    if t in CANONICAL:
        return CANONICAL[t]
    if t.endswith(".") and t[:-1] in CANONICAL:
        return CANONICAL[t[:-1]]
    return None


def heading_unit(text):
    """Unit printed inside a column heading: "Qty (pcs)", "Menge [kg]"."""
    m = re.search(r"[(\[]([^()\[\]]{1,12})[)\]]\s*$", text or "")
    return canonical_unit(m.group(1)) if m else None


def normalization(quote, unit):
    """Record for a quote that differs from its canonical unit."""
    return None if quote == unit else {"rule": POLICY, "unit": unit}


def recompute(quote, record):
    """Recompute a recorded unit normalization; raises ValueError on mismatch."""
    if not isinstance(record, dict) or record.get("rule") != POLICY:
        raise ValueError("UNIT_NORMALIZATION_INVALID")
    unit = canonical_unit(quote)
    if unit is None or unit != record.get("unit"):
        raise ValueError("UNIT_NORMALIZATION_INVALID")
    return unit
