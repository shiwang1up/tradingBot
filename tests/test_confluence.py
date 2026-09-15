# tests/test_confluence.py
import random

import pytest

from tradebot.strategy.confluence import ConfluenceStrategy
from tradebot.strategy.ema_rsi import build_strategy
from tradebot.types import Candle

P = {"threshold": 3.0, "adx_min": 20, "volume_spike_min": 150, "product": "MIS"}


def _series(n, drift, noise=0.15, seed=7, sym="X", start=100.0, spike_from=None, t0=1000):
    """Random-walk bars with a drift. Bars from `spike_from` for ten bars carry 3x volume, the
    confirmation a breakout needs: a clean trend alone scores trend+macd+momentum-exhaustion = 1.5."""
    rnd = random.Random(seed)
    out, px = [], start
    for i in range(n):
        o = px
        px = px * (1 + drift) + rnd.uniform(-noise, noise)
        h, l = max(o, px) + 0.1, min(o, px) - 0.1
        vol = 3000 if (spike_from is not None and spike_from <= i < spike_from + 10) else 1000 + (i % 7) * 50
        out.append(Candle(sym, t0 + i * 300, round(o, 2), round(h, 2), round(l, 2), round(px, 2), vol))
    return out


def _oscillation(n, amp=0.8, period=0.7, sym="X"):
    import math
    return [Candle(sym, 1000 + i * 300, 100 + amp * math.sin(i / period), 100 + amp * math.sin(i / period) + 0.2,
                   100 + amp * math.sin(i / period) - 0.2, 100 + amp * math.sin((i + 0.5) / period), 1000) for i in range(n)]


def _signals(strategy, candles):
    return [s for s in (strategy.on_candle(c) for c in candles) if s is not None]


def test_warmup_then_trending_series_gives_one_long_and_no_repeats():
    s = ConfluenceStrategy(P)
    flat = _series(110, 0.0, noise=0.05)
    rise = _series(80, 0.002, noise=0.05, seed=3, start=flat[-1].close, spike_from=0, t0=flat[-1].ts + 300)
    assert _signals(s, flat) == []
    sigs = _signals(s, rise)
    assert len(sigs) == 1 and sigs[0].direction == "LONG"
    sig = sigs[0]
    assert sig.stop_price < sig.entry_price < sig.target_price and sig.strategy == "confluence"
    assert s.is_ready("X") and "score" in s.snapshot("X")


def test_falling_series_gives_one_short():
    s = ConfluenceStrategy(P)
    flat = _series(110, 0.0, noise=0.05)
    fall = _series(80, -0.002, noise=0.05, seed=5, start=flat[-1].close, spike_from=0, t0=flat[-1].ts + 300)
    _signals(s, flat)
    sigs = _signals(s, fall)
    assert len(sigs) == 1 and sigs[0].direction == "SHORT"
    assert sigs[0].target_price < sigs[0].entry_price < sigs[0].stop_price


def test_oscillation_gives_nothing_and_adx_gate_blocks():
    s = ConfluenceStrategy(P)
    assert _signals(s, _oscillation(220)) == []
    assert s.snapshot("X")["adx"] < 20  # a tight oscillation has no trend
    gated = ConfluenceStrategy({**P, "adx_min": 101})
    flat = _series(110, 0.0, noise=0.05)
    rise = _series(80, 0.002, noise=0.05, seed=3, start=flat[-1].close, spike_from=0, t0=flat[-1].ts + 300)
    assert _signals(gated, flat + rise) == []
    assert gated.is_ready("X")  # gated, not unready: the score is still computed


def test_a_trend_without_volume_confirmation_does_not_enter():
    s = ConfluenceStrategy(P)
    flat = _series(110, 0.0, noise=0.05)
    rise = _series(80, 0.002, noise=0.05, seed=3, start=flat[-1].close, t0=flat[-1].ts + 300)  # no spike
    scores = []
    for c in flat + rise:
        assert s.on_candle(c) is None
        if s.is_ready("X"):
            scores.append(s.snapshot("X")["score"])
    assert max(scores) == pytest.approx(1.5)  # trend + macd + momentum - exhaustion, never the 3.0 threshold


def test_votes_and_exhaustion_direction():
    s = ConfluenceStrategy(P)
    snap = {"trend": 1, "macd_hist": 0.5, "macd_turned": 1, "mom_60": 1.0, "mom_5": 0.2, "mom_1": 0.1,
            "breakout": 1, "breakdown": 0, "volume_spike_pct": 200.0, "engulfing": 1, "double_top": 0,
            "double_bottom": 0, "rsi": 50.0, "pct_b": 0.5}
    v = s.votes(snap)
    assert v == {"trend": 1.0, "macd": 1.0, "momentum": 1.0, "breakout": 1.5, "pattern": 1.0, "exhaustion": 0.0}
    assert s.score(snap) == 5.5
    hot = {**snap, "rsi": 75.0, "pct_b": 0.9}
    assert s.votes(hot)["exhaustion"] == -1.5          # overbought counts against the long
    cold = {**snap, "rsi": 25.0, "pct_b": 0.1}
    assert s.votes(cold)["exhaustion"] == 1.5          # oversold counts against a short
    no_vol = {**snap, "volume_spike_pct": 90.0}
    assert s.votes(no_vol)["breakout"] == 0.0          # a break without volume is a fake break
    disagree = {**snap, "macd_hist": -0.5, "macd_turned": 0}
    assert s.votes(disagree)["macd"] == 0.0            # histogram against the trend and not turning: ignored
    nulls = {k: None for k in snap}
    assert s.score({**nulls, "trend": 0}) == 0.0


@pytest.mark.parametrize("bad", [
    {"threshold": 0}, {"weights": {"exhaustion": 1.0}}, {"weights": {"volume": 1.0}}, {"product": "NRML"},
    {"rsi_oversold": 80}, {"pct_b_low": 0.9},
])
def test_invalid_params_rejected(bad):
    with pytest.raises(ValueError):
        ConfluenceStrategy({**P, **bad})


def test_symbols_independent_and_recompute():
    s = ConfluenceStrategy(P)
    a = _series(110, 0.0, noise=0.05, sym="A") + _series(80, 0.002, noise=0.05, seed=3, sym="A", spike_from=0, t0=1000 + 110 * 300)
    b = _series(110, 0.0, noise=0.05, sym="B") + _series(80, -0.002, noise=0.05, seed=5, sym="B", spike_from=0, t0=1000 + 110 * 300)
    sa, sb = _signals(s, a), _signals(s, b)
    assert sa and sb and sa[0].symbol == "A" and sb[0].symbol == "B"
    fresh = ConfluenceStrategy(P)
    fresh.recompute("A", a)
    assert fresh.snapshot("A") == pytest.approx(s.snapshot("A"))


def test_registry_and_config_defaults():
    from tradebot.config import load_config
    cfg = load_config("config.yaml", "/nonexistent.env")
    strat = build_strategy("confluence", cfg.strategy["confluence"])
    assert isinstance(strat, ConfluenceStrategy) and strat.weights["breakout"] == 1.5
    with pytest.raises(ValueError):
        build_strategy("nope", {})
