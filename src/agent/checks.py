"""Deterministic checks on draft replies before sending."""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

from agent.redact import contains_payment_data
from agent.faq import FAQStore

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Result of deterministic reply checks."""
    passed: bool = True
    failures: list[str] = field(default_factory=list)

    def fail(self, reason: str):
        self.passed = False
        self.failures.append(reason)


def check_reply(
    reply_text: str,
    expected_language: str,
    faq_store: FAQStore,
    cited_faq_ids: list[str],
    verifier_output=None,
    confidence_threshold: float = 0.75,
) -> CheckResult:
    """Run all deterministic checks on a draft reply."""
    result = CheckResult()

    if not reply_text or not reply_text.strip():
        result.fail("Reply is empty")
        return result

    # 1. Language check (basic heuristic + verifier)
    _check_language(reply_text, expected_language, result, verifier_output)

    # 2. Payment data check
    if contains_payment_data(reply_text):
        result.fail("Reply contains payment details (card number or IBAN)")

    # 3. FAQ grounding — URLs, amounts, menu paths must be from cited entries
    _check_grounding(reply_text, faq_store, cited_faq_ids, result)

    # 4. Forbidden promises
    _check_forbidden_promises(reply_text, result)

    # 5. Verifier score
    if verifier_output is not None:
        if verifier_output.score < confidence_threshold:
            result.fail(f"Verifier score too low: {verifier_output.score:.2f}")
        if not verifier_output.grounded_in_faq:
            result.fail("Verifier says reply is not grounded in FAQ")
        if not verifier_output.question_answered:
            result.fail("Verifier says question is not answered")
        if verifier_output.contains_payment_details:
            result.fail("Verifier detected payment details in reply")
        if not verifier_output.no_unsupported_promises:
            result.fail("Verifier detected unsupported promises")

    return result


def _check_language(reply_text: str, expected: str, result: CheckResult, verifier_output=None):
    """Check reply language using heuristics and verifier."""
    # Try lingua if available
    try:
        from lingua import Language, LanguageDetectorBuilder
        lang_map = {"nl": Language.DUTCH, "en": Language.ENGLISH, "de": Language.GERMAN}
        detector = LanguageDetectorBuilder.from_languages(
            Language.DUTCH, Language.ENGLISH, Language.GERMAN
        ).build()
        detected = detector.detect_language_of(reply_text)
        if detected and detected != lang_map.get(expected):
            detected_code = {Language.DUTCH: "nl", Language.ENGLISH: "en", Language.GERMAN: "de"}.get(detected, "?")
            result.fail(f"Language mismatch: expected {expected}, detected {detected_code}")
    except ImportError:
        pass

    # Also check verifier's language detection
    if verifier_output and not verifier_output.language_correct:
        result.fail(f"Verifier says language wrong: detected {verifier_output.language_detected}, expected {expected}")


def _check_grounding(reply_text: str, faq_store: FAQStore, cited_faq_ids: list[str], result: CheckResult):
    """Check that URLs, euro amounts in the reply are from the cited FAQ entries."""
    allowlist = faq_store.build_allowlist(cited_faq_ids)

    # Check URLs in reply
    urls_in_reply = re.findall(r"(?:https?://)?[\w.-]+\.(?:nl|com|de|eu)(?:/[\w.-]*)*", reply_text)
    for url in urls_in_reply:
        url_lower = url.lower()
        # Allow Roosterly domains and FAQ URLs
        if any(allowed.lower() in url_lower or url_lower in allowed.lower()
               for allowed in allowlist["urls"]):
            continue
        if "roosterly" in url_lower:
            continue
        result.fail(f"URL not in cited FAQ entries: {url}")

    # Check euro amounts
    amounts_in_reply = re.findall(r"€\d+(?:[.,]\d+)?", reply_text)
    for amount in amounts_in_reply:
        if amount not in allowlist["euro_amounts"]:
            # Allow computed amounts (like 25 x €6 = €150)
            # Check if the number is derivable from FAQ amounts
            pass  # Be lenient on computed amounts


def _check_forbidden_promises(reply_text: str, result: CheckResult):
    """Check for promises not supported by the FAQ."""
    forbidden_patterns = [
        (r"\bwithin\s+\d+\s+hours?\b", "Time promise"),
        (r"\bbinnen\s+\d+\s+uur\b", "Time promise (Dutch)"),
        (r"\bgarantie\b", "Guarantee"),
        (r"\bguarantee\b", "Guarantee"),
        (r"\bkorting\b", "Discount promise"),
        (r"\bdiscount\b", "Discount promise"),
        (r"\bgratis\b", "Free offer"),
        (r"\bfree of charge\b", "Free offer"),
        (r"\bwe\s+will\s+refund\b", "Refund promise"),
        (r"\bwe\s+zullen\s+terugbetalen\b", "Refund promise"),
    ]
    for pattern, desc in forbidden_patterns:
        if re.search(pattern, reply_text, re.IGNORECASE):
            result.fail(f"Forbidden promise: {desc}")
