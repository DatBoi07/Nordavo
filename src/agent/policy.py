"""Policy: pure functions that apply the client's rules to decide actions and priorities.

Safety lives in code, not in the prompt. The LLM extracts facts and flags;
this module applies the deterministic rules.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex safety net — high-precision nl/en/de lexicon
# ---------------------------------------------------------------------------

ESCALATION_PATTERNS: dict[str, list[re.Pattern]] = {
    "refund": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bterugbetaling\b", r"\brestitutie\b", r"\bcreditnota\b",
            r"\bterugkrijgen\b", r"\bgeld\s+terug\b", r"\bterugstort",
            r"\brefund\b", r"\brefunded\b", r"\bmoney\s+back\b",
            r"\berstattung\b", r"\brückerstattung\b",
        ]
    ],
    "dispute": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bbetwist", r"\bdubbel\s+afgeschreven\b", r"\btweemaal\b.*\bafgeschreven\b",
            r"\btwee\s+keer\b.*\bafgeschreven\b", r"\bdubbele?\s+betaling\b",
            r"\bchargeback\b", r"\bdispute[ds]?\b", r"\bdouble\s+charge[ds]?\b",
        ]
    ],
    "cancellation": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bopzeggen\b", r"\bopzegging\b", r"\bbeeindigen\b", r"\bbeëindigen\b",
            r"\bcontract\b.*\b(?:stop|eind|wijzig)\b",
            r"\babonnement\b.*\b(?:stop|opzeg|eind)\b",
            r"\bcancel\b.*\bsubscription\b", r"\bterminate\b.*\bcontract\b",
            r"\bkündigen\b", r"\bkündigung\b",
        ]
    ],
    "gdpr": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bAVG\b", r"\bGDPR\b", r"\bDSGVO\b",
            r"\bpersoonsgegevens\b", r"\binzage\b", r"\binzageverzoek\b",
            r"\bgegevens\s+verwijderen\b", r"\bgegevens\s+wissen\b",
            r"\brecht\s+op\s+vergetelheid\b",
            r"\bpersonal\s+data\b", r"\bdata\s+deletion\b", r"\bdata\s+request\b",
            r"\bright\s+to\s+be\s+forgotten\b",
        ]
    ],
    "security": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bgehackt\b", r"\bhacked\b", r"\bdatalek\b", r"\bdata\s*breach\b",
            r"\bphishing\b", r"\bonbevoegd\b", r"\bunauthorized\b",
            r"\bbreach\b", r"\bcompromised\b",
            r"\bvreemde\s+login\b", r"\bvreemde\s+inlog\b",
            r"\bsuspicious\s+login\b",
        ]
    ],
    "legal": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\badvocaat\b", r"\bjuridisch\b", r"\baansprakelijk\b",
            r"\bschadevergoeding\b", r"\bdagvaard",
            r"\blawyer\b", r"\blegal\s+action\b", r"\bliable\b", r"\bliability\b",
            r"\bAnwalt\b", r"\brechtlich\b",
        ]
    ],
    "churn": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\boverstappen\b", r"\bconcurrent\b",
            r"\bswitch\s+to\b.*\b(?:competitor|alternative)\b",
            r"\bwechseln\b",
        ]
    ],
}


@dataclass
class RegexSignals:
    """Signals from the regex safety net."""
    refund: bool = False
    dispute: bool = False
    cancellation: bool = False
    gdpr: bool = False
    security: bool = False
    legal: bool = False
    churn: bool = False
    matched_patterns: list[str] = field(default_factory=list)

    @property
    def any_escalation(self) -> bool:
        return any([self.refund, self.dispute, self.cancellation,
                     self.gdpr, self.security, self.legal, self.churn])


def run_regex_safety_net(text: str) -> RegexSignals:
    """Run the regex safety net on ticket text."""
    signals = RegexSignals()
    full_text = text.lower() if text else ""

    for category, patterns in ESCALATION_PATTERNS.items():
        for pat in patterns:
            if pat.search(text):  # use original text for case-insensitive patterns
                setattr(signals, category, True)
                signals.matched_patterns.append(f"{category}:{pat.pattern}")
                break  # one match per category is enough

    return signals


# ---------------------------------------------------------------------------
# Policy: determine action and priority
# ---------------------------------------------------------------------------

@dataclass
class PolicyDecision:
    """The action decided by policy."""
    action: str         # "reply", "handoff", "close"
    priority: str       # "P1", "P2", "P3", "P4"
    category: str       # from classifier, possibly adjusted
    reason: str         # English explanation
    handoff_reasons: list[str] = field(default_factory=list)


def decide_action(
    classifier: object,  # ClassifierOutput
    regex_signals: RegexSignals,
    body_text: str,
    confidence_threshold: float = 0.75,
) -> PolicyDecision:
    """Apply the client's rules to decide action, priority, and category.

    Safety lives in code: the LLM flags are OR'd with the regex signals.
    """
    reasons: list[str] = []
    category = classifier.category

    # -- Step 1: Check for empty/spam --
    body_stripped = (body_text or "").strip()
    if not body_stripped:
        return PolicyDecision(
            action="close",
            priority="P4",
            category="other",
            reason="Empty ticket body.",
        )

    if classifier.is_empty_or_spam and classifier.spam_confidence >= 0.9:
        # Only close spam if no must-escalate signals
        has_any_escalate = _has_any_must_escalate(classifier, regex_signals)
        if not has_any_escalate:
            return PolicyDecision(
                action="close",
                priority="P4",
                category="other",
                reason=f"Spam detected (confidence {classifier.spam_confidence:.2f}).",
            )

    # -- Step 2: Check must-escalate flags (LLM OR regex — union) --
    if classifier.refund_or_disputed_charge or regex_signals.refund or regex_signals.dispute:
        reasons.append("Refund or disputed charge — always human")
        category = "billing"
    if classifier.cancellation_or_contract_change or regex_signals.cancellation:
        reasons.append("Cancellation or contract change — always human")
    if classifier.personal_data_request or regex_signals.gdpr:
        reasons.append("Personal data / GDPR request — always human")
    # Security and legal
    if classifier.possible_security_incident or regex_signals.security:
        reasons.append("Possible security incident — always human")
    if classifier.legal_threat or regex_signals.legal:
        reasons.append("Legal threat — always human")
    if classifier.churn_threat or regex_signals.churn:
        reasons.append("Customer threatening to leave — always human")

    # Outages & blocking bugs (client rule: broken app is not answerable by FAQ)
    if classifier.many_users_blocked:
        reasons.append("Many users blocked / platform outage — always human (P1)")
    if classifier.customer_blocked and category == "bug":
        reasons.append("Customer business blocked by bug — needs engineer (P2)")

    # Prompt injection
    if classifier.prompt_injection_attempt:
        reasons.append("Prompt injection attempt detected — handoff for safety")

    # FAQ coverage
    if classifier.has_uncovered_parts:
        reasons.append("Ticket has parts not covered by the FAQ")
    if not classifier.fully_answerable_from_faq:
        reasons.append("Not fully answerable from the FAQ")
    if not classifier.faq_ids:
        reasons.append("No FAQ entry matches the ticket")

    # Confidence
    if classifier.confidence < confidence_threshold:
        reasons.append(f"Low confidence ({classifier.confidence:.2f} < {confidence_threshold})")

    # Log disagreements between regex and LLM
    _log_disagreements(classifier, regex_signals)

    # -- Step 3: If any reason, handoff --
    if reasons:
        priority = _compute_priority(classifier, regex_signals, action="handoff", category=category)
        return PolicyDecision(
            action="handoff",
            priority=priority,
            category=category,
            reason=classifier.reason_en,
            handoff_reasons=reasons,
        )

    # -- Step 4: Reply --
    priority = _compute_priority(classifier, regex_signals, action="reply", category=category)
    return PolicyDecision(
        action="reply",
        priority=priority,
        category=category,
        reason=classifier.reason_en,
    )


def _has_any_must_escalate(classifier, regex_signals: RegexSignals) -> bool:
    """Check if any must-escalate flag is set (LLM or regex)."""
    return any([
        classifier.refund_or_disputed_charge, regex_signals.refund, regex_signals.dispute,
        classifier.cancellation_or_contract_change, regex_signals.cancellation,
        classifier.personal_data_request, regex_signals.gdpr,
        classifier.possible_security_incident, regex_signals.security,
        classifier.legal_threat, regex_signals.legal,
        classifier.churn_threat, regex_signals.churn,
        classifier.prompt_injection_attempt,
    ])


def _priority_severity(p: str) -> int:
    return {"P1": 1, "P2": 2, "P3": 3, "P4": 4}.get(p, 4)


def _max_priority(p1: str, p2: str) -> str:
    """Return the more severe priority (P1 > P2 > P3 > P4)."""
    return p1 if _priority_severity(p1) <= _priority_severity(p2) else p2


def _compute_priority(
    classifier,
    regex_signals: RegexSignals,
    action: str = "reply",
    category: str = "question",
) -> str:
    """Compute priority by combining deterministic safety floors with classifier suggestion."""
    suggested = getattr(classifier, "suggested_priority", None) or "P4"

    # P1 floors: platform security, legal risks, or widespread multi-business outage
    if (classifier.possible_security_incident or regex_signals.security
            or classifier.legal_threat or regex_signals.legal
            or classifier.many_users_blocked):
        return "P1"

    # P2 floors: money, contract, GDPR, churn
    if (classifier.refund_or_disputed_charge or regex_signals.refund or regex_signals.dispute
            or classifier.cancellation_or_contract_change or regex_signals.cancellation
            or classifier.personal_data_request or regex_signals.gdpr
            or classifier.churn_threat or regex_signals.churn):
        return "P2"

    # Action-specific resolution
    if action == "handoff":
        if classifier.customer_blocked:
            return "P2"
        if classifier.has_workaround:
            return _max_priority(suggested, "P3")
        # Default for handed-off issues
        return _max_priority(suggested, "P2" if classifier.category == "billing" else "P3")

    if action == "close":
        return "P4"

    # action == "reply": fully covered by FAQ
    # Bugs answered by FAQ (e.g. E01 iOS 15 crash resolved by F12) are P3 (workaround/requirement exists)
    if category == "bug":
        return "P3"

    # Normal how-to questions, feature requests, cosmetic, pricing info are P4
    if category in ("question", "feature_request", "other"):
        return "P4"

    if category == "billing":
        # Pure billing questions covered by FAQ (e.g. F05 price, F06 invoice details) are P4
        return "P4"

    return suggested


def _log_disagreements(classifier, regex_signals: RegexSignals):
    """Log when regex and LLM disagree on flags."""
    checks = [
        ("refund", classifier.refund_or_disputed_charge, regex_signals.refund or regex_signals.dispute),
        ("cancellation", classifier.cancellation_or_contract_change, regex_signals.cancellation),
        ("gdpr", classifier.personal_data_request, regex_signals.gdpr),
        ("security", classifier.possible_security_incident, regex_signals.security),
        ("legal", classifier.legal_threat, regex_signals.legal),
        ("churn", classifier.churn_threat, regex_signals.churn),
    ]
    for name, llm_flag, regex_flag in checks:
        if llm_flag != regex_flag:
            logger.info(
                "DISAGREEMENT on %s: LLM=%s, regex=%s",
                name, llm_flag, regex_flag,
            )


# ---------------------------------------------------------------------------
# Handoff note builder
# ---------------------------------------------------------------------------

def build_handoff_note(
    classifier,
    policy: PolicyDecision,
    customer_plan: str,
) -> str:
    """Build a concise English handoff note (under 90 words)."""
    flags = []
    if classifier.refund_or_disputed_charge:
        flags.append("refund/dispute")
    if classifier.cancellation_or_contract_change:
        flags.append("cancellation/contract")
    if classifier.personal_data_request:
        flags.append("GDPR/AVG")
    if classifier.possible_security_incident:
        flags.append("security")
    if classifier.legal_threat:
        flags.append("legal")
    if classifier.churn_threat:
        flags.append("churn risk")
    if classifier.prompt_injection_attempt:
        flags.append("prompt injection")
    if classifier.customer_blocked:
        flags.append("customer blocked")
    if classifier.many_users_blocked:
        flags.append("multiple users blocked")

    why = policy.handoff_reasons[0] if policy.handoff_reasons else "Needs human review"

    parts = [
        f"[{policy.priority}][{policy.category}] {classifier.summary_en}.",
        f"Why human: {why}.",
        f"Suggested: {classifier.suggested_next_step_en}.",
        f"Language: {classifier.language}. Plan: {customer_plan}.",
    ]
    if flags:
        parts.append(f"Flags: {', '.join(flags)}.")

    note = " ".join(parts)
    # Trim to ~90 words
    words = note.split()
    if len(words) > 90:
        note = " ".join(words[:90]) + "..."
    return note
