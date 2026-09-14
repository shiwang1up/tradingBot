import pytest

from tradebot.execution.backtest import STOP_FIRST_ON_SAME_BAR, BacktestBroker
from tradebot.execution.broker import Closed, Filled, Unfilled
from tradebot.types import ApprovedOrder, Candle, Signal


def _order(direction="LONG", entry=100.0, stop=99.0, target=102.0, qty=10, sym="X", product="MIS"):
    return ApprovedOrder(Signal("ema_rsi", sym, direction, entry, stop, target, product, 1000), qty, "cid-" + sym)


def _c(o, h, l, c, ts=1300, sym="X"):
    return Candle(sym, ts, o, h, l, c, 1)


def _broker(slip=0.0):
    return BacktestBroker(capital=100_000.0, slippage_pct=slip, mis_leverage=5.0)


def test_constant_documented():
    assert STOP_FIRST_ON_SAME_BAR is True


def test_entry_fills_at_next_open_with_slippage():
    b = _broker(slip=0.05)
    b.place_entry(_order())
    assert b.pending_symbols() == {"X"}
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    assert isinstance(ev[0], Filled)
    assert ev[0].position.avg_price == pytest.approx(100.05)
    assert ev[0].position.opened_ts == 1300
    assert b.pending_symbols() == set()
    assert set(b.open_positions()) == {"X"}


def test_short_entry_slippage_is_downward():
    b = _broker(slip=0.05)
    b.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    assert ev[0].position.avg_price == pytest.approx(99.95)


def test_unfilled_when_no_candle_for_symbol():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"Y": _c(1, 1, 1, 1, sym="Y")})
    assert isinstance(ev[0], Unfilled) and ev[0].order.client_id == "cid-X"
    assert b.pending_symbols() == set()


def test_stop_hit_on_entry_bar():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 98.5, 99.5)})
    assert [type(e) for e in ev] == [Filled, Closed]
    pos = ev[1].position
    assert pos.exit_reason == "STOP" and pos.exit_price == 99.0 and pos.pnl == pytest.approx(-10.0)
    assert b.open_positions() == {}


def test_target_hit_on_entry_bar():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"X": _c(100.0, 102.5, 99.5, 102.0)})
    assert ev[1].position.exit_reason == "TARGET" and ev[1].position.pnl == pytest.approx(20.0)


def test_both_hit_resolves_to_stop_first():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"X": _c(100.0, 103.0, 98.0, 100.0)})
    assert ev[1].position.exit_reason == "STOP"


def test_stop_exit_applies_slippage_target_does_not():
    b = _broker(slip=0.05)
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.1, 98.9, 99.0)})
    assert ev[0].position.exit_price == pytest.approx(98.95)   # 99 * (1 - 0.0005) rounded to tick
    b2 = _broker(slip=0.05)
    b2.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    b2.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    ev2 = b2.on_bar(1600, {"X": _c(100.0, 102.1, 99.9, 102.0)})
    assert ev2[0].position.exit_price == 102.0


def test_short_stop_and_target():
    b = _broker()
    b.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.0)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.5, 97.5, 98.0)})
    assert ev[0].position.exit_reason == "TARGET" and ev[0].position.pnl == pytest.approx(20.0)
    b2 = _broker()
    b2.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    b2.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.0)})
    ev2 = b2.on_bar(1600, {"X": _c(100.0, 101.5, 99.5, 101.0)})
    assert ev2[0].position.exit_reason == "STOP" and ev2[0].position.pnl == pytest.approx(-10.0)


def test_square_off_closes_mis_only_with_slippage():
    b = _broker(slip=0.05)
    b.place_entry(_order(sym="M", product="MIS"))
    b.place_entry(_order(sym="C", product="CNC"))
    b.on_bar(1300, {"M": _c(100.0, 100.1, 99.9, 100.0, sym="M"), "C": _c(100.0, 100.1, 99.9, 100.0, sym="C")})
    ev = b.square_off(1600, {"M": _c(101.0, 101.2, 100.8, 101.0, 1600, "M"), "C": _c(101.0, 101.2, 100.8, 101.0, 1600, "C")})
    assert [e.position.symbol for e in ev] == ["M"]
    assert ev[0].position.exit_reason == "SQUARE_OFF"
    assert ev[0].position.exit_price == pytest.approx(100.95)
    assert set(b.open_positions()) == {"C"}


def test_flatten_closes_everything():
    b = _broker()
    b.place_entry(_order(sym="M", product="MIS"))
    b.place_entry(_order(sym="C", product="CNC"))
    b.on_bar(1300, {"M": _c(100.0, 100.1, 99.9, 100.0, sym="M"), "C": _c(100.0, 100.1, 99.9, 100.0, sym="C")})
    ev = b.square_off(1600, {"M": _c(101, 101, 101, 101, 1600, "M"), "C": _c(101, 101, 101, 101, 1600, "C")},
                      products=("MIS", "CNC"), reason="FLATTEN")
    assert sorted(e.position.symbol for e in ev) == ["C", "M"]
    assert all(e.position.exit_reason == "FLATTEN" for e in ev)


def test_available_margin_reflects_leverage_positions_and_pending():
    b = _broker()
    assert b.available_margin("MIS") == pytest.approx(500_000.0)
    assert b.available_margin("CNC") == pytest.approx(100_000.0)
    b.place_entry(_order(qty=100))                 # 100 * 100 = 10,000 notional, 2,000 margin at 5x
    assert b.available_margin("MIS") == pytest.approx(490_000.0)
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    assert b.available_margin("MIS") == pytest.approx(490_000.0)
    assert b.available_margin("CNC") == pytest.approx(98_000.0)


def test_cash_and_unrealised():
    b = _broker()
    b.place_entry(_order(qty=10))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    assert b.unrealised_pnl({"X": 101.0}) == pytest.approx(10.0)
    b.on_bar(1600, {"X": _c(101.0, 102.5, 100.9, 102.0)})
    assert b.cash == pytest.approx(100_020.0)
    assert b.unrealised_pnl({"X": 50.0}) == 0.0
