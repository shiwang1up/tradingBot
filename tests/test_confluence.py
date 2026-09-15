# tests/test_confluence.py
import random

import pytest

from tradebot.strategy.confluence import ConfluenceStrategy
from tradebot.strategy.ema_rsi import build_strategy, strategy_params
from tradebot.types import Candle

P = {"threshold": 3.0, "adx_min": 20, "volume_spike_min": 150, "product": "MIS"}
SPIKE = 18  # bar of the trending segment from which volume is 3x, once ADX has climbed past 20


def _series(n, drift, noise=0.15, seed=7, sym="X", start=100.0, spike_from=None, t0=1000):
    """Random-walk bars with a drift. Bars from `spike_from` for ten bars carry 3x volume, the
    confirmation a breakout needs: a clean trend alone scores trend+macd+momentum-exhaustion = 1.5.
    Tests warm up on `_oscillation`, whose ADX stays below 20; a trend then needs about 18 bars of
    ADX build-up before the gate opens, so the spike is placed there."""
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
    flat = _oscillation(110)
    rise = _series(80, 0.002, noise=0.05, seed=3, start=flat[-1].close, spike_from=SPIKE, t0=flat[-1].ts + 300)
    assert _signals(s, flat) == []
    sigs = _signals(s, rise)
    assert len(sigs) == 1 and sigs[0].direction == "LONG"
    sig = sigs[0]
    assert sig.stop_price < sig.entry_price < sig.target_price and sig.strategy == "confluence"
    assert s.is_ready("X") and "score" in s.snapshot("X")


def test_falling_series_gives_one_short():
    s = ConfluenceStrategy(P)
    flat = _oscillation(110)
    fall = _series(80, -0.002, noise=0.05, seed=5, start=flat[-1].close, spike_from=SPIKE, t0=flat[-1].ts + 300)
    _signals(s, flat)
    sigs = _signals(s, fall)
    assert len(sigs) == 1 and sigs[0].direction == "SHORT"
    assert sigs[0].target_price < sigs[0].entry_price < sigs[0].stop_price


def test_oscillation_gives_nothing_and_adx_gate_blocks():
    s = ConfluenceStrategy(P)
    assert _signals(s, _oscillation(220)) == []
    assert s.snapshot("X")["adx"] < 20  # a tight oscillation has no trend
    gated = ConfluenceStrategy({**P, "adx_min": 101})
    flat = _oscillation(110)
    rise = _series(80, 0.002, noise=0.05, seed=3, start=flat[-1].close, spike_from=SPIKE, t0=flat[-1].ts + 300)
    assert _signals(gated, flat + rise) == []
    assert gated.is_ready("X")  # gated, not unready: the score is still computed


def test_a_trend_without_volume_confirmation_does_not_enter():
    s = ConfluenceStrategy(P)
    flat = _oscillation(110)
    rise = _series(80, 0.002, noise=0.05, seed=3, start=flat[-1].close, t0=flat[-1].ts + 300)  # no spike
    eligible = []
    for c in flat + rise:
        assert s.on_candle(c) is None
        if s.is_ready("X") and s.snapshot("X")["adx"] >= 20:
            eligible.append(s.snapshot("X")["score"])
    assert eligible and max(eligible) == pytest.approx(1.5)  # trend + macd + momentum - exhaustion, never 3.0


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
    turn_up = {**snap, "macd_hist": -0.5, "macd_turned": 1}
    assert s.votes(turn_up)["macd"] == 1.0             # a turn counts in the turn's direction, not the histogram's sign
    turn_down = {**snap, "macd_hist": 0.5, "macd_turned": -1}
    assert s.votes(turn_down)["macd"] == -1.0
    nulls = {k: None for k in snap}
    assert s.score({**nulls, "trend": 0}) == 0.0


@pytest.mark.parametrize("bad", [
    {"threshold": 0}, {"weights": {"exhaustion": 1.0}}, {"weights": {"volume": 1.0}}, {"product": "NRML"},
    {"rsi_oversold": 80}, {"pct_b_low": 0.9}, {"weights": {"trend": -1.0}}, {"weights": {"macd": "1.0"}},
    {"adx_min": -1}, {"volume_spike_min": -5},
])
def test_invalid_params_rejected(bad):
    with pytest.raises(ValueError):
        ConfluenceStrategy({**P, **bad})


def test_symbols_independent_and_recompute():
    s = ConfluenceStrategy(P)
    a = _oscillation(110, sym="A") + _series(80, 0.002, noise=0.05, seed=3, sym="A", spike_from=SPIKE, t0=1000 + 110 * 300)
    b = _oscillation(110, sym="B") + _series(80, -0.002, noise=0.05, seed=5, sym="B", spike_from=SPIKE, t0=1000 + 110 * 300)
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


def test_first_ready_bar_never_fires_and_crossing_is_required():
    s = ConfluenceStrategy(P)
    assert s._direction("X", 5.0, 50.0) is None      # first ready bar: nothing to have crossed from
    assert s._direction("X", 5.0, 50.0) is None      # still beyond the threshold, no crossing
    assert s._direction("X", 2.0, 50.0) is None      # back inside
    assert s._direction("X", 3.0, 50.0) == "LONG"    # crossed
    assert s._direction("X", 3.5, 50.0) is None      # no repeat
    assert s._direction("X", -3.0, 50.0) == "SHORT"  # straight through to the other side counts as a crossing
    s.reset("X")
    assert s._direction("X", -4.0, 50.0) is None     # a reset (new day after a disable) starts over


def test_adx_gate_does_not_consume_the_crossing():
    s = ConfluenceStrategy(P)
    assert s._direction("X", 1.0, 30.0) is None
    assert s._direction("X", 3.5, 15.0) is None      # crossed while gated: no trade, but not consumed either
    assert s._direction("X", 3.5, 25.0) == "LONG"    # the first trending bar fires
    assert s._direction("X", 3.5, 25.0) is None
    assert s._direction("X", 3.5, None) is None      # ADX not ready counts as gated
    assert s._direction("X", 3.5, 25.0) == "LONG"    # re-arming after a gate is intended: a fresh trend confirmation


def test_weights_coerced_to_float_and_indicators_merged_from_shared_section():
    s = ConfluenceStrategy({**P, "weights": {"trend": 2}})
    assert s.weights["trend"] == 2.0 and isinstance(s.weights["trend"], float)
    cfg = {"confluence": dict(P), "indicators": {"levels_period": 30}, "ema_rsi": {"x": 1}}
    params = strategy_params(cfg, "confluence")
    assert params["indicators"] == {"levels_period": 30} and "indicators" not in cfg["confluence"]
    own = strategy_params({"confluence": {**P, "indicators": {"levels_period": 10}}, "indicators": {"levels_period": 30}}, "confluence")
    assert own["indicators"] == {"levels_period": 10}  # a strategy's own block wins
    with pytest.raises(ValueError):
        strategy_params(cfg, "nope")
    short, default = ConfluenceStrategy(params), ConfluenceStrategy(P)
    for c in _oscillation(80):
        short.on_candle(c)
        default.on_candle(c)
    assert short.is_ready("X") and not default.is_ready("X")  # 30-bar levels: ready before the default 100
