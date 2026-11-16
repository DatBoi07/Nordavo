"""Redaction of payment details (IBANs and card numbers) before LLM and in logs."""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# IBAN detection with mod-97 check
# ---------------------------------------------------------------------------

# IBAN: 2 letter country code + 2 check digits + up to 30 alphanumeric
_IBAN_PATTERN = re.compile(
    r"\b([A-Z]{2}\d{2}\s?(?:[A-Z0-9]\s?){4,30}[A-Z0-9])\b", re.IGNORECASE
)

def _normalize_iban(raw: str) -> str:
    return raw.replace(" ", "").upper()

def _iban_mod97_valid(iban: str) -> bool:
    """Check IBAN with mod-97 algorithm (ISO 13616)."""
    if len(iban) < 5:
        return False
    # Move first 4 chars to end
    rearranged = iban[4:] + iban[:4]
    # Convert letters to numbers (A=10, B=11, ..., Z=35)
    numeric = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric += ch
        elif ch.isalpha():
            numeric += str(ord(ch) - ord("A") + 10)
        else:
            return False
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Card number detection with Luhn check
# ---------------------------------------------------------------------------

# Matches 13-19 digit sequences (with optional spaces/dashes)
_CARD_PATTERN = re.compile(
    r"\b(\d[\d\s\-]{11,22}\d)\b"
)

def _normalize_card(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw)

def _luhn_valid(number: str) -> bool:
    """Luhn algorithm for credit card validation."""
    if not number.isdigit() or len(number) < 13 or len(number) > 19:
        return False
    digits = [int(d) for d in number]
    # Double every second digit from the right
    for i in range(len(digits) - 2, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    return sum(digits) % 10 == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

REDACTED_IBAN = "[REDACTED_IBAN]"
REDACTED_CARD = "[REDACTED_CARD]"

def redact(text: str) -> str:
    """Replace valid IBANs and Luhn-valid card numbers with placeholders."""
    if not text:
        return text

    # Redact IBANs
    def _replace_iban(m: re.Match) -> str:
        raw = m.group(1)
        normalized = _normalize_iban(raw)
        if _iban_mod97_valid(normalized):
            return REDACTED_IBAN
        return raw

    result = _IBAN_PATTERN.sub(_replace_iban, text)

    # Redact card numbers
    def _replace_card(m: re.Match) -> str:
        raw = m.group(1)
        normalized = _normalize_card(raw)
        if _luhn_valid(normalized):
            return REDACTED_CARD
        return raw

    result = _CARD_PATTERN.sub(_replace_card, result)
    return result


def contains_payment_data(text: str) -> bool:
    """Check if text contains un-redacted payment details."""
    if not text:
        return False
    # Check for IBANs
    for m in _IBAN_PATTERN.finditer(text):
        if _iban_mod97_valid(_normalize_iban(m.group(1))):
            return True
    # Check for card numbers
    for m in _CARD_PATTERN.finditer(text):
        if _luhn_valid(_normalize_card(m.group(1))):
            return True
    return False
