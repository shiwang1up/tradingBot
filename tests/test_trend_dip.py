"""trend_dip: a dip inside a long uptrend, on daily bars, held as delivery.

The series below are built so that each test isolates ONE of the three conditions. Every test
asserts the preconditions it relies on from the strategy's own snapshot, so a test that stops
testing what its name says fails rather than passing for the wrong reason.
"""
import pytest

from tradebot.strategy.ema_rsi import build_strategy
from tradebot.strategy.trend_dip import PRODUCTS, TrendDipStrategy
from tradebot.types import Candle, round_tick_down

PARAMS = dict(sma_fast=20, sma_mid=50, sma_slow=200, atr_period=20, atr_stop_mult=2.0,
              max_hold_bars=60, product="CNC")

UPTREND = list(range(100, 320))          # 220 bars, +1 a bar: SMA20 > SMA50 > SMA200
# 200 bars down from 300, then 60 back up: SMA20 > SMA50 (the rally) but SMA50 < SMA200 (the fall)
V_SHAPE = list(range(300, 100, -1)) + list(range(102, 162))


def _bars(closes, symbol="X", start=1_577_817_000, step=86400):
    """One daily bar per close, stamped 00:00 IST a day apart, as daily candles are stored."""
    return [Candle(symbol, start + i * step, float(c), float(c) + 0.5, float(c) - 0.5, float(c), 1000)
            for i, c in enumerate(closes)]


def _run(closes, params=None):
    s = TrendDipStrategy(params or PARAMS)
    out = [s.on_candle(c) for c in _bars(closes)]
    return s, out


def test_fires_on_a_dip_inside_an_uptrend():
    """SMA50 > SMA200, close below SMA20, close above SMA50."""
    s, out = _run(UPTREND + [304])
    snap = s.snapshot("X")
    assert snap["sma_mid"] > snap["sma_slow"], "precondition: the long trend is up"
    assert 304 < snap["sma_fast"], "precondition: this bar is a dip"
    assert 304 > snap["sma_mid"], "precondition: a dip, not a breakdown"
    sig = out[-1]
    assert sig is not None
    assert [x for x in out[:-1] if x is not None] == [], "only the dip bar fires"
    assert sig.entry_price == 304
    assert sig.stop_price == round_tick_down(304 - 2.0 * snap["atr"]), "2 x ATR(20) below the close"
    assert sig.target_price is None, "the screen's horizon is the exit, not a target"


def test_does_not_fire_when_the_close_is_below_the_mid_sma():
    """A breakdown, not a dip."""
    s, out = _run(UPTREND + [290])
    snap = s.snapshot("X")
    assert snap["sma_mid"] > snap["sma_slow"], "the long trend is still up"
    assert 290 < snap["sma_fast"] and 290 < snap["sma_mid"], "below SMA20 AND below SMA50"
    assert out[-1] is None


def test_does_not_fire_without_the_long_uptrend():
    """Every dip condition holds; only SMA50 > SMA200 fails, and that alone blocks the entry."""
    s, out = _run(V_SHAPE + [145])
    snap = s.snapshot("X")
    assert snap["sma_mid"] < snap["sma_slow"], "no uptrend: SMA50 is below SMA200"
    assert 145 < snap["sma_fast"] and 145 > snap["sma_mid"], "the dip conditions do hold"
    assert [x for x in out if x is not None] == []


def test_no_signal_before_two_hundred_bars_of_warm_up():
    """SMA200 needs its full window; nothing may fire, or be called ready, before it."""
    s, out = _run(UPTREND[:199])
    assert [x for x in out if x is not None] == []
    assert s.is_ready("X") is False and s.snapshot("X") == {}
    s.on_candle(_bars(UPTREND[:200])[-1])
    assert s.is_ready("X") is True


def test_the_signal_is_a_long_cnc_position_with_a_fixed_hold():
    s, out = _run(UPTREND + [304])
    sig = out[-1]
    assert sig.direction == "LONG"
    assert sig.product == "CNC"
    assert sig.strategy == "trend_dip" and sig.symbol == "X"
    assert sig.max_hold_bars == 60, "the screen's 60-bar horizon, carried to the broker's time exit"


def test_products_contains_cnc():
    """MIS is not offered: an intraday product squared off at 15:10 cannot hold a 60-day swing."""
    assert PRODUCTS == ("CNC",)


def test_factory_registers_trend_dip():
    assert isinstance(build_strategy("trend_dip", PARAMS), TrendDipStrategy)


@pytest.mark.parametrize("bad, msg", [
    ({"sma_mid": 200}, "sma_fast must be <"),
    ({"max_hold_bars": 0}, "max_hold_bars"),
    ({"atr_stop_mult": 0}, "atr_stop_mult"),
    ({"product": "MIS"}, "product"),
])
def test_parameter_validation(bad, msg):
    with pytest.raises(ValueError, match=msg):
        TrendDipStrategy(dict(PARAMS, **bad))
