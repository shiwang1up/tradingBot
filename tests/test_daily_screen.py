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
    Plus 0.05% slippage each side = 0.1%. Total 0.572405%."""
    assert ds.round_trip_cost() == pytest.approx(0.00572405, abs=1e-9)


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
