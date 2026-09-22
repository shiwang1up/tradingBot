"""Unit tests for scripts/swing_screen.py, loaded from its path like the other script tests."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "swing_screen.py"
_spec = importlib.util.spec_from_file_location("swing_screen", SCRIPT)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


def _bars(closes, highs=None, lows=None, opens=None, vols=None):
    """Daily bars from a close series; the other fields default to something inert."""
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    opens = opens or list(closes)
    vols = vols or [1000] * n
    d0 = date(2020, 1, 1)
    return [sw.Bar(d0 + timedelta(days=i), opens[i], highs[i], lows[i], closes[i], vols[i])
            for i in range(n)]


def test_pullback_needs_both_an_uptrend_and_an_oversold_reading():
    """close > SMA200 AND RSI2 < 10. Either alone must not fire, or the setup is just
    'the market went up', which the baseline already accounts for."""
    up = [100.0 + i for i in range(230)]          # steady rise: above SMA200, RSI2 high
    ind = sw.Indicators(_bars(up))
    assert not sw.pullback(ind, 229)
    dipped = up[:]                                 # three sharp down closes at the end
    for k in (227, 228, 229):
        dipped[k] = up[226] * 0.94
    ind2 = sw.Indicators(_bars(dipped))
    assert sw.pullback(ind2, 229)


def test_breakout_needs_a_new_hundred_day_high():
    closes = [100.0] * 220 + [101.0]
    ind = sw.Indicators(_bars(closes))
    assert sw.breakout(ind, 220)
    assert not sw.breakout(ind, 219)


def test_trend_dip_needs_price_between_sma50_and_sma20():
    """Below SMA20 but still above SMA50, in an established uptrend. Below both is a
    breakdown, not a dip, and must not fire."""
    closes = [100.0 + i * 0.5 for i in range(230)]
    ind = sw.Indicators(_bars(closes))
    i = 229
    assert ind.sma20[i] is not None and ind.sma50[i] is not None
    assert not sw.trend_dip(ind, i)                # riding above SMA20
    dipped = closes[:]
    dipped[229] = (ind.sma20[229] + ind.sma50[229]) / 2.0
    assert sw.trend_dip(sw.Indicators(_bars(dipped)), 229)


def test_squeeze_needs_contraction_then_an_expansion_bar():
    """Quiet YESTERDAY, then a break today. The contraction is measured on the prior bar
    because the expansion bar's own range is what lifts ATR -- measuring both on the same
    bar makes a genuine squeeze-then-break disqualify itself."""
    closes = [100.0 + (i % 7) * 3.0 for i in range(200)] + [100.0] * 30
    bars = _bars(closes)
    ind = sw.Indicators(bars)
    assert not sw.squeeze(ind, 229)                # quiet, but no expansion
    closes2 = closes[:]
    closes2[229] = bars[228].high * 1.02           # break out of the quiet range
    ind2 = sw.Indicators(_bars(closes2))
    assert ind2.atrpct[228] <= ind2.atrpct_min100_prev[228], (
        "the fixture must actually be quiet on bar 228, or this proves nothing")
    assert sw.squeeze(ind2, 229)


def test_gap_vol_needs_a_gap_volume_and_a_green_close():
    n = 230
    closes = [100.0] * n
    opens = [100.0] * n
    vols = [1000] * n
    opens[229] = 102.0                              # gap up 2%
    closes[229] = 103.0                             # closes green
    vols[229] = 3000                                # 3x the 20-day average
    assert sw.gap_vol(sw.Indicators(_bars(closes, opens=opens, vols=vols)), 229)
    red = closes[:]
    red[229] = 101.0                                # still above prior close but below its open
    assert not sw.gap_vol(sw.Indicators(_bars(red, opens=opens, vols=vols)), 229)


def test_three_down_needs_three_lower_closes_inside_an_uptrend():
    closes = [100.0 + i for i in range(227)] + [320.0, 319.0, 318.0]
    ind = sw.Indicators(_bars(closes))
    assert sw.three_down(ind, 229)
    assert not sw.three_down(sw.Indicators(_bars([100.0 + i for i in range(230)])), 229)


def test_every_setup_declines_before_warmup():
    """SMA200 needs 200 bars. A setup that fires on bar 10 is reading a None as a number."""
    ind = sw.Indicators(_bars([100.0 + i for i in range(30)]))
    for name, fn in sw.SETUPS:
        if name == "random":
            continue
        assert not fn(ind, 10), "%s fired before warm-up" % name
