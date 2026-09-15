import pytest

from tradebot.ai.filter import StubFilter, build_filter
from tradebot.config import AIConfig
from tradebot.types import Candidate, Signal


def _cand(sym):
    sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", 1)
    return Candidate(sig, 10, {"rsi": 60.0}, ())


def test_stub_approves_everything_in_order():
    f = StubFilter()
    out = f.review([_cand("A"), _cand("B")])
    assert [d.signal.symbol for d in out] == ["A", "B"]
    assert all(d.approved and d.filter_kind == "stub" and d.confidence == 1.0 for d in out)


def test_stub_handles_empty():
    assert StubFilter().review([]) == []


def test_build_filter_stub():
    cfg = AIConfig(filter="stub", model="x", candles_in_context=30, on_failure="reject")
    assert isinstance(build_filter(cfg, api_key=""), StubFilter)


def test_build_filter_unknown_raises():
    cfg = AIConfig(filter="nope", model="x", candles_in_context=30, on_failure="reject")
    with pytest.raises(ValueError):
        build_filter(cfg, api_key="")


def check_filter_contract(flt, candidates):
    """Shared contract every AIFilter implementation must satisfy (reuse in Plan 2)."""
    out = flt.review(candidates)
    assert len(out) == len(candidates)
    for d, c in zip(out, candidates):
        assert d.signal is c.signal
        assert 0.0 <= d.confidence <= 1.0 and d.filter_kind == flt.kind and isinstance(d.approved, bool)


def test_stub_satisfies_filter_contract():
    check_filter_contract(StubFilter(), [_cand("A"), _cand("B"), _cand("C")])


# -- Claude filters against a fake client --------------------------------------------------------
from tradebot.ai.cache import AICache
from tradebot.ai.claude_client import ClaudeReviewError, ReviewResponse
from tradebot.ai.filter import CachedClaudeFilter, ClaudeFilter
from tradebot.types import Candle


def _rich_cand(sym, bar_ts=1789357500):
    sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", bar_ts)
    candles = (Candle(sym, bar_ts - 300, 99.5, 100.2, 99.4, 100.0, 500),)
    return Candidate(sig, 10, {"rsi": 60.0, "ema_fast": 100.1, "ema_slow": 99.9, "atr": 1.0}, candles)


class FakeClaude:
    def __init__(self, decisions=None, error=None):
        self.decisions, self.error, self.calls = decisions, error, []

    def review(self, system, user, schema):
        self.calls.append(user)
        if self.error:
            raise self.error
        return ReviewResponse({"decisions": self.decisions}, 700, 1500, 90, 1000, 250, "req_x")


def _ai(**over):
    base = dict(filter="claude", model="claude-opus-5", candles_in_context=30, on_failure="reject")
    base.update(over)
    return AIConfig(**base)


def test_claude_filter_maps_decisions_by_index_and_symbol():
    fake = FakeClaude([
        {"index": 1, "symbol": "B", "approve": False, "confidence": 0.2, "reason": "chop"},
        {"index": 0, "symbol": "A", "approve": True, "confidence": 0.71, "reason": "clean break"},
    ])
    f = ClaudeFilter(_ai(), fake)
    out = f.review([_rich_cand("A"), _rich_cand("B")])
    check_filter_contract(f, [_rich_cand("A"), _rich_cand("B")])
    assert [d.approved for d in out] == [True, False]
    assert out[0].reason == "clean break" and out[0].confidence == 0.71 and out[0].filter_kind == "claude"
    assert (out[0].input_tokens, out[0].output_tokens, out[0].cache_read_tokens, out[0].cache_write_tokens) == (1500, 90, 1000, 250)
    assert (out[1].input_tokens, out[1].output_tokens) == (0, 0)  # usage attributed once per batch
    assert out[0].latency_ms == 700 and out[0].failure is None


def test_claude_filter_missing_or_mismatched_decision_uses_failure_policy():
    fake = FakeClaude([{"index": 0, "symbol": "WRONG", "approve": True, "confidence": 0.9, "reason": "x"}])
    out = ClaudeFilter(_ai(on_failure="reject"), fake).review([_rich_cand("A"), _rich_cand("B")])
    assert [d.approved for d in out] == [False, False]
    assert all(d.failure and d.filter_kind == "claude" for d in out)
    out2 = ClaudeFilter(_ai(on_failure="pass_through"), fake).review([_rich_cand("A")])
    assert out2[0].approved and out2[0].failure


def test_claude_filter_transport_failure_policy():
    fake = FakeClaude(error=ClaudeReviewError("rate limited"))
    out = ClaudeFilter(_ai(on_failure="reject"), fake).review([_rich_cand("A")])
    assert not out[0].approved and out[0].failure == "rate limited" and out[0].reason.startswith("ai_failure")
    out = ClaudeFilter(_ai(on_failure="pass_through"), fake).review([_rich_cand("A")])
    assert out[0].approved and out[0].failure == "rate limited"


def test_claude_filter_clamps_confidence_and_truncates_reason():
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 7.5, "reason": "r" * 500}])
    d = ClaudeFilter(_ai(), fake).review([_rich_cand("A")])[0]
    assert d.confidence == 1.0 and len(d.reason) == 200


def test_claude_filter_spend_guard():
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}])
    f = ClaudeFilter(_ai(max_calls_per_run=1), fake)
    assert f.review([_rich_cand("A")])[0].approved
    d = f.review([_rich_cand("A", bar_ts=1789357800)])[0]
    assert not d.approved and "max_calls_per_run" in d.failure and len(fake.calls) == 1


def test_consecutive_failures_trip_the_circuit_breaker():
    fake = FakeClaude(error=ClaudeReviewError("down"))
    f = ClaudeFilter(_ai(max_consecutive_failures=3), fake)
    f.review([_rich_cand("A")])
    f.review([_rich_cand("A", bar_ts=1789357800)])
    with pytest.raises(RuntimeError, match="3 consecutive failures"):
        f.review([_rich_cand("A", bar_ts=1789358100)])


def test_cached_filter_hits_skip_the_call_and_misses_populate(repo):
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": False, "confidence": 0.3, "reason": "flat"}])
    f = CachedClaudeFilter(_ai(filter="claude_cached"), fake, AICache(repo))
    first = f.review([_rich_cand("A")])
    second = f.review([_rich_cand("A")])
    assert len(fake.calls) == 1
    assert first[0].approved is False and second[0].approved is False
    assert second[0].reason == "flat" and second[0].filter_kind == "claude_cached"
    assert second[0].input_tokens == 0 and second[0].latency_ms == 0  # cache hit costs nothing
    check_filter_contract(f, [_rich_cand("A")])


def test_cached_filter_partial_hit_calls_for_the_whole_batch(repo):
    fake = FakeClaude([
        {"index": 0, "symbol": "A", "approve": True, "confidence": 0.6, "reason": "a"},
        {"index": 1, "symbol": "B", "approve": True, "confidence": 0.6, "reason": "b"},
    ])
    f = CachedClaudeFilter(_ai(filter="claude_cached"), fake, AICache(repo))
    f.review([_rich_cand("A")])          # caches A alone under the single-candidate prompt
    f.review([_rich_cand("A"), _rich_cand("B")])  # different prompt bytes: call again, cache both
    assert len(fake.calls) == 2
    f.review([_rich_cand("A"), _rich_cand("B")])
    assert len(fake.calls) == 2


def test_cached_filter_does_not_cache_failures(repo):
    fake = FakeClaude(error=ClaudeReviewError("boom"))
    f = CachedClaudeFilter(_ai(filter="claude_cached"), fake, AICache(repo))
    f.review([_rich_cand("A")])
    fake.error = None
    fake.decisions = [{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}]
    assert f.review([_rich_cand("A")])[0].approved and len(fake.calls) == 2


def test_claude_filter_passes_session_facts_when_given_a_clock():
    import json
    from tradebot.config import SessionConfig
    from tradebot.engine.clock import SessionClock
    clock = SessionClock(SessionConfig("09:15", "15:30", "15:10", "14:45", ()), 5)
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}])
    ClaudeFilter(_ai(), fake, clock=clock).review([_rich_cand("A", bar_ts=1789357500)])  # 09:15 IST
    sent = json.loads(fake.calls[0])
    assert sent["session"] == {"square_off": "15:10", "entry_cutoff": "14:45", "close": "15:30", "bars_left": 70}
    fake2 = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}])
    ClaudeFilter(_ai(), fake2).review([_rich_cand("A")])
    assert json.loads(fake2.calls[0])["session"] is None


def test_build_filter_claude_variants_need_key_and_repo(repo):
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_filter(_ai(filter="claude"), api_key="")
    assert isinstance(build_filter(_ai(filter="claude"), api_key="k"), ClaudeFilter)
    with pytest.raises(ValueError, match="repo"):
        build_filter(_ai(filter="claude_cached"), api_key="k")
    assert isinstance(build_filter(_ai(filter="claude_cached"), api_key="k", repo=repo), CachedClaudeFilter)
