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
    cfg = AIConfig(filter="claude", model="x", candles_in_context=30, on_failure="reject")
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
