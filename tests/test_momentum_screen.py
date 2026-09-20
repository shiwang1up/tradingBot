"""Unit tests for scripts/momentum_screen.py, loaded from its path like the other script tests."""
import importlib.util
import sqlite3
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "momentum_screen.py"
_spec = importlib.util.spec_from_file_location("momentum_screen", SCRIPT)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


def _series(pairs):
    """pairs: (iso date, close) -> the [(date, close)] shape the screen works in."""
    return [(date.fromisoformat(d), c) for d, c in pairs]


def test_month_end_dates_are_the_last_trading_day_of_each_month():
    """Month ends come from the dates actually present, so a holiday or a weekend at the end of a
    month simply means the last trading day is earlier; nothing is invented."""
    days = _series([("2021-01-28", 1.0), ("2021-01-29", 1.0),      # 30th, 31st are a weekend
                    ("2021-02-01", 1.0), ("2021-02-26", 1.0),      # Feb ends on a Friday
                    ("2021-03-01", 1.0), ("2021-03-31", 1.0)])
    assert ms.month_ends([d for d, _ in days]) == [date(2021, 1, 29), date(2021, 2, 26),
                                                    date(2021, 3, 31)]


def test_month_end_dates_ignore_a_partial_final_month():
    """A month whose data stops mid-month still yields its last available day; the caller drops the
    final rank date instead, because there is no full month to hold after it."""
    days = [date(2021, 1, 29), date(2021, 2, 26), date(2021, 3, 5)]
    assert ms.month_ends(days) == [date(2021, 1, 29), date(2021, 2, 26), date(2021, 3, 5)]


def test_momentum_score_skips_the_most_recent_month():
    """12-1: the return from 13 months before the rank date to 1 month before it. The most recent
    month is skipped because short-horizon reversal contaminates it and would fight the signal.
    Here the stock doubles over the twelve months to m-1 and then halves in the final month; the
    score must be +1.0, untouched by the halving."""
    closes = {date(2020, 1, 31): 100.0, date(2021, 1, 29): 200.0, date(2021, 2, 26): 100.0}
    assert ms.momentum_score(closes, date(2021, 2, 26), date(2021, 1, 29),
                             date(2020, 1, 31)) == pytest.approx(1.0)


def test_momentum_score_is_none_when_a_leg_is_missing():
    closes = {date(2021, 1, 29): 200.0}
    assert ms.momentum_score(closes, date(2021, 2, 26), date(2021, 1, 29), date(2020, 1, 31)) is None
    zero = {date(2020, 1, 31): 0.0, date(2021, 1, 29): 200.0}
    assert ms.momentum_score(zero, date(2021, 2, 26), date(2021, 1, 29), date(2020, 1, 31)) is None


def test_load_closes_reads_daily_candles_only(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL, h REAL,"
                 " l REAL, c REAL, v INTEGER, source TEXT)")
    conn.execute("INSERT INTO candles VALUES ('A', 1577836800, 1440, 1, 2, 0.5, 1.5, 0, 'official')")
    conn.execute("INSERT INTO candles VALUES ('A', 1577923200, 1440, 2, 3, 1.5, 2.5, 0, 'official')")
    conn.execute("INSERT INTO candles VALUES ('A', 1577836800, 5, 9, 9, 9, 9, 0, 'official')")
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    closes, bars = ms.load_closes(ro, ["A", "MISSING"])
    assert list(closes) == ["A"] and len(closes["A"]) == 2
    assert closes["A"][date(2020, 1, 1)] == 1.5
    assert len(bars["A"]) == 2 and bars["A"][0].open == 1      # bars kept for the gap mask only
