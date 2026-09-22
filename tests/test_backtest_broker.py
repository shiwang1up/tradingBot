import logging

import pytest

from tradebot.execution.backtest import STOP_FIRST_ON_SAME_BAR, BacktestBroker, check_exit
from tradebot.execution.broker import Broker
from tradebot.types import Position
from tradebot.execution.broker import Closed, Filled, Unfilled
from tradebot.types import ApprovedOrder, Candle, Signal


def _order(direction="LONG", entry=100.0, stop=99.0, target=102.0, qty=10, sym="X", product="MIS",
           max_hold=None):
    return ApprovedOrder(Signal("ema_rsi", sym, direction, entry, stop, target, product, 1000,
                                max_hold_bars=max_hold), qty, "cid-" + sym)


def _c(o, h, l, c, ts=1300, sym="X"):
    return Candle(sym, ts, o, h, l, c, 1)


def _broker(slip=0.0, buffer=None, fill_on_close=False, charges=None):
    return BacktestBroker(capital=100_000.0, slippage_pct=slip, mis_leverage=5.0, entry_buffer_pct=buffer,
                          charges=charges, fill_on_close=fill_on_close)


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


def test_restore_then_bar_fills_pending_and_exits_positions():
    b = BacktestBroker(100_000, slippage_pct=0.0, mis_leverage=5.0, entry_buffer_pct=None)
    pos = Position("A", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 900, "c1", "s", db_id=7)
    sig = Signal("s", "B", "LONG", 50.0, 49.0, 52.0, "MIS", 900)
    b.restore([pos], [ApprovedOrder(sig, 4, "c2")], cash=99_000.0)
    assert b.cash == 99_000.0
    assert b.open_positions() == {"A": pos} and b.pending_symbols() == {"B"}

    events = b.on_bar(1200, {"A": Candle("A", 1200, 101.0, 103.0, 100.5, 102.5, 1),
                             "B": Candle("B", 1200, 50.0, 51.0, 49.5, 50.5, 1)})
    assert {type(e).__name__ for e in events} == {"Filled", "Closed"}
    closed = next(e for e in events if isinstance(e, Closed)).position
    assert closed.db_id == 7 and closed.exit_reason == "TARGET" and closed.pnl == pytest.approx(20.0)
    assert set(b.open_positions()) == {"B"} and b.pending_symbols() == set()
    assert b.cash == pytest.approx(99_020.0)


def test_close_books_charges_and_moves_cash_by_net():
    from tradebot.config import ChargesConfig
    b = BacktestBroker(100_000.0, 0.0, 5.0, None, charges=ChargesConfig())
    b.place_entry(_order(qty=100))                                   # LONG 100 @ 100, stop 99, target 102
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    ev = b.on_bar(1600, {"X": _c(101.0, 102.5, 100.8, 102.2, ts=1600)})
    pos = ev[0].position
    assert pos.exit_reason == "TARGET" and pos.pnl == pytest.approx(200.0)   # pnl stays gross
    assert pos.charges == pytest.approx(27.42)                       # bought 10,000, sold 10,200
    assert b.cash == pytest.approx(100_000.0 + 200.0 - 27.42)


def test_short_charges_put_the_entry_on_the_sell_side():
    from tradebot.config import ChargesConfig
    b = BacktestBroker(100_000.0, 0.0, 5.0, None, charges=ChargesConfig())
    b.place_entry(_order(direction="SHORT", entry=100.0, stop=101.0, target=98.0, qty=100))
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 99.8)})
    ev = b.on_bar(1600, {"X": _c(99.0, 99.2, 97.5, 97.8, ts=1600)})
    pos = ev[0].position
    assert pos.exit_reason == "TARGET" and pos.exit_price == pytest.approx(98.0)
    assert pos.charges == pytest.approx(26.88)                       # sold 10,000, bought 9,800
    assert pos.pnl == pytest.approx(200.0)
    assert b.cash == pytest.approx(100_000.0 + 200.0 - 26.88)


def test_without_a_charges_config_the_cost_is_zero_not_none():
    b = _broker()
    b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.2, 98.5, 98.8, ts=1600)})
    assert ev[0].position.exit_reason == "STOP" and ev[0].position.charges == 0.0
    assert b.cash == pytest.approx(100_000.0 + ev[0].position.pnl)


def test_charges_are_computed_on_the_slipped_fills():
    from tradebot.config import ChargesConfig
    from tradebot.execution.charges import position_charges
    b = BacktestBroker(100_000.0, 0.05, 5.0, None, charges=ChargesConfig())
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.1, 98.9, 99.0)})
    pos = ev[0].position
    assert pos.exit_reason == "STOP"
    assert pos.charges == pytest.approx(
        position_charges(pos.direction, pos.avg_price, pos.exit_price, pos.quantity, ChargesConfig())
    )


@pytest.mark.parametrize("with_charges", [False, True])
@pytest.mark.parametrize("bad_price", [float("nan"), 0.0, -1.0, float("inf")])
def test_close_rejects_a_bad_price_and_leaves_the_position_open(bad_price, with_charges):
    # Reaches into _close directly: through the public API a NaN dies earlier in round_tick, and a
    # non-positive or infinite price is normally screened out by parse_candles/check_exit, so the
    # guard inside _close is otherwise untestable.
    from tradebot.config import ChargesConfig
    charges = ChargesConfig() if with_charges else None
    b = BacktestBroker(100_000.0, 0.0, 5.0, None, charges=charges)
    b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    pos = b.open_positions()["X"]
    cash_before = b.cash
    with pytest.raises(ValueError):
        b._close(pos, 1600, bad_price, "TARGET")
    assert b.open_positions() == {"X": pos}
    assert pos.closed_ts is None
    assert pos.exit_price is None
    assert pos.exit_reason is None
    assert pos.pnl is None
    assert pos.charges is None
    assert b.cash == cash_before
    assert b.closed == []


def test_on_bar_contains_a_failed_close_and_returns_the_other_events(caplog):
    """A hits its target, C's pending entry fills, and B's candle is all zeros: B's LONG stop (99)
    is triggered by low <= stop, and the fill (min(open, stop) = 0.0) is a bad exit price. The
    review scenario this reproduces: B's failed close must not swallow A's Closed or C's Filled."""
    b = _broker()
    pos_a = Position("A", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 900, "ca", "s")
    pos_b = Position("B", "MIS", "LONG", 10, 50.0, 49.0, 52.0, 900, "cb", "s")
    b.restore([pos_a, pos_b], [], cash=100_000.0)
    b.place_entry(_order(sym="C", entry=10.0, stop=9.0, target=12.0))
    with caplog.at_level(logging.ERROR, logger="tradebot.backtest"):
        ev = b.on_bar(1200, {
            "A": _c(100.0, 103.0, 99.5, 102.5, ts=1200, sym="A"),   # target hit
            "B": Candle("B", 1200, 0.0, 0.0, 0.0, 0.0, 0),          # stopped, but the fill price is 0.0
            "C": _c(10.0, 10.5, 9.8, 10.2, ts=1200, sym="C"),       # fills, no exit this bar
        })
    kinds = sorted((type(e).__name__, e.position.symbol) for e in ev)
    assert kinds == [("Closed", "A"), ("Filled", "C")]
    assert "B" in caplog.text
    assert {"B", "C"} <= set(b.open_positions())   # B stays open (failed close); C fills and isn't exited this bar
    b_pos = b.open_positions()["B"]
    assert (b_pos.closed_ts, b_pos.exit_price, b_pos.exit_reason, b_pos.pnl, b_pos.charges) == (None,) * 5
    assert b.cash == pytest.approx(100_000.0 + 20.0)   # only A's net (pnl 20, no charges configured)


def test_square_off_contains_a_failed_close_and_still_closes_the_rest(caplog):
    b = _broker()
    b.place_entry(_order(sym="M", product="MIS"))
    b.place_entry(_order(sym="Z", product="MIS", entry=50.0, stop=49.0, target=52.0))
    b.on_bar(1300, {"M": _c(100.0, 100.1, 99.9, 100.0, sym="M"), "Z": _c(50.0, 50.1, 49.9, 50.0, sym="Z")})
    with caplog.at_level(logging.ERROR, logger="tradebot.backtest"):
        ev = b.square_off(1600, {"M": _c(101.0, 101.2, 100.8, 101.0, 1600, "M"),
                                 "Z": Candle("Z", 1600, 0.0, 0.0, 0.0, 0.0, 0)})
    assert [e.position.symbol for e in ev] == ["M"]
    assert "Z" in caplog.text
    assert set(b.open_positions()) == {"Z"}
    z_pos = b.open_positions()["Z"]
    assert z_pos.closed_ts is None and z_pos.exit_price is None


# -- daily mode: entries fill at the close, and a hold can time out ---------------------------
def test_a_daily_broker_fills_at_the_close_not_the_open():
    """Groww's daily open is synthetic for 2025, so filling there would be fiction. Build a bar
    whose open and close differ clearly and assert which one the fill used."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0))
    ev = b.on_bar(1300, {"X": _c(99.5, 101.5, 99.2, 101.0)})
    assert isinstance(ev[0], Filled)
    assert ev[0].position.avg_price == pytest.approx(101.0)


def test_the_default_broker_still_fills_at_the_open():
    """The control. Intraday fills must not move."""
    b = _broker()
    b.place_entry(_order(entry=100.0))
    ev = b.on_bar(1300, {"X": _c(99.5, 101.5, 99.2, 101.0)})
    assert isinstance(ev[0], Filled)
    assert ev[0].position.avg_price == pytest.approx(99.5)


def test_the_entry_buffer_is_measured_against_the_fill_price():
    """_beyond_buffer rejects a signal whose price has run away. In daily mode the fill is the
    close, so the buffer must be judged against the close too -- judging it against the open
    while filling at the close would accept entries the buffer exists to reject."""
    b = _broker(buffer=0.1, fill_on_close=True)
    b.place_entry(_order(entry=100.0))
    # Opens inside the buffer and closes 1% above it: judged against the open this fills at 101.0,
    # exactly the runaway entry the buffer exists to reject.
    ev = b.on_bar(1300, {"X": _c(100.05, 101.2, 99.9, 101.0)})
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "beyond_buffer"
    # The mirror: an open far above the buffer still fills when the close -- the fill price -- is
    # inside it.
    b2 = _broker(buffer=0.1, fill_on_close=True)
    b2.place_entry(_order(entry=100.0, target=None))
    ev2 = b2.on_bar(1300, {"X": _c(103.0, 103.2, 99.9, 100.05)})
    assert isinstance(ev2[0], Filled) and ev2[0].position.avg_price == pytest.approx(100.05)


def test_a_daily_stop_still_exits_at_the_stop_level():
    """Only the ENTRY convention changes. Exits on a stop or target are unaffected."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    assert isinstance(b.on_bar(1300, {"X": _c(99.8, 100.4, 99.6, 100.0)})[0], Filled)
    ev = b.on_bar(1600, {"X": _c(99.9, 100.0, 98.0, 98.5)})
    closed = [e for e in ev if isinstance(e, Closed)]
    assert len(closed) == 1
    assert closed[0].position.exit_reason == "STOP"
    assert closed[0].position.exit_price == pytest.approx(99.0), "the stop level, not the bar's close"


def test_a_time_exit_closes_at_the_close_of_the_nth_bar_after_entry():
    """A swing hold has no stop-or-target exit of its own; without this a 60-day hold never ends.
    The entry bar does not count, so max_hold_bars=2 exits two bars after the fill."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=90.0, target=None, max_hold=2))
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})       # entry bar
    b.on_bar(1600, {"X": _c(100.2, 100.6, 99.9, 100.4)})       # one bar held
    assert set(b.open_positions()) == {"X"}, "the hold is not up yet"
    ev = b.on_bar(1900, {"X": _c(100.4, 100.8, 100.0, 100.6)})  # two bars held
    closed = [e for e in ev if isinstance(e, Closed)]
    assert len(closed) == 1
    assert closed[0].position.exit_reason == "TIME_EXIT"
    assert closed[0].position.exit_price == pytest.approx(100.6), "closes at that bar's close"
    assert b.open_positions() == {}


def test_a_position_without_max_hold_bars_never_times_out():
    """The default, and every intraday signal: no time exit at all."""
    b = _broker()
    b.place_entry(_order(entry=100.0, stop=90.0, target=None))
    for k in range(20):
        b.on_bar(1300 + 300 * k, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    assert set(b.open_positions()) == {"X"}


def test_a_stop_on_the_time_exit_bar_is_still_a_stop():
    """The time exit runs after the stop/target check, so a bar that does both is a stop."""
    b = _broker()
    b.place_entry(_order(entry=100.0, stop=99.0, target=None, max_hold=1))
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.2, 98.0, 99.5)})
    closed = [e for e in ev if isinstance(e, Closed)]
    assert len(closed) == 1
    assert closed[0].position.exit_reason == "STOP"
    assert closed[0].position.exit_price == pytest.approx(99.0)


def test_a_cnc_close_is_charged_the_delivery_schedule():
    """The schedule is selected by the position's product. Charged as MIS, a delivery round trip
    is understated by STT on the buy side, the higher stamp duty and the DP fee -- and every
    charge would still be non-zero, so a test that only checked "charges > 0" would not see it."""
    from tradebot.config import ChargesConfig
    from tradebot.execution.charges import position_charges
    schedule = ChargesConfig()

    def close_one(product):
        b = _broker(charges=schedule)
        b.place_entry(_order(entry=100.0, stop=90.0, target=None, product=product))
        b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.0)})
        b.square_off(1600, {"X": _c(100.0, 102.5, 99.9, 102.0)}, products=("MIS", "CNC"))
        return b.closed[0]

    mis, cnc = close_one("MIS"), close_one("CNC")
    assert mis.avg_price == cnc.avg_price and mis.exit_price == cnc.exit_price, "the same trade"
    assert cnc.charges > mis.charges
    assert cnc.charges == pytest.approx(
        position_charges("LONG", cnc.avg_price, cnc.exit_price, cnc.quantity, schedule, product="CNC"))
    assert mis.charges == pytest.approx(
        position_charges("LONG", mis.avg_price, mis.exit_price, mis.quantity, schedule, product="MIS"))


def test_a_daily_entry_bar_cannot_stop_the_position_it_just_opened():
    """The fill is this bar's CLOSE, so the bar's own high and low have already printed by the
    time the position exists. Exiting against them books a trade at a price from before the
    entry: in the 2022 smoke run an ADANIENT long filled at 3644.0 on a bar whose low was
    3616.8 and was "stopped" at 3649.9 -- ABOVE its own entry, a profit on a losing setup."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    ev = b.on_bar(1300, {"X": _c(101.0, 101.5, 98.0, 100.0)})  # the low is through the stop
    assert isinstance(ev[0], Filled)
    assert not [e for e in ev if isinstance(e, Closed)], "that low printed before the fill"
    assert set(b.open_positions()) == {"X"}


def test_a_daily_entry_bar_cannot_hit_the_target_it_just_opened_against():
    """The same reasoning the other way: a bar whose high ran through the target must not book a
    win on a position that did not exist until that bar's close."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=90.0, target=102.0))
    ev = b.on_bar(1300, {"X": _c(101.0, 103.0, 100.5, 101.0)})  # the high is through the target
    assert isinstance(ev[0], Filled)
    assert not [e for e in ev if isinstance(e, Closed)]
    assert set(b.open_positions()) == {"X"}


def test_the_bar_after_a_daily_entry_exits_normally():
    """The skip is the entry bar and nothing more. Bar i+1 lies entirely after the fill, so its
    range is real and a stop on it is a real stop."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    assert isinstance(b.on_bar(1300, {"X": _c(101.0, 101.5, 98.0, 100.0)})[0], Filled)
    ev = b.on_bar(1600, {"X": _c(100.0, 100.2, 98.5, 98.8)})
    closed = [e for e in ev if isinstance(e, Closed)]
    assert len(closed) == 1 and closed[0].position.exit_reason == "STOP"
    assert closed[0].position.exit_price == pytest.approx(99.0)


def test_an_intraday_entry_bar_can_still_stop_out():
    """The control. Intraday fills at the OPEN, so the rest of that bar genuinely follows the
    fill and a same-bar stop is real. The golden fixture depends on this staying true."""
    b = _broker()
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 98.0, 98.5)})
    closed = [e for e in ev if isinstance(e, Closed)]
    assert len(closed) == 1 and closed[0].position.exit_reason == "STOP"


# -- an entry already through its own stop or target ------------------------------------------
def test_a_long_filling_at_or_below_its_own_stop_is_not_filled():
    """The signal's stop is computed on the signal bar; the fill lands a bar later. A gap between
    the two can leave a long already below the stop it was sized against. Opening it books a
    "stop" ABOVE the entry -- a profit on a setup that has already failed -- and the quantity came
    from a risk distance that no longer exists. ADANIENT, 2022-12-23: filled 3644.00, stop 3649.90.
    """
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    ev = b.on_bar(1300, {"X": _c(99.5, 99.6, 98.4, 98.7)})   # closes below the stop
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "through_stop"
    assert b.open_positions() == {}


def test_a_long_filling_exactly_at_its_stop_is_not_filled():
    """Risk per share is zero there, and check_exit's `low <= stop` fires on the next bar that so
    much as touches it. Nothing to size and nothing to hold."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    ev = b.on_bar(1300, {"X": _c(99.5, 99.6, 98.4, 99.0)})
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "through_stop"


def test_a_short_filling_at_or_above_its_own_stop_is_not_filled():
    """The mirror: a short's stop sits above it, so a gap up leaves it already stopped."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order("SHORT", entry=100.0, stop=101.0, target=None))
    ev = b.on_bar(1300, {"X": _c(100.5, 102.0, 100.4, 101.5)})
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "through_stop"
    assert b.open_positions() == {}


def test_a_long_filling_at_or_beyond_its_own_target_is_not_filled():
    """The same defect at the other end: the move the trade was waiting for has already happened,
    and check_exit would close it at a target BELOW the entry, booking a loss on a winning setup."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    ev = b.on_bar(1300, {"X": _c(101.0, 102.6, 100.9, 102.4)})
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "through_target"
    assert b.open_positions() == {}


def test_an_intraday_entry_gapping_through_its_stop_still_fills_and_scratches():
    """The control, and the reason the guard is daily-only. Intraday the fill IS the open, so the
    rest of the bar follows it: the position opens and stops at the clamped open for a scratch,
    which is what a marketable entry meeting its stop at once really does. Under fill_on_close
    that exit does not exist -- the bar is over -- so there the entry is declined instead."""
    b = _broker()
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    ev = b.on_bar(1300, {"X": _c(98.5, 99.2, 98.0, 99.1)})
    assert [type(e) for e in ev] == [Filled, Closed]
    assert ev[1].position.exit_reason == "STOP" and ev[1].position.pnl == 0.0


def test_an_entry_between_its_stop_and_target_still_fills():
    """The control. A fill that has moved against the signal but is still short of the stop is an
    ordinary entry and must not be rejected -- that is most of them."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    ev = b.on_bar(1300, {"X": _c(100.2, 100.4, 99.2, 99.3)})
    assert isinstance(ev[0], Filled)
    assert ev[0].position.avg_price == pytest.approx(99.3)


def test_a_position_with_no_target_is_judged_on_its_stop_alone():
    """trend_dip has no target. A None target must not be compared against anything."""
    b = _broker(fill_on_close=True)
    b.place_entry(_order(entry=100.0, stop=99.0, target=None))
    ev = b.on_bar(1300, {"X": _c(100.0, 140.0, 99.5, 138.0)})
    assert isinstance(ev[0], Filled), "a runaway move with no target is a buffer question, not a target one"
