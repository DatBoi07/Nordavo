"""Interactive debugging CLI for step-by-step pipeline inspection.

Usage:
  python -m agent debug T005
  python -m agent debug E03
  python -m agent debug --subject "Vraag" --body "Hoe ruil ik een dienst?"
"""
from __future__ import annotations

import json
import time
from typing import Any

from agent.agent import SupportAgent, TicketDecision
from agent.checks import check_reply
from agent.faq import FAQStore
from agent.helpdesk import HelpdeskClient
from agent.llm import LLMClient
from agent.policy import decide_action, run_regex_safety_net, build_handoff_note
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
from agent.redact import redact, contains_payment_data


def debug_ticket(ticket: dict[str, Any]):
    """Execute the pipeline step-by-step with verbose console output."""
    t_id = ticket.get("id", "DEBUG_01")
    subject = ticket.get("subject", "")
    raw_body = ticket.get("body", "") or ""
    plan = ticket.get("customer_plan", "Basis")
    channel = ticket.get("channel", "email")

    print("\n" + "=" * 70)
    print(f"STEP-BY-STEP PIPELINE DEBUG TRACE FOR: {t_id}")
    print("=" * 70)

    # -------------------------------------------------------------
    # STEP 1: RAW INPUT
    # -------------------------------------------------------------
    print("\n[STEP 1] RAW TICKET INPUT")
    print(f"  Channel:       {channel}")
    print(f"  Customer Plan: {plan}")
    print(f"  Subject:       {subject}")
    print(f"  Body:\n    {raw_body.replace(chr(10), chr(10) + '    ')}")

    # -------------------------------------------------------------
    # STEP 2: REDACTION (PCI & GDPR)
    # -------------------------------------------------------------
    redacted_subject = redact(subject)
    redacted_body = redact(raw_body)
    has_redaction = (redacted_body != raw_body) or (redacted_subject != subject)

    print("\n[STEP 2] PCI / GDPR REDACTION")
    print(f"  Payment data detected: {contains_payment_data(raw_body)}")
    print(f"  Was content masked:    {has_redaction}")
    if has_redaction:
        print(f"  Redacted Body:\n    {redacted_body.replace(chr(10), chr(10) + '    ')}")
    else:
        print("  -> Clean (no card or IBAN detected)")

    # Check for empty body
    if not redacted_body.strip():
        print("\n[STOP] Ticket body is empty -> Action is immediately CLOSE (P4).")
        return

    # -------------------------------------------------------------
    # STEP 3: REGEX SAFETY NET
    # -------------------------------------------------------------
    full_text = f"{redacted_subject}\n{redacted_body}"
    regex_signals = run_regex_safety_net(full_text)
    print("\n[STEP 3] REGEX SAFETY NET SCAN")
    print(f"  Any escalation pattern matched: {regex_signals.any_escalation}")
    if regex_signals.matched_patterns:
        print(f"  Matched keywords: {', '.join(regex_signals.matched_patterns)}")
    else:
        print("  -> No must-escalate keywords detected by regex.")

    # -------------------------------------------------------------
    # STEP 4: LLM CALL A (CLASSIFIER)
    # -------------------------------------------------------------
    faq_store = FAQStore()
    llm = LLMClient()

    print("\n[STEP 4] LLM CALL A: METADATA EXTRACTION (Gemini 2.5 Flash)")
    print("  Calling model with structured Pydantic schema...")
    t0 = time.monotonic()
    sys_prompt = build_classifier_prompt(faq_store.full_faq_text())
    user_prompt = build_classifier_user_prompt(
        ticket_id=t_id,
        subject=redacted_subject,
        body=redacted_body,
        customer_plan=plan,
        channel=channel,
    )
    res_a = llm.classify(sys_prompt, user_prompt, ClassifierOutput)
    clf: ClassifierOutput = res_a.data
    t_llm_a = int((time.monotonic() - t0) * 1000)

    print(f"  Latency: {t_llm_a}ms | In: {res_a.tokens_in} tokens | Out: {res_a.tokens_out} tokens")
    print("  Extraction Results:")
    print(f"    - Language:                 {clf.language}")
    print(f"    - Category:                 {clf.category}")
    print(f"    - Suggested Priority:       {clf.suggested_priority}")
    print(f"    - Confidence:               {clf.confidence}")
    print(f"    - Answerable from FAQ:      {clf.fully_answerable_from_faq}")
    print(f"    - Matched FAQ IDs:          {clf.faq_ids}")
    print(f"    - Has Uncovered Parts:      {clf.has_uncovered_parts}")
    print(f"    - Prompt Injection Flag:    {clf.prompt_injection_attempt}")
    print(f"    - Refund / Dispute Flag:    {clf.refund_or_disputed_charge}")
    print(f"    - Cancellation Flag:        {clf.cancellation_or_contract_change}")
    print(f"    - GDPR / AVG Flag:          {clf.personal_data_request}")
    print(f"    - Security Incident Flag:   {clf.possible_security_incident}")
    print(f"    - Legal Threat Flag:        {clf.legal_threat}")
    print(f"    - Churn Threat Flag:        {clf.churn_threat}")
    print(f"    - Customer Blocked Flag:    {clf.customer_blocked}")
    print(f"    - Many Users Blocked Flag:  {clf.many_users_blocked}")
    print(f"    - English Summary:          '{clf.summary_en}'")

    # -------------------------------------------------------------
    # STEP 5: PURE CODE POLICY DECISION
    # -------------------------------------------------------------
    print("\n[STEP 5] DETERMINISTIC POLICY DECISION (policy.py)")
    policy = decide_action(
        classifier=clf,
        regex_signals=regex_signals,
        body_text=redacted_body,
    )
    print(f"  -> FINAL ACTION:   {policy.action.upper()}")
    print(f"  -> FINAL PRIORITY: {policy.priority}")
    print(f"  -> CATEGORY:       {policy.category}")
    print(f"  -> REASON:         {policy.reason}")
    if policy.handoff_reasons:
        print(f"  -> TRIGGER RULES:  {', '.join(policy.handoff_reasons)}")

    # -------------------------------------------------------------
    # STEP 6: ACTION EXECUTION & VERIFICATION
    # -------------------------------------------------------------
    if policy.action == "close":
        print("\n[STEP 6] ACTION: CLOSE")
        print("  Spam or empty ticket closed without reply.")

    elif policy.action == "handoff":
        print("\n[STEP 6] ACTION: HANDOFF TO HUMAN")
        note = build_handoff_note(clf, policy, customer_plan=plan)
        print("  Generated English note for support dashboard (< 90 words):")
        print(f"    \"{note}\"")

    elif policy.action == "reply":
        print("\n[STEP 6] ACTION: REPLY (DRAFT & VERIFY)")
        faq_text = faq_store.get_text(clf.faq_ids)
        print(f"  Retrieving FAQ knowledge for: {clf.faq_ids}")

        # Call B: Drafter
        print("  [6a] Drafting reply in customer language (gemini-2.5-flash)...")
        sys_draft = build_drafter_prompt(faq_text, clf.language, plan)
        user_draft = build_drafter_user_prompt(redacted_subject, redacted_body)
        res_b = llm.draft_reply(sys_draft, user_draft)
        draft = str(res_b.data).strip()
        print(f"    Draft reply:\n      {draft.replace(chr(10), chr(10) + '      ')}")

        # Call C: Verifier
        print("\n  [6b] Verifying with independent model (gemini-3.5-flash-lite)...")
        sys_ver = build_verifier_prompt()
        user_ver = build_verifier_user_prompt(
            ticket_subject=redacted_subject,
            ticket_body=redacted_body,
            reply_text=draft,
            expected_language=clf.language,
            faq_entries_text=faq_text,
        )
        res_c = llm.verify(sys_ver, user_ver, VerifierOutput)
        ver: VerifierOutput = res_c.data

        print(f"    - Verifier Score:      {ver.score}")
        print(f"    - Language Correct:    {ver.language_correct} ({ver.language_detected})")
        print(f"    - Grounded in FAQ:     {ver.grounded_in_faq}")
        print(f"    - Question Answered:   {ver.question_answered}")
        print(f"    - Back-Translation (EN):\n      {ver.reply_en}")

        # Deterministic checks
        chk = check_reply(
            reply_text=draft,
            expected_language=clf.language,
            faq_store=faq_store,
            cited_faq_ids=clf.faq_ids,
            verifier_output=ver,
        )
        print(f"\n  [6c] Deterministic Check Result: {'PASSED' if chk.passed else 'FAILED'}")
        if chk.failures:
            print(f"    Issues detected: {chk.failures}")

    # -------------------------------------------------------------
    # STEP 7: FINAL API PAYLOAD
    # -------------------------------------------------------------
    decision = TicketDecision(
        ticket_id=t_id,
        action=policy.action,
        language=clf.language,
        category=policy.category,
        priority=policy.priority,
        reason=policy.reason,
        confidence=clf.confidence,
    )
    if policy.action == "reply":
        decision.draft_reply = draft
    elif policy.action == "handoff":
        decision.handoff_note = note

    print("\n[STEP 7] FINAL API PAYLOAD (Ready to POST)")
    print(json.dumps(decision.to_api_payload(), indent=2, ensure_ascii=False))
    print("=" * 70 + "\n")
