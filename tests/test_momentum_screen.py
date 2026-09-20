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
    got = ms.eligible(closes, masked, rank, skip, start, date(2021, 4, 1))
    assert sorted(got) == ["OK"]


def test_eligibility_ignores_a_corporate_action_outside_the_lookback():
    rank, skip, start = date(2021, 2, 26), date(2021, 1, 29), date(2020, 1, 31)
    closes = {"A": {start: 100.0, skip: 150.0, rank: 160.0}}
    assert ms.eligible(closes, {"A": {date(2019, 6, 15)}}, rank, skip, start,
                       date(2021, 4, 1)) == ["A"]
    assert ms.eligible(closes, {"A": {date(2021, 6, 15)}}, rank, skip, start,
                       date(2021, 4, 1)) == ["A"]


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


def _toy_closes():
    """Four symbols, 26 month ends from 2020-01-28. WINNER compounds up, LOSER down, FLAT1 and
    FLAT2 do nothing. Month ends are the 28th so every month has one, and the next trading day is
    the 1st of the following month, which is where entries and exits land."""
    out = {}
    ends, nexts = [], []
    for k in range(26):
        y, mth = 2020 + (k // 12), (k % 12) + 1
        ends.append(date(y, mth, 28))
        ny, nm = (y + 1, 1) if mth == 12 else (y, mth + 1)
        nexts.append(date(ny, nm, 1))
    for sym, path in (("WINNER", lambda i: 100.0 * (1 + 0.06) ** i),
                      ("LOSER", lambda i: 100.0 * (1 - 0.05) ** i),
                      ("FLAT1", lambda i: 100.0),
                      ("FLAT2", lambda i: 100.0)):
        by = {}
        for i, (e, n) in enumerate(zip(ends, nexts)):
            by[e] = path(i)
            # The entry/exit day must NOT equal the month-end close, or a test cannot tell
            # entering on the rank date from entering the day after it.
            by[n] = path(i) * 1.01
        out[sym] = by
    return out


def _toy_masked():
    return {s: set() for s in ("WINNER", "LOSER", "FLAT1", "FLAT2")}


def test_t_of_a_monthly_series_is_the_sample_t():
    """Three months of spread +2%, -1%, +2%: mean 1%, sample sd 1.7320508%, se 1%, t 1.0."""
    t = ms.series_t([0.02, -0.01, 0.02])
    assert t == pytest.approx(1.0, abs=1e-9)
    assert ms.series_t([0.01]) is None                  # one month says nothing
    assert ms.series_t([0.01, 0.01, 0.01]) is None      # no variance


def test_run_months_ranks_holds_and_charges():
    """Four symbols over enough months to rank once. WINNER compounded up over the lookback and
    LOSER down; with TOP_N forced to 1 the portfolio is WINNER alone and the baseline is all four."""
    months = ms.run_months(_toy_closes(), _toy_masked(), top_n=1, cost=0.0,
                            lookback=12, skip=1)
    assert months, "the toy data must produce at least one rebalance"
    m = months[0]
    assert m["held"] == ["WINNER"]
    assert m["port"] == pytest.approx(m["port_gross"])          # cost 0 -> gross == net
    assert m["base"] is not None and m["n_eligible"] == 4


def test_run_months_charges_the_first_month_a_full_round_trip():
    months = ms.run_months(_toy_closes(), _toy_masked(), top_n=1, cost=0.006,
                            lookback=12, skip=1)
    assert months[0]["port"] == pytest.approx(months[0]["port_gross"] - 0.006)


def test_quintile_split_handles_a_count_not_divisible_by_five():
    """12 names into 5 groups: the remainder goes to the top groups, and every name lands in
    exactly one group. Group 0 is the highest-ranked."""
    groups = ms.split_quintiles(list("ABCDEFGHIJKL"), n_groups=5)
    assert [len(g) for g in groups] == [3, 3, 2, 2, 2]
    assert sorted(sum(groups, [])) == sorted(list("ABCDEFGHIJKL"))
    assert groups[0] == ["A", "B", "C"]


def test_quintile_split_returns_empty_groups_when_there_are_too_few_names():
    groups = ms.split_quintiles(list("AB"), n_groups=5)
    assert [len(g) for g in groups] == [1, 1, 0, 0, 0]


def test_summarise_reports_the_spread_its_t_and_drawdown():
    """Two months of portfolio +3%, -1% against a baseline +1%, +1%: spread +2%, -2%, mean 0."""
    months = [dict(port=0.03, base=0.01, spread=0.02, turnover=4, n_eligible=40),
              dict(port=-0.01, base=0.01, spread=-0.02, turnover=2, n_eligible=40)]
    s = ms.summarise(months)
    assert s["months"] == 2
    assert s["mean_spread"] == pytest.approx(0.0)
    assert s["mean_port"] == pytest.approx(0.01) and s["mean_base"] == pytest.approx(0.01)
    assert s["cum_port"] == pytest.approx(1.03 * 0.99 - 1)
    assert s["max_dd_port"] == pytest.approx(0.01)          # +3% then -1%: a 1% drawdown from peak
    assert s["mean_turnover"] == pytest.approx(3.0)
    assert s["mean_eligible"] == pytest.approx(40.0)


def test_portfolio_cost_falls_as_each_position_gets_larger():
    """1 lakh across 10 names is 10,000 a position; across 5 it is 20,000, which is cheaper per
    rupee because the 20 rupee brokerage floor and the flat DP charge are spread wider. Charging
    the daily screen's 25,000 position would understate every cell."""
    assert ms.portfolio_cost(5) < ms.portfolio_cost(10) < ms.portfolio_cost(20)
    assert ms.portfolio_cost(10) == pytest.approx(ms.round_trip_cost(10000.0))


def test_run_phase_prints_the_primary_the_grid_and_the_caveats(tmp_path):
    """Shape, not numbers: a tiny four-symbol database must still produce a primary block, the
    secondary grid and the caveat text, and must never crash on a universe smaller than TOP_N."""
    import io
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL, h REAL,"
                 " l REAL, c REAL, v INTEGER, source TEXT)")
    base = 1577836800                                   # 2020-01-01 00:00 UTC
    for si, sym in enumerate(("A", "B", "C", "D")):
        for k in range(800):                            # ~2.2 years of consecutive days
            c = 100.0 * (1.0 + 0.0005 * (si + 1)) ** k
            conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,0,'official')",
                         (sym, base + k * 86400, 1440, c, c, c, c))
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    buf = io.StringIO()
    ms.run_phase(ro, ["A", "B", "C", "D"], top_n=2, out=buf)
    text = buf.getvalue()
    assert "primary" in text and "spread" in text and "Caveats" in text
    assert "12-1" in text


def test_quintile_months_are_dropped_for_every_group_or_none():
    """A month where any group fails to price is dropped from ALL groups. Otherwise the
    top-minus-bottom figure subtracts two means taken over different months, which is not a
    comparison. Two such months exist in the real data, both in late 2025."""
    kept, dropped = ms.quintile_months([[0.01, 0.02, 0.03, 0.04, 0.05],
                                        [0.01, 0.02, None, 0.04, 0.05],
                                        [0.02, 0.03, 0.04, 0.05, 0.06]])
    assert dropped == 1
    assert kept == [[0.01, 0.02, 0.03, 0.04, 0.05], [0.02, 0.03, 0.04, 0.05, 0.06]]


def test_run_months_enters_after_the_rank_date_and_exits_after_the_next_one():
    """Pins the dates, not just the helper. With the entry day priced differently from the month
    end, entering on the rank date itself would change the returns and this test would fail."""
    months = ms.run_months(_toy_closes(), _toy_masked(), top_n=1, cost=0.0, lookback=12, skip=1)
    m = months[0]
    assert m["entry"] > m["rank_date"]
    assert m["exit"] > months[1]["rank_date"] if len(months) > 1 else m["exit"] > m["entry"]
    assert m["entry"].day == 1 and m["rank_date"].day == 28


def test_run_months_baseline_covers_exactly_the_eligible_set():
    """The baseline must be the eligible names, not every symbol in the dict. A baseline over a
    different set is not a comparison, and the spec requires this test by name."""
    closes = _toy_closes()
    closes["NOHIST"] = {date(2021, 3, 1): 100.0, date(2021, 4, 1): 200.0}   # ineligible, huge return
    masked = _toy_masked()
    masked["NOHIST"] = set()
    months = ms.run_months(closes, masked, top_n=1, cost=0.0, lookback=12, skip=1)
    assert months[0]["n_eligible"] == 4              # NOHIST excluded, not 5
    clean = ms.run_months(_toy_closes(), _toy_masked(), top_n=1, cost=0.0, lookback=12, skip=1)
    assert months[0]["base"] == pytest.approx(clean[0]["base"])


def test_eligible_rejects_a_corporate_action_during_the_month_actually_held():
    """The hold runs from the day after the rank date to the day after the next month end, so a
    split in that window prices as a real -50% return. The guard must cover it."""
    rank, skip, start = date(2021, 2, 26), date(2021, 1, 29), date(2020, 1, 31)
    exit_ = date(2021, 4, 1)
    closes = {"A": {start: 100.0, skip: 150.0, rank: 160.0}}
    assert ms.eligible(closes, {"A": set()}, rank, skip, start, exit_) == ["A"]
    # inside the hold, after the rank date: previously invisible
    assert ms.eligible(closes, {"A": {date(2021, 3, 15)}}, rank, skip, start, exit_) == []
    # exactly on the exit date, and exactly on the start date: both disqualify
    assert ms.eligible(closes, {"A": {exit_}}, rank, skip, start, exit_) == []
    assert ms.eligible(closes, {"A": {start}}, rank, skip, start, exit_) == []
    # outside the whole window: fine
    assert ms.eligible(closes, {"A": {date(2021, 4, 2)}}, rank, skip, start, exit_) == ["A"]


def test_quintiles_rank_highest_first():
    """A sign flip in quintiles_phase alone was not caught by any test."""
    ranked = ["HIGH", "MID", "LOW"]
    assert ms.split_quintiles(ranked, n_groups=3)[0] == ["HIGH"]
