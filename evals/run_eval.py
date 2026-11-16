"""Evaluation harness for testing against gold labels.

Tests:
1. Official 10 examples from GET /examples (cached to evals/data/examples.json)
2. Synthetic 10 extra cases from evals/data/extra_cases.json
Saves detailed JSON and Markdown results under evals/results/<tag>.*
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from agent.agent import SupportAgent, TicketDecision
from agent.faq import FAQStore
from agent.helpdesk import HelpdeskClient
from agent.llm import LLMClient, PROMPT_VERSION
from evals.metrics import (
    EvalExampleResult,
    EvalSummary,
    compute_metrics,
    format_markdown_report,
)


def load_or_fetch_examples(helpdesk: HelpdeskClient) -> list[dict]:
    """Load cached examples or fetch from helpdesk API."""
    cache_path = os.path.join("evals", "data", "examples.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("examples", data)

    print("Fetching gold examples from helpdesk API...")
    examples = helpdesk.examples()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"examples": examples}, f, indent=2, ensure_ascii=False)
    return examples


def load_extra_cases() -> list[dict]:
    """Load the synthetic extra test cases."""
    path = os.path.join("evals", "data", "extra_cases.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data.get("extra_cases", data)


def evaluate_dataset(
    agent: SupportAgent,
    dataset: list[dict],
    dataset_name: str = "examples",
) -> EvalSummary:
    """Run agent against a dataset and compare to labels."""
    results: list[EvalExampleResult] = []

    for i, ex in enumerate(dataset, start=1):
        ex_id = ex["id"]
        subj = ex.get("subject", "")
        body = ex.get("body", "")
        label = ex.get("label", {})

        print(f"[{i}/{len(dataset)}] Evaluating {ex_id}: '{subj[:35]}'...")

        # IMPORTANT: Feed only ticket content to agent — NEVER label.reason!
        ticket_payload = {
            "id": ex_id,
            "subject": subj,
            "body": body,
            "customer_plan": ex.get("customer_plan", "Basis"),
            "channel": ex.get("channel", "email"),
        }

        decision: TicketDecision = agent.process_ticket(ticket_payload)

        exp_act = label.get("action", "")
        exp_cat = label.get("category", "")
        exp_pri = label.get("priority", "")
        exp_lng = label.get("language", "")
        exp_rsn = label.get("reason", "")

        is_exact = (
            decision.action == exp_act
            and decision.category == exp_cat
            and decision.priority == exp_pri
            and decision.language == exp_lng
        )

        is_safe = not (exp_act in ("handoff", "close") and decision.action == "reply")

        results.append(
            EvalExampleResult(
                example_id=ex_id,
                subject=subj,
                body=body,
                expected_action=exp_act,
                predicted_action=decision.action,
                expected_category=exp_cat,
                predicted_category=decision.category,
                expected_priority=exp_pri,
                predicted_priority=decision.priority,
                expected_language=exp_lng,
                predicted_language=decision.language,
                expected_reason=exp_rsn,
                predicted_reason=decision.reason,
                confidence=decision.confidence,
                is_exact_match=is_exact,
                is_safe=is_safe,
                reply_checks_passed=decision.check_passed,
                draft_reply=decision.draft_reply,
                reply_en=decision.reply_en,
            )
        )

    return compute_metrics(results)


def run_eval_suite(tag: str = "baseline", repeats: int = 1) -> int:
    """Execute evaluation across examples and extra test cases."""
    print("=" * 60)
    print(f"ROOSTERLY AGENT EVALUATION: {tag} (repeats={repeats})")
    print("=" * 60)

    helpdesk = HelpdeskClient()
    faq_store = FAQStore()
    llm = LLMClient()
    agent = SupportAgent(faq_store=faq_store, llm_client=llm)

    official_examples = load_or_fetch_examples(helpdesk)
    extra_cases = load_extra_cases()

    print(f"\nLoaded {len(official_examples)} official examples and {len(extra_cases)} extra cases.\n")

    # Run official examples
    print("--- EVALUATING OFFICIAL 10 EXAMPLES ---")
    official_summary = evaluate_dataset(agent, official_examples, dataset_name="official")

    # Run extra cases
    extra_summary: EvalSummary | None = None
    if extra_cases:
        print("\n--- EVALUATING 10 EXTRA SYNTHETIC CASES ---")
        extra_summary = evaluate_dataset(agent, extra_cases, dataset_name="extra")

    # Save Results
    os.makedirs(os.path.join("evals", "results"), exist_ok=True)
    json_path = os.path.join("evals", "results", f"{tag}.json")
    md_path = os.path.join("evals", "results", f"{tag}.md")

    report_md = format_markdown_report(official_summary, f"{tag} (Official Examples)")
    if extra_summary:
        report_md += "\n\n" + format_markdown_report(extra_summary, f"{tag} (Extra Synthetic Cases)")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    # Save JSON dump
    json_dump = {
        "tag": tag,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": llm._model,
        "verifier_model": llm._verifier_model,
        "prompt_version": PROMPT_VERSION,
        "official_summary": {
            "total": official_summary.total,
            "exact_match_accuracy": official_summary.exact_match_accuracy,
            "action_accuracy": official_summary.action_accuracy,
            "category_accuracy": official_summary.category_accuracy,
            "priority_accuracy": official_summary.priority_accuracy,
            "language_accuracy": official_summary.language_accuracy,
            "unsafe_replies": official_summary.unsafe_replies,
            "escalation_recall": official_summary.escalation_recall,
            "over_escalation_rate": official_summary.over_escalation_rate,
        },
    }
    if extra_summary:
        json_dump["extra_summary"] = {
            "total": extra_summary.total,
            "exact_match_accuracy": extra_summary.exact_match_accuracy,
            "action_accuracy": extra_summary.action_accuracy,
            "category_accuracy": extra_summary.category_accuracy,
            "priority_accuracy": extra_summary.priority_accuracy,
            "language_accuracy": extra_summary.language_accuracy,
            "unsafe_replies": extra_summary.unsafe_replies,
            "escalation_recall": extra_summary.escalation_recall,
            "over_escalation_rate": extra_summary.over_escalation_rate,
        }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_dump, f, indent=2)

    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY (OFFICIAL EXAMPLES)")
    print("=" * 60)
    print(f"Exact Match: {official_summary.exact_match_count}/{official_summary.total} ({official_summary.exact_match_accuracy:.1%})")
    print(f"Action Accuracy: {official_summary.action_accuracy:.1%}")
    print(f"Category Accuracy: {official_summary.category_accuracy:.1%}")
    print(f"Priority Accuracy: {official_summary.priority_accuracy:.1%}")
    print(f"Language Accuracy: {official_summary.language_accuracy:.1%}")
    print(f"Unsafe Replies: {official_summary.unsafe_replies} (must be 0)")
    print(f"Escalation Recall: {official_summary.escalation_recall:.1%}")
    print(f"Over-escalation Rate: {official_summary.over_escalation_rate:.1%}")

    if extra_summary:
        print("\n" + "=" * 60)
        print("EVALUATION SUMMARY (EXTRA SYNTHETIC CASES)")
        print("=" * 60)
        print(f"Exact Match: {extra_summary.exact_match_count}/{extra_summary.total} ({extra_summary.exact_match_accuracy:.1%})")
        print(f"Action Accuracy: {extra_summary.action_accuracy:.1%}")
        print(f"Unsafe Replies: {extra_summary.unsafe_replies} (must be 0)")

    print(f"\nReports saved to:")
    print(f"  Markdown: {md_path}")
    print(f"  JSON:     {json_path}")
    print("=" * 60)

    # Fail if there are any unsafe replies!
    if official_summary.unsafe_replies > 0:
        print("\nCRITICAL FAILURE: Agent produced unsafe replies where handoff was required!")
        return 1

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="baseline")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    sys.exit(run_eval_suite(tag=args.tag, repeats=args.repeats))
