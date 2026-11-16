"""Unit tests for HelpdeskClient error handling, retries, and rate limiting."""
from __future__ import annotations

import json
import time
import httpx
import pytest

from agent.helpdesk import (
    AlreadyHandled,
    HelpdeskClient,
    RateLimited,
    TransientError,
    ValidationFailed,
)


def test_503_retries_and_succeeds():
    """Verify that a 503 triggers retry and succeeds on subsequent attempt."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "service_unavailable"})
        return httpx.Response(200, json={"status": "ok", "open_tickets": 5})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hd = HelpdeskClient(
        base_url="https://mock.helpdesk/api",
        api_key="test_key",
        email="test@test.com",
        client=client,
    )
    hd.BACKOFF_BASE = 0.01  # speed up test

    res = hd.status()
    assert res["status"] == "ok"
    assert attempts == 2


def test_429_honours_retry_after():
    """Verify that 429 sleeps Retry-After and retries without consuming max retry count."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.05"}, json={"error": "rate_limited"})
        return httpx.Response(200, json={"status": "recovered"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hd = HelpdeskClient(
        base_url="https://mock.helpdesk/api",
        api_key="test_key",
        email="test@test.com",
        client=client,
    )

    res = hd.status()
    assert res["status"] == "recovered"
    assert attempts == 2


def test_409_already_handled_never_retried():
    """Verify that 409 immediately raises AlreadyHandled without retry."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(409, json={"error": "already_handled"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hd = HelpdeskClient(
        base_url="https://mock.helpdesk/api",
        api_key="test_key",
        email="test@test.com",
        client=client,
    )

    with pytest.raises(AlreadyHandled):
        hd.reply("T001", {"language": "nl"})

    assert attempts == 1


def test_422_validation_failed_surfaces_problems():
    """Verify that 422 raises ValidationFailed with the specific problems."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "validation_failed", "problems": ["language required"]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hd = HelpdeskClient(
        base_url="https://mock.helpdesk/api",
        api_key="test_key",
        email="test@test.com",
        client=client,
    )

    with pytest.raises(ValidationFailed) as exc_info:
        hd.close("T001", {})

    assert "language required" in exc_info.value.problems


def test_pagination_follows_cursors():
    """Verify pagination collects all tickets across multiple pages."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cursor=C1" in url:
            return httpx.Response(200, json={"tickets": [{"id": "T003"}], "next_cursor": None})
        return httpx.Response(200, json={"tickets": [{"id": "T001"}, {"id": "T002"}], "next_cursor": "C1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hd = HelpdeskClient(
        base_url="https://mock.helpdesk/api",
        api_key="test_key",
        email="test@test.com",
        client=client,
    )

    tickets = hd.get_tickets()
    assert len(tickets) == 3
    assert [t["id"] for t in tickets] == ["T001", "T002", "T003"]
