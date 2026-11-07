"""Pinned ISO 4217 minor-unit table and currency-mark reading (folio-currency-v1).

Money is a decimal string with an explicit currency and the currency's scale.
No exchange rate exists anywhere in Folio: two currencies are a mismatch,
never a conversion.

Currency marks printed on documents are read as:
* an ISO 4217 alphabetic code ("EUR", "jpy") from the pinned table below;
* an unambiguous symbol or prefixed dollar ("€", "£", "₩", "US$", "C$");
* an ambiguous symbol ("$", "¥", "kr", "Rs") as a *family* of codes. A family
  never decides a currency by itself; it is only compatible or incompatible
  with an explicit code found elsewhere in the packet.
"""

POLICY = "iso4217-folio-pin-2026-10"

# Active ISO 4217 codes with their minor units (fund codes, precious metals,
# testing codes and 4-decimal units of account are deliberately absent).
_ZERO = "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF"
_THREE = "BHD IQD JOD KWD LYD OMR TND"
_TWO = (
    "AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BRL BSD BTN BWP "
    "BYN BZD CAD CDF CHF CNY COP CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP "
    "GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW "
    "KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR "
    "MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR "
    "SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS "
    "UAH USD UYU UZS VED VES XCD XCG YER ZAR ZMW ZWG"
)
MINOR_UNITS = {
    **{c: 0 for c in _ZERO.split()},
    **{c: 2 for c in _TWO.split()},
    **{c: 3 for c in _THREE.split()},
}

DOLLARS = frozenset(
    "USD CAD AUD NZD SGD HKD TWD MXN ARS CLP COP BSD BBD BMD BND BZD FJD GYD JMD KYD LRD "
    "NAD SBD SRD TTD XCD ZWG".split()
)
SYMBOLS = {
    "€": "EUR", "£": "GBP", "₩": "KRW", "₹": "INR", "₺": "TRY", "₽": "RUB", "₪": "ILS",
    "₫": "VND", "₱": "PHP", "₦": "NGN", "฿": "THB", "₴": "UAH", "₸": "KZT", "₼": "AZN",
    "₾": "GEL", "円": "JPY", "元": "CNY", "us$": "USD", "u$s": "USD", "c$": "CAD", "ca$": "CAD",
    "can$": "CAD", "a$": "AUD", "au$": "AUD", "nz$": "NZD", "s$": "SGD", "sg$": "SGD",
    "hk$": "HKD", "nt$": "TWD", "mx$": "MXN", "r$": "BRL", "zł": "PLN", "kč": "CZK",
    "lei": "RON",
}
FAMILIES = {
    "$": DOLLARS,
    "¥": frozenset({"JPY", "CNY"}),
    "￥": frozenset({"JPY", "CNY"}),
    "kr": frozenset({"SEK", "NOK", "DKK", "ISK"}),
    "kr.": frozenset({"SEK", "NOK", "DKK", "ISK"}),
    "rs": frozenset({"INR", "PKR", "LKR", "NPR"}),
    "rs.": frozenset({"INR", "PKR", "LKR", "NPR"}),
    "fr": frozenset({"CHF", "XOF", "XAF", "XPF"}),
    "fr.": frozenset({"CHF", "XOF", "XAF", "XPF"}),
}


class Mark:
    """One printed currency mark: an exact code, or a family of codes."""

    __slots__ = ("code", "family", "text")

    def __init__(self, text, code=None, family=None):
        self.text, self.code, self.family = text, code, family

    def compatible(self, code):
        return code == self.code if self.code else code in (self.family or ())

    def as_dict(self):
        if self.code:
            return {"code": self.code}
        return {"family": sorted(self.family)}


def read_mark(text):
    """Mark for a standalone token ("EUR", "€", "$", "kr"), or None."""
    if not isinstance(text, str) or not text or len(text) > 6:
        return None
    t = text.strip().rstrip(":;,")
    if len(t) == 3 and t.isascii() and t.isalpha() and t.upper() in MINOR_UNITS:
        # A lower-case word that happens to be a code ("all", "top", "cup")
        # is ordinary text; codes are printed in capitals.
        if t.isupper():
            return Mark(text, code=t.upper())
        return None
    low = t.casefold()
    if low in SYMBOLS:
        return Mark(text, code=SYMBOLS[low])
    if low in FAMILIES:
        return Mark(text, family=FAMILIES[low])
    return None


# Longest first, so "US$" is not read as "$".
PREFIXES = sorted(
    [k for k in SYMBOLS if not k.isalpha()] + [k for k in FAMILIES if not k.isalpha() or k == "kr"],
    key=len,
    reverse=True,
)


def split_mark(token):
    """(mark, number_text, offset) for "€12,50", "12.50€", "USD1,200", else None."""
    if not isinstance(token, str) or not token:
        return None
    low = token.casefold()
    for p in PREFIXES:
        if low.startswith(p) and len(token) > len(p) and token[len(p)].isdigit():
            mark = read_mark(token[: len(p)])
            if mark:
                return mark, token[len(p):], len(p)
        if low.endswith(p) and len(token) > len(p) and token[-len(p) - 1].isdigit() and p not in ("kr", "kr.", "rs", "rs."):
            mark = read_mark(token[-len(p):])
            if mark:
                return mark, token[: -len(p)], 0
    if len(token) > 3 and token[:3].isupper() and token[:3].isalpha() and token[3].isdigit():
        mark = read_mark(token[:3])
        if mark:
            return mark, token[3:], 3
    if len(token) > 3 and token[-3:].isupper() and token[-3:].isalpha() and token[-4].isdigit():
        mark = read_mark(token[-3:])
        if mark:
            return mark, token[:-3], 0
    return None


def minor_units(code):
    return MINOR_UNITS.get(code)


def scale_ok(value, code):
    """An amount at currency scale has at most the currency's minor units."""
    places = minor_units(code)
    if places is None or not isinstance(value, str):
        return False
    return len(value.partition(".")[2]) <= places


def resolve(marks):
    """Packet currency from marks: (code, None) | (None, "conflict") | (None, None).

    Exactly one explicit code, with every family mark compatible with it,
    decides. Two codes, or a family mark incompatible with the code, is a
    conflict. Families alone decide nothing.
    """
    codes = {m.code for m in marks if m.code}
    if len(codes) > 1:
        return None, "conflict"
    families = [m for m in marks if not m.code]
    if codes:
        code = codes.pop()
        if any(not m.compatible(code) for m in families):
            return None, "conflict"
        return code, None
    if families:
        common = frozenset.intersection(*[frozenset(m.family) for m in families])
        if not common:
            return None, "conflict"
    return None, None
