"""Thin wrapper over the Anthropic SDK: one structured-output request, typed failure, usage numbers.
Nothing else in the project imports `anthropic`."""
from __future__ import annotations

import json
import time
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


class ClaudeClient:
    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int, timeout_sec: int):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set in .env to use the Claude filter")
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # The SDK retries 429/5xx/connection errors twice with backoff on its own.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout_sec), max_retries=2)

    def review(self, system: str, user: str, schema: dict) -> ReviewResponse:
        t0 = time.monotonic()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.RateLimitError as e:
            raise ClaudeReviewError(f"rate limited after retries: {e}") from e
        except anthropic.APIStatusError as e:
            raise ClaudeReviewError(f"API error {getattr(e, 'status_code', '?')}: {getattr(e, 'message', e)}") from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ClaudeReviewError(f"connection error: {e}") from e
        except anthropic.APIError as e:  # response/webhook validation errors subclass APIError directly
            raise ClaudeReviewError(f"SDK error: {type(e).__name__}: {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise ClaudeReviewError("refusal: the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise ClaudeReviewError("max_tokens: response truncated; raise ai.max_tokens")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
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

    def count_tokens(self, system: str, user: str) -> int:
        """Exact input token count for a prompt, for the estimate-ai command."""
        r = self._client.messages.count_tokens(model=self.model, system=system,
                                               messages=[{"role": "user", "content": user}])
        return int(r.input_tokens)
