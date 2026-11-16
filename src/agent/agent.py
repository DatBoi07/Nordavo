"""Per-ticket processing pipeline.

Orchestrates:
1. Redaction of payment data from ticket input.
2. Regex safety net scanning for must-escalate keywords.
3. LLM Call A: classification, metadata, FAQ mapping, flags.
4. Policy enforcement (code decides action, priority, category).
5. If action == reply:
   - LLM Call B: draft grounded reply from cited FAQ.
   - Deterministic checks (grounding, payment, language, forbidden promises).
   - LLM Call C: verifier model + English back-translation.
   - If checks fail: 1 retry/regeneration; if still fail: fallback to handoff.
6. If action == handoff: build structured English note.
7. If action == close: set required fields (language, category, priority, reason).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from agent.checks import check_reply
from agent.faq import FAQStore
from agent.llm import LLMClient, PROMPT_VERSION
from agent.policy import (
    PolicyDecision,
    decide_action,
    run_regex_safety_net,
    build_handoff_note,
)
from agent.prompts import (
    ClassifierOutput,
    VerifierOutput,
    build_classifier_prompt,
    build_classifier_user_prompt,
    build_drafter_prompt,
    build_drafter_user_prompt,
    build_verifier_prompt,
    build_verifier_user_prompt,
)
from agent.redact import redact

logger = logging.getLogger(__name__)


@dataclass
class TicketDecision:
    """Complete decision container for a ticket, including payload and audit trail."""
    ticket_id: str
    action: str  # "reply", "handoff", "close"
    language: str
    category: str
    priority: str
    reason: str
    confidence: float | None = None
    draft_reply: str | None = None
    handoff_note: str | None = None

    # Audit & report data
    ticket_subject: str = ""
    ticket_body: str = ""
    summary_en: str = ""
    reply_en: str | None = None
    faq_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    regex_signals: list[str] = field(default_factory=list)
    check_passed: bool = True
    check_failures: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_eur: float = 0.0
    latency_ms: int = 0

    def to_api_payload(self) -> dict[str, Any]:
        """Format the exact payload expected by the helpdesk API action endpoints."""
        payload: dict[str, Any] = {
            "language": self.language,
            "category": self.category,
            "priority": self.priority,
            "reason": redact(self.reason),
        }
        if self.confidence is not None:
            payload["confidence"] = round(float(self.confidence), 4)

        if self.action == "reply":
            payload["draft_reply"] = redact(self.draft_reply or "")
        elif self.action == "handoff":
            payload["handoff_note"] = redact(self.handoff_note or "")
        # close only requires language, category, priority, reason

        return payload


class SupportAgent:
    """The core support agent pipeline."""

    def __init__(
        self,
        faq_store: FAQStore | None = None,
        llm_client: LLMClient | None = None,
        confidence_threshold: float | None = None,
    ):
        self.faq_store = faq_store or FAQStore()
        self.llm = llm_client or LLMClient()
        self.confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
        )

    def process_ticket(self, ticket: dict[str, Any]) -> TicketDecision:
        """Process a single ticket through the complete safety and decision pipeline."""
        start_time = time.monotonic()
        ticket_id = ticket["id"]
        subject = ticket.get("subject", "")
        raw_body = ticket.get("body", "") or ""
        customer_plan = ticket.get("customer_plan", "Basis")
        channel = ticket.get("channel", "email")

        # Track tokens and calls for this ticket
        initial_tokens_in = self.llm.total_tokens_in
        initial_tokens_out = self.llm.total_tokens_out
        initial_calls = self.llm.call_count

        # Step 1: Preprocessing & Redaction (PCI-DSS & GDPR safety)
        redacted_body = redact(raw_body)
        redacted_subject = redact(subject)
        full_text = f"{redacted_subject}\n{redacted_body}".strip()

        # Step 2: Deterministic Empty Check
        if not full_text or not redacted_body.strip():
            latency = int((time.monotonic() - start_time) * 1000)
            return TicketDecision(
                ticket_id=ticket_id,
                action="close",
                language="nl",
                category="other",
                priority="P4",
                reason="Empty ticket body.",
                confidence=1.0,
                ticket_subject=subject,
                ticket_body=raw_body,
                summary_en="Empty ticket.",
                latency_ms=latency,
            )

        # Step 3: Regex Safety Net
        regex_signals = run_regex_safety_net(full_text)

        # Step 4: LLM Call A (Classifier)
        classifier: ClassifierOutput
        try:
            sys_prompt = build_classifier_prompt(self.faq_store.full_faq_text())
            user_prompt = build_classifier_user_prompt(
                ticket_id=ticket_id,
                subject=redacted_subject,
                body=redacted_body,
                customer_plan=customer_plan,
                channel=channel,
            )
            res_a = self.llm.classify(sys_prompt, user_prompt, ClassifierOutput)
            classifier = res_a.data
        except Exception as e:
            logger.error("LLM Classifier failed for ticket %s: %s", ticket_id, e)
            # Fail toward human: safe minimal handoff
            latency = int((time.monotonic() - start_time) * 1000)
            tokens_in = self.llm.total_tokens_in - initial_tokens_in
            tokens_out = self.llm.total_tokens_out - initial_tokens_out
            calls = self.llm.call_count - initial_calls
            return TicketDecision(
                ticket_id=ticket_id,
                action="handoff",
                language="nl",
                category="other",
                priority="P3",
                reason="Automated classification failed; escalated to human for manual review.",
                confidence=0.0,
                handoff_note=f"[P3][other] Automated analysis failed for ticket {ticket_id}. Please review manually.",
                ticket_subject=subject,
                ticket_body=raw_body,
                summary_en="Classification failed.",
                llm_calls=calls,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_eur=self._calc_ticket_cost(tokens_in, tokens_out),
                latency_ms=latency,
            )

        # Collect active flags for auditing
        active_flags = [
            flag for flag in [
                "refund_or_disputed_charge" if classifier.refund_or_disputed_charge else None,
                "cancellation_or_contract_change" if classifier.cancellation_or_contract_change else None,
                "personal_data_request" if classifier.personal_data_request else None,
                "possible_security_incident" if classifier.possible_security_incident else None,
                "legal_threat" if classifier.legal_threat else None,
                "churn_threat" if classifier.churn_threat else None,
                "many_users_blocked" if classifier.many_users_blocked else None,
                "customer_blocked" if classifier.customer_blocked else None,
                "prompt_injection_attempt" if classifier.prompt_injection_attempt else None,
                "has_uncovered_parts" if classifier.has_uncovered_parts else None,
            ] if flag is not None
        ]

        # Step 5: Policy decision
        policy = decide_action(
            classifier=classifier,
            regex_signals=regex_signals,
            body_text=redacted_body,
            confidence_threshold=self.confidence_threshold,
        )

        decision_action = policy.action
        draft_reply: str | None = None
        reply_en: str | None = None
        handoff_note: str | None = None
        check_passed = True
        check_failures: list[str] = []
        final_confidence = classifier.confidence

        # Step 6: Execute Action
        if decision_action == "close":
            final_confidence = (
                classifier.spam_confidence if classifier.is_empty_or_spam else 1.0
            )

        elif decision_action == "handoff":
            handoff_note = build_handoff_note(
                classifier=classifier,
                policy=policy,
                customer_plan=customer_plan,
            )
            final_confidence = classifier.confidence

        elif decision_action == "reply":
            # Attempt drafting and verification (up to 2 attempts: 1 initial + 1 regeneration)
            cited_faq_ids = classifier.faq_ids
            faq_text = self.faq_store.get_text(cited_faq_ids)
            lang = classifier.language

            for attempt in range(2):
                check_failures = []
                check_passed = True
                try:
                    # Call B: Draft reply
                    sys_draft = build_drafter_prompt(faq_text, lang, customer_plan)
                    user_draft = build_drafter_user_prompt(redacted_subject, redacted_body)
                    res_b = self.llm.draft_reply(sys_draft, user_draft)
                    draft_reply = str(res_b.data).strip()

                    # Call C: Verifier
                    sys_ver = build_verifier_prompt()
                    user_ver = build_verifier_user_prompt(
                        ticket_subject=redacted_subject,
                        ticket_body=redacted_body,
                        reply_text=draft_reply,
                        expected_language=lang,
                        faq_entries_text=faq_text,
                    )
                    res_c = self.llm.verify(sys_ver, user_ver, VerifierOutput)
                    ver_out: VerifierOutput = res_c.data
                    reply_en = ver_out.reply_en

                    # Deterministic reply checks
                    chk = check_reply(
                        reply_text=draft_reply,
                        expected_language=lang,
                        faq_store=self.faq_store,
                        cited_faq_ids=cited_faq_ids,
                        verifier_output=ver_out,
                        confidence_threshold=self.confidence_threshold,
                    )

                    if chk.passed:
                        # Confidence is min(classifier, verifier)
                        final_confidence = min(classifier.confidence, ver_out.score)
                        check_passed = True
                        break
                    else:
                        check_passed = False
                        check_failures = chk.failures
                        logger.warning(
                            "Reply check failed on attempt %d for ticket %s: %s",
                            attempt + 1, ticket_id, chk.failures
                        )
                except Exception as e:
                    logger.warning("Reply draft/verification error on attempt %d: %s", attempt + 1, e)
                    check_passed = False
                    check_failures.append(f"LLM draft/verification exception: {e}")

            # If checks failed after regeneration, fallback toward human handoff
            if not check_passed:
                decision_action = "handoff"
                policy.action = "handoff"
                policy.priority = max_priority(policy.priority, "P3")
                policy.handoff_reasons.append(f"Draft verification failed: {'; '.join(check_failures)}")
                handoff_note = build_handoff_note(classifier, policy, customer_plan)
                final_confidence = classifier.confidence

        # Token and cost tracking
        tokens_in = self.llm.total_tokens_in - initial_tokens_in
        tokens_out = self.llm.total_tokens_out - initial_tokens_out
        calls = self.llm.call_count - initial_calls
        cost_eur = self._calc_ticket_cost(tokens_in, tokens_out)
        latency = int((time.monotonic() - start_time) * 1000)

        return TicketDecision(
            ticket_id=ticket_id,
            action=decision_action,
            language=classifier.language,
            category=policy.category,
            priority=policy.priority,
            reason=policy.reason,
            confidence=final_confidence,
            draft_reply=draft_reply if decision_action == "reply" else None,
            handoff_note=handoff_note if decision_action == "handoff" else None,
            ticket_subject=subject,
            ticket_body=raw_body,
            summary_en=classifier.summary_en,
            reply_en=reply_en,
            faq_ids=classifier.faq_ids,
            flags=active_flags,
            regex_signals=regex_signals.matched_patterns,
            check_passed=check_passed,
            check_failures=check_failures,
            llm_calls=calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_eur=cost_eur,
            latency_ms=latency,
        )

    def _calc_ticket_cost(self, tokens_in: int, tokens_out: int) -> float:
        cost_usd = (
            (tokens_in / 1_000_000) * self.llm._price_in
            + (tokens_out / 1_000_000) * self.llm._price_out
        )
        return round(cost_usd * self.llm._usd_to_eur, 6)


def max_priority(p1: str, p2: str) -> str:
    """Return the higher priority (P1 > P2 > P3 > P4)."""
    order = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
    r1 = order.get(p1, 4)
    r2 = order.get(p2, 4)
    return p1 if r1 <= r2 else p2
