"""Audit report generator.

Produces reports/review.md in clean English so non-Dutch reviewers
can audit every single ticket decision, draft reply, and back-translation.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Sequence

from agent.agent import TicketDecision


def generate_review_report(
    decisions: Sequence[TicketDecision],
    output_path: str = "reports/review.md",
    run_tag: str = "Dry Run",
) -> str:
    """Generate a markdown audit report from processed ticket decisions."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    total_tickets = len(decisions)
    actions_count: dict[str, int] = {}
    categories_count: dict[str, int] = {}
    priorities_count: dict[str, int] = {}
    total_tokens_in = sum(d.tokens_in for d in decisions)
    total_tokens_out = sum(d.tokens_out for d in decisions)
    total_cost_eur = sum(d.cost_eur for d in decisions)

    for d in decisions:
        actions_count[d.action] = actions_count.get(d.action, 0) + 1
        categories_count[d.category] = categories_count.get(d.category, 0) + 1
        priorities_count[d.priority] = priorities_count.get(d.priority, 0) + 1

    lines: list[str] = []
    lines.append(f"# Roosterly Support Agent: Review & Audit Report ({run_tag})\n")
    lines.append(f"**Generated at:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ")
    lines.append(f"**Total tickets processed:** {total_tickets}  ")
    lines.append(f"**Total cost:** €{total_cost_eur:.4f} (€{(total_cost_eur / total_tickets * 100):.4f} per 100 tickets)\n")

    lines.append("## Executive Summary\n")
    lines.append("| Dimension | Breakdown |")
    lines.append("|---|---|")
    lines.append(f"| **Actions** | {', '.join(f'{k}: {v}' for k, v in actions_count.items())} |")
    lines.append(f"| **Categories** | {', '.join(f'{k}: {v}' for k, v in categories_count.items())} |")
    lines.append(f"| **Priorities** | {', '.join(f'{k}: {v}' for k, v in priorities_count.items())} |")
    lines.append(f"| **Tokens** | Input: {total_tokens_in:,} / Output: {total_tokens_out:,} |")
    lines.append("\n---\n")

    lines.append("## Ticket Details\n")

    for i, d in enumerate(decisions, start=1):
        lines.append(f"### Ticket {d.ticket_id} ({i}/{total_tickets}): {d.ticket_subject}\n")
        lines.append(f"- **Decision Action:** `{d.action.upper()}`")
        lines.append(f"- **Language:** `{d.language}` | **Category:** `{d.category}` | **Priority:** `{d.priority}`")
        lines.append(f"- **Confidence:** `{d.confidence if d.confidence is not None else 'N/A'}`")
        lines.append(f"- **Reason:** {d.reason}")
        if d.faq_ids:
            lines.append(f"- **Cited FAQs:** {', '.join(d.faq_ids)}")
        if d.flags:
            lines.append(f"- **Triggered Safety Flags:** {', '.join(d.flags)}")
        if d.regex_signals:
            lines.append(f"- **Regex Signals:** {', '.join(d.regex_signals)}")

        lines.append("\n**Customer Ticket (Original):**")
        lines.append(f"> {d.ticket_body.replace(chr(10), chr(10) + '> ')}\n")

        lines.append(f"**English Summary:** *{d.summary_en}*\n")

        if d.action == "reply":
            lines.append("**Drafted Reply (Customer Language):**")
            lines.append(f"```\n{d.draft_reply}\n```\n")
            if d.reply_en:
                lines.append("**English Back-Translation (for verification):**")
                lines.append(f"> {d.reply_en.replace(chr(10), chr(10) + '> ')}\n")
            lines.append(f"**Checks Status:** {'Passed' if d.check_passed else 'Failed'}")
            if d.check_failures:
                lines.append(f"**Check Issues:** {', '.join(d.check_failures)}")

        elif d.action == "handoff":
            lines.append("**Handoff Note for Human Agent (English):**")
            lines.append(f"```\n{d.handoff_note}\n```\n")

        elif d.action == "close":
            lines.append(f"**Closed Ticket:** {d.reason}\n")

        lines.append(f"*Latency: {d.latency_ms}ms | Cost: €{d.cost_eur:.6f} | LLM Calls: {d.llm_calls}*\n")
        lines.append("\n---\n")

    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return content
