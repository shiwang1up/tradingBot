# tests/test_ta.py
import random
import statistics

import pytest

from tradebot.strategy.ta import (ADX, MACD, Bollinger, IndicatorSet, Momentum, Patterns, RollingLevels, VolumeSpike,
                                  bar_position)
from tradebot.types import Candle


def _c(o, h, l, c, v=100, ts=0, sym="X"):
    return Candle(sym, ts, o, h, l, c, v)


def _ema_ref(xs, n):
    out, val, seed = [], None, []
    k = 2 / (n + 1)
    for x in xs:
        if val is None:
            seed.append(x)
            if len(seed) == n:
                val = sum(seed) / n
        else:
            val = x * k + val * (1 - k)
        out.append(val)
    return out


def test_macd_matches_reference_loop():
    rnd = random.Random(3)
    xs = [100 + rnd.uniform(-2, 2) for _ in range(120)]
    m = MACD(12, 26, 9)
    got = [m.update(x) for x in xs]
    fast, slow = _ema_ref(xs, 12), _ema_ref(xs, 26)
    macd = [None if (f is None or s is None) else f - s for f, s in zip(fast, slow)]
    sig = _ema_ref([x for x in macd if x is not None], 9)
    sig = [None] * (len(macd) - len(sig)) + sig
    hist = [None if (a is None or b is None) else a - b for a, b in zip(macd, sig)]
    assert all((g is None and h is None) or g == pytest.approx(h) for g, h in zip(got, hist))
    assert m.ready and m.macd is not None


def test_macd_hist_turned_flags_direction_changes():
    m = MACD(2, 3, 2)
    turns = []
    for x in [1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1, 2, 3, 4, 5, 6, 7]:
        m.update(x)
        turns.append(m.hist_turned)
    assert -1 in turns and 1 in turns
    assert turns.index(-1) < turns.index(1)


def test_macd_rejects_bad_periods():
    with pytest.raises(ValueError):
        MACD(26, 12, 9)


def test_adx_trending_high_flat_zero():
    a = ADX(5)
    for i in range(40):
        a.update(_c(100 + i, 101 + i, 99 + i, 100.5 + i))
    assert a.ready and a.value > 40 and a.di_plus > a.di_minus
    flat = ADX(5)
    for _ in range(40):
        flat.update(_c(100, 100, 100, 100))
    assert flat.value == 0.0
    chop = ADX(5)
    for i in range(60):
        chop.update(_c(100, 101, 99, 100 + (0.4 if i % 2 else -0.4)))
    assert chop.value < 25


def test_adx_ready_after_two_periods():
    a = ADX(3)
    vals = [a.update(_c(100 + i, 101 + i, 99 + i, 100 + i)) for i in range(8)]
    assert vals[:5] == [None] * 5 and vals[5] is not None  # 1 prev bar + 3 seed + 3 dx seed (overlapping)


def test_bollinger_known_window_and_flat():
    b = Bollinger(20, 2.0)
    for x in range(1, 21):
        v = b.update(float(x))
    xs = list(range(1, 21))
    mean, sd = statistics.mean(xs), statistics.pstdev(xs)
    assert b.mid == pytest.approx(mean) and b.upper == pytest.approx(mean + 2 * sd)
    assert v == pytest.approx((20 - (mean - 2 * sd)) / (4 * sd))
    flat = Bollinger(3)
    for _ in range(3):
        v = flat.update(5.0)
    assert v == 0.5


def test_momentum_and_volume_and_bar_position():
    m = Momentum(1)
    assert m.update(100.0) is None and m.update(101.0) == pytest.approx(1.0)
    m5b = Momentum(5)
    vals = [m5b.update(100.0 + i) for i in range(6)]
    assert vals[:5] == [None] * 5 and vals[5] == pytest.approx(5.0)
    v = VolumeSpike(3)
    assert [v.update(x) for x in (10, 10, 10, 30)] == [None, None, None, 300.0]
    assert bar_position(_c(10, 12, 8, 11)) == 0.75 and bar_position(_c(10, 10, 10, 10)) == 0.5


def test_rolling_levels_breakout_and_breakdown():
    lv = RollingLevels(3)
    bars = [_c(10, 11, 9, 10), _c(10, 12, 9.5, 11), _c(11, 11.5, 10, 10.5), _c(10.5, 13, 10.4, 12.5), _c(12, 12.2, 8, 8.5)]
    seen = []
    for b in bars:
        lv.update(b)
        seen.append((lv.support, lv.resistance, lv.breakout, lv.breakdown))
    assert seen[2] == (None, None, 0, 0)          # not ready until 3 prior bars exist
    assert seen[3] == (9.0, 12.0, 1, 0)           # close 12.5 > prior max high 12
    assert seen[4] == (9.5, 13.0, 0, 1)           # close 8.5 < prior min low 9.5


def test_engulfing_detection():
    p = Patterns()
    p.update(_c(10.0, 10.2, 8.9, 9.0))            # bearish bar
    p.update(_c(8.9, 10.5, 8.8, 10.2))            # bullish bar engulfing the previous body
    assert p.engulfing == 1
    p.update(_c(10.2, 10.4, 10.0, 10.3))          # small bullish
    p.update(_c(10.4, 10.5, 9.9, 10.0))           # bearish engulfing
    assert p.engulfing == -1
    p.update(_c(10.0, 10.1, 9.9, 10.05))
    assert p.engulfing == 0


def _peaks(levels):
    """Bars whose highs trace the given level path; body inside the range."""
    return [_c(lv - 0.2, lv, lv - 0.6, lv - 0.3) for lv in levels]


def test_double_top_and_bottom():
    p = Patterns(wing=2, tolerance_pct=0.5, min_gap=4)
    path = [100, 101, 102, 101, 100, 99, 98, 99, 100, 101, 102.1, 101, 100, 99]
    flags = []
    for b in _peaks(path):
        p.update(b)
        flags.append(p.double_top)
    assert flags[-1] == 0  # second peak confirmed but the neck (98 low) not broken yet
    p.update(_c(99, 99.2, 96.5, 96.6))  # close below the trough between the peaks
    assert p.double_top == 1 and p.double_bottom == 0
    q = Patterns(wing=2, tolerance_pct=0.5, min_gap=4)
    inv = [200 - x for x in path]
    for lv in inv:
        q.update(_c(lv + 0.2, lv + 0.6, lv, lv + 0.3))
    q.update(_c(101, 104, 100.9, 103.8))  # close above the peak between the troughs
    assert q.double_bottom == 1 and q.double_top == 0


def test_indicator_set_snapshot_keys_and_readiness():
    s = IndicatorSet()
    rnd = random.Random(1)
    px = 100.0
    for i in range(130):
        px += rnd.uniform(-0.5, 0.6)
        s.update(_c(px, px + 0.4, px - 0.4, px + rnd.uniform(-0.2, 0.2), 1000 + i, ts=i * 300))
        if i < 99:
            assert not s.ready
    assert s.ready
    snap = s.snapshot()
    expected = {"ema20", "ema50", "trend", "macd_hist", "macd_turned", "mom_1", "mom_5", "mom_60", "adx", "di_plus",
                "di_minus", "rsi", "pct_b", "bb_upper", "bb_lower", "atr", "volume_spike_pct", "bar_position", "support",
                "resistance", "breakout", "breakdown", "engulfing", "double_top", "double_bottom"}
    assert set(snap) == expected
    assert all(v is not None for v in snap.values())
    for k in ("trend", "macd_turned", "breakout", "breakdown", "engulfing", "double_top", "double_bottom"):
        assert isinstance(snap[k], int)
    assert snap["trend"] in (-1, 1) and 0 <= snap["bar_position"] <= 1
    assert IndicatorSet().snapshot() == {}
