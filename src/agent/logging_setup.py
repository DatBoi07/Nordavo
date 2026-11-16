"""JSON Lines structured logger for run audits.

Writes one redacted JSON line per ticket to logs/run-<timestamp>.jsonl.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from agent.redact import redact


class RunLogger:
    """Writes structured, redacted JSON lines per processed ticket."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.log_file = os.path.join(self.log_dir, f"run-{ts_str}.jsonl")
        self.entries: list[dict[str, Any]] = []

    def log_ticket(
        self,
        *,
        ticket_id: str,
        language: str,
        category: str,
        priority: str,
        action: str,
        confidence: float | None,
        faq_ids: list[str],
        flags: list[str],
        regex_signals: list[str],
        http_status: int | None,
        attempts: int,
        llm_calls: int,
        tokens_in: int,
        tokens_out: int,
        cost_eur: float,
        latency_ms: int,
        outcome: str,  # "sent", "already_handled", "failed", "dry_run"
        reason: str,
    ) -> dict[str, Any]:
        """Record a single ticket handling event, redacting sensitive content."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticket_id": ticket_id,
            "language": language,
            "category": category,
            "priority": priority,
            "action": action,
            "confidence": round(confidence, 4) if confidence is not None else None,
            "faq_ids": faq_ids,
            "flags": flags,
            "regex_signals": regex_signals,
            "http_status": http_status,
            "attempts": attempts,
            "llm_calls": llm_calls,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_eur": round(cost_eur, 6),
            "latency_ms": latency_ms,
            "outcome": outcome,
            "reason": redact(reason),
        }

        # Ensure no payment data accidentally sneaks into any string field
        for k, v in entry.items():
            if isinstance(v, str):
                entry[k] = redact(v)

        self.entries.append(entry)

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return entry
