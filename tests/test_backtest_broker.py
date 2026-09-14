import pytest

from tradebot.execution.backtest import STOP_FIRST_ON_SAME_BAR, BacktestBroker, check_exit
from tradebot.execution.broker import Broker
from tradebot.types import Position
from tradebot.execution.broker import Closed, Filled, Unfilled
from tradebot.types import ApprovedOrder, Candle, Signal


def _order(direction="LONG", entry=100.0, stop=99.0, target=102.0, qty=10, sym="X", product="MIS"):
    return ApprovedOrder(Signal("ema_rsi", sym, direction, entry, stop, target, product, 1000), qty, "cid-" + sym)


def _c(o, h, l, c, ts=1300, sym="X"):
    return Candle(sym, ts, o, h, l, c, 1)


def _broker(slip=0.0, buffer=None):
    return BacktestBroker(capital=100_000.0, slippage_pct=slip, mis_leverage=5.0, entry_buffer_pct=buffer)


_: Broker = _broker()  # BacktestBroker must satisfy the Protocol (checked at import by type checkers)


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
    assert isinstance(ev[0], Unfilled) and ev[0].order.client_id == "cid-X" and ev[0].reason == "no_candle"
    assert b.pending_symbols() == set()


def test_unfilled_when_open_gaps_beyond_entry_buffer():
    b = _broker(buffer=0.1)  # 0.1% like the live marketable limit
    b.place_entry(_order(entry=100.0))
    ev = b.on_bar(1300, {"X": _c(100.5, 101.0, 100.4, 100.8)})  # opens 0.5% above the signal
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "beyond_buffer"
    b2 = _broker(buffer=0.1)
    b2.place_entry(_order(entry=100.0))
    assert isinstance(b2.on_bar(1300, {"X": _c(100.05, 101.0, 99.9, 100.8)})[0], Filled)
    b3 = _broker(buffer=0.1)
    b3.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    assert b3.on_bar(1300, {"X": _c(99.5, 99.6, 99.0, 99.2)})[0].reason == "beyond_buffer"


def test_place_entry_refuses_duplicate_pending_or_open_symbol():
    b = _broker()
    b.place_entry(_order())
    with pytest.raises(ValueError):
        b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    with pytest.raises(ValueError):
        b.place_entry(_order())


def test_gap_through_stop_fills_at_open_not_stop():
    pos = Position("X", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c", "s")
    assert check_exit(pos, _c(95.0, 96.0, 94.0, 95.5)) == ("STOP", 95.0)
    short = Position("X", "MIS", "SHORT", 10, 100.0, 101.0, 98.0, 1, "c", "s")
    assert check_exit(short, _c(105.0, 106.0, 104.0, 105.0)) == ("STOP", 105.0)


def test_gap_through_target_fills_at_open():
    pos = Position("X", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c", "s")
    assert check_exit(pos, _c(103.0, 104.0, 102.5, 103.5)) == ("TARGET", 103.0)
    no_target = Position("X", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c", "s")
    assert check_exit(no_target, _c(103.0, 110.0, 102.5, 103.5)) is None


def test_entry_bar_gapping_through_stop_is_a_loss_not_a_profit():
    b = _broker()
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    ev = b.on_bar(1300, {"X": _c(95.0, 96.0, 94.0, 95.5)})
    assert [type(e) for e in ev] == [Filled, Closed]
    pos = ev[1].position
    assert pos.avg_price == 95.0 and pos.exit_price == 95.0 and pos.exit_reason == "STOP"
    assert pos.pnl == 0.0  # filled and stopped at the same gapped open; never +40


def test_cnc_position_carries_across_bars_then_stops():
    b = _broker()
    b.place_entry(_order(product="CNC"))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    assert b.on_bar(1600, {"X": _c(100.0, 100.5, 99.5, 100.2)}) == []
    assert b.square_off(1900, {"X": _c(100.0, 100.5, 99.5, 100.2, 1900)}) == []
    ev = b.on_bar(2200, {"X": _c(100.0, 100.1, 98.9, 99.0, 2200)})
    assert isinstance(ev[0], Closed) and ev[0].position.exit_reason == "STOP" and ev[0].position.closed_ts == 2200


def test_unrealised_requires_price_for_every_open_position():
    b = _broker()
    b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    with pytest.raises(KeyError):
        b.unrealised_pnl({})


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


def test_square_off_uses_last_price_when_symbol_has_no_candle():
    b = _broker()
    b.place_entry(_order(sym="M", product="MIS"))
    b.on_bar(1300, {"M": _c(100.0, 100.1, 99.9, 100.0, sym="M")})
    assert b.square_off(1600, {}) == []                       # no candle, no fallback: stays open
    ev = b.square_off(1600, {}, last_prices={"M": 101.0})
    assert ev[0].position.exit_price == 101.0 and ev[0].position.exit_reason == "SQUARE_OFF"
