import pytest

from tradebot.config import ChargesConfig
from tradebot.report.benchmark import Benchmark, build_benchmark
from tradebot.types import Candle

CFG = ChargesConfig()


def _c(sym, ts, close):
    """A flat bar. Candle is (symbol, ts, open, high, low, close, volume, source='official')."""
    return Candle(sym, ts, close, close, close, close, 1000)


def test_one_name_doubling_is_the_whole_calculation_by_hand():
    """10,000 of capital, one name at 100 rising to 200. 100 shares bought, sold for 20,000, so
    gross is +10,000. Charges are one CNC round trip on a 10,000 buy and a 20,000 sell."""
    from tradebot.execution.charges import round_trip_charges
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 200.0)]
    b = build_benchmark(candles, capital=10_000.0, charges=CFG)
    assert b.names_held == 1 and b.names_skipped == 0
    assert b.gross == pytest.approx(10_000.0)
    assert b.charges == pytest.approx(round_trip_charges(10_000.0, 20_000.0, CFG, product="CNC"))
    assert b.net == pytest.approx(b.gross - b.charges)


def test_capital_is_split_equally_across_priced_names():
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 110.0),
               _c("B", 1000, 50.0), _c("B", 2000, 55.0)]
    b = build_benchmark(candles, capital=10_000.0, charges=None)
    # 5,000 each: 50 shares of A (+500), 100 shares of B (+500)
    assert b.names_held == 2
    assert b.gross == pytest.approx(1_000.0)


def test_a_name_with_one_candle_is_excluded_and_does_not_take_a_slice():
    """One candle is not a round trip. Counting it would shrink every other name's slice."""
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 200.0), _c("B", 1000, 50.0)]
    b = build_benchmark(candles, capital=10_000.0, charges=None)
    assert b.names_held == 1 and b.names_skipped == 0
    assert b.gross == pytest.approx(10_000.0), "A took the whole capital, not half"


def test_a_name_dearer_than_its_slice_is_skipped_and_its_slice_stays_in_cash():
    """Eleven of universe.yaml's fifty names do this at a 2,000 slice, so it is the common case,
    not an edge case. The slice is NOT redistributed: you would not have bought more of the others.
    """
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 200.0),
               _c("EXPENSIVE", 1000, 9_000.0), _c("EXPENSIVE", 2000, 18_000.0)]
    b = build_benchmark(candles, capital=10_000.0, charges=None)
    assert b.names_held == 1 and b.names_skipped == 1
    # 5,000 slice: 50 shares of A rising 100 -> +5,000. EXPENSIVE contributes nothing.
    assert b.gross == pytest.approx(5_000.0)


def test_whole_shares_only_and_the_remainder_earns_nothing():
    """A 1,000 slice at 300 buys three shares, not 3.33. The leftover 100 is idle cash."""
    candles = [_c("A", 1000, 300.0), _c("A", 2000, 600.0)]
    b = build_benchmark(candles, capital=1_000.0, charges=None)
    assert b.gross == pytest.approx(900.0), "3 shares x 300 gain, not 3.33 x 300"


def test_a_falling_basket_is_negative():
    """charges=None (used elsewhere in this file) zeroes out costs by contract, which would make
    `net < gross` impossible to satisfy on its own -- so this one test needs a real schedule to
    show that charges make a loss worse, not just that the basket lost money."""
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 50.0)]
    b = build_benchmark(candles, capital=10_000.0, charges=CFG)
    assert b.gross == pytest.approx(-5_000.0) and b.net < b.gross


def test_the_charge_schedule_reaches_it():
    """Same basket, two schedules: zerodha's zero delivery brokerage must cost less than groww's."""
    from tradebot.brokers import load_brokers
    brokers = load_brokers("brokers.yaml")
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 110.0)]
    groww = build_benchmark(candles, 10_000.0, brokers["groww"].charges)
    zerodha = build_benchmark(candles, 10_000.0, brokers["zerodha"].charges)
    assert groww.charges > zerodha.charges > 0
    assert groww.gross == pytest.approx(zerodha.gross), "only the costs differ"


def test_no_candles_yields_none():
    """A run whose window holds no stored candles has no benchmark, and the report omits it
    rather than printing a zero that reads like a real result."""
    assert build_benchmark([], capital=10_000.0, charges=CFG) is None


def test_every_name_skipped_yields_none():
    """If nothing could be bought there is no basket to compare against."""
    candles = [_c("A", 1000, 90_000.0), _c("A", 2000, 99_000.0)]
    assert build_benchmark(candles, capital=10_000.0, charges=None) is None
