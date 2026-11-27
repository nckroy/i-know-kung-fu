# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Personal identifiers: government ID numbers, financial account numbers and birthdates.

Three kinds of rule, from most to least certain:

1. **Self-validating formats, removed wherever they appear.** Their structure or checksum makes a
   false match unlikely: US SSN / ITIN (``123-45-6789``, skipping never-issued ranges), payment
   card numbers (known issuer prefix + Luhn checksum), IBAN (mod-97), UK National Insurance number,
   UK driving licence number, Canadian SIN written ``123-456-789`` (Luhn).
2. **Labelled values.** Formats too varied or too ambiguous to spot alone are removed only after a
   label: driver's licence, passport, bank routing number (ABA checksum), account number, tax IDs
   (EIN, ITIN, TIN, UTR), NHS number (mod-11), SIN, health card, and member / policy / patient /
   student / employee IDs. The label stays and the value is replaced.
3. **Birthdates, in birth context only.** A date (or a year) next to "DOB", "date of birth", "born",
   "birthday" or "birthdate" becomes ``[birthdate]``. Other dates are left alone, because dates in
   notes ("torn down 2026-11-10") are often the useful part.

4. **Where someone lives or is, and their things.** Street addresses and PO boxes, UK and Canadian
   postcodes (US ZIP codes only inside an address or after a label), GPS coordinates, vehicle VINs
   (check digit), and after a label: IMEIs (Luhn), device serial numbers and licence plates.

AWS account IDs and MAC addresses aren't personal on their own, but they identify an organization or
a device, so they're replaced too (``[aws-account-id]``, ``[mac-address]``) without holding the
learning back.
"""

from __future__ import annotations

import re

# --- checksums -------------------------------------------------------------------------------


def luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def aba(digits: str) -> bool:
    if len(digits) != 9 or digits == "000000000":
        return False
    d = [int(c) for c in digits]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        n = int("".join(str(int(c, 36)) for c in moved))
    except ValueError:
        return False
    return n % 97 == 1


def nhs_ok(raw: str) -> bool:
    d = re.sub(r"\D", "", raw)
    if len(d) != 10:
        return False
    total = sum(int(d[i]) * (10 - i) for i in range(9))
    check = 11 - total % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(d[9])


def ssn_ok(area: str, group: str, serial: str) -> bool:
    a = int(area)
    return a != 0 and a != 666 and a < 900 and group != "00" and serial != "0000"


def itin_ok(area: str, group: str) -> bool:
    g = int(group)
    return area.startswith("9") and (70 <= g <= 88 or 90 <= g <= 92 or 94 <= g <= 99)


_VIN_VALUES = {**{str(d): d for d in range(10)}, **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))),
               "P": 7, "R": 9, **dict(zip("STUVWXYZ", range(2, 10)))}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin_ok(vin: str) -> bool:
    if len(vin) != 17 or not re.search(r"\d", vin) or not re.search(r"[A-Z]", vin):
        return False
    try:
        total = sum(_VIN_VALUES[c] * w for c, w in zip(vin, _VIN_WEIGHTS))
    except KeyError:
        return False
    check = total % 11
    return vin[8] == ("X" if check == 10 else str(check))


CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2(?:2[2-9]|[3-6]\d|7[01]|720)|3[47]|3(?:0[0-5]|[689])|6(?:011|5|4[4-9])|35)")


def card_ok(digits: str) -> bool:
    return 13 <= len(digits) <= 19 and bool(CARD_PREFIX.match(digits)) and len(set(digits)) > 1 and luhn(digits)


# --- patterns --------------------------------------------------------------------------------

SSN = re.compile(r"(?<![\d-])(\d{3})-(\d{2})-(\d{4})(?![\d-])")
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
UK_NINO = re.compile(r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b")
UK_DRIVING = re.compile(r"\b[A-Z9]{5}\d{6}[A-Z9]{2}\d[A-Z]{2}\b")
CA_SIN = re.compile(r"(?<![\d-])([1-79]\d{2})[ -](\d{3})[ -](\d{3})(?![\d-])")

_NUM = r"(?:\s*(?:number|num|no\.?|#))?"
_SEP = r"\s*[:#=]?\s*(?:is\s+)?"
# label regex, value regex, replacement, kind, validator(value) -> bool
LABELLED: list[tuple[str, str, str, str, object]] = [
    (r"driver'?s?\s+licen[cs]e" + _NUM + r"|driving\s+licen[cs]e" + _NUM + r"|\bDL\s*(?:#|no\.?|number)|\bDLN",
     r"(?:\([A-Z]{2}\)\s*)?([A-Z0-9][A-Z0-9 -]{3,18}[A-Z0-9])", "[license-number]", "drivers-license", None),
    (r"passport" + _NUM, r"([A-Z0-9]{6,9})", "[passport-number]", "passport", None),
    (r"routing" + _NUM + r"|\bABA" + _NUM + r"|\bRTN", r"(\d{9})", "[routing-number]", "bank-routing",
     lambda v: aba(v)),
    (r"(?:bank|checking|savings)\s+account" + _NUM + r"|account\s+(?:number|no\.?|#)|\bacct\.?\s*(?:#|no\.?)?",
     r"(\d[\d -]{4,18}\d)", "[account-number]", "bank-account", None),
    (r"\b(?:EIN|FEIN)|employer identification number", r"(\d{2}-?\d{7})", "[tax-id]", "tax-id", None),
    (r"\b(?:SSN|SS#)|social security(?: number| no\.?)?", r"(\d{3}[- ]?\d{2}[- ]?\d{4})", "[ssn]", "ssn", None),
    (r"\b(?:ITIN|TIN)|tax\s*id(?:entification)?" + _NUM, r"(\d{3}[- ]?\d{2}[- ]?\d{4})", "[tax-id]", "tax-id", None),
    (r"\bUTR" + _NUM + r"|unique taxpayer reference", r"(\d{5}\s?\d{5})", "[tax-id]", "uk-utr", None),
    (r"\bNHS" + _NUM, r"(\d{3}[ -]?\d{3}[ -]?\d{4})", "[nhs-number]", "uk-nhs", lambda v: nhs_ok(v)),
    (r"\bSIN" + _NUM + r"|social insurance(?: number| no\.?)?", r"(\d{3}[ -]?\d{3}[ -]?\d{3})", "[sin]", "ca-sin",
     lambda v: luhn(re.sub(r"\D", "", v))),
    (r"health\s*card" + _NUM + r"|\bOHIP" + _NUM + r"|\bRAMQ" + _NUM + r"|care\s*card" + _NUM,
     r"([A-Z0-9][A-Z0-9 -]{5,16}[A-Z0-9])", "[health-card]", "ca-health-card", None),
    (r"\bIMEI" + _NUM, r"(\d{15})", "[device-id]", "device-imei", lambda v: luhn(v)),
    (r"serial" + _NUM, r"([A-Z0-9][A-Z0-9-]{5,24})", "[device-id]", "device-serial", None),
    (r"licen[cs]e\s+plate" + _NUM + r"|\bplate" + _NUM + r"|registration\s*(?:plate|mark)",
     r"([A-Z0-9][A-Z0-9 -]{2,8}[A-Z0-9])", "[plate]", "vehicle-plate",
     lambda v: bool(re.search(r"[A-Z]", v)) and len(re.sub(r"\W", "", v)) >= 5),
    (r"\bzip(?:\s*code)?|postcode|postal\s*code", r"(\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}|"
     r"[A-Z]\d[A-Z][ -]?\d[A-Z]\d)", "[postcode]", "postcode", None),
    (r"(?:member|policy|patient|subscriber|insurance|student|employee|badge|benefits|medical record)\s*"
     r"(?:id|number|no\.?|#)|\bMRN", r"([A-Z0-9][A-Z0-9-]{3,18})", "[id-number]", "id-number", None),
]
LABELLED_RE = [(re.compile(r"(?i)(" + lab + r")" + _SEP + r"(?-i:" + val + r")"), repl, kind, check)
               for lab, val, repl, kind, check in LABELLED]

CVV = re.compile(r"(?i)\b(cvv2?|cvc2?|cid|security code)" + _SEP + r"(\d{3,4})\b")
EXPIRY = re.compile(r"(?i)\b(exp(?:iry|ires|iration)?(?: date)?|valid thru)\s*[:.]?\s*(0[1-9]|1[0-2])\s*/\s*(\d{4}|\d{2})\b")

AWS_ACCOUNT = re.compile(r"(?i)(\b(?:aws|amazon)\s+account(?:\s+id)?" + _SEP + r")(\d{4}-?\d{4}-?\d{4})\b")
ARN_ACCOUNT = re.compile(r"(arn:aws[\w-]*:[\w-]+:[\w-]*:)(\d{12})(:)")

_STREET = (r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace|Ter|"
           r"Circle|Cir|Parkway|Pkwy|Highway|Hwy|Square|Sq|Close|Crescent|Cres|Row|Trail|Trl|Mews|Gardens|Grove)")
STREET_ADDRESS = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:(?:[NSEW]|North|South|East|West)\.?\s+)?(?:[A-Z][a-z]+\.?\s+){1,4}" + _STREET + r"\b\.?"
    r"(?:,?\s+(?:Apt|Apartment|Suite|Ste|Unit|Flat|#)\.?\s*[\w-]+)?"
    r"(?:,\s*[A-Z][A-Za-z .'\-]+(?:,\s*[A-Z]{2})?(?:\s+\d{5}(?:-\d{4})?)?)?")
PO_BOX = re.compile(r"(?i)\bP\.?\s?O\.?\s+Box\s+\d+")
UK_POSTCODE = re.compile(r"\b(?:[A-Z]{1,2}\d[A-Z\d]?) \d(?!GB|MB|KB|TB|PB)[A-Z]{2}\b")
CA_POSTCODE = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d\b")
GPS_PAIR = re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})(?![\d.])")
GPS_DMS = re.compile(r"\b\d{1,2}(?:\.\d+)?°?\s*[NS],?\s*\d{1,3}(?:\.\d+)?°?\s*[EW]\b")
MAC = re.compile(r"(?<![\w:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])")
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
DATE = (r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|" + MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
        r"|\d{1,2}(?:st|nd|rd|th)?\s+" + MONTHS + r"\.?,?\s+\d{4})")
BIRTH_LABEL = r"(?:D\.?O\.?B\.?|date of birth|birth\s*date|birthdate|birthday|born(?:\s+on)?)"
BIRTHDATE = re.compile(r"(?i)(\b" + BIRTH_LABEL + r"\s*[:\-]?\s*(?:is\s+|was\s+)?)(" + DATE + r")")
BIRTHDATE_AFTER = re.compile(r"(?i)(" + DATE + r")(\s*\((?:DOB|D\.O\.B\.|date of birth|born)\))")
BIRTH_YEAR = re.compile(r"(?i)(\bborn\s+in\s+)((?:19|20)\d{2})\b")


def scrub(text: str) -> tuple[str, list[str], list[str]]:
    """Return (text with identifiers replaced, personal kinds found, organizational kinds found)."""
    personal: list[str] = []
    org: list[str] = []

    def found(kind: str) -> None:
        if kind not in personal:
            personal.append(kind)

    # Birthdates first, so the date patterns below don't see them as something else.
    def _bd(m: re.Match) -> str:
        found("birthdate")
        return m.group(1) + "[birthdate]"

    text = BIRTHDATE.sub(_bd, text)
    text = BIRTHDATE_AFTER.sub(lambda m: (found("birthdate"), "[birthdate]" + m.group(2))[1], text)
    text = BIRTH_YEAR.sub(lambda m: (found("birthdate"), m.group(1) + "[birth year]")[1], text)

    # Where someone lives or is.
    def _addr(m: re.Match) -> str:
        found("address")
        return "[address]"

    text = STREET_ADDRESS.sub(_addr, text)
    text = PO_BOX.sub(_addr, text)

    def _gps(m: re.Match) -> str:
        lat, lon = float(m.group(1)), float(m.group(2))
        if abs(lat) > 90 or abs(lon) > 180 or (abs(lat) < 1 and abs(lon) < 1):
            return m.group(0)  # out of range, or near 0,0: probabilities and ratios, not places
        context = text[max(0, m.start() - 24):m.start()].lower()
        said_so = any(w in context for w in ("gps", "coord", "lat", "lon", "location", "position", "@", "map"))
        if abs(lat) < 10 and abs(lon) < 10 and not said_so:
            return m.group(0)  # two small numbers ("1.2345, 2.3456") are measurements unless the text says otherwise
        found("location")
        return "[location]"

    text = GPS_PAIR.sub(_gps, text)
    text = GPS_DMS.sub(lambda m: (found("location"), "[location]")[1], text)

    # Labelled values.
    for pat, repl, kind, check in LABELLED_RE:
        def _lab(m: re.Match, repl=repl, kind=kind, check=check) -> str:
            value = m.group(m.lastindex)
            if not re.search(r"\d", value) or value.startswith("["):
                return m.group(0)
            if check and not check(value.strip()):
                return m.group(0)
            found(kind)
            return m.group(0)[: m.start(m.lastindex) - m.start()] + repl

        text = pat.sub(_lab, text)

    # Self-validating formats.
    def _ssn(m: re.Match) -> str:
        a, g, s = m.groups()
        if itin_ok(a, g):
            found("itin")
            return "[tax-id]"
        if ssn_ok(a, g, s):
            found("ssn")
            return "[ssn]"
        return m.group(0)

    text = SSN.sub(_ssn, text)

    def _card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if card_ok(digits):
            found("payment-card")
            return "[card]"
        return m.group(0)

    text = CARD.sub(_card, text)
    if "payment-card" in personal or CVV.search(text):
        text = CVV.sub(lambda m: (found("payment-card"), m.group(0)[: m.start(2) - m.start()] + "[cvv]")[1], text)
        text = EXPIRY.sub(lambda m: m.group(0)[: m.start(2) - m.start()] + "[expiry]", text)

    def _iban(m: re.Match) -> str:
        if iban_ok(m.group(0)):
            found("iban")
            return "[iban]"
        return m.group(0)

    text = IBAN.sub(_iban, text)
    text = UK_NINO.sub(lambda m: (found("uk-ni-number"), "[ni-number]")[1], text)
    text = UK_DRIVING.sub(lambda m: (found("drivers-license"), "[license-number]")[1], text)

    text = UK_POSTCODE.sub(lambda m: (found("postcode"), "[postcode]")[1], text)
    text = CA_POSTCODE.sub(lambda m: (found("postcode"), "[postcode]")[1], text)

    def _vin(m: re.Match) -> str:
        if vin_ok(m.group(0)):
            found("vehicle-vin")
            return "[vin]"
        return m.group(0)

    text = VIN.sub(_vin, text)

    def _sin(m: re.Match) -> str:
        if luhn("".join(m.groups())):
            found("ca-sin")
            return "[sin]"
        return m.group(0)

    text = CA_SIN.sub(_sin, text)

    # Device addresses: replaced, but they don't hold a learning back on their own.
    def _mac(m: re.Match) -> str:
        v = re.sub(r"[:-]", "", m.group(0)).lower()
        if v in ("000000000000", "ffffffffffff"):
            return m.group(0)
        if "mac-address" not in org:
            org.append("mac-address")
        return "[mac-address]"

    text = MAC.sub(_mac, text)

    # Organizational: AWS account IDs.
    def _aws(m: re.Match) -> str:
        if "aws-account-id" not in org:
            org.append("aws-account-id")
        return m.group(1) + "[aws-account-id]" + (m.group(3) if m.lastindex and m.lastindex >= 3 else "")

    text = AWS_ACCOUNT.sub(_aws, text)
    text = ARN_ACCOUNT.sub(_aws, text)
    return text, personal, org


def residual(text: str) -> list[str]:
    """Self-validating identifiers still in final text: used to refuse packing."""
    _, personal, _ = scrub(text)
    return sorted(set(personal) & {"ssn", "itin", "payment-card", "iban", "uk-ni-number", "ca-sin",
                                   "drivers-license", "birthdate", "bank-routing", "uk-nhs", "passport",
                                   "bank-account", "tax-id", "uk-utr", "ca-health-card", "id-number",
                                   "address", "location", "vehicle-vin", "device-imei"})
