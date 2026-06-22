"""
Normalization layer.

Goal: make values *comparable* so that cosmetic differences ("St" vs "Street",
"(239) 555-1234" vs "239-555-1234", "John Smith, MD" vs "Smith, John M.D.")
never get flagged as changes. Every "is this different?" decision in the
pipeline compares NORMALIZED values, never raw strings. Killing fake diffs here
is the cheapest cost control we have — it prevents pointless source calls,
pointless LLM calls, and pointless human review downstream.

Uses best-in-class libraries when available (phonenumbers, usaddress) and falls
back to dependency-free logic so the prototype always runs.
"""
from __future__ import annotations

import re
from typing import Optional

# --- optional deps, graceful fallback ---------------------------------------
try:
    import phonenumbers  # type: ignore
    _HAS_PHONENUMBERS = True
except Exception:
    _HAS_PHONENUMBERS = False

try:
    import usaddress  # type: ignore
    _HAS_USADDRESS = True
except Exception:
    _HAS_USADDRESS = False


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------
def normalize_phone(raw: Optional[str], region: str = "US") -> Optional[str]:
    if not raw:
        return None
    if _HAS_PHONENUMBERS:
        try:
            p = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(p):
                return phonenumbers.format_number(
                    p, phonenumbers.PhoneNumberFormat.E164
                )
        except Exception:
            pass
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return "+1" + digits
    return digits or None


# ---------------------------------------------------------------------------
# Provider name
# ---------------------------------------------------------------------------
_CREDENTIALS = {
    "md", "do", "phd", "mbbs", "np", "pa", "rn", "dds", "dmd", "dpm",
    "od", "pharmd", "msn", "facc", "faha", "facs",
}
_NICKNAMES = {"bill": "william", "bob": "robert", "jim": "james", "mike": "michael",
              "tom": "thomas", "dave": "david", "joe": "joseph", "chris": "christopher",
              "dan": "daniel", "rich": "richard", "rick": "richard", "steve": "stephen"}


def split_name_credentials(raw: str) -> tuple[str, list[str]]:
    """Return (name_part, [credentials]) for a string like 'John Smith, MD, FACC'."""
    creds: list[str] = []
    # credentials are usually comma-separated tokens after the name
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    name_tokens: list[str] = []
    for i, part in enumerate(parts):
        toks = part.split()
        # a part is "credentials" if all its tokens are known credentials
        if i > 0 and all(re.sub(r"\.", "", t).lower() in _CREDENTIALS for t in toks):
            creds.extend(re.sub(r"\.", "", t).lower() for t in toks)
        else:
            name_tokens.append(part)
    return ", ".join(name_tokens), creds


_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "miss"}


def normalize_name(raw: Optional[str]) -> Optional[str]:
    """
    Canonical key for a person's name: lowercase, honorific- and
    credential-stripped, 'Last, First' reordered to 'first last', nicknames
    expanded, middle initials and punctuation dropped. Used for equality/
    blocking only, never for display.

    Distinguishes 'Last, First' (reorder) from 'Name, Credential' (don't) by
    checking whether the text after the first comma is all credential tokens.
    """
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]

    def clean(tokens_src: str) -> list[str]:
        s = re.sub(r"[.\-']", " ", tokens_src.lower())
        return [t for t in s.split()
                if t not in _CREDENTIALS and t not in _HONORIFICS]

    reorder = False
    if len(parts) >= 2:
        right = re.sub(r"[.\-]", " ", parts[1].lower()).split()
        if not all(tok in _CREDENTIALS for tok in right):
            reorder = True

    if reorder:
        tokens = clean(" ".join(parts[1:])) + clean(parts[0])  # first... + last
    else:
        tokens = clean(" ".join(parts))

    tokens = [t for t in tokens if len(t) > 1]          # drop middle initials
    tokens = [_NICKNAMES.get(t, t) for t in tokens]
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------
_STREET_ABBR = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
    "road": "rd", "lane": "ln", "court": "ct", "place": "pl", "suite": "ste",
    "parkway": "pkwy", "highway": "hwy", "north": "n", "south": "s",
    "east": "e", "west": "w", "building": "bldg", "floor": "fl",
}


def _canon_token(t: str) -> str:
    t = re.sub(r"[.,]", "", t.lower())
    return _STREET_ABBR.get(t, t)


def normalize_address(raw: Optional[str]) -> Optional[dict]:
    """
    Returns structured components + a canonical comparison key.
    Comparison is done on (street_number, street_name, city, state, zip5) so
    that suite-number-only changes or 'St' vs 'Street' don't read as a move,
    while a real street/city/zip change does.
    """
    if not raw:
        return None
    comp = {"number": "", "street": "", "city": "", "state": "", "zip5": ""}

    if _HAS_USADDRESS:
        try:
            tagged, _ = usaddress.tag(raw)
            comp["number"] = tagged.get("AddressNumber", "")
            street_parts = [
                tagged.get(k, "")
                for k in ("StreetNamePreDirectional", "StreetName",
                          "StreetNamePostType", "StreetNamePostDirectional")
            ]
            comp["street"] = " ".join(_canon_token(p) for p in street_parts if p)
            comp["city"] = (tagged.get("PlaceName", "") or "").lower()
            comp["state"] = (tagged.get("StateName", "") or "").upper()
            zipc = tagged.get("ZipCode", "") or ""
            comp["zip5"] = zipc[:5]
        except Exception:
            comp = _regex_address(raw)
    else:
        comp = _regex_address(raw)

    comp["key"] = "|".join([
        comp["number"], comp["street"], comp["city"],
        comp["state"], comp["zip5"],
    ]).strip("|")
    return comp


def _regex_address(raw: str) -> dict:
    comp = {"number": "", "street": "", "city": "", "state": "", "zip5": ""}
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", raw)
    if m:
        comp["zip5"] = m.group(1)
    sm = re.search(r"\b([A-Z]{2})\b(?:\s+\d{5})?", raw)
    if sm:
        comp["state"] = sm.group(1)
    # crude: first chunk before first comma is street; chunk before state is city
    chunks = [c.strip() for c in raw.split(",")]
    if chunks:
        first = chunks[0].split()
        if first and re.match(r"\d", first[0]):
            comp["number"] = first[0]
            # strip suite tokens from street comparison
            street_toks = [
                _canon_token(t) for t in first[1:]
                if _canon_token(t) not in ("ste", "unit", "#")
                and not re.match(r"^#?\d+[a-z]?$", t.lower())
            ]
            comp["street"] = " ".join(street_toks)
    if len(chunks) >= 2:
        comp["city"] = chunks[1].lower().strip()
    return comp


# ---------------------------------------------------------------------------
# Specialty -> NUCC taxonomy (subset crosswalk; the full file is free from NUCC)
# ---------------------------------------------------------------------------
_SPECIALTY_TO_TAXONOMY = {
    "cardiology": "207RC0000X",
    "cardiovascular disease": "207RC0000X",
    "interventional cardiology": "207RI0011X",
    "family medicine": "207Q00000X",
    "internal medicine": "207R00000X",
    "pediatrics": "208000000X",
    "dermatology": "207N00000X",
    "orthopedic surgery": "207X00000X",
    "orthopedics": "207X00000X",
    "psychiatry": "2084P0800X",
}


def normalize_specialty(raw: Optional[str]) -> Optional[str]:
    """Map free-text specialty to a NUCC taxonomy code so 'Cardiology' and
    'Cardiovascular Disease' compare equal."""
    if not raw:
        return None
    key = re.sub(r"[^a-z ]", "", raw.lower()).strip()
    return _SPECIALTY_TO_TAXONOMY.get(key, key)  # fall back to cleaned text


# ---------------------------------------------------------------------------
# Dispatch: normalize any tracked field
# ---------------------------------------------------------------------------
def normalize_field(fname: str, value):
    if value is None:
        return None
    if fname == "phone":
        return normalize_phone(value)
    if fname == "provider_name":
        return normalize_name(value)
    if fname == "address":
        a = normalize_address(value)
        return a["key"] if a else None
    if fname == "specialty":
        return normalize_specialty(value)
    if fname in ("practice_name",):
        return re.sub(r"[^a-z0-9 ]", "", str(value).lower()).strip()
    return str(value).strip().lower()
