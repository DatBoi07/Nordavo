"""Gemini LLM wrapper with structured output, retries, and token accounting."""
from __future__ import annotations

import os
import time
import json
import logging
from typing import Any, Type

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v2"


class LLMResult:
    """Container for an LLM call result with token counts."""
    def __init__(self, data: Any, tokens_in: int, tokens_out: int, model: str, thinking_tokens: int = 0):
        self.data = data
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.thinking_tokens = thinking_tokens
        self.model = model


class LLMClient:
    """Wrapper around the Gemini API with retries and accounting."""

    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0

    def __init__(self):
        self._api_key = os.environ["GEMINI_API_KEY"]
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._verifier_model = os.environ.get("GEMINI_VERIFIER_MODEL", "gemini-3.5-flash-lite")
        self._client = genai.Client(api_key=self._api_key)

        # Token accounting
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_thinking_tokens = 0
        self.call_count = 0

        # Pricing (per million tokens)
        self._price_in = float(os.environ.get("PRICE_IN_PER_MTOK", "0.15"))
        self._price_out = float(os.environ.get("PRICE_OUT_PER_MTOK", "0.60"))
        self._usd_to_eur = float(os.environ.get("USD_TO_EUR", "0.92"))

    def _call(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[BaseModel] | None = None,
        model: str | None = None,
        temperature: float = 0,
    ) -> LLMResult:
        """Make a Gemini API call with retries."""
        model = model or self._model
        last_exc: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                config_kwargs: dict[str, Any] = {
                    "system_instruction": system_prompt,
                    "temperature": temperature,
                }
                if schema is not None:
                    config_kwargs["response_mime_type"] = "application/json"
                    config_kwargs["response_schema"] = schema

                # Control thinking budget to save cost
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=256)

                config = types.GenerateContentConfig(**config_kwargs)

                response = self._client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=config,
                )

                # Extract token counts
                usage = response.usage_metadata
                tokens_in = getattr(usage, "prompt_token_count", 0) or 0
                tokens_out = getattr(usage, "candidates_token_count", 0) or 0
                thinking = getattr(usage, "thoughts_token_count", 0) or 0

                self.total_tokens_in += tokens_in
                self.total_tokens_out += tokens_out + thinking
                self.total_thinking_tokens += thinking
                self.call_count += 1

                text = response.text
                if not text or not text.strip():
                    raise ValueError("Empty response from LLM")

                # Parse structured output
                if schema is not None:
                    data = schema.model_validate_json(text)
                else:
                    data = text

                return LLMResult(
                    data=data,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out + thinking,
                    thinking_tokens=thinking,
                    model=model,
                )

            except Exception as e:
                last_exc = e
                error_str = str(e)
                # Retry on transient errors (429, 503, network)
                if any(code in error_str for code in ["429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED"]):
                    backoff = self.BACKOFF_BASE * (2 ** attempt)
                    logger.warning("LLM transient error (attempt %d): %s, retrying in %.1fs", attempt + 1, error_str[:100], backoff)
                    time.sleep(backoff)
                    continue
                # Non-transient: retry with backoff anyway (schema validation, blocked, etc.)
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning("LLM error (attempt %d): %s, retrying", attempt + 1, error_str[:100])
                    time.sleep(self.BACKOFF_BASE)
                    continue
                raise

        raise last_exc or RuntimeError("LLM max retries exhausted")

    def classify(self, system_prompt: str, user_prompt: str, schema: Type[BaseModel]) -> LLMResult:
        """Call A: classify a ticket."""
        return self._call(system_prompt, user_prompt, schema=schema, model=self._model)

    def draft_reply(self, system_prompt: str, user_prompt: str) -> LLMResult:
        """Call B: draft a reply."""
        return self._call(system_prompt, user_prompt, model=self._model)

    def verify(self, system_prompt: str, user_prompt: str, schema: Type[BaseModel]) -> LLMResult:
        """Call C: verify a draft reply (uses verifier model)."""
        return self._call(system_prompt, user_prompt, schema=schema, model=self._verifier_model)

    def cost_so_far_eur(self) -> float:
        """Calculate cost in EUR based on token usage."""
        cost_usd = (
            (self.total_tokens_in / 1_000_000) * self._price_in
            + (self.total_tokens_out / 1_000_000) * self._price_out
        )
        return cost_usd * self._usd_to_eur

    def cost_per_ticket_eur(self) -> float:
        """Average cost per ticket (if call_count > 0)."""
        if self.call_count == 0:
            return 0.0
        # Rough: each ticket has ~2-3 calls
        return self.cost_so_far_eur()

    def usage_summary(self) -> dict:
        return {
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_thinking_tokens": self.total_thinking_tokens,
            "call_count": self.call_count,
            "cost_eur": round(self.cost_so_far_eur(), 6),
        }
