"""Thin wrapper over the Anthropic SDK: one structured-output request, typed failure, usage numbers.
Nothing else in the project imports `anthropic`; every SDK failure leaves here as ClaudeReviewError."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import anthropic


class ClaudeReviewError(RuntimeError):
    """The review could not produce a usable decision set. The filter applies ai.on_failure."""


@dataclass(frozen=True)
class ReviewResponse:
    data: dict
    latency_ms: int
    input_tokens: int          # uncached input tokens
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    request_id: Optional[str]


@contextmanager
def _sdk_errors():
    """Map every SDK exception class to ClaudeReviewError. Most specific first: RateLimitError is an
    APIStatusError; APITimeoutError is an APIConnectionError; validation errors are bare APIError."""
    try:
        yield
    except anthropic.RateLimitError as e:
        raise ClaudeReviewError(f"rate limited after retries: {e}") from e
    except anthropic.APIStatusError as e:
        raise ClaudeReviewError(f"API error {getattr(e, 'status_code', '?')}: {getattr(e, 'message', e)}") from e
    except anthropic.APIConnectionError as e:
        raise ClaudeReviewError(f"connection error: {e}") from e
    except anthropic.APIError as e:
        raise ClaudeReviewError(f"SDK error: {type(e).__name__}: {e}") from e


class ClaudeClient:
    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int, timeout_sec: int):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set in .env to use the Claude filter")
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # The SDK retries 429/5xx/connection errors twice with backoff on its own.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout_sec), max_retries=2)

    @staticmethod
    def _system_blocks(system: str) -> list:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def review(self, system: str, user: str, schema: dict) -> ReviewResponse:
        t0 = time.monotonic()
        with _sdk_errors():
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._system_blocks(system),
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise ClaudeReviewError("refusal: the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise ClaudeReviewError("max_tokens: response truncated; raise ai.max_tokens")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if not text:
            raise ClaudeReviewError("no text block in response")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ClaudeReviewError(f"invalid JSON in response: {e}") from e
        usage = resp.usage
        return ReviewResponse(
            data=data,
            latency_ms=latency_ms,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            request_id=getattr(resp, "_request_id", None),
        )

    def count_tokens(self, system: str, user: str, schema: Optional[dict] = None) -> int:
        """Exact input token count for a prompt shaped like the real request (the JSON schema is part
        of the input too), for estimate-ai. Free endpoint."""
        kwargs = {"model": self.model, "system": self._system_blocks(system),
                  "messages": [{"role": "user", "content": user}]}
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        with _sdk_errors():
            r = self._client.messages.count_tokens(**kwargs)
        return int(r.input_tokens)
