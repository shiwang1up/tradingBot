# Exit screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether any of five exit designs beats the one the entry screen actually validated, so the engine stops trading a 2×ATR stop that no screen has ever tested.

**Architecture:** A new `scripts/exit_screen.py` built on `swing_screen.py`'s harness — its `Bar`, `Indicators`, corporate-action mask, point-in-time membership, cost model and date-clustered t are imported rather than rewritten. Entry is fixed at `trend_dip`; only the exit varies. Every cell sees identical entries, so cells are compared as a **paired per-date difference** against the incumbent.

**Tech Stack:** Python 3.9, pytest. `scripts/` is not a package and its files carry no `from __future__ import annotations` — do not add it.

**Spec:** `docs/superpowers/specs/2026-09-23-exit-screen-design.md`

**Branch:** `dev-daily-engine` (current).

---

## Standing constraints

- **Never write to anything under `data/`.** Read it only via
  `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`. The screen writes nothing.
- **Do not run** `tradebot paper`, `tradebot fetch-data`, `tradebot backtest`,
  `scripts/orb_experiment.py`, `scripts/momentum_screen.py`, or **`scripts/swing_screen.py holdout`**.
  `scripts/swing_screen.py insample` IS allowed and Task 2 uses it as a cross-check.
- **THE HOLDOUT, 2024-01-01 ONWARD, IS RESERVED AND UNSPENT, AND THIS PLAN DOES NOT TOUCH IT.**
  The screen ships a `guard_holdout` and **the `--i-am-sure` flag is never to be passed** as part of
  this work. This was decided before the grid was written; see spec §6.
- **This system places no orders.** Nothing here moves it closer to doing so.
- **Do not change the entry.** `trend_dip` is SMA(20)/SMA(50)/SMA(200) and stays that way. Re-opening
  entry parameters here would be a second look at data the entry screen already spent.
- **Do not change `config-daily.yaml`, the engine, or `risk/engine.py`.** This plan measures; it
  does not act on what it measures.
- Python 3.9. `.venv/bin/pytest`, `.venv/bin/python`.
- **Stage explicit paths by name. Never `git add -u` or `git add -A`.**
- **No Claude attribution in commit messages.**
- `tests/fixtures/golden_trades.json` must stay byte-identical.
- **Report the REAL test count.**

## Baseline

`.venv/bin/pytest -q` gives **808 passed** as this plan is written. If the report-benchmark plan has
landed first the baseline is higher — run the suite, record what it actually says, and state every
later count as a delta against that.

## The pre-registered grid and bar — fixed, not to be edited

Six cells. Entry fires on `trend_dip`; entry price is the signal bar's close.

| Cell | Exit |
|---|---|
| `hold60` | close of bar 60. **Incumbent** — what the entry screen validated. |
| `stop2atr` | 2×ATR(20) below entry, else bar 60. **What the engine trades today.** |
| `stop3atr` | 3×ATR(20) below entry, else bar 60. |
| `stop4atr` | 4×ATR(20) below entry, else bar 60. |
| `trail3atr` | 3×ATR(20) below the highest close since entry, else bar 60. |
| `target2r_stop2atr` | target at entry + 4×ATR (2× the 2×ATR risk), stop 2×ATR, else bar 60. |

**Pass bar:** a challenger beats `hold60` if its mean paired per-date difference is positive AND the
date-clustered t on that difference is ≥ **2.58** (Bonferroni, five challengers, α=0.05).

**If nothing clears, `hold60` stands — and that is already an instruction to change the engine**,
because `hold60` is not what the engine trades. The bar is not to be relaxed after the numbers are
seen.

**Disclosure carried from spec §3:** `hold60` versus `stop2atr` has already been informally seen
(+3.97% vs +1.30% mean per trade over 112 backtest trades). That cell is confirmatory. The untested
cells are `stop3atr`, `stop4atr`, `trail3atr` and `target2r_stop2atr`.

## File structure

| File | Change |
|---|---|
| `scripts/exit_screen.py` | New: exit rules, paired observations, the verdict, `guard_holdout`. |
| `tests/test_exit_screen.py` | New. |

---

### Task 1: the exit rules

**Files:**
- Create: `scripts/exit_screen.py` (rules and constants only)
- Test: `tests/test_exit_screen.py`

Pure functions over a bar series. No database, no membership, no statistics yet — this task is the
part that must be exactly right, because every number the screen later prints rests on it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_exit_screen.py`:

```python
"""Unit tests for scripts/exit_screen.py, loaded from its path like the other script tests."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "exit_screen.py"
_spec = importlib.util.spec_from_file_location("exit_screen", SCRIPT)
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def _bars(closes, highs=None, lows=None, opens=None):
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    opens = opens or list(closes)
    d0 = date(2020, 1, 1)
    return [ex.Bar(d0 + timedelta(days=i), opens[i], highs[i], lows[i], closes[i], 1000)
            for i in range(n)]


def test_hold60_exits_at_the_close_of_the_sixtieth_bar():
    bars = _bars([100.0 + i for i in range(70)])
    out = ex.EXITS["hold60"](bars, 0, atr=5.0, horizon=60)
    assert out.index == 60 and out.price == pytest.approx(bars[60].close)
    assert out.reason == "HOLD"


def test_hold60_equals_the_entry_screens_forward_return():
    """The incumbent must BE the design the entry screen validated. If these ever diverge, this
    screen is not measuring what passed the bar, and the whole comparison is against the wrong
    thing."""
    bars = _bars([100.0 + i for i in range(70)])
    out = ex.EXITS["hold60"](bars, 0, atr=5.0, horizon=60)
    assert out.price / bars[0].close - 1.0 == pytest.approx(bars[60].close / bars[0].close - 1.0)


def test_a_stop_triggers_on_the_first_bar_whose_low_reaches_it():
    """Entry 100, ATR 5, so the 2xATR stop is 90. The low touches 89 on bar 3."""
    bars = _bars([100.0, 99.0, 95.0, 91.0, 99.0], lows=[100.0, 98.0, 94.0, 89.0, 98.0])
    out = ex.EXITS["stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.index == 3 and out.reason == "STOP"
    assert out.price == pytest.approx(90.0), "the stop level, not the bar's low"


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    """Matches check_exit's clamp in execution/backtest.py. Without it a bar that gaps far below
    the stop would book a loss smaller than the one actually taken."""
    bars = _bars([100.0, 80.0], opens=[100.0, 82.0], lows=[100.0, 79.0])
    out = ex.EXITS["stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "STOP" and out.price == pytest.approx(82.0)


def test_a_wider_stop_survives_what_a_narrower_one_does_not():
    """Entry 100, ATR 5: the 2xATR stop is 90 and the 3xATR stop is 85. A dip to 88 takes one."""
    bars = _bars([100.0] + [95.0] * 3 + [110.0] * 60, lows=[100.0, 94.0, 88.0, 94.0] + [109.0] * 60)
    assert ex.EXITS["stop2atr"](bars, 0, atr=5.0, horizon=60).reason == "STOP"
    assert ex.EXITS["stop3atr"](bars, 0, atr=5.0, horizon=60).reason == "HOLD"


def test_the_trail_ratchets_up_and_never_down():
    """Rises to 130, so the 3xATR trail sits at 115 and a fall to 114 exits -- even though the
    entry-based stop at 85 was never threatened."""
    closes = [100.0] + [110.0, 120.0, 130.0] + [114.0] + [130.0] * 60
    bars = _bars(closes, lows=[c * 0.999 for c in closes])
    out = ex.EXITS["trail3atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "TRAIL" and out.index == 4


def test_the_trail_uses_the_highest_close_since_entry_not_the_highest_high():
    """A single spiky high must not ratchet the trail somewhere price never closed."""
    closes = [100.0, 105.0, 104.0] + [104.0] * 60
    bars = _bars(closes, highs=[100.0, 140.0, 104.0] + [104.0] * 60)
    out = ex.EXITS["trail3atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "HOLD", "a 140 high would have trailed to 125 and exited immediately"


def test_a_target_exits_at_the_target_level():
    """Entry 100, ATR 5: risk is 2xATR = 10, so the 2R target is 120."""
    bars = _bars([100.0, 110.0, 125.0] + [100.0] * 60, highs=[100.0, 111.0, 126.0] + [101.0] * 60)
    out = ex.EXITS["target2r_stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "TARGET" and out.index == 2 and out.price == pytest.approx(120.0)


def test_a_stop_and_a_target_on_the_same_bar_resolve_as_a_stop():
    """Matches STOP_FIRST_ON_SAME_BAR in execution/backtest.py, so the screen and the engine cannot
    disagree about an ambiguous bar."""
    bars = _bars([100.0, 105.0], highs=[100.0, 125.0], lows=[100.0, 85.0])
    out = ex.EXITS["target2r_stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "STOP"


def test_an_exit_past_the_end_of_the_series_is_none():
    """A signal too close to the end of the data has no measurable outcome and must not be counted
    as a flat trade."""
    bars = _bars([100.0] * 10)
    assert ex.EXITS["hold60"](bars, 0, atr=5.0, horizon=60) is None


def test_every_cell_in_the_registered_grid_is_present():
    assert set(ex.EXITS) == {"hold60", "stop2atr", "stop3atr", "stop4atr",
                             "trail3atr", "target2r_stop2atr"}
    assert ex.INCUMBENT == "hold60"
    assert ex.BONFERRONI_T == pytest.approx(2.58)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_exit_screen.py -q`
Expected: collection fails — `scripts/exit_screen.py` does not exist.

- [ ] **Step 3: Write the implementation**

Create `scripts/exit_screen.py`. Start with the module docstring, the shared imports from
`swing_screen.py` (load it by path with `importlib`, exactly as `swing_screen.py` itself loads
`daily_screen.py` — `scripts/` is not a package), the grid constants, and the rules:

```python
"""Does any exit rule beat the one the entry screen actually validated?

    .venv/bin/python scripts/exit_screen.py insample

swing_screen.py measured ENTRIES with a pure horizon exit and no stop, and trend_dip cleared its
bar that way. config-daily.yaml then trades something else: it adds a 2xATR stop that no screen has
ever measured, and in the 2020-2023 backtest that stop produced 70 of 112 exits. The majority of
the traded system's behaviour comes from a component nobody tested.

The entry is FIXED at trend_dip 20/50/200 and is not varied here. Only the exit changes.

Every cell sees identical entries, so cells are compared as a PAIRED per-date difference against
hold60. The pairing is what makes this measurable: it removes the market variance that would
otherwise swamp an exit effect, and an unpaired test at this sample size would have little power.

DISCLOSURE: hold60 versus stop2atr has already been seen informally (+3.97% vs +1.30% mean per
trade over 112 backtest trades). That cell is confirmatory, not exploratory. The untested cells are
stop3atr, stop4atr, trail3atr and target2r_stop2atr.

Entry is at the signal bar's close, matching swing_screen's forward_return so hold60 IS the design
that passed. The ENGINE fills a bar later, at the next bar's close. That divergence is known, it
cancels out of a paired comparison, and it is not fixed here.

This reads the database read-only and writes nothing.
"""
```

Then the constants and rules:

```python
HORIZON = 60                      # trading days; the entry screen's primary cell
INCUMBENT = "hold60"
BONFERRONI_T = 2.58               # five challengers at alpha 0.05
INSAMPLE = (date(2020, 1, 1), date(2023, 12, 31))
HOLDOUT_START = date(2024, 1, 1)  # reserved; see guard_holdout

Exit = namedtuple("Exit", "index price reason")


def _horizon_exit(bars, i, horizon):
    j = i + horizon
    return Exit(j, bars[j].close, "HOLD") if j < len(bars) else None


def _stop_exit(bars, i, atr, horizon, mult):
    """A fixed stop `mult` x ATR below the entry close, else the horizon exit.

    The fill is clamped to the bar's open exactly as check_exit does in execution/backtest.py: a
    bar that gaps through the stop fills at the open, worse than the stop. Without the clamp a gap
    would book a smaller loss than the one actually taken.
    """
    if i + horizon >= len(bars):
        return None
    stop = bars[i].close - mult * atr
    for k in range(i + 1, i + horizon + 1):
        if bars[k].low <= stop:
            return Exit(k, min(bars[k].open, stop), "STOP")
    return _horizon_exit(bars, i, horizon)
```

Write `_trail_exit` and `_target_exit` in the same shape, then:

```python
EXITS = {
    "hold60": lambda bars, i, atr, horizon=HORIZON: _horizon_exit(bars, i, horizon),
    "stop2atr": lambda bars, i, atr, horizon=HORIZON: _stop_exit(bars, i, atr, horizon, 2.0),
    "stop3atr": lambda bars, i, atr, horizon=HORIZON: _stop_exit(bars, i, atr, horizon, 3.0),
    "stop4atr": lambda bars, i, atr, horizon=HORIZON: _stop_exit(bars, i, atr, horizon, 4.0),
    "trail3atr": lambda bars, i, atr, horizon=HORIZON: _trail_exit(bars, i, atr, horizon, 3.0),
    "target2r_stop2atr": lambda bars, i, atr, horizon=HORIZON: _target_exit(bars, i, atr, horizon),
}
```

Requirements the tests pin, restated so they are not lost in the lambdas:

- **`_trail_exit`** trails `mult × atr` below the **highest CLOSE since entry** (not the highest
  high), recomputed as the run progresses and never lowered. Its reason is `"TRAIL"`. It exits on
  the first bar whose low reaches the current trail level, clamped to the open like `_stop_exit`.
- **`_target_exit`** places the stop at 2×ATR below entry and the target at entry + 4×ATR — 2× the
  initial risk. On a bar that reaches both, **the stop wins**, matching
  `STOP_FIRST_ON_SAME_BAR` in `execution/backtest.py`. Reasons are `"STOP"` and `"TARGET"`; the
  target fill clamps to `max(bars[k].open, target)`.
- **Every rule returns `None` when `i + horizon >= len(bars)`**, so a signal too near the end of
  the data is dropped rather than counted as a flat trade. All six must agree on this, or the cells
  stop being paired.

- [ ] **Step 4: Run**

`.venv/bin/pytest tests/test_exit_screen.py -q` → 11 passed.
Then `.venv/bin/pytest -q` → the recorded baseline **+11**.

- [ ] **Step 5: Commit**

```bash
git add scripts/exit_screen.py tests/test_exit_screen.py
git commit -m "exit screen: the six registered exit rules

The entry screen measured a pure 60-bar hold with no stop; the engine trades a
2xATR stop that produced 70 of 112 exits and that no screen has measured. These
are the six cells that will be compared against the validated one.

Rules only -- nothing measures anything yet. The gap clamp and the
stop-wins-on-an-ambiguous-bar rule match execution/backtest.py so the screen and
the engine cannot disagree about a bar."
```

---

### Task 2: paired observations over the universe

**Files:**
- Modify: `scripts/exit_screen.py`
- Test: `tests/test_exit_screen.py`

Reuse `swing_screen.py`'s universe loading, corporate-action mask, point-in-time membership and cost
model. Do not reimplement any of them.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_exit_screen.py`:

```python
def test_every_cell_reports_the_same_entries():
    """The pairing IS the method. If cells ever see different entries, the per-date difference is
    comparing different trades and the t is meaningless."""
    raise NotImplementedError("fill in using this module's _bars helper and observations()")


def test_a_signal_whose_hold_spans_a_corporate_action_is_dropped_from_every_cell():
    """An unadjusted split makes the return fiction wherever it lands. Dropping it from one cell
    and not another would silently unpair them."""
    raise NotImplementedError("fill in using this module's _bars helper")


def test_returns_are_net_of_costs():
    raise NotImplementedError("fill in")
```

**Build these three on the helpers this file already has after Task 1**, plus whatever
`scripts/exit_screen.py` exposes. The placeholders are deliberate: the shape of `observations()` is
yours to choose in Step 3, and prescribing test bodies against an interface that does not exist yet
would send you chasing names. Each placeholder says what the test must prove.

- [ ] **Step 2: Run to verify they fail** — `NotImplementedError`, then real failures once written.

- [ ] **Step 3: Write the implementation**

Add to `scripts/exit_screen.py`:

- Load `swing_screen.py` by path with `importlib` and take from it: `Bar`, `Indicators`,
  `load_universe`, `is_member`, `hold_is_clean`, `trend_dip`, `date_means`, `t_of_means`, and
  `round_trip_cost` (which `swing_screen` itself takes from `daily_screen`). Reusing
  `hold_is_clean` matters: the mask must span the **whole** hold, and an exit rule that resolves
  early must still reject a hold spanning a split, because the entry was chosen on prices the split
  makes fiction.
- `observations(uni, window)`: for every member name-day `trend_dip` fires on, evaluate **all six**
  cells. If any cell returns `None`, **drop the observation from every cell** — that is what keeps
  them paired. Record `(symbol, entry_date, cell, net_return)` where the net return is
  `exit_price / entry_close - 1 - round_trip_cost(...)`.
- `paired_diffs(obs)`: per entry date, the mean challenger return minus the mean `hold60` return,
  one number per (cell, date).

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → baseline **+14**.

Then cross-check against the entry screen, which must still reproduce:

```bash
.venv/bin/python scripts/swing_screen.py insample
```

`trend_dip h=60` must still print `+0.618%/date t 2.96`. This plan changes nothing in that screen;
if it moves, something shared was edited and that is a STOP.

- [ ] **Step 5: Commit**

```bash
git add scripts/exit_screen.py tests/test_exit_screen.py
git commit -m "exit screen: paired observations over the point-in-time universe

Every cell sees identical entries and an observation any cell cannot resolve is
dropped from all of them, which is what keeps the comparison paired. The mask,
membership and cost model come from swing_screen rather than being written
twice."
```

---

### Task 3: the verdict, and the holdout guard

**Files:**
- Modify: `scripts/exit_screen.py`
- Test: `tests/test_exit_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_exit_screen.py`:

```python
def test_a_challenger_needs_both_a_positive_mean_and_the_bonferroni_t():
    assert ex.verdict(mean_diff=0.01, t=3.0) == "PASS"
    assert ex.verdict(mean_diff=0.01, t=2.0) == "fail"       # t below 2.58
    assert ex.verdict(mean_diff=-0.01, t=3.0) == "fail"      # beats on t, loses on sign
    assert ex.verdict(mean_diff=0.01, t=2.58) == "PASS"      # the boundary is inclusive


def test_the_incumbent_is_not_judged_against_itself():
    """hold60's paired difference with itself is identically zero; printing 'fail' beside it would
    read as evidence against the design that actually passed the entry screen."""
    assert ex.verdict_for_cell("hold60", mean_diff=0.0, t=0.0) == "incumbent"


def test_the_holdout_guard_refuses_without_the_flag():
    with pytest.raises(SystemExit) as e:
        ex.guard_holdout(False)
    assert "RESERVED" in str(e.value)


def test_the_holdout_guard_allows_with_the_flag():
    ex.guard_holdout(True)          # must not raise
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Write the implementation**

```python
def verdict(mean_diff, t):
    """A challenger beats the incumbent only on BOTH counts. The bar is 2.58, Bonferroni for five
    challengers at alpha 0.05, and it is not to be relaxed after the numbers are seen."""
    return "PASS" if (mean_diff > 0 and t >= BONFERRONI_T) else "fail"


def verdict_for_cell(cell, mean_diff, t):
    return "incumbent" if cell == INCUMBENT else verdict(mean_diff, t)


def guard_holdout(confirmed):
    """2024-01-01 onward is reserved and unspent. Looking at it before a cell passes in-sample
    turns the only honest out-of-sample test this project has into another in-sample one, and it
    cannot be un-looked-at."""
    if not confirmed:
        sys.exit("the holdout from %s is RESERVED and unspent. Pass --i-am-sure to spend it, "
                 "and only after a cell has passed in-sample." % HOLDOUT_START)
```

Add `run_phase(conn, window, label)` printing one row per cell — observations, dates, mean net
return per trade, mean paired difference per date, t, and verdict — and a `main()` with
`insample` and `holdout` subcommands, the latter behind `guard_holdout`, modelled on
`swing_screen.main`. Print the registered bar and the `hold60`-vs-`stop2atr` disclosure beneath the
table, so a reader of the output alone cannot mistake that cell for a fresh discovery.

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → baseline **+18**.

- [ ] **Step 5: Commit**

```bash
git add scripts/exit_screen.py tests/test_exit_screen.py
git commit -m "exit screen: the pre-registered bar and the holdout guard

A challenger needs a positive mean paired difference AND t >= 2.58, Bonferroni
for five challengers. The incumbent is labelled rather than judged against
itself. The holdout refuses to run without an explicit flag."
```

---

### Task 4: run it, in-sample only

**Files:** none. This is the measurement.

- [ ] **Step 1: Run the screen**

```bash
.venv/bin/python scripts/exit_screen.py insample
```

**Do not pass `--i-am-sure`. Do not run the holdout phase.**

- [ ] **Step 2: Report, as facts**

- the full table, verbatim
- which cells passed, if any, and their mean paired difference and t
- `hold60` versus `stop2atr` specifically: the direction and size, stated as **confirmatory** of the
  informal +3.97%/+1.30% figures rather than as a new finding
- the observation count and date count, and whether every cell reports the same ones — if they
  differ, the pairing is broken and that is the finding, outranking every number in the table

- [ ] **Step 3: Say plainly what it means for the engine**

Whatever wins — including `hold60` — is **not** to be wired into `config-daily.yaml` as part of
this plan. State the implication and stop:

- If `hold60` or a wide-stop cell wins, **position sizing breaks**: `risk/engine.py` divides planned
  risk by stop distance, and there is nothing to divide by without a stop. That is separate work and
  this screen does not solve it.
- A per-trade result is not a portfolio result. Trades that are not stopped occupy a slot for longer,
  so with five slots the portfolio takes fewer of them. The two can disagree and this screen
  measures only the first.

- [ ] **Step 4: Write the results note**

Create `docs/superpowers/notes/2026-09-23-exit-screen-results.md`, following the shape of
`docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`: what was measured, the
table, what passed and what did not, the limits, and a conclusion that separates *supported* from
*not supported*. Record that the holdout was deliberately not run, and why.

Commit it:

```bash
git add docs/superpowers/notes/2026-09-23-exit-screen-results.md
git commit -m "notes: exit screen results, in-sample"
```

---

## Self-review

**Spec coverage.** §2 fixed entry and the six cells → Task 1, with the grid pinned by a test.
§3 disclosure → Task 1's docstring and Task 4's reporting instruction. §4 unit, metric and the
paired baseline → Task 2. §5 pass bar → Task 3, boundary tested inclusive. §6 windows and the
holdout → the standing constraints and Task 3's guard. §7 the sizing problem a winner creates →
Task 4 Step 3, stated and not solved. §8 scope → the standing constraints. §9 testing → every task,
including the `hold60` ≡ `forward_return` equivalence that proves the incumbent is the validated
design.

**Knowingly deferred.** The holdout, position sizing, portfolio simulation, any engine change, and
any exit outside the six cells. The first is a decision already taken; the second is the reason
Task 4 stops at a recommendation.

**Placeholders.** Three, all in Task 2 Step 1, because the shape of `observations()` is chosen in
Step 3 of that same task and prescribing tests against an interface that does not exist yet would
send the implementer chasing names. Each says what the test must prove.

**Type consistency.** `Exit = namedtuple("Exit", "index price reason")` is returned by every entry
in `EXITS` and read in Tasks 2 and 4. Every rule takes `(bars, i, atr, horizon)` and returns
`Optional[Exit]`. `verdict(mean_diff, t) -> str` in Task 3 is called by `verdict_for_cell`, which
is what `run_phase` prints. `INCUMBENT`, `BONFERRONI_T` and `HORIZON` are module constants defined
in Task 1 and read in Tasks 2 and 3.
