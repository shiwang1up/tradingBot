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
    assert a.update(_c(10, 11, 9, 10)) is None      # first TR = high - low; ATR(2) needs two of them
    assert a.update(_c(10, 11, 9, 10)) == pytest.approx(2.0)
    assert a.update(_c(10, 11, 9, 10)) == pytest.approx(2.0) and a.ready


def test_atr_uses_gap_from_prev_close():
    a = ATR(1)
    a.update(_c(10, 11, 9, 10))
    # gap up: prev close 10, low 12 => TR = max(13-12, |13-10|, |12-10|) = 3
    assert a.update(_c(12, 13, 12, 13)) == pytest.approx(3.0)


# Wilder's worked example (StockCharts): 33 closes, RSI(14) first value 70.46.
STOCKCHARTS = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
               46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18,
               44.22, 44.57, 43.42, 42.66, 43.13]


def test_rsi_matches_wilder_reference_series():
    r = RSI(14)
    values = [v for v in (r.update(x) for x in STOCKCHARTS) if v is not None]
    assert values[:6] == pytest.approx([70.4641, 66.2496, 66.4809, 69.3469, 66.2947, 57.9150], abs=1e-3)


def test_rsi_flat_series_is_50_not_100():
    r = RSI(3)
    for _ in range(6):
        v = r.update(10.0)
    assert v == 50.0


@pytest.mark.parametrize("cls", [EMA, RSI, ATR])
@pytest.mark.parametrize("period", [0, -1, 2.5])
def test_invalid_period_rejected(cls, period):
    with pytest.raises(ValueError):
        cls(period)


def test_non_finite_input_fails_loud_instead_of_poisoning():
    e = EMA(2)
    e.update(1.0)
    with pytest.raises(ValueError):
        e.update(float("nan"))
    with pytest.raises(ValueError):
        RSI(2).update(float("inf"))
    with pytest.raises(ValueError):
        ATR(2).update(_c(1, float("nan"), 1, 1))
