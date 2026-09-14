import pytest

from tradebot.strategy.ema_rsi import EmaRsiStrategy, build_strategy
from tradebot.types import Candle

PARAMS = dict(fast=3, slow=5, rsi_period=3, rsi_long_min=55, rsi_short_max=45,
              atr_period=3, atr_stop_mult=1.0, reward_risk=2.0, product="MIS")


def _candles(closes, symbol="X"):
    return [Candle(symbol, 1000 + i * 300, c, c + 0.5, c - 0.5, c, 100) for i, c in enumerate(closes)]


def _run(strategy, candles):
    return [(c, strategy.on_candle(c)) for c in candles]


def test_warmup_yields_no_signals_and_not_ready():
    s = EmaRsiStrategy(PARAMS)
    out = _run(s, _candles([10, 9, 8, 7]))
    assert all(sig is None for _, sig in out)
    assert not s.is_ready("X")


def test_bullish_cross_with_strong_rsi_gives_one_long():
    s = EmaRsiStrategy(PARAMS)
    closes = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    sigs = [(c, sig) for c, sig in _run(s, _candles(closes)) if sig]
    assert len(sigs) == 1
    c, sig = sigs[0]
    assert sig.direction == "LONG"
    assert sig.symbol == "X" and sig.strategy == "ema_rsi" and sig.product == "MIS"
    assert sig.bar_ts == c.ts
    assert sig.entry_price == c.close
    assert sig.stop_price < sig.entry_price < sig.target_price
    risk = sig.entry_price - sig.stop_price
    assert sig.target_price == pytest.approx(sig.entry_price + 2 * risk, abs=0.05)
    assert s.is_ready("X")


def test_bearish_cross_with_weak_rsi_gives_one_short():
    s = EmaRsiStrategy(PARAMS)
    closes = [20, 21, 22, 23, 24, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16]
    sigs = [sig for _, sig in _run(s, _candles(closes)) if sig]
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.direction == "SHORT"
    assert sig.target_price < sig.entry_price < sig.stop_price


def test_symbols_are_independent():
    s = EmaRsiStrategy(PARAMS)
    closes = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12]
    a = [sig for _, sig in _run(s, _candles(closes, "A")) if sig]
    b = [sig for _, sig in _run(s, _candles(closes, "B")) if sig]
    assert len(a) == 1 and len(b) == 1
    assert a[0].symbol == "A" and b[0].symbol == "B"


def test_recompute_reproduces_state():
    closes = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10]
    live = EmaRsiStrategy(PARAMS)
    _run(live, _candles(closes))
    rebuilt = EmaRsiStrategy(PARAMS)
    rebuilt.recompute("X", _candles(closes))
    assert live.snapshot("X") == pytest.approx(rebuilt.snapshot("X"))
    assert set(live.snapshot("X")) == {"ema_fast", "ema_slow", "rsi", "atr"}


def test_rsi_filter_gates_both_directions():
    long_series = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    s = EmaRsiStrategy({**PARAMS, "rsi_long_min": 101, "rsi_short_max": 45})
    assert not [sig for _, sig in _run(s, _candles(long_series)) if sig]
    short_series = [20, 21, 22, 23, 24, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16]
    s = EmaRsiStrategy({**PARAMS, "rsi_long_min": 55, "rsi_short_max": -1})
    assert not [sig for _, sig in _run(s, _candles(short_series)) if sig]


def test_zero_or_sub_tick_atr_emits_nothing():
    # flat bars (high == low == close) then a bullish cross with a near-zero ATR
    flat = [Candle("X", 1000 + i * 300, 100.0, 100.0, 100.0, 100.0, 1) for i in range(8)]
    rise = [Candle("X", 1000 + (8 + i) * 300, c, c, c, c, 1) for i, c in enumerate([100.01, 100.02, 100.03, 100.04])]
    s = EmaRsiStrategy(PARAMS)
    assert not [sig for _, sig in _run(s, flat + rise) if sig]


def test_stop_and_target_are_on_the_correct_side_after_rounding():
    s = EmaRsiStrategy({**PARAMS, "atr_stop_mult": 0.3})
    closes = [10.03, 9.03, 8.03, 7.03, 6.03, 5.03, 6.03, 7.03, 8.03, 9.03, 10.03, 11.03, 12.03]
    sigs = [sig for _, sig in _run(s, _candles(closes)) if sig]
    assert sigs and all(sig.stop_price < sig.entry_price < sig.target_price for sig in sigs)
    for sig in sigs:
        assert round(sig.stop_price * 20) == pytest.approx(sig.stop_price * 20)  # on the 0.05 grid


@pytest.mark.parametrize("bad", [
    {"fast": 5, "slow": 3}, {"reward_risk": 0}, {"reward_risk": -1}, {"atr_stop_mult": 0},
    {"rsi_long_min": 45, "rsi_short_max": 55}, {"product": "NRML"}, {"fast": 2.5}, {"min_stop_pct": 0},
])
def test_invalid_params_rejected(bad):
    with pytest.raises(ValueError):
        EmaRsiStrategy({**PARAMS, **bad})


def test_build_strategy_registry():
    assert isinstance(build_strategy("ema_rsi", PARAMS), EmaRsiStrategy)
    with pytest.raises(ValueError):
        build_strategy("nope", PARAMS)


def test_reset_clears_symbol_state_only():
    s = EmaRsiStrategy(PARAMS)
    _run(s, _candles([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10], "A"))
    _run(s, _candles([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10], "B"))
    s.reset("A")
    assert not s.is_ready("A") and s.is_ready("B")


def test_recompute_rejects_wrong_symbol_candles():
    s = EmaRsiStrategy(PARAMS)
    with pytest.raises(ValueError):
        s.recompute("A", _candles([1, 2, 3], "B"))


def test_strategy_exception_is_not_swallowed():
    s = EmaRsiStrategy(PARAMS)
    with pytest.raises(AttributeError):
        s.on_candle(None)
