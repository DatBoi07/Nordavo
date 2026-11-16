"""Roosterly helpdesk API client with retry, rate limiting, and typed errors."""
from __future__ import annotations

import os
import time
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class HelpdeskError(Exception):
    """Base error for helpdesk API calls."""
    def __init__(self, status_code: int, body: dict | None = None, message: str = ""):
        self.status_code = status_code
        self.body = body or {}
        super().__init__(message or f"Helpdesk API error {status_code}: {self.body}")


class AlreadyHandled(HelpdeskError):
    """409: ticket was already handled — never retry."""
    pass


class ValidationFailed(HelpdeskError):
    """422: payload validation failed."""
    @property
    def problems(self) -> list[str]:
        return self.body.get("problems", [])


class Unauthorized(HelpdeskError):
    """401: bad or missing key."""
    pass


class TransientError(HelpdeskError):
    """503 or network error — safe to retry."""
    pass


class RateLimited(HelpdeskError):
    """429: too many requests."""
    def __init__(self, retry_after: float, **kwargs):
        self.retry_after = retry_after
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Rate limiter (token bucket, 4 req/s — under the 5 limit)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple token-bucket limiter: at most `rate` calls per second."""

    def __init__(self, rate: float = 4.0):
        self._min_interval = 1.0 / rate
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class HelpdeskClient:
    """HTTP client for the Roosterly helpdesk API."""

    MAX_RETRIES = 6
    BACKOFF_BASE = 0.5  # seconds: 0.5, 1, 2, 4, 8, 16

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        email: str | None = None,
        name: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.base_url = (base_url or os.environ["HELPDESK_BASE_URL"]).rstrip("/")
        self._api_key = api_key or os.environ["HELPDESK_API_KEY"]
        self._email = email or os.environ["HELPDESK_EMAIL"]
        self._name = name or os.environ.get("HELPDESK_NAME", "")
        self._limiter = _RateLimiter(rate=4.0)
        self._client = client or httpx.Client(timeout=30.0)

    # -- internal helpers --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Candidate-Email": self._email,
        }
        if self._name:
            h["X-Candidate-Name"] = self._name
        return h

    def _request(
        self, method: str, path: str, json: dict | None = None
    ) -> dict[str, Any]:
        """Make a request with rate limiting, retries (503), and 429 handling."""
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            self._limiter.wait()
            try:
                if method == "GET":
                    resp = self._client.get(url, headers=self._headers())
                else:
                    resp = self._client.post(
                        url,
                        headers={**self._headers(), "Content-Type": "application/json"},
                        json=json or {},
                    )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_exc = TransientError(
                    status_code=0, message=f"Network error: {exc}"
                )
                backoff = self.BACKOFF_BASE * (2 ** attempt)
                logger.warning("Network error on %s %s (attempt %d), retrying in %.1fs", method, path, attempt + 1, backoff)
                time.sleep(backoff)
                continue

            status = resp.status_code

            if 200 <= status < 300:
                return resp.json()

            body = {}
            try:
                body = resp.json()
            except Exception:
                pass

            if status == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                logger.warning("429 rate limited, sleeping %.1fs", retry_after)
                time.sleep(retry_after)
                continue  # don't count as a failure attempt

            if status == 503:
                backoff = self.BACKOFF_BASE * (2 ** attempt)
                logger.warning("503 on %s %s (attempt %d), retrying in %.1fs", method, path, attempt + 1, backoff)
                time.sleep(backoff)
                last_exc = TransientError(status_code=503, body=body, message="503 Service Unavailable")
                continue

            if status == 409:
                raise AlreadyHandled(status_code=409, body=body, message="Ticket already handled")

            if status == 422:
                raise ValidationFailed(status_code=422, body=body, message=f"Validation failed: {body.get('problems', [])}")

            if status == 401:
                raise Unauthorized(status_code=401, body=body, message="Unauthorized")

            # 400, 404, or anything else — don't retry
            raise HelpdeskError(status_code=status, body=body, message=f"HTTP {status}: {body}")

        # exhausted retries
        raise last_exc or TransientError(status_code=0, message="Max retries exhausted")

    # -- public API --------------------------------------------------------

    def status(self) -> dict:
        return self._request("GET", "/status")

    def examples(self) -> list[dict]:
        data = self._request("GET", "/examples")
        return data.get("examples", data if isinstance(data, list) else [])

    def get_tickets(self, status_filter: str = "open") -> list[dict]:
        """Fetch all tickets (handles pagination)."""
        tickets: list[dict] = []
        cursor: str | None = None
        while True:
            path = f"/tickets?limit=10&status={status_filter}"
            if cursor:
                path += f"&cursor={cursor}"
            data = self._request("GET", path)
            batch = data.get("tickets", [])
            tickets.extend(batch)
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return tickets

    def get_ticket(self, ticket_id: str) -> dict:
        return self._request("GET", f"/tickets/{ticket_id}")

    def reply(self, ticket_id: str, payload: dict) -> dict:
        return self._request("POST", f"/tickets/{ticket_id}/reply", json=payload)

    def handoff(self, ticket_id: str, payload: dict) -> dict:
        return self._request("POST", f"/tickets/{ticket_id}/handoff", json=payload)

    def close(self, ticket_id: str, payload: dict) -> dict:
        return self._request("POST", f"/tickets/{ticket_id}/close", json=payload)

    def reset(self) -> dict:
        return self._request("POST", "/practice/reset")
