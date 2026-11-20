"""Payment cards as a structured secret kind (`--kind card`, GH #251).

A card is several fields a bank shows separately: number, expiry, CVC,
name and billing postal code. Portunus asks for them as fields (one form
where the surface can show one, otherwise one field at a time), checks
them, and stores them together as ONE reference whose value is a small
JSON object:

    {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030,
     "cvc": "123", "name": "A Person", "postal": "94110"}

Only the brand and the last four digits are ever kept outside the value
(``Reference.card``), so `list`, `status`, the MCP tools and the UI can say
``visa ••••4242`` without touching the vault.

Nothing in this module puts the number or the CVC in an exception
message, a log line or a return value other than the stored JSON.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

KIND = "card"


@dataclass(frozen=True)
class CardField:
    key: str
    label: str
    hint: str
    required: bool
    hidden: bool  # mask the input on surfaces that can


# Order is the order a guided sequence asks in.
FIELDS: Tuple[CardField, ...] = (
    CardField("number", "Card number", "the long number on the card", True, True),
    CardField("expiry", "Expiry", "MM/YY", True, False),
    CardField("cvc", "Security code (CVC)", "3 or 4 digits", True, True),
    CardField("name", "Name on card", "as printed on the card", False, False),
    CardField("postal", "Billing postal code", "the postal or ZIP code of the billing address", False, False),
)
FIELD_KEYS = tuple(f.key for f in FIELDS)
REQUIRED_KEYS = tuple(f.key for f in FIELDS if f.required)

# Leading-digit ranges -> brand. First match wins; checked longest prefix first.
_BRANDS: Tuple[Tuple[str, Tuple[Tuple[int, int], ...], Tuple[int, ...]], ...] = (
    ("amex", ((34, 34), (37, 37)), (15,)),
    ("diners", ((300, 305), (36, 36), (38, 39)), (14, 15, 16, 17, 18, 19)),
    ("discover", ((6011, 6011), (644, 649), (65, 65)), (16, 17, 18, 19)),
    ("jcb", ((3528, 3589),), (16, 17, 18, 19)),
    ("unionpay", ((62, 62),), (16, 17, 18, 19)),
    ("mastercard", ((51, 55), (2221, 2720)), (16,)),
    ("maestro", ((50, 50), (56, 58), (6304, 6304), (67, 67)), (12, 13, 14, 15, 16, 17, 18, 19)),
    ("visa", ((4, 4),), (13, 16, 19)),
)


class CardError(ValueError):
    """A card failed a check. The message never contains the number or CVC."""


@dataclass(frozen=True)
class Card:
    number: str
    exp_month: int
    exp_year: int
    cvc: str
    name: str = ""
    postal: str = ""

    @property
    def brand(self) -> str:
        return brand_of(self.number)

    @property
    def last4(self) -> str:
        return self.number[-4:]

    def to_value(self) -> str:
        """The JSON stored in the vault as this reference's value."""
        body = {"number": self.number, "exp_month": self.exp_month,
                "exp_year": self.exp_year, "cvc": self.cvc}
        if self.name:
            body["name"] = self.name
        if self.postal:
            body["postal"] = self.postal
        return json.dumps(body, separators=(",", ":"))

    def meta(self) -> Dict[str, str]:
        """What may live outside the vault: brand and last four only."""
        return {"brand": self.brand, "last4": self.last4}


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def brand_of(number: str) -> str:
    for brand, ranges, lengths in _BRANDS:
        for lo, hi in ranges:
            width = len(str(lo))
            if len(number) < width:
                continue
            if lo <= int(number[:width]) <= hi and len(number) in lengths:
                return brand
    return "card"


def normalize_number(raw: str) -> str:
    """Drop the spaces and dashes people type or paste; anything else that
    is not a digit is an error."""
    cleaned = re.sub(r"[\s-]", "", raw or "")
    if not cleaned:
        raise CardError("the card number is empty")
    if not cleaned.isdigit():
        raise CardError("the card number may only contain digits, spaces and dashes")
    if not 12 <= len(cleaned) <= 19:
        raise CardError(f"the card number has {len(cleaned)} digits (want 12 to 19)")
    if not luhn_ok(cleaned):
        raise CardError("the card number fails the Luhn check -- probably a typo")
    return cleaned


def parse_expiry(raw: str, today: Optional[date] = None) -> Tuple[int, int]:
    """Accept MM/YY, MM/YYYY, MM-YY, MMYY or YYYY-MM. A card is valid
    through the last day of its expiry month, so this month still passes."""
    text = (raw or "").strip()
    m = (re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{2}|\d{4})", text)
         or re.fullmatch(r"(\d{2})(\d{2})", text))
    if m:
        month, year = int(m.group(1)), int(m.group(2))
    else:
        iso = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
        if not iso:
            raise CardError("the expiry is not MM/YY")
        year, month = int(iso.group(1)), int(iso.group(2))
    if year < 100:
        year += 2000
    if not 1 <= month <= 12:
        raise CardError("the expiry month is not 01 to 12")
    today = today or date.today()
    if (year, month) < (today.year, today.month):
        raise CardError(f"the card expired ({month:02d}/{year % 100:02d})")
    if year > today.year + 25:
        raise CardError("the expiry year is too far in the future -- probably a typo")
    return month, year


def normalize_cvc(raw: str, number: str) -> str:
    cvc = (raw or "").strip()
    want = (4,) if brand_of(number) == "amex" else (3, 4)
    if not cvc.isdigit() or len(cvc) not in want:
        raise CardError("the security code must be 3 digits (4 on American Express)")
    return cvc


def check_field(key: str, raw: str, partial: Dict[str, str],
                today: Optional[date] = None) -> Optional[str]:
    """Check one field as it is entered, so a guided sequence can ask again
    straight away. Returns an error message (never containing the value)
    or None."""
    try:
        if key == "number":
            normalize_number(raw)
        elif key == "expiry":
            parse_expiry(raw, today)
        elif key == "cvc":
            number = partial.get("number", "")
            normalize_cvc(raw, normalize_number(number) if number else "")
        elif key in ("name", "postal"):
            if len(raw.strip()) > 100:
                return f"the {key} is longer than 100 characters"
    except CardError as exc:
        return str(exc)
    return None


def from_fields(fields: Dict[str, str], today: Optional[date] = None) -> Card:
    """Build and check a Card from raw field text. Raises CardError."""
    missing = [k for k in REQUIRED_KEYS if not (fields.get(k) or "").strip()]
    if missing:
        raise CardError(f"missing card field(s): {', '.join(missing)}")
    unknown = sorted(set(fields) - set(FIELD_KEYS))
    if unknown:
        raise CardError(f"unknown card field(s): {', '.join(unknown)} "
                        f"(want {', '.join(FIELD_KEYS)})")
    number = normalize_number(fields["number"])
    month, year = parse_expiry(fields["expiry"], today)
    cvc = normalize_cvc(fields["cvc"], number)
    name = " ".join((fields.get("name") or "").split())
    postal = (fields.get("postal") or "").strip()
    for key, val in (("name", name), ("postal", postal)):
        if len(val) > 100:
            raise CardError(f"the {key} is longer than 100 characters")
    return Card(number=number, exp_month=month, exp_year=year, cvc=cvc,
                name=name, postal=postal)


def parse_single(text: str) -> Dict[str, str]:
    """The fields from one blob of text, for a surface that only has one
    input (a single T3 Code card, a single dialog). Accepts a JSON object
    ({"number": ..., "expiry": ...} or the stored shape with exp_month /
    exp_year), or one field per line as ``key: value``. Raises CardError."""
    text = (text or "").strip()
    if not text:
        raise CardError("the card is empty")
    if text.startswith("{"):
        try:
            raw = json.loads(text)
        except ValueError:
            raise CardError("the card is not valid JSON")
        if not isinstance(raw, dict):
            raise CardError("the card must be a JSON object")
        fields = {str(k): "" if v is None else str(v) for k, v in raw.items()}
        if "expiry" not in fields and "exp_month" in fields and "exp_year" in fields:
            fields["expiry"] = f"{fields.pop('exp_month')}/{fields.pop('exp_year')}"
        return fields
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, val = line.partition(":")
        if not sep:
            raise CardError("each line must be `field: value` "
                            f"(fields: {', '.join(FIELD_KEYS)})")
        fields[key.strip().lower()] = val.strip()
    return fields


def display(meta: Optional[Dict[str, str]]) -> str:
    """``visa ••••4242`` -- the only way a card is ever shown."""
    if not meta or not meta.get("last4"):
        return ""
    return f"{meta.get('brand') or 'card'} ••••{meta['last4']}"


def field_prompt(field: CardField, step: int, total: int) -> str:
    optional = " (optional, leave empty to skip)" if not field.required else ""
    return f"Step {step} of {total}: {field.label}{optional}. {field.hint[0].upper()}{field.hint[1:]}."


def fields_in_order() -> List[CardField]:
    return list(FIELDS)
