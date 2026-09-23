# Swing setup screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure, at name-day resolution over the point-in-time NIFTY 200, whether six technical setups predict 20/60/120-day forward returns better than two baselines — and whether any edge found is large enough to cover its own costs.

**Architecture:** One script, `scripts/swing_screen.py`, importing indicators, the corporate-action mask, the cost model and the date-clustered t-statistic from `scripts/daily_screen.py` rather than reimplementing them, and point-in-time membership from `tradebot.data.membership`. No positions, no engine, no portfolio: the unit is a (symbol, date) observation.

**Tech Stack:** Python 3.9 (no `X | None`, no builtin generics, no `match`, no `from __future__ import annotations`), sqlite3, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-swing-setup-screen-design.md`

**Baseline:** `.venv/bin/pytest -q` from the repo root gives `689 passed`.

**Branch:** `dev-swing-screen`, already checked out.

---

## Standing constraints for every task

- **Never write to anything under `data/`.** Read it only via `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`.
- **Do not run** `tradebot backtest`, `tradebot paper`, `tradebot fetch-data`, `scripts/orb_experiment.py`, `scripts/daily_screen.py`, or `scripts/momentum_screen.py`. Importing them as modules is required and fine.
- **The holdout is 2024-01-01 onward and is RESERVED.** No task here computes any statistic over it. The script gains a `holdout` phase in Task 6 that refuses to run without an explicit flag; nobody invokes it.
- Python 3.9 only. `Optional`, `List`, `Dict`, `Tuple` from `typing`.
- Use `.venv/bin/pytest` and `.venv/bin/python`.
- **Stage explicit paths by name when committing. Never `git add -u` or `git add -A`.**
- End every commit message with, on its own last line:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Report the REAL test count. If it differs from this plan, say so; the plan's arithmetic has been wrong before and that is the planner's error, not yours to hide.

## File structure

| File | Responsibility |
|---|---|
| `scripts/swing_screen.py` | Setups, name-day observations, both baselines, the report. New. |
| `tests/test_swing_screen.py` | Unit tests. New. |

Test counts: Task 1 +7, Task 2 +5, Task 3 +5, Task 4 +4, Task 5 +3, Task 6 +2. From 689: 696, 701, 706, 710, 713, 715.

---

### Task 1: the six setups and the control

**Files:**
- Create: `scripts/swing_screen.py`
- Test: `tests/test_swing_screen.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_swing_screen.py`:

```python
"""Unit tests for scripts/swing_screen.py, loaded from its path like the other script tests."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "swing_screen.py"
_spec = importlib.util.spec_from_file_location("swing_screen", SCRIPT)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


def _bars(closes, highs=None, lows=None, opens=None, vols=None):
    """Daily bars from a close series; the other fields default to something inert."""
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    opens = opens or list(closes)
    vols = vols or [1000] * n
    d0 = date(2020, 1, 1)
    return [sw.Bar(d0 + timedelta(days=i), opens[i], highs[i], lows[i], closes[i], vols[i])
            for i in range(n)]


def test_pullback_needs_both_an_uptrend_and_an_oversold_reading():
    """close > SMA200 AND RSI2 < 10. Either alone must not fire, or the setup is just
    'the market went up', which the baseline already accounts for."""
    up = [100.0 + i for i in range(230)]          # steady rise: above SMA200, RSI2 high
    ind = sw.Indicators(_bars(up))
    assert not sw.pullback(ind, 229)
    dipped = up[:]                                 # three sharp down closes at the end
    for k in (227, 228, 229):
        dipped[k] = up[226] * 0.94
    ind2 = sw.Indicators(_bars(dipped))
    assert sw.pullback(ind2, 229)


def test_breakout_needs_a_new_hundred_day_high():
    closes = [100.0] * 220 + [101.0]
    ind = sw.Indicators(_bars(closes))
    assert sw.breakout(ind, 220)
    assert not sw.breakout(ind, 219)


def test_trend_dip_needs_price_between_sma50_and_sma20():
    """Below SMA20 but still above SMA50, in an established uptrend. Below both is a
    breakdown, not a dip, and must not fire."""
    closes = [100.0 + i * 0.5 for i in range(230)]
    ind = sw.Indicators(_bars(closes))
    i = 229
    assert ind.sma20[i] is not None and ind.sma50[i] is not None
    assert not sw.trend_dip(ind, i)                # riding above SMA20
    dipped = closes[:]
    dipped[229] = (ind.sma20[229] + ind.sma50[229]) / 2.0
    assert sw.trend_dip(sw.Indicators(_bars(dipped)), 229)


def test_squeeze_needs_contraction_then_an_expansion_bar():
    """Quiet YESTERDAY, then a break today. The contraction is measured on the prior bar
    because the expansion bar's own range is what lifts ATR -- measuring both on the same
    bar makes a genuine squeeze-then-break disqualify itself."""
    closes = [100.0 + (i % 7) * 3.0 for i in range(200)] + [100.0] * 30
    bars = _bars(closes)
    ind = sw.Indicators(bars)
    assert not sw.squeeze(ind, 229)                # quiet, but no expansion
    closes2 = closes[:]
    closes2[229] = bars[228].high * 1.02           # break out of the quiet range
    ind2 = sw.Indicators(_bars(closes2))
    assert ind2.atrpct[228] <= ind2.atrpct_min100_prev[228], (
        "the fixture must actually be quiet on bar 228, or this proves nothing")
    assert sw.squeeze(ind2, 229)


def test_gap_vol_needs_a_gap_volume_and_a_green_close():
    n = 230
    closes = [100.0] * n
    opens = [100.0] * n
    vols = [1000] * n
    opens[229] = 102.0                              # gap up 2%
    closes[229] = 103.0                             # closes green
    vols[229] = 3000                                # 3x the 20-day average
    assert sw.gap_vol(sw.Indicators(_bars(closes, opens=opens, vols=vols)), 229)
    red = closes[:]
    red[229] = 101.0                                # still above prior close but below its open
    assert not sw.gap_vol(sw.Indicators(_bars(red, opens=opens, vols=vols)), 229)


def test_three_down_needs_three_lower_closes_inside_an_uptrend():
    closes = [100.0 + i for i in range(227)] + [320.0, 319.0, 318.0]
    ind = sw.Indicators(_bars(closes))
    assert sw.three_down(ind, 229)
    assert not sw.three_down(sw.Indicators(_bars([100.0 + i for i in range(230)])), 229)


def test_every_setup_declines_before_warmup():
    """SMA200 needs 200 bars. A setup that fires on bar 10 is reading a None as a number."""
    ind = sw.Indicators(_bars([100.0 + i for i in range(30)]))
    for name, fn in sw.SETUPS:
        if name == "random":
            continue
        assert not fn(ind, 10), "%s fired before warm-up" % name
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_swing_screen.py -q`
Expected: `ModuleNotFoundError: No module named 'swing_screen'`

- [ ] **Step 3: Write the implementation**

Create `scripts/swing_screen.py`:

```python
"""Do technical setups pick better-than-average stocks to hold for weeks?

    .venv/bin/python scripts/swing_screen.py insample

Of roughly 200 index members on any morning, can a setup identify the few worth holding?
The unit of observation is a NAME-DAY, not a trade and not a portfolio: for every (symbol,
date) a setup fires on, record the forward return at each horizon. That gives hundreds of
thousands of observations where a portfolio construction would give a few hundred, and the
momentum screen showed that resolution is what decides whether a small edge is visible.

MEASURED AGAINST TWO BASELINES, because they answer different questions:

  time-series     the mean return of holding this same symbol this many days, entered on
                  every eligible date -- is this a good MOMENT to buy this stock?
  cross-sectional the equal-weight forward return of every eligible index member on this
                  same date -- is this a better STOCK to buy today than the others?

The second is the decision-relevant one when choosing a few names on a morning, and it
removes market-wide moves automatically. A setup that beats the first but not the second
is firing on days the whole market rose.

Costs are charged to the signal and to both baselines alike, so they cancel out of the
excess: excess measures SELECTION. Whether the selection pays is a separate question,
reported as net expectancy beside it, and the two can disagree -- in a rising market a
setup can be profitable and still worse than random.

Horizons are long on purpose. Cost scales with turnover, so the holding period fixes the
hurdle before any signal is considered: a 10-day hold needs 1.43%/mo excess to break even
and a 60-day hold needs 0.238%/mo. See spec section 4.

This reads the database read-only and writes nothing.
"""
import argparse
import importlib.util
import random
import sys
from collections import defaultdict, namedtuple
from datetime import date
from pathlib import Path

DB = "data/tradebot.db"
HORIZONS = (20, 60, 120)                 # trading days
PRIMARY_HORIZON = 60
INSAMPLE = (date(2020, 1, 1), date(2023, 12, 31))
HOLDOUT_START = date(2024, 1, 1)         # reserved; see guard_holdout
WARMUP_BARS = 200                        # SMA(200)
CAPITAL = 100_000.0
POSITIONS = 8                            # the book the hurdle is computed for
BONFERRONI_T = 2.64                      # six primary cells at alpha 0.05
RANDOM_SEED = 20260922

# Indicators, the corporate-action mask, the cost model and the date-clustered t come from
# the daily screen rather than being written twice: one definition of a split, of a round
# trip, and of how to count clustered observations.
_DS = Path(__file__).resolve().parent / "daily_screen.py"
_spec = importlib.util.spec_from_file_location("daily_screen", _DS)
daily_screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_screen)
gap_mask = daily_screen.gap_mask
load_series = daily_screen.load_series
round_trip_cost = daily_screen.round_trip_cost
sma = daily_screen.sma
rsi_wilder = daily_screen.rsi_wilder
atr_wilder = daily_screen.atr_wilder
rolling_max_prev = daily_screen.rolling_max_prev

Bar = namedtuple("Bar", "date open high low close volume")


class Indicators(object):
    """Everything the setups read, computed once per symbol."""

    def __init__(self, bars):
        self.bars = bars
        self.closes = [b.close for b in bars]
        self.sma20 = sma(self.closes, 20)
        self.sma50 = sma(self.closes, 50)
        self.sma200 = sma(self.closes, 200)
        self.rsi2 = rsi_wilder(self.closes, 2)
        self.atr20 = atr_wilder(bars, 20)
        self.max100_prev = rolling_max_prev(self.closes, 100)
        self.vol20 = sma([float(b.volume) for b in bars], 20)
        # ATR as a fraction of price, and its own 100-day prior low, for the squeeze
        self.atrpct = [None if (self.atr20[i] is None or self.closes[i] <= 0)
                       else self.atr20[i] / self.closes[i] for i in range(len(bars))]
        self.atrpct_min100_prev = _rolling_min_prev_opt(self.atrpct, 100)


def _rolling_min_prev_opt(values, n):
    """Rolling min of the n values BEFORE each index, skipping Nones. None until n are seen."""
    out = [None] * len(values)
    for i in range(len(values)):
        window = [v for v in values[max(0, i - n):i] if v is not None]
        if len(window) == n:
            out[i] = min(window)
    return out


def _ready(ind, i):
    """Every setup needs the slowest indicator warm. Reading a None as a number is the
    failure this guards; it would make setups fire in the first months of the window."""
    return i >= WARMUP_BARS and ind.sma200[i] is not None


def pullback(ind, i):
    """An oversold dip inside an uptrend."""
    if not _ready(ind, i) or ind.rsi2[i] is None:
        return False
    return ind.closes[i] > ind.sma200[i] and ind.rsi2[i] < 10.0


def breakout(ind, i):
    """A new hundred-day closing high, in an uptrend."""
    if not _ready(ind, i) or ind.max100_prev[i] is None:
        return False
    return ind.closes[i] > ind.max100_prev[i] and ind.closes[i] > ind.sma200[i]


def trend_dip(ind, i):
    """A dip to the twenty-day average that holds the fifty. Below both is a breakdown."""
    if not _ready(ind, i) or ind.sma20[i] is None or ind.sma50[i] is None:
        return False
    return (ind.sma50[i] > ind.sma200[i] and ind.closes[i] < ind.sma20[i]
            and ind.closes[i] > ind.sma50[i])


def squeeze(ind, i):
    """Volatility was at a hundred-day low YESTERDAY, and today closes above yesterday's high.

    The contraction is measured on bar i-1 deliberately. Measuring it on bar i is
    self-contradictory: the expansion bar's own range is what lifts ATR, so a genuine
    squeeze-then-break would disqualify itself and the setup would fire only on days that
    broke out without moving."""
    if not _ready(ind, i) or i < 1:
        return False
    if ind.atrpct[i - 1] is None or ind.atrpct_min100_prev[i - 1] is None:
        return False
    return (ind.atrpct[i - 1] <= ind.atrpct_min100_prev[i - 1]
            and ind.closes[i] > ind.bars[i - 1].high)


def gap_vol(ind, i):
    """A gap up on heavy volume that holds its gain into the close."""
    if not _ready(ind, i) or ind.vol20[i] is None:
        return False
    b, prev = ind.bars[i], ind.bars[i - 1]
    return (b.open > prev.close * 1.01 and b.volume > 2.0 * ind.vol20[i]
            and b.close > b.open)


def three_down(ind, i):
    """Three consecutive lower closes inside an uptrend."""
    if not _ready(ind, i) or i < 3:
        return False
    c = ind.closes
    return (c[i] < c[i - 1] < c[i - 2] < c[i - 3] and c[i] > ind.sma200[i])


def make_random(rate, seed=RANDOM_SEED):
    """A control that fires at `rate`, independent of price.

    Not decoration. On the momentum screen a random ranking produced a LARGER apparent edge
    than the real signal, and that number is what made the result interpretable. A setup
    that cannot beat this is noise.
    """
    rng = random.Random(seed)

    def fire(ind, i):
        return _ready(ind, i) and rng.random() < rate

    return fire


SETUPS = (
    ("pullback", pullback),
    ("breakout", breakout),
    ("trend_dip", trend_dip),
    ("squeeze", squeeze),
    ("gap_vol", gap_vol),
    ("three_down", three_down),
)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["insample", "holdout"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/test_swing_screen.py -q` → `7 passed`. Then `.venv/bin/pytest -q` → `696 passed`.

**If a setup test fails, do NOT weaken the setup to make it pass.** Report it: either my fixture does not construct the condition I claimed, or the setup definition is wrong, and I need to know which. The setup definitions are pre-registered in the spec and cannot be changed to fit a fixture.

- [ ] **Step 5: Commit**

```bash
git add scripts/swing_screen.py tests/test_swing_screen.py
git commit -m "scripts(swing): the six pre-registered setups and a random control"
```

---

### Task 2: name-day observations with point-in-time membership

**Files:**
- Modify: `scripts/swing_screen.py`
- Test: `tests/test_swing_screen.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_forward_return_is_close_to_close_over_the_horizon():
    closes = [100.0] * 220 + [110.0]
    bars = _bars(closes)
    assert sw.forward_return(bars, 200, 20) == pytest.approx(0.10)
    assert sw.forward_return(bars, 210, 20) is None      # exit lands past the end


def test_an_observation_needs_the_symbol_to_be_an_index_member_that_day():
    """The whole point of the point-in-time work. A name-day for a symbol that was not in
    the index that morning is a name-day you could not have traded."""
    members = {date(2021, 3, 1): ("AAA",), date(2021, 3, 2): ("BBB",)}
    assert sw.is_member(members, "AAA", date(2021, 3, 1))
    assert not sw.is_member(members, "AAA", date(2021, 3, 2))
    assert not sw.is_member(members, "AAA", date(2099, 1, 1))


def test_a_masked_date_anywhere_in_the_hold_disqualifies_the_observation():
    """Same inclusive check the daily screen applies to a trade: an unadjusted split inside
    the holding window makes the return fiction, wherever in the window it falls."""
    bars = _bars([100.0] * 230)
    masked = {bars[205].date}
    assert sw.hold_is_clean(bars, 200, 20, set())
    assert not sw.hold_is_clean(bars, 200, 20, masked)
    assert sw.hold_is_clean(bars, 100, 20, masked)        # window ends before the mask


def test_a_masked_entry_or_exit_date_also_disqualifies():
    bars = _bars([100.0] * 230)
    assert not sw.hold_is_clean(bars, 200, 20, {bars[200].date})
    assert not sw.hold_is_clean(bars, 200, 20, {bars[220].date})


def test_observations_carry_symbol_date_horizon_and_return():
    bars = _bars([100.0 + i for i in range(230)])
    ind = sw.Indicators(bars)
    members = {b.date: ("AAA",) for b in bars}
    obs = sw.observations("AAA", bars, ind, set(), members, INSAMPLE_TEST, ("breakout", sw.breakout), (20,))
    assert obs, "the fixture rises monotonically, so breakout must fire somewhere"
    o = obs[0]
    assert o.symbol == "AAA" and o.horizon == 20
    assert o.entry_date in [b.date for b in bars]
    assert isinstance(o.ret, float)
```

and near the top of the test file, after the imports:

```python
INSAMPLE_TEST = (date(2020, 1, 1), date(2021, 12, 31))
```

- [ ] **Step 2: Run to verify they fail**

Expected: `AttributeError: module 'swing_screen' has no attribute 'forward_return'`

- [ ] **Step 3: Write the implementation**

Add after `SETUPS`:

```python
Obs = namedtuple("Obs", "symbol entry_date horizon ret")


def forward_return(bars, i, h):
    """Close-to-close return from bar i to bar i+h, or None if the exit is past the end.

    Close, not open: Groww's daily `open` is synthetic for 2025 -- 98.6% of bars carry the
    previous close forward -- so a fill at an open would be fictional over much of the window.
    """
    j = i + h
    if j >= len(bars) or bars[i].close <= 0:
        return None
    return bars[j].close / bars[i].close - 1.0


def is_member(members_by_date, symbol, d):
    """Was `symbol` in the index on `d`? Unknown dates are not members: silently treating a
    missing date as membership is how survivorship bias gets back in."""
    return symbol in members_by_date.get(d, ())


def hold_is_clean(bars, i, h, masked):
    """No corporate action anywhere in [entry, exit] inclusive. An unadjusted split makes the
    return fiction wherever in the window it lands, so the check spans the whole hold."""
    j = min(i + h, len(bars) - 1)
    return not any(bars[k].date in masked for k in range(i, j + 1))


def observations(symbol, bars, ind, masked, members_by_date, window, setup, horizons):
    """Every name-day this setup fires on, one Obs per horizon."""
    name, fires = setup
    lo, hi = window
    out = []
    for i in range(len(bars)):
        d = bars[i].date
        if not (lo <= d <= hi) or not is_member(members_by_date, symbol, d):
            continue
        if not fires(ind, i):
            continue
        for h in horizons:
            if not hold_is_clean(bars, i, h, masked):
                continue
            r = forward_return(bars, i, h)
            if r is not None:
                out.append(Obs(symbol, d, h, r))
    return out
```

- [ ] **Step 4: Run**

`.venv/bin/pytest -q` → `701 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/swing_screen.py tests/test_swing_screen.py
git commit -m "scripts(swing): name-day observations, gated on point-in-time membership"
```

---

### Task 3: the two baselines

**Files:**
- Modify: `scripts/swing_screen.py`
- Test: `tests/test_swing_screen.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_time_series_baseline_is_the_mean_hold_in_that_symbol():
    """Every eligible entry in the window, held h days. A steady 1%-a-bar riser held 2 bars
    returns 2.01% from any entry, so the mean is that."""
    closes = [100.0 * (1.01 ** i) for i in range(230)]
    bars = _bars(closes)
    b = sw.ts_baseline(bars, (bars[200].date, bars[229].date), 2, set())
    assert b == pytest.approx(1.01 ** 2 - 1.0)


def test_time_series_baseline_skips_masked_windows():
    closes = [100.0 * (1.01 ** i) for i in range(230)]
    bars = _bars(closes)
    clean = sw.ts_baseline(bars, (bars[200].date, bars[229].date), 2, set())
    masked = sw.ts_baseline(bars, (bars[200].date, bars[229].date), 2, {bars[210].date})
    assert clean == pytest.approx(masked)     # same value, but computed over fewer holds
    assert sw.ts_baseline(bars, (bars[200].date, bars[202].date), 2, {bars[201].date}) is None


def test_cross_sectional_baseline_is_the_mean_over_that_days_members():
    """Two members on the date, one returning 10% and one 0%, gives 5%."""
    rets = {("AAA", date(2021, 3, 1), 20): 0.10, ("BBB", date(2021, 3, 1), 20): 0.0}
    members = {date(2021, 3, 1): ("AAA", "BBB")}
    assert sw.xs_baseline(rets, members, date(2021, 3, 1), 20) == pytest.approx(0.05)


def test_cross_sectional_baseline_covers_only_that_days_members():
    """A symbol not in the index that day must not enter the comparison, or the baseline is
    computed over a universe you could not have chosen from."""
    rets = {("AAA", date(2021, 3, 1), 20): 0.10, ("ZZZ", date(2021, 3, 1), 20): 1.00}
    members = {date(2021, 3, 1): ("AAA",)}
    assert sw.xs_baseline(rets, members, date(2021, 3, 1), 20) == pytest.approx(0.10)


def test_cross_sectional_baseline_is_none_without_members():
    assert sw.xs_baseline({}, {}, date(2021, 3, 1), 20) is None
```

- [ ] **Step 2: Run to verify they fail** — `AttributeError: ... 'ts_baseline'`

- [ ] **Step 3: Write the implementation**

```python
def ts_baseline(bars, window, h, masked):
    """Mean h-day close-to-close return of this symbol, entered on every eligible date in the
    window. Answers: is this a good MOMENT to buy this stock?

    Indian large caps rose over this period, so any rule that buys shows a positive return
    from drift alone; only the excess over this is evidence about timing."""
    lo, hi = window
    rs = []
    for i in range(len(bars)):
        if not (lo <= bars[i].date <= hi):
            continue
        j = i + h
        if j < len(bars) and bars[j].date <= hi and hold_is_clean(bars, i, h, masked):
            r = forward_return(bars, i, h)
            if r is not None:
                rs.append(r)
    return (sum(rs) / len(rs)) if rs else None


def xs_baseline(returns_by_key, members_by_date, d, h):
    """Equal-weight forward return of every index member on `d` at horizon `h`. Answers: is
    this a better STOCK to buy today than the others?

    This is the decision-relevant comparison when choosing a few names on a morning, and it
    removes market-wide moves on that date automatically -- a setup that beats the
    time-series baseline but not this one is firing on days everything rose."""
    rs = [returns_by_key[(s, d, h)] for s in members_by_date.get(d, ())
          if (s, d, h) in returns_by_key]
    return (sum(rs) / len(rs)) if rs else None
```

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → `706 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/swing_screen.py tests/test_swing_screen.py
git commit -m "scripts(swing): a time-series and a cross-sectional baseline"
```

---

### Task 4: the date-clustered statistic and the cost hurdle

**Files:**
- Modify: `scripts/swing_screen.py`
- Test: `tests/test_swing_screen.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_t_is_computed_across_dates_not_observations():
    """Forty names firing on one dip is ONE observation, not forty. Counting them
    separately inflates t by about the square root of the cluster size, which is how the
    last screen produced a positive mean beside a negative t."""
    one_day = [sw.Ex("S%d" % k, date(2021, 3, 1), 60, 0.05, 0.05) for k in range(40)]
    two_more = [sw.Ex("A", date(2021, 4, 1), 60, -0.05, -0.05),
                sw.Ex("A", date(2021, 5, 1), 60, -0.04, -0.04)]
    t = sw.t_across_dates(one_day + two_more)
    assert t is not None and t < 1.0, "clustered day must not dominate"
    assert sw.t_across_dates(one_day) is None        # a single date has no variance


def test_date_means_average_within_a_date_first():
    exs = [sw.Ex("A", date(2021, 3, 1), 60, 0.10, 0.10),
           sw.Ex("B", date(2021, 3, 1), 60, 0.00, 0.00),
           sw.Ex("C", date(2021, 4, 1), 60, 0.04, 0.04)]
    assert sw.date_means(exs) == pytest.approx([0.05, 0.04])


def test_the_hurdle_scales_with_turnover():
    """Cost per year is the round trip times the rotations per year, so a longer hold is a
    lower bar. This is the single biggest lever in the design."""
    h20 = sw.hurdle_per_month(20)
    h60 = sw.hurdle_per_month(60)
    h120 = sw.hurdle_per_month(120)
    assert h20 > h60 > h120
    assert h60 == pytest.approx(0.00238, abs=2e-4)


def test_the_hurdle_is_per_month_not_per_trade():
    """A 60-day hold turns over about 4.2 times a year, so its annual cost spread over
    twelve months is much smaller than one round trip."""
    assert sw.hurdle_per_month(60) < sw.round_trip_cost(sw.CAPITAL / sw.POSITIONS)
```

- [ ] **Step 2: Run to verify they fail** — `AttributeError: ... 'Ex'`

- [ ] **Step 3: Write the implementation**

```python
Ex = namedtuple("Ex", "symbol entry_date horizon ts_excess xs_excess")


def date_means(exs, attr="xs_excess"):
    """Per-entry-date means, sorted for determinism. Observations sharing a date are averaged
    into one before any statistic sees them: setups cluster, and one market-wide dip firing a
    setup across forty names is one piece of evidence, not forty."""
    by_date = defaultdict(list)
    for e in exs:
        by_date[e.entry_date].append(getattr(e, attr))
    return [sum(by_date[d]) / len(by_date[d]) for d in sorted(by_date)]


def t_across_dates(exs, attr="xs_excess"):
    """t of the mean of `date_means`. None for fewer than two dates or zero variance."""
    import math
    means = date_means(exs, attr)
    n = len(means)
    if n < 2:
        return None
    mean = sum(means) / n
    var = sum((m - mean) ** 2 for m in means) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def hurdle_per_month(horizon, capital=CAPITAL, positions=POSITIONS):
    """Monthly excess a system must clear just to pay for its own turnover.

    Cost scales with how often you trade, so the holding period fixes this before any signal
    is considered: 20 days needs 0.715%/mo, 60 needs 0.238%, 120 needs 0.119%. Published
    anomalies run 0.3-0.8%/mo, which is why the primary horizon is 60 and not 10."""
    rt = round_trip_cost(capital / float(positions))
    rotations_per_year = 252.0 / float(horizon)
    return rt * rotations_per_year / 12.0
```

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → `710 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/swing_screen.py tests/test_swing_screen.py
git commit -m "scripts(swing): date-clustered t and the turnover hurdle"
```

---

### Task 5: assemble and report

**Files:**
- Modify: `scripts/swing_screen.py`
- Test: `tests/test_swing_screen.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_summarise_reports_both_excesses_and_their_ts():
    exs = [sw.Ex("A", date(2021, 3, 1), 60, 0.02, 0.01),
           sw.Ex("B", date(2021, 4, 1), 60, -0.01, 0.03),
           sw.Ex("C", date(2021, 5, 1), 60, 0.03, -0.02)]
    s = sw.summarise(exs, 60)
    assert s["n"] == 3 and s["dates"] == 3
    assert s["ts_excess"] == pytest.approx((0.02 - 0.01 + 0.03) / 3)
    assert s["xs_excess"] == pytest.approx((0.01 + 0.03 - 0.02) / 3)
    assert s["xs_t"] is not None


def test_summarise_of_nothing_does_not_divide_by_zero():
    s = sw.summarise([], 60)
    assert s["n"] == 0 and s["xs_t"] is None


def test_the_verdict_requires_both_a_positive_excess_and_the_t_bar():
    """Pre-registered: excess > 0 AND t >= 2.64. Either alone is not a pass, and a pass is
    separately reported as tradable only if it also clears the turnover hurdle."""
    assert sw.verdict(0.01, 3.0, 60) == "PASS"
    assert sw.verdict(-0.01, 3.0, 60) == "fail"        # negative excess
    assert sw.verdict(0.01, 1.0, 60) == "fail"         # under the t bar
    assert sw.verdict(0.0001, 3.0, 60) == "PASS but below the cost hurdle"
```

- [ ] **Step 2: Run to verify they fail** — `AttributeError: ... 'summarise'`

- [ ] **Step 3: Write the implementation**

```python
def summarise(exs, horizon):
    """Headline figures for one (setup, horizon) cell."""
    n = len(exs)
    if n == 0:
        return dict(n=0, dates=0, ts_excess=0.0, xs_excess=0.0, ts_t=None, xs_t=None,
                    win=0.0, horizon=horizon)
    return dict(n=n, dates=len(set(e.entry_date for e in exs)),
                ts_excess=sum(e.ts_excess for e in exs) / n,
                xs_excess=sum(e.xs_excess for e in exs) / n,
                ts_t=t_across_dates(exs, "ts_excess"),
                xs_t=t_across_dates(exs, "xs_excess"),
                win=100.0 * sum(1 for e in exs if e.xs_excess > 0) / n,
                horizon=horizon)


def verdict(xs_excess, xs_t, horizon):
    """The pre-registered bar: cross-sectional excess > 0 with t >= 2.64.

    Clearing the bar and clearing the COST hurdle are different questions, and collapsing
    them would hide which one we have: a real-but-too-small edge is a different finding from
    no edge at all."""
    if xs_t is None or xs_excess <= 0 or xs_t < BONFERRONI_T:
        return "fail"
    monthly = xs_excess / (horizon / 21.0)
    return "PASS" if monthly >= hurdle_per_month(horizon) else "PASS but below the cost hurdle"
```

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → `713 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/swing_screen.py tests/test_swing_screen.py
git commit -m "scripts(swing): per-cell summary and the pre-registered verdict"
```

---

### Task 6: wire the phases, guard the holdout, and run in-sample

**Files:**
- Modify: `scripts/swing_screen.py`
- Test: `tests/test_swing_screen.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_the_holdout_phase_refuses_without_the_explicit_flag():
    """2024 onward is reserved. It must not be spendable by typing the wrong word."""
    with pytest.raises(SystemExit) as e:
        sw.guard_holdout(confirmed=False)
    assert "reserved" in str(e.value).lower()


def test_the_holdout_guard_allows_an_explicit_confirmation():
    sw.guard_holdout(confirmed=True)        # must not raise
```

- [ ] **Step 2: Run to verify they fail** — `AttributeError: ... 'guard_holdout'`

- [ ] **Step 3: Write the implementation**

```python
def guard_holdout(confirmed):
    """2024-01-01 onward is reserved and unspent. Looking at it before a setup passes
    in-sample turns the only honest out-of-sample test this project has into another
    in-sample one, and it cannot be un-looked-at."""
    if not confirmed:
        sys.exit("the holdout from %s is RESERVED and unspent. Pass --i-am-sure to spend it, "
                 "and only after a setup has passed in-sample." % HOLDOUT_START)


def run_phase(conn, window, label, out=sys.stdout):
    from tradebot.data.membership import MembershipError, constituents_on, load_timeline
    timeline = load_timeline("index_membership.yaml")

    all_symbols = sorted(set(timeline.anchor) |
                         set(s for e in timeline.events for s in (e.include + e.exclude)) |
                         set(c for _, c in timeline.aliases))
    series = load_series(conn, all_symbols)
    # gap_vol needs volume and daily_screen.Bar does not carry it. Join on DATE, not on
    # position: zipping two orderings assumes they agree, and a single missing bar would
    # then shift every volume by one day without any error.
    bars_by_symbol = {}
    for sym, bs in series.items():
        vol_by_date = {}
        for ts, v in conn.execute(
                "SELECT ts, v FROM candles WHERE symbol=? AND interval=1440", (sym,)):
            vol_by_date[date.fromtimestamp(ts + daily_screen.IST_OFFSET)] = v or 0
        bars_by_symbol[sym] = [Bar(b.date, b.open, b.high, b.low, b.close,
                                   vol_by_date.get(b.date, 0)) for b in bs]

    masked = {s: gap_mask(series[s], mask_days=0) for s in series}
    inds = {s: Indicators(bars_by_symbol[s]) for s in bars_by_symbol}

    lo, hi = window
    all_dates = sorted(set(b.date for bs in bars_by_symbol.values() for b in bs
                           if lo <= b.date <= hi))
    members_by_date = {}
    for d in all_dates:
        try:
            members_by_date[d] = constituents_on(timeline, d)
        except MembershipError:
            # Only "outside the timeline's coverage" is an expected miss. Catching every
            # exception here would swallow a broken count invariant, which is the one error
            # this whole dataset exists to surface.
            members_by_date[d] = ()

    # forward returns for every member name-day, so the cross-sectional baseline can be built
    returns_by_key = {}
    for sym, bars in bars_by_symbol.items():
        idx = {b.date: i for i, b in enumerate(bars)}
        for d in all_dates:
            i = idx.get(d)
            if i is None or not is_member(members_by_date, sym, d):
                continue
            for h in HORIZONS:
                if hold_is_clean(bars, i, h, masked.get(sym, set())):
                    r = forward_return(bars, i, h)
                    if r is not None:
                        returns_by_key[(sym, d, h)] = r

    rates = []
    for name, fires in SETUPS:
        fired = total = 0
        for sym, bars in bars_by_symbol.items():
            ind = inds[sym]
            for i, b in enumerate(bars):
                if lo <= b.date <= hi and is_member(members_by_date, sym, b.date):
                    total += 1
                    fired += bool(fires(ind, i))
        rates.append(fired / float(total) if total else 0.0)
    mean_rate = sum(rates) / len(rates) if rates else 0.01
    setups = list(SETUPS) + [("random", make_random(mean_rate))]

    print("swing setup screen -- %s, %s..%s" % (label, lo, hi), file=out)
    print("  %d symbols, %d dates, mean firing rate %.2f%%"
          % (len(bars_by_symbol), len(all_dates), 100.0 * mean_rate), file=out)
    print("  cost hurdle: %s" % ", ".join(
        "%dd %.3f%%/mo" % (h, 100.0 * hurdle_per_month(h)) for h in HORIZONS), file=out)
    print("", file=out)
    print("  %-11s %5s %8s %7s %10s %8s %10s %8s  %s"
          % ("setup", "h", "obs", "dates", "xs excess", "t", "ts excess", "t", "verdict"),
          file=out)

    for name, fires in setups:
        for h in HORIZONS:
            exs = []
            for sym, bars in bars_by_symbol.items():
                ind = inds[sym]
                obs = observations(sym, bars, ind, masked.get(sym, set()), members_by_date,
                                   window, (name, fires), (h,))
                if not obs:
                    continue
                tsb = ts_baseline(bars, window, h, masked.get(sym, set()))
                for o in obs:
                    xsb = xs_baseline(returns_by_key, members_by_date, o.entry_date, h)
                    if tsb is None or xsb is None:
                        continue
                    exs.append(Ex(sym, o.entry_date, h, o.ret - tsb, o.ret - xsb))
            s = summarise(exs, h)
            v = verdict(s["xs_excess"], s["xs_t"], h) if s["n"] else "no observations"
            mark = " <-- primary" if h == PRIMARY_HORIZON else ""
            print("  %-11s %5d %8d %7d %+9.3f%% %8s %+9.3f%% %8s  %s%s"
                  % (name, h, s["n"], s["dates"], 100.0 * s["xs_excess"],
                     "n/a" if s["xs_t"] is None else "%.2f" % s["xs_t"],
                     100.0 * s["ts_excess"],
                     "n/a" if s["ts_t"] is None else "%.2f" % s["ts_t"], v, mark), file=out)
    print("", file=out)
    print("Bar: cross-sectional excess > 0 with t >= %.2f at horizon %d, fixed before the run."
          % (BONFERRONI_T, PRIMARY_HORIZON), file=out)
    print("t is across entry DATES, not observations: setups cluster and forty names firing on"
          " one dip\nis one piece of evidence. Costs cancel out of both excesses, so these"
          " measure selection;\nthe hurdle line above is what selection must beat to pay for"
          " itself.", file=out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["insample", "holdout"])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--i-am-sure", action="store_true",
                    help="required to spend the reserved holdout")
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    try:
        if a.phase == "insample":
            run_phase(conn, INSAMPLE, "in-sample")
        else:
            guard_holdout(a.i_am_sure)
            run_phase(conn, (HOLDOUT_START, date.today()), "HOLDOUT -- now spent")
    finally:
        conn.close()
```

Add `import sqlite3` to the imports.

- [ ] **Step 4: Run the tests**

`.venv/bin/pytest -q` → `715 passed`.

- [ ] **Step 5: Run the screen IN-SAMPLE ONLY, then stop**

```bash
.venv/bin/python scripts/swing_screen.py insample
```

Paste the complete output. **Do NOT run the holdout phase under any circumstances.**

Then answer, as facts and without interpretation:
- How many symbols, dates and observations did each setup produce?
- What is the primary cell (horizon 60) for each setup, and does any clear the bar?
- How does each setup compare to `random` at the same horizon?
- Is any setup's cross-sectional excess positive while its time-series excess is negative, or vice versa?

**REPORTING DISCIPLINE:** report the numbers as they are. Do NOT interpret whether the setups work, do NOT recommend anything, and do NOT change any setup, horizon, threshold or window in response to what you see. The construction was fixed in an approved spec before these numbers existed. If a figure looks structurally broken — zero observations for every setup, a t of exactly zero, `random` beating everything — say so plainly as a possible defect and stop.

- [ ] **Step 6: Commit**

```bash
git add scripts/swing_screen.py tests/test_swing_screen.py
git commit -m "scripts(swing): phases, the holdout guard, and the in-sample run"
```

---

## Self-review

**Spec coverage.** §2 name-day unit → Task 2. §3 setups and control → Task 1. §4 horizons and the hurdle → Tasks 1 and 4. §5 both baselines → Task 3. §6 pass bar and date-clustered t → Tasks 4 and 5. §7 windows and the holdout guard → Task 6. §8 membership, gap mask, warm-up, slippage, close-to-close → Tasks 1, 2 and 6. §9 testing → every task.

One gap I am accepting knowingly: spec §8 says slippage comes from `liquidity.slippage_for` per name. Costs cancel out of both excesses, so per-name slippage cannot change any excess figure or any verdict — it would only alter the hurdle line, and only in the third decimal. Wiring it would add a database read per name-day for no effect on the result. If a setup passes and a portfolio gets built, per-name slippage belongs in THAT work, where positions actually differ.

**Placeholders.** None. Every step carries the code it needs.

**Type consistency.** `Bar`, `Obs`, `Ex`, `Indicators`, `observations`, `ts_baseline`, `xs_baseline`, `date_means`, `t_across_dates`, `hurdle_per_month`, `summarise`, `verdict`, `guard_holdout`, `run_phase` are each defined once and used with the same signature throughout. `Bar` here carries `volume`, which `daily_screen.Bar` does not — that is why it is redefined rather than imported, and Task 6 reads volume separately to fill it.
