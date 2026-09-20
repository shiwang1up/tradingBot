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


def test_eligibility_needs_all_three_closes_and_a_clean_lookback():
    """A symbol qualifies for a rank date only if it has closes at m-13, m-1 and m, and no
    corporate action anywhere in [m-13, m]: a split inside the lookback makes the score garbage."""
    rank, skip, start = date(2021, 2, 26), date(2021, 1, 29), date(2020, 1, 31)
    closes = {"OK": {start: 100.0, skip: 150.0, rank: 160.0},
              "SHORT": {skip: 150.0, rank: 160.0},                    # no m-13 close
              "SPLIT": {start: 100.0, skip: 150.0, rank: 160.0}}
    masked = {"OK": set(), "SHORT": set(), "SPLIT": {date(2020, 6, 15)}}   # inside the lookback
    got = ms.eligible(closes, masked, rank, skip, start)
    assert sorted(got) == ["OK"]


def test_eligibility_ignores_a_corporate_action_outside_the_lookback():
    rank, skip, start = date(2021, 2, 26), date(2021, 1, 29), date(2020, 1, 31)
    closes = {"A": {start: 100.0, skip: 150.0, rank: 160.0}}
    assert ms.eligible(closes, {"A": {date(2019, 6, 15)}}, rank, skip, start) == ["A"]
    assert ms.eligible(closes, {"A": {date(2021, 6, 15)}}, rank, skip, start) == ["A"]


def test_next_trading_day_is_strictly_after_the_rank_date():
    """Entering on the rank date itself would buy at a price used to rank the name."""
    days = [date(2021, 1, 29), date(2021, 2, 1), date(2021, 2, 26)]
    assert ms.next_trading_day(days, date(2021, 1, 29)) == date(2021, 2, 1)
    assert ms.next_trading_day(days, date(2021, 2, 26)) is None


def test_turnover_cost_is_charged_only_on_the_names_that_changed():
    """Ten names held, four replaced: 40% of a round trip, not a whole one. Holding the identical
    basket costs nothing; replacing every name costs a full round trip."""
    cost = 0.006
    assert ms.turnover_cost(set("ABCDEFGHIJ"), set("ABCDEFGHIJ"), cost) == pytest.approx(0.0)
    assert ms.turnover_cost(set("ABCDEFGHIJ"), set("ABCDEFWXYZ"), cost) == pytest.approx(0.4 * cost)
    assert ms.turnover_cost(set("ABCDEFGHIJ"), set("KLMNOPQRST"), cost) == pytest.approx(cost)
    assert ms.turnover_cost(set(), set("ABCDEFGHIJ"), cost) == pytest.approx(cost)  # first month


def test_basket_return_is_the_equal_weight_mean_of_its_names():
    """A 10% and a 30% name held equally return 20% before costs."""
    closes = {"A": {date(2021, 2, 1): 100.0, date(2021, 3, 1): 110.0},
              "B": {date(2021, 2, 1): 50.0, date(2021, 3, 1): 65.0}}
    r = ms.basket_return(closes, ["A", "B"], date(2021, 2, 1), date(2021, 3, 1))
    assert r == pytest.approx(0.20)


def test_basket_return_is_none_when_a_name_lacks_an_exit_close():
    closes = {"A": {date(2021, 2, 1): 100.0, date(2021, 3, 1): 110.0},
              "B": {date(2021, 2, 1): 50.0}}
    assert ms.basket_return(closes, ["A", "B"], date(2021, 2, 1), date(2021, 3, 1)) is None
