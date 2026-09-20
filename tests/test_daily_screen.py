"""Unit tests for scripts/daily_screen.py. The script lives outside the package and has no
`from __future__ import annotations`, so it is loaded straight from its path rather than imported
as `tradebot.*` (amendment D7)."""
import importlib.util
import math
import sqlite3
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_screen.py"
spec = importlib.util.spec_from_file_location("daily_screen", SCRIPT)
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)


def _bars(rows):
    """rows: (iso date, open, high, low, close)."""
    return [ds.Bar(date.fromisoformat(d), o, h, l, c) for d, o, h, l, c in rows]


def test_round_trip_cost_is_hand_worked():
    """25,000 position, Groww delivery: brokerage 20 x 2 = 40; STT 0.1% x 2 = 50; exchange
    0.00297% x 2 = 1.485; SEBI 0.0001% x 2 = 0.05; stamp 0.015% buy = 3.75;
    GST 18% x (40 + 1.485 + 0.05) = 7.4763; DP 15.34. Charges 118.1013 = 0.472405% of 25,000.
    Plus 0.05% slippage each side = 0.1%. Total 0.5724052% exactly."""
    assert ds.round_trip_cost() == pytest.approx(0.005724052, abs=1e-9)


def test_gap_mask_covers_the_gap_day_and_the_days_after_it():
    """A 50% overnight drop is an unadjusted split, not a crash: the 200-day average is wrong for
    200 days afterwards, so those dates cannot host an entry."""
    rows = [("2020-01-%02d" % d, 100.0, 101.0, 99.0, 100.0) for d in range(1, 10)]
    rows[5] = ("2020-01-06", 50.0, 51.0, 49.0, 50.0)          # opens at half the previous close
    masked = ds.gap_mask(_bars(rows), mask_days=3)
    assert date(2020, 1, 6) in masked
    assert date(2020, 1, 9) in masked                          # within 3 trading days after
    assert date(2020, 1, 5) not in masked


def test_gap_mask_ignores_an_ordinary_move():
    rows = [("2020-01-%02d" % d, 100.0, 101.0, 99.0, 100.0) for d in range(1, 6)]
    rows[3] = ("2020-01-04", 110.0, 111.0, 109.0, 110.0)       # +10% gap: a real move
    assert ds.gap_mask(_bars(rows), mask_days=3) == set()


def test_load_series_reads_daily_candles_only(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL, h REAL, l REAL,
                              c REAL, v INTEGER, source TEXT);
    """)
    # 1440 rows for A, plus a 5-minute row that must be ignored
    conn.execute("INSERT INTO candles VALUES ('A', 1577836800, 1440, 1, 2, 0.5, 1.5, 0, 'official')")
    conn.execute("INSERT INTO candles VALUES ('A', 1577923200, 1440, 2, 3, 1.5, 2.5, 0, 'official')")
    conn.execute("INSERT INTO candles VALUES ('A', 1577836800, 5, 9, 9, 9, 9, 0, 'official')")
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    series = ds.load_series(ro, ["A", "MISSING"])
    assert list(series) == ["A"] and len(series["A"]) == 2
    assert series["A"][0].close == 1.5 and series["A"][1].open == 2
    assert series["A"][0].date < series["A"][1].date


def test_sma_is_none_until_warm_then_the_mean_of_the_last_n():
    assert ds.sma([1.0, 2.0, 3.0, 4.0], 3) == [None, None, 2.0, 3.0]


def test_rsi_wilder_on_a_monotone_rise_is_100_and_on_a_fall_is_0():
    up = ds.rsi_wilder([1.0, 2.0, 3.0, 4.0, 5.0], 2)
    assert up[0] is None and up[1] is None and up[2] == pytest.approx(100.0)
    down = ds.rsi_wilder([5.0, 4.0, 3.0, 2.0, 1.0], 2)
    assert down[-1] == pytest.approx(0.0)


def test_rsi_wilder_hand_worked():
    """closes 10, 11, 10.5, 11.5 with n=2. Deltas +1, -0.5, +1.
    Seed after 2 deltas: avg gain 0.5, avg loss 0.25 -> RS 2 -> RSI 66.6667.
    Next: gain (0.5*1 + 1)/2 = 0.75, loss (0.25*1 + 0)/2 = 0.125 -> RS 6 -> RSI 85.7143."""
    out = ds.rsi_wilder([10.0, 11.0, 10.5, 11.5], 2)
    assert out[2] == pytest.approx(66.66667, abs=1e-4)
    assert out[3] == pytest.approx(85.71429, abs=1e-4)


def test_atr_wilder_hand_worked():
    """Three bars of true range 2 each after the first: ATR(2) seeds at 2 and stays 2."""
    bars = _bars([("2020-01-01", 10, 11, 9, 10), ("2020-01-02", 10, 11, 9, 10),
                  ("2020-01-03", 10, 11, 9, 10), ("2020-01-04", 10, 11, 9, 10)])
    out = ds.atr_wilder(bars, 2)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0) and out[3] == pytest.approx(2.0)


def test_rolling_extremes_exclude_the_current_bar():
    """A breakout must clear the PREVIOUS n closes; including today's would make it trivially true."""
    assert ds.rolling_max_prev([1.0, 5.0, 3.0, 2.0], 2) == [None, None, 5.0, 5.0]
    assert ds.rolling_min_prev([4.0, 1.0, 3.0, 2.0], 2) == [None, None, 1.0, 1.0]


from datetime import timedelta


def _flat(n, price=100.0, start=date(2020, 1, 1)):
    return [ds.Bar(start + timedelta(days=k), price, price + 1, price - 1, price) for k in range(n)]


def _ind(bars):
    return ds.Indicators(bars)


# -- entry and exit rules ---------------------------------------------------------------------
def test_mean_reversion_enters_on_an_oversold_dip_in_an_uptrend():
    """Rising series (close above SMA200), then two sharp down closes drive RSI(2) under 10."""
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), 100.0 + k, 101.0 + k, 99.0 + k, 100.0 + k)
            for k in range(240)]
    for k, c in ((238, 300.0), (239, 250.0)):          # two big down bars at the end
        bars[k] = ds.Bar(bars[k].date, c, c + 1, c - 1, c)
    ind = _ind(bars)
    assert ind.rsi2[239] < 10.0 and ind.closes[239] > ind.sma200[239]
    assert ds.mr_entry(ind, 239) is True
    assert ds.mr_entry(ind, 200) is False              # no dip there


def test_mean_reversion_exits_above_the_5_day_average_or_after_10_days():
    bars = _flat(30)
    ind = _ind(bars)
    # flat series: close == sma5, so no sma5 exit; the time stop must fire on the 10th day
    assert ds.mr_exit(ind, 15, 10, 0.0) is None
    assert ds.mr_exit(ind, 20, 10, 0.0) == "time"
    rising = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), 100.0 + k, 101.0 + k, 99.0 + k, 100.0 + k)
              for k in range(30)]
    assert ds.mr_exit(_ind(rising), 15, 14, 0.0) == "sma5"      # close is above its 5-day mean


def test_trend_following_enters_only_on_the_bar_the_averages_cross():
    """A series that falls for 150 bars then rises brings SMA50 up through SMA200 exactly once."""
    closes = [200.0 - k for k in range(150)] + [50.0 + 2.0 * k for k in range(150)]
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), c, c + 1, c - 1, c) for k, c in enumerate(closes)]
    ind = _ind(bars)
    crosses = [i for i in range(len(bars)) if ds.tf_entry(ind, i)]
    assert len(crosses) == 1, crosses
    i = crosses[0]
    assert ind.sma50[i - 1] <= ind.sma200[i - 1] and ind.sma50[i] > ind.sma200[i]


def test_trend_following_exits_when_price_falls_3_atr_below_the_run_high():
    bars = _flat(30)                       # true range 2 every bar -> ATR(20) = 2, trail = 6
    ind = _ind(bars)
    assert ds.tf_exit(ind, 25, 20, run_high=100.0) is None       # close 100, high 100: no drop
    assert ds.tf_exit(ind, 25, 20, run_high=106.5) == "trail"    # 100 < 106.5 - 6
    assert ds.tf_exit(ind, 25, 20, run_high=105.0) is None       # 100 > 105 - 6


def test_breakout_needs_a_new_100_day_high_above_the_200_day_average():
    closes = [100.0 + (k % 7) for k in range(300)]               # range-bound: no new highs
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), c, c + 1, c - 1, c) for k, c in enumerate(closes)]
    assert ds.bo_entry(_ind(bars), 250) is False
    bars[250] = ds.Bar(bars[250].date, 130.0, 131.0, 129.0, 130.0)
    ind = _ind(bars)
    assert ind.closes[250] > ind.max100_prev[250] and ind.closes[250] > ind.sma200[250]
    assert ds.bo_entry(ind, 250) is True


def test_breakout_exits_below_the_50_day_low():
    closes = [100.0 + k for k in range(120)] + [60.0]
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), c, c + 1, c - 1, c) for k, c in enumerate(closes)]
    ind = _ind(bars)
    assert ds.bo_exit(ind, 120, 100, 0.0) == "min50"
    assert ds.bo_exit(ind, 110, 100, 0.0) is None


# -- the simulation --------------------------------------------------------------------------
_ALWAYS = (lambda ind, i: True, lambda ind, i, e, h: "x")        # enter every bar, exit immediately
_NEVER_EXIT = (lambda ind, i: i == 3, lambda ind, i, e, h: None)


def test_entry_and_exit_both_happen_at_the_next_bar_open():
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), 10.0 + k, 12.0 + k, 9.0 + k, 10.0 + k)
            for k in range(8)]
    trades, excluded = ds.simulate("A", "mr", bars, _ind(bars), set(), (bars[0].date, bars[-1].date),
                                   cost=0.0, system=_NEVER_EXIT, warmup=0)
    assert excluded == 0 and len(trades) == 1
    t = trades[0]
    assert t.entry_date == bars[4].date and t.entry_price == bars[4].open   # signal on bar 3, enter bar 4
    assert t.exit_date == bars[7].date and t.exit_price == bars[7].open     # no exit: window end
    assert t.reason == "window_end" and t.days == 3
    assert t.gross == pytest.approx(bars[7].open / bars[4].open - 1.0)


def test_only_one_trade_is_open_at_a_time():
    bars = _flat(10)
    trades, _ = ds.simulate("A", "mr", bars, _ind(bars), set(), (bars[0].date, bars[-1].date),
                            cost=0.0, system=_ALWAYS, warmup=0)
    # entry on the bar after each signal, exit on the bar after that: trades cannot overlap
    for a, b in zip(trades, trades[1:]):
        assert a.exit_date <= b.entry_date


def test_cost_is_deducted_from_the_net_return():
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), 100.0, 101.0, 99.0, 100.0) for k in range(8)]
    bars[7] = ds.Bar(bars[7].date, 110.0, 111.0, 109.0, 110.0)
    trades, _ = ds.simulate("A", "mr", bars, _ind(bars), set(), (bars[0].date, bars[-1].date),
                            cost=0.01, system=_NEVER_EXIT, warmup=0)
    assert trades[0].gross == pytest.approx(0.10)
    assert trades[0].net == pytest.approx(0.09)


def test_a_trade_touching_a_masked_date_is_excluded_not_counted():
    bars = _flat(8)
    masked = {bars[6].date}
    trades, excluded = ds.simulate("A", "mr", bars, _ind(bars), masked, (bars[0].date, bars[-1].date),
                                   cost=0.0, system=_NEVER_EXIT, warmup=0)
    assert trades == [] and excluded == 1


def test_no_entry_on_a_masked_signal_bar():
    bars = _flat(8)
    trades, excluded = ds.simulate("A", "mr", bars, _ind(bars), {bars[3].date},
                                   (bars[0].date, bars[-1].date), cost=0.0, system=_NEVER_EXIT, warmup=0)
    assert trades == [] and excluded == 0        # the signal never fired, so nothing was excluded


def test_the_window_bounds_which_bars_can_trade():
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), 10.0, 11.0, 9.0, 10.0) for k in range(20)]
    window = (bars[5].date, bars[10].date)
    trades, _ = ds.simulate("A", "mr", bars, _ind(bars), set(), window, cost=0.0,
                            system=_ALWAYS, warmup=0)
    assert trades, "the window must contain trades"
    for t in trades:
        assert window[0] <= t.entry_date <= window[1] and t.exit_date <= window[1]


def test_baseline_is_the_average_hold_of_the_same_length():
    """A series compounding 1% a day: holding 3 days always pays 1.01^3 - 1, so the baseline is
    that minus cost, whichever date you start on."""
    closes = [100.0 * (1.01 ** k) for k in range(30)]
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), c, c, c, c) for k, c in enumerate(closes)]
    b = ds.baseline_return(bars, (bars[0].date, bars[-1].date), 3, cost=0.0)
    assert b == pytest.approx(1.01 ** 3 - 1.0)
    assert ds.baseline_return(bars, (bars[0].date, bars[-1].date), 3, cost=0.005) == pytest.approx(
        1.01 ** 3 - 1.0 - 0.005)


def test_baseline_is_none_when_no_hold_of_that_length_fits():
    bars = _flat(4)
    assert ds.baseline_return(bars, (bars[0].date, bars[-1].date), 10, cost=0.0) is None


def test_a_system_that_only_matches_the_drift_has_zero_excess():
    """The point of the baseline: in a market that rose, being long pays even with no skill."""
    closes = [100.0 * (1.01 ** k) for k in range(30)]
    bars = [ds.Bar(date(2020, 1, 1) + timedelta(days=k), c, c, c, c) for k, c in enumerate(closes)]
    window = (bars[0].date, bars[-1].date)
    trades, _ = ds.simulate("A", "mr", bars, _ind(bars), set(), window, cost=0.0,
                            system=(lambda ind, i: i == 5, lambda ind, i, e, h: "x" if i - e >= 3 else None),
                            warmup=0)
    scored = ds.attach_excess(trades, bars, window, cost=0.0)
    assert len(scored) == 1 and scored[0].excess == pytest.approx(0.0, abs=1e-12)


def test_t_averages_trades_entered_on_the_same_date_first():
    """Three dates whose excesses are 0.02, 0.00 and 0.01, the first repeated 50 times because one
    market-wide dip fired it across 50 names. Collapsed: means 0.02/0.00/0.01, mean 0.01,
    var 0.0001, t = 0.01 / (0.01 / sqrt(3)) = sqrt(3) = 1.7320508. Counted per trade instead, n
    would be 52 and t several times larger -- repetition, not evidence."""
    d1, d2, d3 = date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)
    trades = ([ds.Trade("A", "mr", d1, d1, 1.0, 1.0, 1, 0.0, 0.0, "x", 0.02) for _ in range(50)]
              + [ds.Trade("B", "mr", d2, d2, 1.0, 1.0, 1, 0.0, 0.0, "x", 0.00),
                 ds.Trade("C", "mr", d3, d3, 1.0, 1.0, 1, 0.0, 0.0, "x", 0.01)])
    assert ds.t_across_dates(trades) == pytest.approx(math.sqrt(3), abs=1e-9)
    # one date is not enough to say anything
    assert ds.t_across_dates(trades[:50]) is None


def test_describe_reports_expectancy_payoff_and_breakeven():
    d = date(2020, 1, 1)
    trades = [ds.Trade("A", "mr", d + timedelta(days=k), d, 1.0, 1.0, 2, 0.0, net, "x", net)
              for k, net in enumerate([0.03, 0.01, -0.02, -0.02])]
    s = ds.describe(trades)
    assert s["trades"] == 4 and s["win_rate"] == pytest.approx(0.5)
    assert s["avg_win"] == pytest.approx(0.02) and s["avg_loss"] == pytest.approx(-0.02)
    assert s["payoff"] == pytest.approx(1.0) and s["breakeven"] == pytest.approx(0.5)
    assert s["expectancy"] == pytest.approx(0.0)
    assert s["median_days"] == 2
