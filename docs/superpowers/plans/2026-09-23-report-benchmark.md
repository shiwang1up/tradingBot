# Report benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Print, on every backtest report, what equal-weight buy-and-hold of the same names over the same window returned net of the same costs — so "did selecting beat not selecting?" stops being a calculation nobody performs.

**Architecture:** A new pure-ish module `src/tradebot/report/benchmark.py` computes a `Benchmark` value object from stored candles. `Summary` carries an optional one and `format_summary` renders it only when present, so every existing caller and test is untouched. `cli.py` builds it and passes it in, keeping I/O out of `build_summary` — the same split `tradebot hurdle` uses.

**Tech Stack:** Python 3.9, pytest, click. `src/` modules carry `from __future__ import annotations`; keep it.

**Spec:** `docs/superpowers/specs/2026-09-23-report-benchmark-design.md`

**Branch:** `dev-daily-engine` (current).

---

## Standing constraints

- **Never write to anything under `data/`.** Read it only via
  `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`, or through `Repo`, which opens the
  database the config names — reads only, and this plan performs no writes.
- **Do not run** `tradebot paper`, `tradebot fetch-data`, `tradebot backtest`,
  `scripts/orb_experiment.py`, `scripts/momentum_screen.py`, `scripts/daily_screen.py`, or
  `scripts/swing_screen.py`. `tradebot report` IS allowed and Task 4 uses it.
- **The holdout, 2024-01-01 onward, is RESERVED AND UNSPENT.** Nothing here touches it. The runs
  this plan reports on already exist; none is re-run.
- **This system places no orders.** Nothing here moves it closer to doing so.
- Python 3.9. `.venv/bin/pytest`, `.venv/bin/python`, `.venv/bin/tradebot`.
- **Stage explicit paths by name. Never `git add -u` or `git add -A`.**
- **No Claude attribution in commit messages** — no `Co-Authored-By`, no "Generated with".
- `tests/fixtures/golden_trades.json` must stay byte-identical. Check after every task with
  `git diff --stat main..HEAD -- tests/fixtures/golden_trades.json`, which must print nothing.
  If it moves, STOP and report.
- **Report the REAL test count.**

## Baseline

`.venv/bin/pytest -q` gives **808 passed**.

## The numbers this plan must reproduce

Computed against `daily-perf-groww` while writing the spec. Task 4 asserts them end to end; if the
implementation produces anything else, that is a finding to report, not a number to adjust.

| | |
|---|---|
| Window | 2020-01-01 to 2023-12-29 |
| Universe | 50 names, all with at least two candles |
| Slice | 2,000.00 |
| Held | **39** |
| Skipped (one share dearer than the slice) | **11** |
| Gross | +100,693.20 |
| Charges | 1,663.49 |
| **Net** | **+99,029.71** |
| That run's own net | +14,043.25 |
| Difference | **−84,986.46** |

## File structure

| File | Change |
|---|---|
| `src/tradebot/report/benchmark.py` | New: `Benchmark` and `build_benchmark`. |
| `src/tradebot/report/summary.py` | `Summary.benchmark`, rendered by `format_summary`. |
| `src/tradebot/cli.py` | Build the benchmark and pass it at both `format_summary` sites. |
| `tests/test_benchmark.py` | New. |
| `tests/test_report.py` | Rendering, present and absent. |
| `tests/test_cli.py` | End-to-end against `daily-perf-groww`. |

---

### Task 1: the benchmark calculation

**Files:**
- Create: `src/tradebot/report/benchmark.py`
- Test: `tests/test_benchmark.py`

Pure arithmetic over candles handed in by the caller. No database access in this module: the caller
loads candles, so the calculation can be tested without a database at all.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_benchmark.py`:

```python
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
    candles = [_c("A", 1000, 100.0), _c("A", 2000, 50.0)]
    b = build_benchmark(candles, capital=10_000.0, charges=None)
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_benchmark.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.report.benchmark'`

- [ ] **Step 3: Write the implementation**

Create `src/tradebot/report/benchmark.py`:

```python
"""What holding the basket would have returned, so a run can be judged against not selecting.

Every statistic a report prints -- win rate, payoff, expectancy, drawdown, t -- describes the
strategy against itself. None of them answers whether picking beat not picking. On the best
in-sample run this project has produced, the answer was no by a factor of seven, and nothing in the
output said so.

This is an equal-weight basket, not an index: there are no market caps in this database, so it
cannot be capitalisation-weighted and must not be labelled as an index. It is bought once and sold
once, with no rebalancing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from tradebot.config import ChargesConfig
from tradebot.execution.charges import round_trip_charges
from tradebot.types import Candle


@dataclass(frozen=True)
class Benchmark:
    gross: float
    charges: float
    net: float
    names_held: int      # bought at least one share
    names_skipped: int   # priced, but one share cost more than the slice
    slice_value: float   # capital / priced names, before whole-share truncation


def build_benchmark(candles: Iterable[Candle], capital: float,
                    charges: Optional[ChargesConfig]) -> Optional[Benchmark]:
    """Equal-weight buy-and-hold over `candles`, or None when there is no basket to build.

    Each symbol is bought at its earliest close in `candles` and sold at its latest. A symbol with
    fewer than two candles is not a round trip and takes no slice -- counting it would shrink every
    other name's allocation. A symbol whose single share costs more than the slice cannot be bought
    at all; its slice stays in cash and is NOT redistributed, because a real buyer would not have
    bought more of the others instead.
    """
    first: dict = {}
    last: dict = {}
    counts: dict = {}
    for c in candles:
        counts[c.symbol] = counts.get(c.symbol, 0) + 1
        if c.symbol not in first or c.ts < first[c.symbol].ts:
            first[c.symbol] = c
        if c.symbol not in last or c.ts > last[c.symbol].ts:
            last[c.symbol] = c
    priced = [s for s in first if counts[s] >= 2 and first[s].close > 0]
    if not priced:
        return None
    slice_value = capital / len(priced)
    gross = charged = 0.0
    held = skipped = 0
    for sym in priced:
        buy, sell = first[sym].close, last[sym].close
        qty = math.floor(slice_value / buy)
        if qty < 1:
            skipped += 1
            continue
        held += 1
        gross += (sell - buy) * qty
        charged += round_trip_charges(buy * qty, sell * qty, charges, product="CNC")
    if held == 0:
        return None
    return Benchmark(gross=round(gross, 2), charges=round(charged, 2),
                     net=round(gross - charged, 2), names_held=held,
                     names_skipped=skipped, slice_value=slice_value)
```

- [ ] **Step 4: Run**

`.venv/bin/pytest tests/test_benchmark.py -q` → 9 passed.
Then `.venv/bin/pytest -q` → **817 passed**.

**If any pre-existing test fails, STOP.** Nothing here touches an existing code path.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/report/benchmark.py tests/test_benchmark.py
git commit -m "benchmark: what holding the basket would have returned

Every statistic a report prints describes the strategy against itself. None
answers whether picking beat not picking, and on the best in-sample run this
project has produced the answer is no by a factor of seven.

Equal-weight, not an index -- there are no market caps here. Whole shares only,
which at a 2,000 slice cannot buy eleven of universe.yaml's fifty names at all;
their slice stays in cash rather than being redistributed, and the count is
carried on the result so the report can say so.

Nothing renders it yet."
```

---

### Task 2: carry it on `Summary` and render it

**Files:**
- Modify: `src/tradebot/report/summary.py`
- Test: `tests/test_report.py`

Optional throughout. A `Summary` without a benchmark renders exactly the text it renders today, and
the existing report tests are the guard.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_report.py`:

```python
def test_a_summary_without_a_benchmark_renders_exactly_as_before(tmp_path):
    """Every existing caller passes none. The block must be absent, not empty or zeroed."""
    s = _summary_for_rendering()          # see note below
    text = format_summary(s)
    assert "Benchmark" not in text
    assert "selecting" not in text


def test_the_benchmark_block_renders_when_present(tmp_path):
    from tradebot.report.benchmark import Benchmark
    b = Benchmark(gross=100_693.20, charges=1_663.49, net=99_029.71,
                  names_held=39, names_skipped=11, slice_value=2_000.0)
    s = replace(_summary_for_rendering(), benchmark=b)
    text = format_summary(s)
    assert "99,029.71" in text
    assert "39 of 50 names" in text or "39" in text
    assert "11" in text, "the skipped count must be visible, not silently dropped"


def test_the_verdict_says_which_side_won():
    from tradebot.report.benchmark import Benchmark
    base = _summary_for_rendering()
    losing = replace(base, total_pnl=14_043.25,
                     benchmark=Benchmark(1.0, 0.0, 99_029.71, 39, 11, 2_000.0))
    winning = replace(base, total_pnl=200_000.0,
                      benchmark=Benchmark(1.0, 0.0, 99_029.71, 39, 11, 2_000.0))
    assert "lost to not selecting" in format_summary(losing)
    assert "beat not selecting" in format_summary(winning)


def test_the_difference_is_strategy_minus_benchmark():
    from tradebot.report.benchmark import Benchmark
    s = replace(_summary_for_rendering(), total_pnl=14_043.25,
                benchmark=Benchmark(1.0, 0.0, 99_029.71, 39, 11, 2_000.0))
    assert "-84,986.46" in format_summary(s)
```

**Read `tests/test_report.py` first.** It already builds `Summary` objects for rendering tests —
use whatever it uses and name the helper accordingly instead of adding `_summary_for_rendering`; the
placeholder name above marks the one thing this plan cannot know without reading that file. Add
`from dataclasses import replace` to its imports if absent.

- [ ] **Step 2: Run to verify they fail**

Expected: `TypeError: __init__() got an unexpected keyword argument 'benchmark'`.

- [ ] **Step 3: Write the implementation**

In `src/tradebot/report/summary.py`, add to the `Summary` dataclass, last, so field ordering holds:

```python
    benchmark: Optional["Benchmark"] = None   # equal-weight buy & hold over the same window
```

and at the top of the module:

```python
from tradebot.report.benchmark import Benchmark
```

In `format_summary`, after the `Total PnL` line is appended to `lines`, insert:

```python
    if s.benchmark is not None:
        b = s.benchmark
        verdict = "beat" if s.total_pnl > b.net else "lost to"
        held = f"{b.names_held} of {b.names_held + b.names_skipped} names"
        lines.append(f"Benchmark             {b.net:,.2f}   equal-weight buy & hold, {held}, "
                     f"net of one round trip")
        if b.names_skipped:
            # Not a footnote: at a 2,000 slice this drops the highest-priced names, and a basket
            # presented as "the universe" while holding four fifths of it would be its own lie.
            lines.append(f"                      {b.names_skipped} name(s) skipped: one share cost "
                         f"more than the {b.slice_value:,.0f} slice")
        lines.append(f"Strategy vs benchmark {s.total_pnl - b.net:+,.2f}   "
                     f"selecting {verdict} not selecting")
```

Place these lines immediately after `Total PnL` and before `Avg R (per trade)`.

- [ ] **Step 4: Run**

`.venv/bin/pytest tests/test_report.py -q`, then `.venv/bin/pytest -q` → **821 passed**.

**If any pre-existing report test fails, STOP.** With no benchmark the output must be identical.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/report/summary.py tests/test_report.py
git commit -m "report: render the benchmark when there is one

Optional throughout, so every existing caller renders byte-identical text and
the existing report tests are the guard. The skipped-name count gets its own
line rather than a footnote: at a 2,000 slice the basket drops the eleven
highest-priced names, and calling that 'the universe' would be its own lie."
```

---

### Task 3: build it in the CLI

**Files:**
- Modify: `src/tradebot/cli.py`
- Test: `tests/test_cli.py`

Two call sites construct a summary today: the `backtest` command's closing `format_summary`, and
the `report` command. Both get the benchmark.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`, following that file's existing `CliRunner` pattern:

```python
def test_report_prints_a_benchmark(tmp_path):
    """The whole point: a report must say what not selecting would have returned."""
    # build a run in a tmp database with two symbols and a handful of daily candles, then
    # `report --run <id>` and assert "Benchmark" and "selecting" appear in the output.
    raise NotImplementedError("fill in using this module's existing CliRunner + repo fixtures")
```

**Read `tests/test_cli.py` and build this on its existing fixtures** — it already creates runs in a
temporary database for the `report` tests. The placeholder is deliberate: this plan has not read
that file's fixtures and inventing names would send you chasing code that does not exist. If its
harness cannot express a run with daily candles, say so and report NEEDS_CONTEXT rather than
building a parallel one.

- [ ] **Step 2: Run to verify it fails** — `NotImplementedError`, then a real failure once written.

- [ ] **Step 3: Write the implementation**

Add to `cli.py`'s imports:

```python
from tradebot.report.benchmark import build_benchmark
```

Add this helper beside the other module-level helpers in `cli.py`:

```python
def _benchmark_for(repo: Repo, cfg: Config, run_id: str):
    """Equal-weight buy & hold over the run's own window, or None when it cannot be built.

    The universe comes from the config this command was given, not from the run's stored config, so
    reporting an old run against a since-edited universe.yaml benchmarks it against a basket it
    never faced. The rendered line names the counts, which makes a mismatch visible; recording the
    resolved symbol list on the run would fix it properly and is a schema change.
    """
    days = repo.daily_pnl(run_id)
    if not days:
        return None
    run = repo.get_run(run_id)
    if run is None:
        return None
    interval = json.loads(run["config_json"])["execution"]["interval_minutes"]
    symbols = list(load_universe(cfg.paths.universe).symbols)
    lo = ist_epoch(date.fromisoformat(days[0]["date"]), "00:00")
    hi = ist_epoch(date.fromisoformat(days[-1]["date"]), "23:59")
    return build_benchmark(repo.load_candles(symbols, interval, lo, hi), cfg.capital, cfg.charges)
```

`date` (line 10), `load_universe` (24), `ist_epoch` (25), `Repo` (36) and `Config` (20) are already
imported in `cli.py`. **`json` is not** — add `import json` to the stdlib import block at the top.
Verify each of these yourself rather than trusting this list; an earlier plan in this repository
asserted a test helper existed when it did not, and blocked its implementer.

Then at both `format_summary` sites, pass the benchmark through. In the `report` command:

```python
    click.echo(format_summary(build_summary(repo, run_id, cfg.charges,
                                            benchmark=_benchmark_for(repo, cfg, run_id))))
```

and the same at the `backtest` command's closing line, using `rid`.

Give `build_summary` the matching keyword-only parameter in `summary.py`:

```python
def build_summary(repo: Repo, run_id: str, schedule: Optional[ChargesConfig] = None,
                  since_ts: Optional[int] = None, *, benchmark: Optional[Benchmark] = None) -> Summary:
```

and set it on the returned `Summary`. Keyword-only so no positional caller can be broken by the new
argument.

- [ ] **Step 4: Run**

`.venv/bin/pytest -q` → the Task 2 count plus however many tests you added. Report the real number.

- [ ] **Step 5: Confirm the fixture did not move**

```bash
git diff --stat main..HEAD -- tests/fixtures/golden_trades.json    # must print nothing
```

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/cli.py src/tradebot/report/summary.py tests/test_cli.py
git commit -m "cli: every report says what not selecting would have returned

Both format_summary sites -- backtest's closing summary and the report command
-- now build the benchmark. build_summary takes it keyword-only so no positional
caller can break.

The universe comes from the config the command was given rather than the run's
stored config, so an old run reported against an edited universe.yaml is
benchmarked against a basket it never faced. The rendered counts make that
visible; fixing it properly needs the resolved symbol list stored on the run."
```

---

### Task 4: read it against a real run

**Files:** none. This is the measurement that proves the machinery.

- [ ] **Step 1: Report an existing run**

```bash
.venv/bin/tradebot --config config-daily.yaml report --run daily-perf-groww 2>&1 | head -12
```

`daily-perf-groww` already exists in the database. **Do not re-run any backtest.**

- [ ] **Step 2: Check it against the figures fixed in this plan**

The benchmark block must read: net **+99,029.71**, **39 of 50 names** held, **11** skipped at a
**2,000** slice, and `Strategy vs benchmark -84,986.46   selecting lost to not selecting`.

**If any figure differs, STOP and report it.** These were computed directly from the database while
the spec was written; a mismatch means the implementation and the spec disagree, and the plan is not
the authority on which is right — report the difference and let it be judged.

- [ ] **Step 3: Report, as facts**

- the benchmark block exactly as printed
- whether each of the five figures above matches
- whether `report` on a run with no daily rows omits the block cleanly rather than erroring

- [ ] **Step 4: Commit the finding**

No code change. If Steps 1-3 produced a discrepancy, do not commit a fix for it without saying so
first — report and stop.

---

## Self-review

**Spec coverage.** §2 what it is and is not → Task 1's docstring and Task 2's rendered labels.
§3.1 window from `daily_pnl` → Task 3's `_benchmark_for`. §3.2 slice, whole shares and the
unaffordable case → Task 1, tested four ways. §3.3 costs at the run's schedule → Task 1, with a
cross-broker test. §3.4 exclusions counted → Task 1 and the rendered skipped line. §4 output and the
mechanical verdict → Task 2. §5 module placement and the universe-drift limitation → Tasks 1 and 3,
the limitation documented in `_benchmark_for`'s docstring. §6 scope → the standing constraints.
§7 testing → every task.

**Knowingly deferred.** Rebalancing, cap weighting, an index tracker, benchmarking `--compare`, and
storing the resolved symbol list on the run — all named out of scope by §6, and the last is the one
that would remove a real limitation rather than add a feature.

**Placeholders.** Two, both deliberate and both naming the file to read and what the test must
prove: `tests/test_report.py`'s existing `Summary`-building helper, and `tests/test_cli.py`'s run
fixtures. Inventing either would send the implementer chasing code that does not exist.

**Type consistency.** `build_benchmark(candles, capital, charges) -> Optional[Benchmark]` in Task 1
is called with three positional arguments in Task 3. `Benchmark`'s six fields are constructed in
Task 1 and read in Task 2 (`net`, `names_held`, `names_skipped`, `slice_value`) and in Task 1's
tests (`gross`, `charges`). `Summary.benchmark` is `Optional[Benchmark]` defaulting to `None` in
Task 2 and set keyword-only in Task 3.
