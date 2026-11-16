"""Unit tests for redaction of IBANs and Luhn-valid credit cards."""
from __future__ import annotations

import pytest
from agent.redact import (
    REDACTED_CARD,
    REDACTED_IBAN,
    contains_payment_data,
    redact,
    _iban_mod97_valid,
    _luhn_valid,
)


def test_iban_mod97_validation():
    # Valid Dutch IBANs
    assert _iban_mod97_valid("NL91ABNA0417164300") is True
    assert _iban_mod97_valid("NL20INGB0001234567") is True

    # Invalid IBANs (wrong checksum or format)
    assert _iban_mod97_valid("NL00ABNA0417164300") is False
    assert _iban_mod97_valid("INVALIDIBAN") is False


def test_card_luhn_validation():
    # Valid Luhn card numbers (standard test cards)
    assert _luhn_valid("4111111111111111") is True
    assert _luhn_valid("4012888888881881") is True

    # Invalid card numbers
    assert _luhn_valid("4111111111111112") is False
    assert _luhn_valid("1234567890123456") is False
    assert _luhn_valid("123") is False


def test_redact_iban_in_sentence():
    text = "Mijn rekeningnummer is NL91 ABNA 0417 1643 00, graag daarop storten."
    redacted = redact(text)
    assert REDACTED_IBAN in redacted
    assert "NL91" not in redacted


def test_redact_card_in_sentence():
    text = "Nieuwe kaart: 4111 1111 1111 1111 exp 12/28."
    redacted = redact(text)
    assert REDACTED_CARD in redacted
    assert "4111" not in redacted


def test_do_not_redact_regular_numbers():
    # Random digits that fail Luhn should not be redacted
    text = "Order nummer 1234567890123456 of telefoon 0612345678"
    redacted = redact(text)
    assert REDACTED_CARD not in redacted
    assert REDACTED_IBAN not in redacted


def test_contains_payment_data():
    assert contains_payment_data("Hier is mijn IBAN NL91ABNA0417164300") is True
    assert contains_payment_data("Mijn kaart 4111111111111111") is True
    assert contains_payment_data("Gewoon een vraag over factuur F06") is False
