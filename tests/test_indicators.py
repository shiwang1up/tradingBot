import pytest

from tradebot.strategy.indicators import ATR, EMA, RSI
from tradebot.types import Candle


def test_ema_seeds_with_sma_then_smooths():
    e = EMA(3)
    assert e.update(1) is None and not e.ready
    assert e.update(2) is None
    assert e.update(3) == pytest.approx(2.0) and e.ready
    assert e.update(4) == pytest.approx(3.0)  # k=0.5: 4*0.5 + 2*0.5


def test_rsi_all_gains_is_100():
    r = RSI(3)
    for x in (1, 2, 3, 4):
        v = r.update(x)
    assert v == pytest.approx(100.0) and r.ready


def test_rsi_alternating_is_50():
    r = RSI(4)
    for x in (10, 11, 10, 11, 10):
        v = r.update(x)
    assert v == pytest.approx(50.0)


def test_rsi_not_ready_before_period_changes():
    r = RSI(3)
    assert r.update(1) is None
    assert r.update(2) is None
    assert r.update(3) is None
    assert not r.ready
    assert r.update(4) is not None


def _c(o, h, l, c, ts=0):
    return Candle("X", ts, o, h, l, c, 1)


def test_atr_constant_range_no_gaps():
    a = ATR(2)
    assert a.update(_c(10, 11, 9, 10)) is None      # no prev close: TR ignored for seeding? No: first TR = h-l
    assert a.update(_c(10, 11, 9, 10)) == pytest.approx(2.0)
    assert a.update(_c(10, 11, 9, 10)) == pytest.approx(2.0) and a.ready


def test_atr_uses_gap_from_prev_close():
    a = ATR(1)
    a.update(_c(10, 11, 9, 10))
    # gap up: prev close 10, low 12 => TR = max(13-12, |13-10|, |12-10|) = 3
    assert a.update(_c(12, 13, 12, 13)) == pytest.approx(3.0)
