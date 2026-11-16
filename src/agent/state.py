"""SQLite state store for crash-safe ticket processing."""
from __future__ import annotations

import json
import sqlite3
import os
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = os.path.join("state", "agent.db")

# Ticket statuses
PENDING = "pending"
DECIDED = "decided"
SENDING = "sending"
SENT = "sent"
FAILED = "failed"


class StateStore:
    """SQLite-backed state for idempotent ticket processing."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                decision_json TEXT,
                content_hash TEXT,
                prompt_version TEXT,
                model TEXT,
                attempts INTEGER DEFAULT 0,
                error_message TEXT,
                updated_at TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- queries -----------------------------------------------------------

    def get_ticket(self, ticket_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_tickets_by_status(self, status: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE status = ?", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_tickets(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM tickets").fetchall()
        return [dict(r) for r in rows]

    # -- state transitions -------------------------------------------------

    def ensure_ticket(self, ticket_id: str, content_hash: str | None = None):
        """Create a pending ticket if it doesn't exist."""
        existing = self.get_ticket(ticket_id)
        if existing is None:
            self._conn.execute(
                "INSERT INTO tickets (id, status, content_hash, updated_at) VALUES (?, ?, ?, ?)",
                (ticket_id, PENDING, content_hash, self._now()),
            )
            self._conn.commit()

    def store_decision(
        self,
        ticket_id: str,
        decision: dict,
        content_hash: str | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
    ):
        """Store the LLM decision — status becomes 'decided'."""
        self._conn.execute(
            """UPDATE tickets
               SET status = ?, decision_json = ?, content_hash = ?,
                   prompt_version = ?, model = ?, updated_at = ?
               WHERE id = ?""",
            (DECIDED, json.dumps(decision), content_hash, prompt_version, model, self._now(), ticket_id),
        )
        self._conn.commit()

    def mark_sending(self, ticket_id: str):
        self._conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (SENDING, self._now(), ticket_id),
        )
        self._conn.commit()

    def mark_sent(self, ticket_id: str):
        self._conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (SENT, self._now(), ticket_id),
        )
        self._conn.commit()

    def mark_failed(self, ticket_id: str, error: str = ""):
        self._conn.execute(
            "UPDATE tickets SET status = ?, error_message = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (FAILED, error, self._now(), ticket_id),
        )
        self._conn.commit()

    def clear_ticket(self, ticket_id: str):
        """Reset a ticket to pending (e.g. after practice reset)."""
        self._conn.execute(
            "UPDATE tickets SET status = ?, decision_json = NULL, error_message = NULL, attempts = 0, updated_at = ? WHERE id = ?",
            (PENDING, self._now(), ticket_id),
        )
        self._conn.commit()

    def clear_all(self):
        """Clear all state (after practice reset)."""
        self._conn.execute("DELETE FROM tickets")
        self._conn.commit()

    def get_cached_decision(
        self, content_hash: str, prompt_version: str, model: str
    ) -> dict | None:
        """Return a cached decision for the same content+prompt+model combo."""
        row = self._conn.execute(
            """SELECT decision_json FROM tickets
               WHERE content_hash = ? AND prompt_version = ? AND model = ?
               AND decision_json IS NOT NULL
               LIMIT 1""",
            (content_hash, prompt_version, model),
        ).fetchone()
        if row and row["decision_json"]:
            return json.loads(row["decision_json"])
        return None

    def get_decision(self, ticket_id: str) -> dict | None:
        row = self.get_ticket(ticket_id)
        if row and row.get("decision_json"):
            return json.loads(row["decision_json"])
        return None

    def close(self):
        self._conn.close()
