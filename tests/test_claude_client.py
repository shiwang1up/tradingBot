import types

import anthropic
import pytest

from tradebot.ai.claude_client import ClaudeClient, ClaudeReviewError, ReviewResponse


class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Usage:
    input_tokens = 1200
    output_tokens = 80
    cache_read_input_tokens = 900
    cache_creation_input_tokens = 300


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block("thinking"), _Block("text", text)]
        self.stop_reason = stop_reason
        self.usage = _Usage()
        self._request_id = "req_123"


class _Messages:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _client(outcome):
    c = ClaudeClient(api_key="k", model="claude-opus-5", effort="low", max_tokens=2000, timeout_sec=30)
    c._client = types.SimpleNamespace(messages=_Messages(outcome))
    return c


def test_review_sends_structured_output_request_and_parses():
    c = _client(_Resp('{"decisions": [{"index": 0, "symbol": "A", "approve": true, "confidence": 0.7, "reason": "ok"}]}'))
    r = c.review("SYS", "USER", {"type": "object"})
    assert isinstance(r, ReviewResponse)
    assert r.data["decisions"][0]["symbol"] == "A"
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.request_id) == (1200, 80, 900, 300, "req_123")
    assert r.latency_ms >= 0
    kw = c._client.messages.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["max_tokens"] == 2000
    assert kw["system"][0]["text"] == "SYS" and kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert "thinking" not in kw  # Opus 5 runs adaptive thinking by default


def _sdk_error(cls, status=None):
    """Instantiate an SDK exception without its initialiser: the wrapper only dispatches on class."""
    e = cls.__new__(cls)
    e.message = "boom"
    if status is not None:
        e.status_code = status
    return e


@pytest.mark.parametrize("exc", [
    _sdk_error(anthropic.APIConnectionError),
    _sdk_error(anthropic.RateLimitError, 429),
    _sdk_error(anthropic.APIStatusError, 500),
    _sdk_error(anthropic.APIResponseValidationError),
])
def test_transport_errors_become_review_errors(exc):
    with pytest.raises(ClaudeReviewError):
        _client(exc).review("S", "U", {})


def test_refusal_and_truncation_are_review_errors():
    with pytest.raises(ClaudeReviewError, match="refusal"):
        _client(_Resp("{}", stop_reason="refusal")).review("S", "U", {})
    with pytest.raises(ClaudeReviewError, match="max_tokens"):
        _client(_Resp('{"decisions": [', stop_reason="max_tokens")).review("S", "U", {})


def test_invalid_json_is_a_review_error():
    with pytest.raises(ClaudeReviewError, match="JSON"):
        _client(_Resp("not json")).review("S", "U", {})


def test_missing_key_is_rejected_early():
    with pytest.raises(ValueError):
        ClaudeClient(api_key="", model="claude-opus-5", effort="low", max_tokens=10, timeout_sec=1)
