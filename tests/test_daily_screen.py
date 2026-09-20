"""Unit tests for scripts/daily_screen.py. The script lives outside the package and has no
`from __future__ import annotations`, so it is loaded straight from its path rather than imported
as `tradebot.*` (amendment D7)."""
import importlib.util
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
