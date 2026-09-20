# Cross-Sectional Momentum Screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether ranking the 50 universe stocks by 12-1 month return and equal-weighting the top 10, rebalanced monthly, beats equal-weighting all of them — net of real delivery costs, over 2021-2025.

**Architecture:** A new self-contained research script, `scripts/momentum_screen.py`, working at PORTFOLIO level (monthly returns of a basket) rather than the trade level of `scripts/daily_screen.py`, so it does not reuse that script's trade simulation. It reads the database read-only and uses CLOSES ONLY, because Groww's daily `open` field is synthetic for 2025. One change lands in `scripts/daily_screen.py`: a second corporate-action detector for splits that appear inside a bar.

**Tech Stack:** Python 3.9 (the scripts have NO `from __future__ import annotations`; keep annotations 3.9-safe or omit them, matching `daily_screen.py`), SQLite read-only, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-cross-sectional-momentum-design.md`. Read it first. The construction, the pass bar and the caveats were fixed before any momentum number existed, and must not be changed in response to a result.

**Branch:** `dev-nonprice-signals` (already checked out, off `main` at `513fed5`). EXPERIMENTAL: nothing reaches `main`.

**Baseline:** `.venv/bin/pytest -q` from the repo root gives `598 passed`.

**Commit trailer:** end every commit message with this line, after a blank line:
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

**Data rules, absolute:**
- Read `data/tradebot.db` ONLY via `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`. Never write under `data/`.
- Do NOT run `tradebot backtest`, `tradebot paper`, `tradebot fetch-data`, `scripts/orb_experiment.py`, or `scripts/daily_screen.py` in ANY phase.
- Two holdout windows are deliberately unspent and must stay so: the ORB window 2026-08-16..2026-09-15 and the daily-screen window 2024-01-01..2025-11-21. The momentum screen legitimately spans 2024-2025 — that is a DIFFERENT experiment on the same calendar dates, and is allowed by the spec. What is forbidden is running `daily_screen.py holdout`.

**What is already in the database (established read-only; do not re-derive):** 50 symbols at interval 1440, 71,654 rows, 2020-01-01..2025-12-05. Groww's daily `open` is synthetic for 2025 (98.6% of bars have `open == previous close`, against 4-7% in 2020-2024), which is why this screen uses closes only. Three symbols have legitimately short histories: SHRIRAMFIN from 2022-12-20 (merger), TATACONSUM from 2020-02-27 (rename), TATAMOTORS to 2025-10-23 (demerger).

## File map

| File | Change | Responsibility |
|---|---|---|
| `scripts/daily_screen.py` | modify | `gap_mask` gains an inside-the-bar detector |
| `tests/test_daily_screen.py` | modify | tests for the new detector |
| `scripts/momentum_screen.py` | create | the whole screen: data, month ends, scores, eligibility, portfolios, costs, statistics, output |
| `tests/test_momentum_screen.py` | create | its unit tests |
| `docs/superpowers/notes/2026-09-20-cross-sectional-momentum-results.md` | create | results |

---

### Task 1: catch corporate actions that happen inside a bar

**Files:**
- Modify: `scripts/daily_screen.py` (`gap_mask` and the constants above it)
- Test: `tests/test_daily_screen.py`

`gap_mask` compares each bar's open to the previous close. That finds a split only when the open is real. For 2025 the open is the previous close carried forward, so a split shows up as a huge move INSIDE the bar and the mask is blind to it. Five such events exist in the data, all in 2025.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daily_screen.py` (the file already has a `_bars(rows)` helper taking `(iso date, open, high, low, close)` tuples — read it first and reuse it):

```python
def test_gap_mask_catches_a_split_that_happens_inside_the_bar():
    """Groww's 2025 daily bars carry the previous close forward as the open, so a split shows up
    as close/open, not as an overnight gap. Bar 5 opens at the pre-split price and closes at half
    it; its open equals bar 4's close, so the overnight detector sees nothing."""
    rows = [("2020-01-%02d" % d, 100.0, 101.0, 99.0, 100.0) for d in range(1, 10)]
    rows[5] = ("2020-01-06", 100.0, 101.0, 49.0, 50.0)
    masked = ds.gap_mask(_bars(rows), mask_days=3)
    assert date(2020, 1, 6) in masked
    assert date(2020, 1, 9) in masked          # the days after are masked too
    assert date(2020, 1, 5) not in masked


def test_gap_mask_does_not_mask_the_largest_genuine_intraday_move_in_the_data():
    """INDUSINDBK rebounded +37.8% intraday on 2020-03-26; ADANIENT fell 33.4% on 2023-02-02.
    Both are real. The 30% threshold sits above them and below the smallest real split (-40.2%)."""
    up = [("2020-03-%02d" % d, 100.0, 101.0, 99.0, 100.0) for d in (24, 25, 26, 27)]
    up[2] = ("2020-03-26", 100.0, 138.0, 99.0, 137.8)       # +37.8% intraday, no overnight gap
    assert ds.gap_mask(_bars(up), mask_days=3) == set()
    down = [("2023-02-%02d" % d, 100.0, 101.0, 99.0, 100.0) for d in (1, 2, 3, 6)]
    down[1] = ("2023-02-02", 100.0, 101.0, 66.0, 66.6)      # -33.4% intraday
    assert ds.gap_mask(_bars(down), mask_days=3) == set()


def test_gap_mask_still_catches_an_overnight_gap():
    """The original detector must keep working: 2020-2024 splits appear at the open."""
    rows = [("2020-01-%02d" % d, 100.0, 101.0, 99.0, 100.0) for d in range(1, 6)]
    rows[3] = ("2020-01-04", 50.0, 51.0, 49.0, 50.0)
    assert date(2020, 1, 4) in ds.gap_mask(_bars(rows), mask_days=2)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q -k gap_mask`
Expected: `test_gap_mask_catches_a_split_that_happens_inside_the_bar` FAILS (the date is not masked). The other two pass already.

- [ ] **Step 3: Implement**

In `scripts/daily_screen.py`, beside `GAP_THRESHOLD`, add:

```python
INTRABAR_THRESHOLD = 0.30               # a close this far from its own open is a corporate action,
                                        # not a market move: the largest genuine intraday move in
                                        # six years of this universe is +37.8% (INDUSINDBK,
                                        # 2020-03-26), and the smallest real split here is -40.2%
```

Replace `gap_mask` with:

```python
def gap_mask(bars, threshold=GAP_THRESHOLD, mask_days=GAP_MASK_DAYS,
             intrabar=INTRABAR_THRESHOLD):
    """Dates unusable because of an unadjusted corporate action, plus the `mask_days` trading days
    after each. An unadjusted split reads as a crash, which would manufacture mean-reversion
    entries and poison every long average while it sits in the window.

    Two detectors, because the data has two shapes. Before 2025 Groww's daily open is real, so a
    split appears as an overnight jump from the previous close. For 2025 the open is the previous
    close carried forward, so the split appears INSIDE the bar as a huge close-to-open move and the
    overnight detector is blind to it. The intrabar threshold is deliberately loose: over-masking
    costs data, under-masking manufactures a -90% return, which is far worse."""
    masked = set()
    for i, bar in enumerate(bars):
        hit = False
        if i > 0 and bars[i - 1].close > 0:
            hit = abs(bar.open / bars[i - 1].close - 1.0) > threshold
        if not hit and bar.open > 0:
            hit = abs(bar.close / bar.open - 1.0) > intrabar
        if hit:
            for k in range(i, min(i + mask_days + 1, len(bars))):
                masked.add(bars[k].date)
    return masked
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: `601 passed`. The daily screen's own tests must still pass unchanged.

- [ ] **Step 5: Confirm the in-sample result did not move**

The daily screen's in-sample window has no inside-the-bar events, so its committed table must be unchanged. You are NOT allowed to run `daily_screen.py`. Instead assert it from the data, read-only:

```bash
.venv/bin/python - <<'EOF'
import sqlite3, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("ds", Path("scripts/daily_screen.py"))
ds = importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)
conn = sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)
import yaml
syms = yaml.safe_load(open("universe.yaml"))["symbols"]
series = ds.load_series(conn, syms)
added = []
for s, bars in series.items():
    for i, b in enumerate(bars):
        if i and bars[i-1].close > 0 and abs(b.open/bars[i-1].close - 1) > 0.20:
            continue
        if b.open > 0 and abs(b.close/b.open - 1) > 0.30:
            added.append((s, b.date.isoformat()))
print("dates newly masked by the intrabar detector:")
for s, d in sorted(added, key=lambda x: x[1]): print("  ", d, s)
print("any inside the daily screen's in-sample window 2020-01-01..2023-12-31:",
      [x for x in added if x[1] <= "2023-12-31"] or "none")
EOF
```
Expected: five dates, all in 2025, and `none` inside the in-sample window. Paste the output in your report. If ANY date falls on or before 2023-12-31, STOP: the daily screen's committed results would need regenerating, which is outside this plan.

- [ ] **Step 6: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "scripts(daily): detect a split that happens inside the bar, not just at the open"
```

### Task 2: load closes, find month ends, score 12-1 momentum

**Files:**
- Create: `scripts/momentum_screen.py`
- Test: `tests/test_momentum_screen.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_momentum_screen.py`:

```python
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
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_momentum_screen.py -q`
Expected: `FileNotFoundError` / `ModuleNotFoundError` for `scripts/momentum_screen.py`.

- [ ] **Step 3: Implement**

Create `scripts/momentum_screen.py`:

```python
"""Does ranking these 50 stocks against EACH OTHER predict, where ranking a stock against its own
past did not?

    .venv/bin/python scripts/momentum_screen.py run         # the primary test and the secondary grid
    .venv/bin/python scripts/momentum_screen.py quintiles   # the five ranked groups, monthly means

Cross-sectional momentum: at each month end rank every eligible symbol by its return over the
twelve months ending ONE month earlier (the skipped month is standard; short-horizon reversal
contaminates it), equal-weight the top 10, hold one month, rebalance. The comparator is
equal-weighting ALL eligible symbols over the same month, because the claim being tested is that
the top group beats the average stock -- not that it beats zero, which in a market that doubled
would prove nothing.

CLOSES ONLY. Groww's daily `open` field is synthetic for 2025 (98.6% of bars carry the previous
close forward), so any fill at an open would be fictional over half the window. Ranking on a
month-end close and entering at the NEXT day's close is also the standard construction.

Costs are charged on TURNOVER: if k of the 10 names change at a rebalance, the month pays
k/10 of a round trip. The baseline pays on its own turnover, which is near zero. That asymmetry is
real -- momentum's cost disadvantage against buy-and-hold is part of what is being measured.

Survivorship warning, stated here because it is the largest threat to any positive result:
universe.yaml is TODAY'S index. Stocks added during the window because they rose are present with
the full history of the rise that earned them a place, which manufactures momentum. Stocks dropped
after falling are absent, which flatters the bottom of the ranking. A pass means "worth
investigating with point-in-time data", never "proven".

This reads the database read-only and writes nothing to it.
"""
import argparse
import importlib.util
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

DB = "data/tradebot.db"
INTERVAL = 1440
IST_OFFSET = 19800                      # daily bars are stamped 00:00 IST
FIRST_RANK_MONTH = (2021, 1)            # the first month with 13 months of history behind it
TOP_N = 10                              # a quintile of 50; five names is too concentrated at 1 lakh
LOOKBACK_MONTHS = 12                    # the "12" of 12-1
SKIP_MONTHS = 1                         # the "-1"
QUINTILES = 5

# The cost model and the corporate-action mask are shared with the daily screen rather than
# duplicated: one definition of what a round trip costs, one definition of a split.
_DS = Path(__file__).resolve().parent / "daily_screen.py"
_spec = importlib.util.spec_from_file_location("daily_screen", _DS)
daily_screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_screen)
round_trip_cost = daily_screen.round_trip_cost
gap_mask = daily_screen.gap_mask
Bar = daily_screen.Bar


def load_closes(conn, symbols):
    """({symbol: {date: close}}, {symbol: [Bar]}) from daily candles. The bars are kept only so the
    corporate-action mask can look at opens; every return in this screen uses closes."""
    closes, bars = {}, {}
    for sym in symbols:
        rows = conn.execute(
            "SELECT ts, o, h, l, c FROM candles WHERE symbol=? AND interval=? ORDER BY ts",
            (sym, INTERVAL)).fetchall()
        if not rows:
            continue
        b = [Bar(date.fromtimestamp(r[0] + IST_OFFSET), r[1], r[2], r[3], r[4]) for r in rows]
        bars[sym] = b
        closes[sym] = {x.date: x.close for x in b}
    return closes, bars


def month_ends(dates):
    """The last available trading day of each calendar month, in order. Derived from the dates that
    exist, so a holiday or weekend at a month boundary just moves the month end earlier."""
    last = {}
    for d in dates:
        last[(d.year, d.month)] = max(d, last.get((d.year, d.month), d))
    return [last[k] for k in sorted(last)]


def momentum_score(closes_for_symbol, rank_date, skip_date, start_date):
    """12-1 momentum: the return from `start_date` to `skip_date`, i.e. the twelve months ending one
    month before the rank date. `rank_date` is accepted so the caller's intent is readable and so a
    symbol missing its rank-date close is rejected here rather than later."""
    a = closes_for_symbol.get(start_date)
    b = closes_for_symbol.get(skip_date)
    if a is None or b is None or closes_for_symbol.get(rank_date) is None or a <= 0:
        return None
    return b / a - 1.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["run", "quintiles"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/test_momentum_screen.py -q` → `5 passed`. Then `.venv/bin/pytest -q` → `606 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/momentum_screen.py tests/test_momentum_screen.py
git commit -m "scripts(momentum): closes, month ends and the 12-1 score"
```

### Task 3: eligibility, the monthly rebalance and turnover costs

**Files:**
- Modify: `scripts/momentum_screen.py`
- Test: `tests/test_momentum_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_momentum_screen.py`:

```python
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
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_momentum_screen.py -q` → `AttributeError: module 'momentum_screen' has no attribute 'eligible'`.

- [ ] **Step 3: Implement**

Add to `scripts/momentum_screen.py`, after `momentum_score`:

```python
def eligible(closes, masked, rank_date, skip_date, start_date):
    """Symbols that may be ranked at `rank_date`, sorted for determinism. A symbol qualifies only
    if it has closes at all three legs and no corporate action anywhere in [start_date, rank_date]:
    an unadjusted split inside the lookback makes the score meaningless, and one at the rank date
    makes the entry price meaningless. The same list feeds BOTH the ranked portfolio and the
    baseline, so the two always compare the same candidate set."""
    out = []
    for sym, by_date in closes.items():
        if momentum_score(by_date, rank_date, skip_date, start_date) is None:
            continue
        if any(start_date <= d <= rank_date for d in masked.get(sym, ())):
            continue
        out.append(sym)
    return sorted(out)


def next_trading_day(all_dates, after):
    """The first trading day strictly after `after`, or None. Entering on the rank date itself
    would buy at a price that was used to rank the name."""
    for d in all_dates:
        if d > after:
            return d
    return None


def turnover_cost(held, target, cost):
    """The month's cost as a fraction of the portfolio: the share of names replaced, times a round
    trip. Selling one name and buying another is one round trip between them, so the share that
    changed is the right multiplier. An unchanged basket costs nothing; the first month costs a
    full round trip because everything is bought."""
    if not target:
        return 0.0
    changed = len(set(target) - set(held))
    return cost * changed / float(len(target))


def basket_return(closes, names, entry_date, exit_date):
    """Equal-weight return of `names` from `entry_date`'s close to `exit_date`'s close, before
    costs. None if any name lacks either close, because a silently smaller basket would not be the
    portfolio the ranking chose."""
    if not names:
        return None
    rs = []
    for sym in names:
        a = closes[sym].get(entry_date)
        b = closes[sym].get(exit_date)
        if a is None or b is None or a <= 0:
            return None
        rs.append(b / a - 1.0)
    return sum(rs) / len(rs)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `612 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/momentum_screen.py tests/test_momentum_screen.py
git commit -m "scripts(momentum): eligibility, the next-day entry, turnover costing and basket returns"
```

### Task 4: the monthly loop, the spread and its t

**Files:**
- Modify: `scripts/momentum_screen.py`
- Test: `tests/test_momentum_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_momentum_screen.py`:

```python
import math


def test_t_of_a_monthly_series_is_the_sample_t():
    """Three months of spread +2%, -1%, +2%: mean 1%, sample sd 1.7320508%, se 1%, t 1.0."""
    t = ms.series_t([0.02, -0.01, 0.02])
    assert t == pytest.approx(1.0, abs=1e-9)
    assert ms.series_t([0.01]) is None                  # one month says nothing
    assert ms.series_t([0.01, 0.01, 0.01]) is None      # no variance


def test_run_months_ranks_holds_and_charges(tmp_path):
    """Four symbols over enough months to rank once. WINNER doubled over the lookback, LOSER
    halved; with TOP_N forced to 1 the portfolio is WINNER alone and the baseline is all four."""
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
```

Add this fixture helper above those tests. It builds 26 month-end closes for four symbols so there is a 13-month lookback plus months to hold:

```python
def _toy_closes():
    """Four symbols, 26 month ends from 2020-01-31. WINNER doubles over the first 12 months then
    drifts; LOSER halves; FLAT1 and FLAT2 do nothing. Month ends are the 28th so every month has
    one, and the next trading day is the 1st of the following month."""
    import datetime
    out = {}
    ends, nexts = [], []
    for k in range(26):
        y, mth = 2020 + (k // 12), (k % 12) + 1
        ends.append(datetime.date(y, mth, 28))
        ny, nm = (y + 1, 1) if mth == 12 else (y, mth + 1)
        nexts.append(datetime.date(ny, nm, 1))
    for sym, path in (("WINNER", lambda i: 100.0 * (1 + 0.06) ** i),
                      ("LOSER", lambda i: 100.0 * (1 - 0.05) ** i),
                      ("FLAT1", lambda i: 100.0),
                      ("FLAT2", lambda i: 100.0)):
        by = {}
        for i, (e, n) in enumerate(zip(ends, nexts)):
            by[e] = path(i)
            by[n] = path(i)          # the entry day carries the same level; returns come from the path
        out[sym] = by
    return out


def _toy_masked():
    return {s: set() for s in ("WINNER", "LOSER", "FLAT1", "FLAT2")}
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_momentum_screen.py -q` → `AttributeError: ... 'series_t'`.

- [ ] **Step 3: Implement**

Add to `scripts/momentum_screen.py`, after `basket_return`:

```python
def series_t(values):
    """t of the mean of a monthly series (sample variance, n-1). None for fewer than two months or
    no variance: either says nothing about whether the mean differs from zero."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def run_months(closes, masked, top_n=TOP_N, cost=None, lookback=LOOKBACK_MONTHS, skip=SKIP_MONTHS):
    """One dict per rebalance: the names held, the portfolio's gross and net return, the
    equal-weight baseline's, and the spread.

    A rank date m needs a close at m, at m-skip and at m-(lookback+skip); the portfolio is entered
    at the first trading day AFTER m and exited at the first trading day after the NEXT rank date,
    so no position is bought at a price used to rank it. The baseline holds every eligible name for
    exactly the same dates, and is charged on its own turnover, which is near zero -- momentum's
    cost disadvantage against buy-and-hold is part of what is being measured, not an artefact to
    remove."""
    if cost is None:
        cost = round_trip_cost()
    all_dates = sorted({d for by in closes.values() for d in by})
    ends = month_ends(all_dates)
    out, held, base_held = [], [], []
    for i, rank_date in enumerate(ends):
        j_skip, j_start = i - skip, i - (lookback + skip)
        if j_start < 0 or i + 1 >= len(ends):
            continue
        entry = next_trading_day(all_dates, rank_date)
        exit_ = next_trading_day(all_dates, ends[i + 1])
        if entry is None or exit_ is None:
            continue
        names = eligible(closes, masked, rank_date, ends[j_skip], ends[j_start])
        if len(names) < top_n:
            continue
        ranked = sorted(names, key=lambda s: momentum_score(
            closes[s], rank_date, ends[j_skip], ends[j_start]), reverse=True)
        target = ranked[:top_n]
        port_gross = basket_return(closes, target, entry, exit_)
        base_gross = basket_return(closes, names, entry, exit_)
        if port_gross is None or base_gross is None:
            continue
        port = port_gross - turnover_cost(held, target, cost)
        base = base_gross - turnover_cost(base_held, names, cost)
        out.append(dict(rank_date=rank_date, entry=entry, exit=exit_, held=target,
                        n_eligible=len(names), port_gross=port_gross, port=port,
                        base_gross=base_gross, base=base, spread=port - base,
                        turnover=len(set(target) - set(held))))
        held, base_held = target, names
    return out
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `616 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/momentum_screen.py tests/test_momentum_screen.py
git commit -m "scripts(momentum): the monthly rebalance loop, the spread and its t"
```

### Task 5: quintiles, the report and the run

**Files:**
- Modify: `scripts/momentum_screen.py`
- Test: `tests/test_momentum_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_momentum_screen.py`:

```python
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
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_momentum_screen.py -q` → `AttributeError: ... 'split_quintiles'`.

- [ ] **Step 3: Implement**

Add to `scripts/momentum_screen.py`:

```python
def split_quintiles(ranked, n_groups=QUINTILES):
    """`ranked` highest first, split into `n_groups` groups with any remainder given to the top
    groups. Every name lands in exactly one group, so the groups together are the baseline."""
    n = len(ranked)
    base, extra = divmod(n, n_groups)
    groups, k = [], 0
    for g in range(n_groups):
        size = base + (1 if g < extra else 0)
        groups.append(ranked[k:k + size])
        k += size
    return groups


def _max_drawdown(returns):
    """Largest peak-to-trough fall of the cumulative curve, as a positive fraction."""
    cum, peak, dd = 1.0, 1.0, 0.0
    for r in returns:
        cum *= (1.0 + r)
        peak = max(peak, cum)
        dd = max(dd, (peak - cum) / peak)
    return dd


def summarise(months):
    """Headline figures over the monthly series."""
    n = len(months)
    if n == 0:
        return dict(months=0, mean_port=0.0, mean_base=0.0, mean_spread=0.0, t=None,
                    cum_port=0.0, cum_base=0.0, max_dd_port=0.0, max_dd_base=0.0,
                    mean_turnover=0.0, mean_eligible=0.0)
    ports = [m["port"] for m in months]
    bases = [m["base"] for m in months]
    spreads = [m["spread"] for m in months]

    def cum(rs):
        out = 1.0
        for r in rs:
            out *= (1.0 + r)
        return out - 1.0

    return dict(months=n, mean_port=sum(ports) / n, mean_base=sum(bases) / n,
                mean_spread=sum(spreads) / n, t=series_t(spreads),
                cum_port=cum(ports), cum_base=cum(bases),
                max_dd_port=_max_drawdown(ports), max_dd_base=_max_drawdown(bases),
                mean_turnover=sum(m["turnover"] for m in months) / float(n),
                mean_eligible=sum(m["n_eligible"] for m in months) / float(n))


CAVEATS = (
    "Caveats: universe.yaml is TODAY'S index, so stocks added during the window because they rose\n"
    "are present with the history of that rise (biases FOR momentum) and stocks dropped after\n"
    "falling are absent (biases against the bottom group); the net direction is unknown and could\n"
    "be material, and cannot be fixed without a point-in-time constituent list.\n"
    "Monthly observations are few for a t. The window is one long bull market plus two corrections.\n"
    "Costs assume the delivery schedule in the spec, unverified against Groww's pricing page, and\n"
    "ignore market impact. The portfolio ignores capital, lot sizes and the fact that ten equal\n"
    "positions at 1 lakh is 10,000 each."
)


def format_run(label, s, cost):
    t = "n/a" if s["t"] is None else "%.2f" % s["t"]
    return "\n".join([
        "%s   %d months" % (label, s["months"]),
        "  portfolio   mean %+.3f%%/mo   cumulative %+.1f%%   max drawdown %.1f%%"
        % (s["mean_port"] * 100, s["cum_port"] * 100, s["max_dd_port"] * 100),
        "  baseline    mean %+.3f%%/mo   cumulative %+.1f%%   max drawdown %.1f%%"
        % (s["mean_base"] * 100, s["cum_base"] * 100, s["max_dd_base"] * 100),
        "  spread      mean %+.3f%%/mo   t %s        turnover %.1f of %d names/mo (round trip %.3f%%)"
        % (s["mean_spread"] * 100, t, s["mean_turnover"], TOP_N, cost * 100),
        "  eligible    %.1f of the universe ranked per month on average"
        % s["mean_eligible"],
    ])
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `619 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/momentum_screen.py tests/test_momentum_screen.py
git commit -m "scripts(momentum): quintile split, summary statistics and the report line"
```

### Task 6: wire the phases and run the primary test

**Files:**
- Modify: `scripts/momentum_screen.py`
- Test: `tests/test_momentum_screen.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_momentum_screen.py`:

```python
def test_run_phase_prints_the_primary_the_grid_and_the_caveats(tmp_path, capsys):
    """Shape, not numbers: a tiny four-symbol database must still produce a primary block, the
    secondary grid and the caveat text, and must never crash on a universe smaller than TOP_N."""
    import io
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL, h REAL,"
                 " l REAL, c REAL, v INTEGER, source TEXT)")
    base = 1577836800                                   # 2020-01-01 00:00 UTC
    for si, sym in enumerate(("A", "B", "C", "D")):
        for k in range(800):                            # ~2.6 years of weekdays
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
```

- [ ] **Step 2: Run to see it fail**

Run: `.venv/bin/pytest tests/test_momentum_screen.py -q` → `AttributeError: ... 'run_phase'`.

- [ ] **Step 3: Implement**

Add to `scripts/momentum_screen.py`, replacing `main`:

```python
SECONDARY_LOOKBACKS = (12, 6, 3)        # the primary is 12; 6 and 3 are descriptive only
SECONDARY_TOP_NS = (5, 10, 15)          # the primary is 10


def run_phase(conn, symbols, top_n=TOP_N, out=sys.stdout):
    cost = round_trip_cost()
    closes, bars = load_closes(conn, symbols)
    masked = {s: gap_mask(b) for s, b in bars.items()}
    missing = [s for s in symbols if s not in closes]
    print("%d symbols with daily candles%s"
          % (len(closes), ("; no candles for " + ", ".join(missing)) if missing else ""), file=out)
    short = sorted((s, min(by), max(by)) for s, by in closes.items()
                   if min(by) > date(2020, 2, 1) or max(by) < date(2025, 11, 1))
    print("short or truncated histories: %s"
          % ("; ".join("%s %s..%s" % x for x in short) if short else "none"), file=out)
    print("", file=out)

    months = run_months(closes, masked, top_n=top_n, cost=cost)
    s = summarise(months)
    label = "primary  12-1 momentum, top %d, monthly" % top_n
    if months:
        label += "   %s..%s" % (months[0]["rank_date"], months[-1]["rank_date"])
    print(format_run(label, s, cost), file=out)
    print("", file=out)

    # Stability, not a holdout: the same primary split at the end of 2023.
    early = [m for m in months if m["rank_date"] < date(2024, 1, 1)]
    late = [m for m in months if m["rank_date"] >= date(2024, 1, 1)]
    for sub, name in ((early, "  sub-period 2021-2023"), (late, "  sub-period 2024-2025")):
        if sub:
            t = summarise(sub)
            tt = "n/a" if t["t"] is None else "%.2f" % t["t"]
            print("%s   %d months   spread %+.3f%%/mo   t %s"
                  % (name, t["months"], t["mean_spread"] * 100, tt), file=out)
    print("", file=out)

    print("secondary grid (descriptive only; the decision is the primary cell above)", file=out)
    print("  %-10s%s" % ("", "".join("%12s" % ("top %d" % n) for n in SECONDARY_TOP_NS)), file=out)
    for lb in SECONDARY_LOOKBACKS:
        cells = []
        for n in SECONDARY_TOP_NS:
            g = summarise(run_months(closes, masked, top_n=n, cost=cost, lookback=lb))
            gt = "n/a" if g["t"] is None else "%.1f" % g["t"]
            cells.append("%12s" % ("%+.2f%% t%s" % (g["mean_spread"] * 100, gt)))
        print("  %-10s%s" % ("%d-1" % lb, "".join(cells)), file=out)
    print("", file=out)
    print(CAVEATS, file=out)


def quintiles_phase(conn, symbols, out=sys.stdout):
    """Mean monthly return of each of the five ranked groups. Monotonic ordering from top to bottom
    is far harder to produce by chance than one significant cell, and the bottom group is a
    built-in control: if top and bottom perform alike, the ranking carries no information."""
    cost = round_trip_cost()
    closes, bars = load_closes(conn, symbols)
    masked = {s: gap_mask(b) for s, b in bars.items()}
    all_dates = sorted({d for by in closes.values() for d in by})
    ends = month_ends(all_dates)
    buckets = [[] for _ in range(QUINTILES)]
    for i, rank_date in enumerate(ends):
        j_skip, j_start = i - SKIP_MONTHS, i - (LOOKBACK_MONTHS + SKIP_MONTHS)
        if j_start < 0 or i + 1 >= len(ends):
            continue
        entry = next_trading_day(all_dates, rank_date)
        exit_ = next_trading_day(all_dates, ends[i + 1])
        if entry is None or exit_ is None:
            continue
        names = eligible(closes, masked, rank_date, ends[j_skip], ends[j_start])
        if len(names) < QUINTILES:
            continue
        ranked = sorted(names, key=lambda s: momentum_score(
            closes[s], rank_date, ends[j_skip], ends[j_start]), reverse=True)
        for g, group in enumerate(split_quintiles(ranked)):
            r = basket_return(closes, group, entry, exit_)
            if r is not None:
                buckets[g].append(r)
    print("quintiles by 12-1 momentum, gross of costs (group 1 = highest ranked)", file=out)
    print("  %-8s%10s%10s" % ("group", "months", "mean/mo"), file=out)
    for g, rs in enumerate(buckets, 1):
        m = (sum(rs) / len(rs) * 100) if rs else 0.0
        print("  %-8d%10d%9.3f%%" % (g, len(rs), m), file=out)
    spread = ((sum(buckets[0]) / len(buckets[0])) - (sum(buckets[-1]) / len(buckets[-1]))) * 100 \
        if buckets[0] and buckets[-1] else 0.0
    print("  top minus bottom: %+.3f%%/mo (gross; a ranking that carries no information gives ~0)"
          % spread, file=out)
    print("", file=out)
    print(CAVEATS, file=out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["run", "quintiles"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    import yaml
    from tradebot.config import load_config
    symbols = list(yaml.safe_load(open(load_config("config.yaml").paths.universe))["symbols"])
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    try:
        if a.phase == "run":
            run_phase(conn, symbols)
        else:
            quintiles_phase(conn, symbols)
    finally:
        conn.close()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q` → `620 passed`.

- [ ] **Step 5: Run the screen and STOP**

Run both, and paste the COMPLETE output of each in your report:

```bash
.venv/bin/python scripts/momentum_screen.py run
.venv/bin/python scripts/momentum_screen.py quintiles
```

Then answer each of these with numbers, as facts, not judgements:
- How many months did the primary test produce, and over what rank dates?
- How many symbols had candles, and which have short or truncated histories?
- What is the mean monthly spread, its t, and the two sub-period spreads?
- Are the five quintile means monotonic from group 1 to group 5, or not? State the five numbers.
- What is the top-minus-bottom figure?
- What is mean monthly turnover, and what does the cost line say?
- Do the portfolio and baseline cumulative returns and drawdowns look plausible for 2021-2025 Indian large caps (the index roughly doubled over this period)? A portfolio cumulative return below the baseline's while the mean monthly spread is positive would be a contradiction worth reporting.

REPORTING DISCIPLINE: report the numbers as they are. Do NOT interpret whether momentum works, do NOT recommend anything, do NOT change any parameter, lookback, portfolio size or window in response to what you see. The construction was fixed in the spec before any of these numbers existed. If something looks structurally broken — zero months, every month holding the same names, a quintile with no observations, the baseline equal to the portfolio — say so plainly as a possible defect and stop.

- [ ] **Step 6: Commit**

```bash
git add scripts/momentum_screen.py tests/test_momentum_screen.py
git commit -m "scripts(momentum): run and quintiles phases"
```

### Task 7: results notes

**Files:**
- Create: `docs/superpowers/notes/2026-09-20-cross-sectional-momentum-results.md`

- [ ] **Step 1: Write the notes**

Read `docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md` first for house style: plain short sentences, tables, conclusions stated flatly, no hype. EVERY number must come from the two runs in Task 6 or from a read-only query you run now; never from memory.

Sections:

1. **What this tested and why** (5-8 lines): the distinction between ranking a stock against its own past (eight failures) and ranking stocks against each other; that momentum is externally pre-registered by the literature rather than discovered here; where the code lives.
2. **The construction**: the 12-1 score, the skipped month and why, next-day-close entry, top 10 of 50, monthly rebalance, turnover-only costing, and that closes are used throughout because Groww's 2025 `open` is synthetic. Give the round-trip cost figure the script printed.
3. **The result**: the primary block verbatim, the two sub-period lines, and the secondary grid. State whether the primary cleared the bar (spread > 0 with t >= 2) in one sentence.
4. **The quintiles**: the five means, whether they are monotonic, and the top-minus-bottom figure. Say plainly which of the two — the t or the ordering — you find more informative here and why.
5. **The data defect**: that Groww's daily `open` is synthetic for 2025 (98.6% of bars against 4-7% in 2020-2024), what it would have broken, and that the screen avoids it by using closes. Note that the daily screen's in-sample results are unaffected but its unspent holdout would be, and that `gap_mask` now has a second detector for the five inside-the-bar corporate actions, all in 2025.
6. **Limits**: survivorship stated first and prominently, with the two directions it pulls and the three short-history symbols; the small number of monthly observations; one bull-market window; unverified charge rates; no market impact; the portfolio ignores lot sizes and capital.
7. **Conclusion**: what is supported, what is not, and what a reader should do next. If the result is null, say the running tally is now nine families tested with none showing an edge, and that the remaining untried directions are the delivery screen already specced and the non-price sources that need data this project does not have.

- [ ] **Step 2: Verify and commit**

Run `.venv/bin/pytest -q` (expect `620 passed`) and `git status --short` (expect only the notes file).

```bash
git add docs/superpowers/notes/2026-09-20-cross-sectional-momentum-results.md
git commit -m "notes: cross-sectional momentum on the 50-name universe, 2021-2025"
```
