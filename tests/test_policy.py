"""Unit tests for the decision policy (pure deterministic code)."""
from __future__ import annotations

import pytest
from agent.policy import (
    PolicyDecision,
    RegexSignals,
    build_handoff_note,
    decide_action,
    run_regex_safety_net,
)
from agent.prompts import ClassifierOutput


def _make_classifier(
    action_type="normal",
    language="nl",
    category="question",
    confidence=0.95,
    **kwargs,
) -> ClassifierOutput:
    defaults = {
        "language": language,
        "category": category,
        "is_empty_or_spam": False,
        "spam_confidence": 0.0,
        "prompt_injection_attempt": False,
        "refund_or_disputed_charge": False,
        "cancellation_or_contract_change": False,
        "personal_data_request": False,
        "possible_security_incident": False,
        "legal_threat": False,
        "churn_threat": False,
        "many_users_blocked": False,
        "customer_blocked": False,
        "has_workaround": False,
        "contains_payment_details": False,
        "faq_ids": ["F02"],
        "fully_answerable_from_faq": True,
        "has_uncovered_parts": False,
        "confidence": confidence,
        "summary_en": "Question about adding an employee.",
        "suggested_next_step_en": "Send FAQ F02 reply.",
        "reason_en": "Answered by F02.",
    }
    defaults.update(kwargs)
    return ClassifierOutput(**defaults)


def test_empty_ticket_is_closed():
    clf = _make_classifier()
    regex = RegexSignals()
    decision = decide_action(clf, regex, body_text="   \n  ")
    assert decision.action == "close"
    assert decision.priority == "P4"
    assert decision.category == "other"


def test_spam_with_high_confidence_is_closed():
    clf = _make_classifier(
        is_empty_or_spam=True,
        spam_confidence=0.95,
        category="other",
        fully_answerable_from_faq=False,
        faq_ids=[],
    )
    regex = RegexSignals()
    decision = decide_action(clf, regex, body_text="Win free crypto now!")
    assert decision.action == "close"
    assert decision.priority == "P4"


def test_spam_with_refund_is_handed_off():
    # If a message looks like spam but mentions refund, safety first!
    clf = _make_classifier(
        is_empty_or_spam=True,
        spam_confidence=0.92,
        refund_or_disputed_charge=True,
        category="billing",
        fully_answerable_from_faq=False,
        faq_ids=[],
    )
    regex = run_regex_safety_net("Crypto refund scheme")
    decision = decide_action(clf, regex, body_text="Crypto refund scheme")
    assert decision.action == "handoff"
    assert decision.priority == "P2"


def test_clean_faq_question_replies():
    clf = _make_classifier()
    regex = RegexSignals()
    decision = decide_action(clf, regex, body_text="Hoe voeg ik een medewerker toe?")
    assert decision.action == "reply"
    assert decision.priority == "P4"
    assert decision.category == "question"


def test_low_confidence_hands_off():
    clf = _make_classifier(confidence=0.60)
    regex = RegexSignals()
    decision = decide_action(clf, regex, body_text="Vreemde vraag...")
    assert decision.action == "handoff"


def test_refund_forces_billing_and_p2():
    clf = _make_classifier(
        category="question",
        refund_or_disputed_charge=True,
        fully_answerable_from_faq=False,
        faq_ids=[],
    )
    regex = run_regex_safety_net("Geld terug van vorige maand")
    decision = decide_action(clf, regex, body_text="Geld terug van vorige maand")
    assert decision.action == "handoff"
    assert decision.category == "billing"
    assert decision.priority == "P2"


def test_security_incident_is_p1():
    clf = _make_classifier(
        possible_security_incident=True,
        category="other",
        fully_answerable_from_faq=False,
        faq_ids=[],
    )
    regex = run_regex_safety_net("We are hacked!")
    decision = decide_action(clf, regex, body_text="We are hacked!")
    assert decision.action == "handoff"
    assert decision.priority == "P1"


def test_legal_threat_is_p1():
    clf = _make_classifier(
        legal_threat=True,
        category="other",
        fully_answerable_from_faq=False,
        faq_ids=[],
    )
    regex = run_regex_safety_net("Onze advocaat stelt jullie aansprakelijk")
    decision = decide_action(clf, regex, body_text="Onze advocaat stelt jullie aansprakelijk")
    assert decision.action == "handoff"
    assert decision.priority == "P1"


def test_regex_safety_net_catches_unflagged_gdpr():
    # LLM failed to set personal_data_request flag, but regex caught it
    clf = _make_classifier(personal_data_request=False)
    regex = run_regex_safety_net("Ik wil inzage in mijn persoonsgegevens conform AVG.")
    decision = decide_action(clf, regex, body_text="Ik wil inzage in mijn persoonsgegevens conform AVG.")
    assert decision.action == "handoff"
    assert decision.priority == "P2"


def test_handoff_note_under_90_words():
    clf = _make_classifier(
        category="billing",
        refund_or_disputed_charge=True,
        summary_en="Customer is asking for an August subscription refund due to renovations.",
        suggested_next_step_en="Check billing history and issue a partial credit note.",
    )
    policy = PolicyDecision(
        action="handoff",
        priority="P2",
        category="billing",
        reason="Refund request",
        handoff_reasons=["Refund or disputed charge — always human"],
    )
    note = build_handoff_note(clf, policy, customer_plan="Plus")
    words = note.split()
    assert len(words) <= 90
    assert "[P2][billing]" in note
    assert "Why human:" in note
    assert "Suggested:" in note
    assert "Plan: Plus" in note
