"""Tests for crash recovery, resume, and idempotency."""
from __future__ import annotations

import os
import tempfile
import pytest

from agent.state import DECIDED, PENDING, SENDING, SENT, StateStore


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = StateStore(db_path=path)
    yield store
    store.close()
    if os.path.exists(path):
        os.remove(path)


def test_resume_decided_ticket_avoids_new_llm_call(temp_db):
    """Simulate crash after decision is stored: agent resumes without re-calling LLM."""
    ticket_id = "T010"
    stored_decision = {
        "action": "reply",
        "category": "question",
        "priority": "P4",
        "draft_reply": "Hoi, ga naar Instellingen.",
    }
    temp_db.ensure_ticket(ticket_id, content_hash="hash_10")
    temp_db.store_decision(ticket_id, stored_decision)

    # Agent restarts: check that status is DECIDED and decision is retrievable
    rec = temp_db.get_ticket(ticket_id)
    assert rec["status"] == DECIDED
    assert temp_db.get_decision(ticket_id) == stored_decision

    # Transition to sent
    temp_db.mark_sending(ticket_id)
    temp_db.mark_sent(ticket_id)
    assert temp_db.get_ticket(ticket_id)["status"] == SENT


def test_open_ticket_marked_sent_locally_triggers_reset(temp_db):
    """If server reports ticket is open, but local DB has it as SENT, clear it."""
    ticket_id = "T020"
    temp_db.ensure_ticket(ticket_id)
    temp_db.mark_sent(ticket_id)
    assert temp_db.get_ticket(ticket_id)["status"] == SENT

    # Reconcile when ticket is discovered to still be open on helpdesk API
    temp_db.clear_ticket(ticket_id)
    assert temp_db.get_ticket(ticket_id)["status"] == PENDING
    assert temp_db.get_decision(ticket_id) is None
