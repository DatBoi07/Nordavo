"""Unit tests for deterministic reply checks."""
from __future__ import annotations

import pytest
from agent.checks import check_reply
from agent.faq import FAQStore
from agent.prompts import VerifierOutput


@pytest.fixture
def faq_store():
    return FAQStore("data/faq.md")


def _make_verifier_output(
    score=0.95,
    language_correct=True,
    grounded_in_faq=True,
    question_answered=True,
    no_unsupported_promises=True,
    contains_payment_details=False,
    reply_en="Go to Team and click Add employee.",
):
    return VerifierOutput(
        language_correct=language_correct,
        language_detected="nl",
        grounded_in_faq=grounded_in_faq,
        question_answered=question_answered,
        no_unsupported_promises=no_unsupported_promises,
        contains_payment_details=contains_payment_details,
        reply_en=reply_en,
        score=score,
    )


def test_clean_grounded_reply_passes(faq_store):
    reply = (
        "Hoi Thomas, je kunt een nieuwe medewerker toevoegen via Team → Medewerker toevoegen. "
        "Vul de gegevens in en klik op Uitnodigen. "
        "Met vriendelijke groet, Team Roosterly Support"
    )
    ver = _make_verifier_output()
    chk = check_reply(
        reply_text=reply,
        expected_language="nl",
        faq_store=faq_store,
        cited_faq_ids=["F02"],
        verifier_output=ver,
    )
    assert chk.passed is True
    assert len(chk.failures) == 0


def test_payment_data_in_reply_fails(faq_store):
    reply = "We hebben uw betaling teruggestort naar NL91ABNA0417164300."
    ver = _make_verifier_output()
    chk = check_reply(
        reply_text=reply,
        expected_language="nl",
        faq_store=faq_store,
        cited_faq_ids=["F05"],
        verifier_output=ver,
    )
    assert chk.passed is False
    assert any("payment" in f.lower() for f in chk.failures)


def test_invented_external_url_fails(faq_store):
    reply = "Ga naar https://external-suspicious-site.nl/help voor meer info."
    ver = _make_verifier_output()
    chk = check_reply(
        reply_text=reply,
        expected_language="nl",
        faq_store=faq_store,
        cited_faq_ids=["F01"],
        verifier_output=ver,
    )
    assert chk.passed is False
    assert any("url not in cited" in f.lower() for f in chk.failures)


def test_forbidden_promises_fail(faq_store):
    reply = "Wij lossen dit binnen 24 uur op en geven u 50% korting op uw abonnement."
    ver = _make_verifier_output()
    chk = check_reply(
        reply_text=reply,
        expected_language="nl",
        faq_store=faq_store,
        cited_faq_ids=["F05"],
        verifier_output=ver,
    )
    assert chk.passed is False
    assert any("promise" in f.lower() for f in chk.failures)


def test_low_verifier_score_fails(faq_store):
    reply = "Hoi, dit is een antwoord."
    ver = _make_verifier_output(score=0.55)
    chk = check_reply(
        reply_text=reply,
        expected_language="nl",
        faq_store=faq_store,
        cited_faq_ids=["F02"],
        verifier_output=ver,
    )
    assert chk.passed is False
    assert any("verifier score too low" in f.lower() for f in chk.failures)
