// Keep this display table aligned with folio/money.py's pinned MINOR_UNITS.
// It is used only to explain server rounding policy; it never rounds values.
const ZERO_MINOR_UNIT = new Set(
  "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split(" "),
);
const TWO_MINOR_UNIT = new Set(
  (
    "AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BRL BSD BTN BWP " +
    "BYN BZD CAD CDF CHF CNY COP CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP " +
    "GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW " +
    "KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR " +
    "MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR " +
    "SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS " +
    "UAH USD UYU UZS VED VES XCD XCG YER ZAR ZMW ZWG"
  ).split(" "),
);
const THREE_MINOR_UNIT = new Set("BHD IQD JOD KWD LYD OMR TND".split(" "));

export function displayCurrency(currency?: string | null): string {
  return currency || "Currency unresolved";
}

export function displayUnit(unit?: string | null): string {
  // Historical v1 packets omitted a unit and are defined as each by policy.
  return unit || "each";
}

export function quantityWithUnit(
  quantity?: string | null,
  unit?: string | null,
): string {
  return `${quantity || "unresolved"} ${displayUnit(unit)}`;
}

export function amountWithCurrency(
  amount?: string | null,
  currency?: string | null,
): string {
  return `${displayCurrency(currency)} ${amount || "unresolved"}`;
}

export function minorUnitQuantum(currency?: string | null): string | null {
  if (!currency) return null;
  const code = currency.toUpperCase();
  const places = ZERO_MINOR_UNIT.has(code)
    ? 0
    : TWO_MINOR_UNIT.has(code)
      ? 2
      : THREE_MINOR_UNIT.has(code)
        ? 3
        : null;
  if (places === null) return null;
  return places === 0 ? "1" : `0.${"0".repeat(places - 1)}1`;
}
