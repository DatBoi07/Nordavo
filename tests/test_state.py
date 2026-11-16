"""Unit tests for SQLite state store and idempotency."""
from __future__ import annotations

import os
import tempfile
import pytest

from agent.state import (
    DECIDED,
    FAILED,
    PENDING,
    SENDING,
    SENT,
    StateStore,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = StateStore(db_path=path)
    yield store
    store.close()
    if os.path.exists(path):
        os.remove(path)


def test_ensure_ticket_creates_pending(temp_db):
    temp_db.ensure_ticket("T001", content_hash="hash123")
    rec = temp_db.get_ticket("T001")
    assert rec is not None
    assert rec["status"] == PENDING
    assert rec["content_hash"] == "hash123"


def test_store_decision_transitions_to_decided(temp_db):
    temp_db.ensure_ticket("T001")
    decision = {"action": "reply", "language": "nl", "priority": "P4"}
    temp_db.store_decision("T001", decision, content_hash="h1", prompt_version="v1", model="gemini")

    rec = temp_db.get_ticket("T001")
    assert rec["status"] == DECIDED
    cached = temp_db.get_decision("T001")
    assert cached["action"] == "reply"


def test_state_lifecycle(temp_db):
    temp_db.ensure_ticket("T002")
    assert temp_db.get_ticket("T002")["status"] == PENDING

    temp_db.store_decision("T002", {"action": "close"})
    assert temp_db.get_ticket("T002")["status"] == DECIDED

    temp_db.mark_sending("T002")
    assert temp_db.get_ticket("T002")["status"] == SENDING

    temp_db.mark_sent("T002")
    assert temp_db.get_ticket("T002")["status"] == SENT


def test_cached_decision_lookup(temp_db):
    temp_db.ensure_ticket("T003")
    temp_db.store_decision(
        "T003",
        {"action": "reply", "category": "question"},
        content_hash="abc12345",
        prompt_version="v1",
        model="flash-model",
    )

    cached = temp_db.get_cached_decision("abc12345", "v1", "flash-model")
    assert cached is not None
    assert cached["action"] == "reply"

    # Different prompt version returns None
    assert temp_db.get_cached_decision("abc12345", "v2", "flash-model") is None


def test_clear_all_resets_table(temp_db):
    temp_db.ensure_ticket("T004")
    temp_db.ensure_ticket("T005")
    assert len(temp_db.all_tickets()) == 2

    temp_db.clear_all()
    assert len(temp_db.all_tickets()) == 0
