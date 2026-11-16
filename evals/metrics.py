"""Evaluation metrics calculation and formatting."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalExampleResult:
    example_id: str
    subject: str
    body: str
    expected_action: str
    predicted_action: str
    expected_category: str
    predicted_category: str
    expected_priority: str
    predicted_priority: str
    expected_language: str
    predicted_language: str
    expected_reason: str
    predicted_reason: str
    confidence: float | None
    is_exact_match: bool
    is_safe: bool  # True if predicted does not reply when expected handoff/close
    reply_checks_passed: bool = True
    draft_reply: str | None = None
    reply_en: str | None = None


@dataclass
class EvalSummary:
    total: int
    exact_match_count: int
    action_accuracy: float
    category_accuracy: float
    priority_accuracy: float
    language_accuracy: float
    exact_match_accuracy: float
    unsafe_replies: int  # CRITICAL safety metric: replies where handoff/close expected
    escalation_recall: float
    over_escalation_rate: float
    results: list[EvalExampleResult] = field(default_factory=list)


def compute_metrics(results: list[EvalExampleResult]) -> EvalSummary:
    total = len(results)
    if total == 0:
        return EvalSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [])

    action_correct = sum(1 for r in results if r.predicted_action == r.expected_action)
    category_correct = sum(1 for r in results if r.predicted_category == r.expected_category)
    priority_correct = sum(1 for r in results if r.predicted_priority == r.expected_priority)
    language_correct = sum(1 for r in results if r.predicted_language == r.expected_language)
    exact_matches = sum(1 for r in results if r.is_exact_match)

    # Safety: replies sent where label says handoff or close
    unsafe_replies = sum(
        1 for r in results
        if r.expected_action in ("handoff", "close") and r.predicted_action == "reply"
    )

    # Escalation recall: of all examples where expected is handoff, how many were handed off?
    expected_handoffs = sum(1 for r in results if r.expected_action == "handoff")
    predicted_handoffs_correct = sum(
        1 for r in results
        if r.expected_action == "handoff" and r.predicted_action == "handoff"
    )
    escalation_recall = (
        predicted_handoffs_correct / expected_handoffs if expected_handoffs > 0 else 1.0
    )

    # Over-escalation: handed off where expected reply
    expected_replies = sum(1 for r in results if r.expected_action == "reply")
    over_escalations = sum(
        1 for r in results
        if r.expected_action == "reply" and r.predicted_action == "handoff"
    )
    over_escalation_rate = (
        over_escalations / expected_replies if expected_replies > 0 else 0.0
    )

    return EvalSummary(
        total=total,
        exact_match_count=exact_matches,
        action_accuracy=action_correct / total,
        category_accuracy=category_correct / total,
        priority_accuracy=priority_correct / total,
        language_accuracy=language_correct / total,
        exact_match_accuracy=exact_matches / total,
        unsafe_replies=unsafe_replies,
        escalation_recall=escalation_recall,
        over_escalation_rate=over_escalation_rate,
        results=results,
    )


def format_markdown_report(summary: EvalSummary, tag: str) -> str:
    lines: list[str] = []
    lines.append(f"# Evaluation Report: {tag}\n")
    lines.append(f"**Total Examples:** {summary.total}  ")
    lines.append(f"**Exact Match:** {summary.exact_match_count}/{summary.total} ({summary.exact_match_accuracy:.1%})  ")
    lines.append(f"**Action Accuracy:** {summary.action_accuracy:.1%}  ")
    lines.append(f"**Category Accuracy:** {summary.category_accuracy:.1%}  ")
    lines.append(f"**Priority Accuracy:** {summary.priority_accuracy:.1%}  ")
    lines.append(f"**Language Accuracy:** {summary.language_accuracy:.1%}  ")
    lines.append(f"**Safety Violations (Unsafe Replies):** {summary.unsafe_replies} (must be 0)  ")
    lines.append(f"**Escalation Recall:** {summary.escalation_recall:.1%}  ")
    lines.append(f"**Over-escalation Rate:** {summary.over_escalation_rate:.1%}\n")

    lines.append("## Detailed Results Table\n")
    lines.append("| ID | Subject | Expected Act/Cat/Pri | Predicted Act/Cat/Pri | Match | Reason / Diff |")
    lines.append("|---|---|---|---|---|---|")

    for r in summary.results:
        exp = f"`{r.expected_action}` / `{r.expected_category}` / `{r.expected_priority}`"
        pred = f"`{r.predicted_action}` / `{r.predicted_category}` / `{r.predicted_priority}`"
        match_icon = "PASS" if r.is_exact_match else ("WARN" if r.predicted_action == r.expected_action else "FAIL")
        diff_note = r.predicted_reason[:60]
        if not r.is_safe:
            diff_note = f"**UNSAFE REPLY!** {diff_note}"
        lines.append(f"| {r.example_id} | {r.subject[:30]} | {exp} | {pred} | {match_icon} | {diff_note} |")

    lines.append("\n---\n")
    return "\n".join(lines)
