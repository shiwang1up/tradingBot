# Expectancy Reporting, Consistent Risk and a Daily Systems Screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report expectancy in its own terms, make every trade risk about the same amount, and screen three classic daily systems over ~6 years of daily candles for expectancy in excess of a same-holding-period baseline.

**Architecture:** Parts 1 and 2 are small changes inside existing modules (`report/summary.py`, `report/compare.py`, `risk/engine.py`, `config.py`). Part 3 is a self-contained research script, `scripts/daily_screen.py`, that reads the `candles` table at interval 1440 and touches no engine code; it is tested through `tests/test_daily_screen.py`, which imports it by file path.

**Tech Stack:** Python 3.9 (every module uses `from __future__ import annotations`; scripts stay 3.9-safe without it), SQLite, click (CLI only, not the script), pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md`. Read it first; it fixes the three systems' rules, the cost schedule and the pass bar, and those must not be changed in response to any result.

**Branch:** `expectancy-daily-screen` (already created, stacked on `charges-orb-regime`).

**Baseline:** `.venv/bin/pytest -q` from the repo root gives `542 passed`. Every task ends with the full suite green.

**Commit trailer:** end every commit message with this line, after a blank line:
`Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

**Data:** daily candles (interval 1440) for the 49 universe symbols from 2020-01-01 to about 2025-11-21 are being loaded into `data/tradebot.db` outside this plan. A backup is at `data/tradebot.pre-daily.bak.db`. NEVER write to anything under `data/`; the screen opens the database read-only:
`sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`.

## File map

| File | Change | Responsibility |
|---|---|---|
| `src/tradebot/report/summary.py` | modify | expectancy fields on `Summary`, their computation, three printed lines |
| `src/tradebot/report/compare.py` | modify | three rows in the side-by-side table |
| `src/tradebot/config.py` | modify | `RiskConfig.min_risk_fraction` and its validation |
| `src/tradebot/risk/engine.py` | modify | the `risk_too_small` check |
| `config.yaml`, `config-15m.yaml`, `config-orb.yaml` | modify | `min_risk_fraction: 0.5` |
| `tests/helpers.py` | modify | base config keeps the rule off |
| `scripts/daily_screen.py` | create | the whole screen: data, gap mask, indicators, systems, costs, baseline, statistics, phases |
| `tests/test_daily_screen.py` | create | the script's unit tests |
| `tests/test_report.py`, `tests/test_compare.py`, `tests/test_risk.py`, `tests/test_config.py` | modify | new cases |
| `docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md` | create | results |
| `README.md` | modify | three short sections |

---

# Part 1 — expectancy in reports

### Task 1: expectancy figures on `Summary`

**Files:**
- Modify: `src/tradebot/report/summary.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_report.py`. Read the top of that file first: it has a `_seed(repo)` helper and the tests use the `repo` fixture from `tests/conftest.py`. `close_position` requires the keyword `charges=`.

```python
def test_expectancy_identity_payoff_and_breakeven(repo):
    """Four closed trades with stored charges: two wins of +20 and +10 net, two losses of -30 and -10 net.
    avg_win 15, avg_loss -20, payoff 0.75, expectancy (2/4)*15 - (2/4)*20 = -2.5 = total/trades = -10/4."""
    repo.create_run("e1", "backtest", 0, "{}")
    for i, (exit_price, pnl, ch) in enumerate([(102.0, 25.0, 5.0), (101.0, 15.0, 5.0),
                                               (97.0, -25.0, 5.0), (99.0, -5.0, 5.0)]):
        p = Position("S%d" % i, "MIS", "LONG", 10, 100.0, 99.0, None, i, "c%d" % i, "ema_rsi")
        repo.close_position(repo.insert_position("e1", p), 10 + i, exit_price, "TARGET", pnl, charges=ch)
    s = build_summary(repo, "e1")
    assert (s.trades, s.wins, s.losses) == (4, 2, 2)
    assert s.avg_win == pytest.approx(15.0)
    assert s.avg_loss == pytest.approx(-20.0)
    assert s.payoff == pytest.approx(0.75)
    assert s.expectancy == pytest.approx(-2.5)
    # the identity the printed line claims
    assert s.expectancy == pytest.approx(s.win_rate * s.avg_win - (1 - s.win_rate) * abs(s.avg_loss))
    assert s.expectancy == pytest.approx(s.total_pnl / s.trades)
    assert s.breakeven_win_rate == pytest.approx(1 / (1 + 0.75))


def test_expectancy_is_zero_and_payoff_zero_without_trades(repo):
    repo.create_run("e2", "backtest", 0, "{}")
    s = build_summary(repo, "e2")
    assert (s.avg_win, s.avg_loss, s.payoff, s.expectancy, s.breakeven_win_rate) == (0.0, 0.0, 0.0, 0.0, 0.0)
    assert (s.evidence_days, s.evidence_t) == (0, None)


def test_evidence_is_computed_across_close_dates_not_trades(repo):
    """Six trades on three IST dates: day sums +100, -50, +10. mean 20; deviations +80, -70, -10,
    so var = (6400 + 4900 + 100) / 2 = 5700, sd 75.498344, se 43.588989, t 0.458831. Trades on one
    day are correlated, so the day is the unit."""
    from tradebot.engine.clock import ist_epoch
    from datetime import date
    repo.create_run("e3", "backtest", 0, "{}")
    per_day = {date(2026, 9, 14): [60.0, 40.0], date(2026, 9, 15): [-20.0, -30.0], date(2026, 9, 16): [30.0, -20.0]}
    i = 0
    for d, pnls in per_day.items():
        for pnl in pnls:
            p = Position("S%d" % i, "MIS", "LONG", 10, 100.0, 99.0, None, ist_epoch(d, "09:20"), "c%d" % i, "ema_rsi")
            repo.close_position(repo.insert_position("e3", p), ist_epoch(d, "10:00"), 100.0 + pnl / 10,
                                "TARGET", pnl, charges=0.0)
            i += 1
    s = build_summary(repo, "e3")
    assert s.evidence_days == 3
    assert s.evidence_mean == pytest.approx(20.0)
    assert s.evidence_t == pytest.approx(0.458831, abs=1e-5)


def test_evidence_t_is_none_without_variance_or_enough_days(repo):
    from tradebot.engine.clock import ist_epoch
    from datetime import date
    repo.create_run("e4", "backtest", 0, "{}")
    for i in range(2):                      # two trades, one day: n=1, no t
        p = Position("S%d" % i, "MIS", "LONG", 10, 100.0, 99.0, None, ist_epoch(date(2026, 9, 14), "09:20"),
                     "c%d" % i, "ema_rsi")
        repo.close_position(repo.insert_position("e4", p), ist_epoch(date(2026, 9, 14), "10:00"), 101.0,
                            "TARGET", 10.0, charges=0.0)
    s = build_summary(repo, "e4")
    assert s.evidence_days == 1 and s.evidence_t is None
```

Add `from tradebot.config import ChargesConfig` and `Position` to the file's imports if they are not already there (check the top of the file).

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_report.py -q -k expectancy or evidence`
Expected: `AttributeError: 'Summary' object has no attribute 'avg_win'`.

- [ ] **Step 3: Implement**

In `src/tradebot/report/summary.py`, add to the module docstring's conventions:

```
- Expectancy is the net PnL per trade; the printed decomposition
  `win_rate x avg_win - loss_rate x |avg_loss|` is the same number by identity, shown because it is
  the form the trade-off between win rate and payoff is usually argued in. The breakeven win rate is
  1 / (1 + payoff): the win rate this system's own payoff would need to break even.
- Evidence (days, mean, t) is computed from the trades' NET PnL summed per IST close date, NOT from
  the daily_pnl rows: those are gross for runs whose charges were estimated after the fact, and one
  source keeps the block internally consistent. The day is the unit because same-day trades are
  correlated.
```

Add these fields to `Summary`, with the other defaulted fields, directly above `days`:

```python
    avg_win: float = 0.0             # mean net PnL of winning trades
    avg_loss: float = 0.0            # mean net PnL of the rest, negative; a scratch counts as a loss
    payoff: float = 0.0              # avg_win / |avg_loss|
    expectancy: float = 0.0          # net PnL per trade
    breakeven_win_rate: float = 0.0  # 1 / (1 + payoff): what this payoff would need to break even
    evidence_days: int = 0           # days with at least one closed trade
    evidence_mean: float = 0.0       # mean net PnL per such day
    evidence_t: Optional[float] = None  # None when fewer than 2 days or no variance
```

Add above `build_summary`:

```python
def _day_t(day_pnls: list) -> Tuple[float, Optional[float]]:
    """(mean, t) of a per-day PnL series. t is None with fewer than two days or no variance: a
    single day, or a run of identical days, says nothing about whether the mean differs from zero."""
    n = len(day_pnls)
    if n == 0:
        return 0.0, None
    mean = sum(day_pnls) / n
    if n < 2:
        return mean, None
    var = sum((p - mean) ** 2 for p in day_pnls) / (n - 1)
    if var <= 0:
        return mean, None
    return mean, mean / math.sqrt(var / n)
```

and `import math` beside the other stdlib imports.

In `build_summary`, after `wins = sum(1 for p in pnls if p > 0)`:

```python
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    payoff = avg_win / abs(avg_loss) if avg_loss < 0 and avg_win > 0 else 0.0
    by_day: dict = {}
    for r, p in zip(closed, pnls):
        by_day[date_of(r["closed_at"])] = by_day.get(date_of(r["closed_at"]), 0.0) + p
    evidence_mean, evidence_t = _day_t([by_day[d] for d in sorted(by_day)])
```

and pass to `Summary(...)`:

```python
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=payoff,
        expectancy=(sum(pnls) / len(pnls)) if pnls else 0.0,
        breakeven_win_rate=(1 / (1 + payoff)) if payoff > 0 else 0.0,
        evidence_days=len(by_day),
        evidence_mean=evidence_mean,
        evidence_t=evidence_t,
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: `546 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/report/summary.py tests/test_report.py
git commit -m "report: expectancy, payoff, breakeven win rate and per-day evidence on Summary"
```

### Task 2: print them, in the summary and the compare table

**Files:**
- Modify: `src/tradebot/report/summary.py` (`format_summary`), `src/tradebot/report/compare.py` (`_side_by_side`)
- Test: `tests/test_report.py`, `tests/test_compare.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_report.py`:

```python
def test_format_summary_prints_the_expectancy_block(repo):
    _seed(repo)                     # 4 trades, 2 wins, from the existing helper
    text = format_summary(build_summary(repo, "r1"))
    assert "Avg win / avg loss" in text and "payoff" in text
    assert "Expectancy" in text and "Breakeven win rate" in text and "Evidence" in text


def test_evidence_line_says_too_few_below_the_thresholds(repo):
    _seed(repo)                     # 4 trades on few days
    text = format_summary(build_summary(repo, "r1"))
    assert "too few to judge" in text


def test_evidence_line_flags_a_weak_t(repo):
    """25 days x 2 trades, day sums alternating +4 and -4: 50 trades and 25 days clear the
    thresholds, but the mean (0.16) is tiny against the spread (t about 0.2), so the line must say
    so rather than let a reader take the sign seriously. The days must differ: identical days have
    no variance and t would be n/a instead."""
    from tradebot.engine.clock import ist_epoch
    from datetime import date, timedelta
    repo.create_run("w1", "backtest", 0, "{}")
    i = 0
    for k in range(25):
        d = date(2026, 6, 1) + timedelta(days=k)
        for pnl in ((100.0, -96.0) if k % 2 == 0 else (100.0, -104.0)):
            p = Position("S%d" % i, "MIS", "LONG", 10, 100.0, 99.0, None, ist_epoch(d, "09:20"), "c%d" % i, "ema_rsi")
            repo.close_position(repo.insert_position("w1", p), ist_epoch(d, "10:00"), 100.0 + pnl / 10,
                                "TARGET", pnl, charges=0.0)
            i += 1
    s = build_summary(repo, "w1")
    assert s.trades == 50 and s.evidence_days == 25
    assert "not distinguishable from zero" in format_summary(s)
```

Append to `tests/test_compare.py`:

```python
def test_side_by_side_carries_the_expectancy_rows(repo):
    _seed_pair(repo)
    text = format_compare(build_compare(repo, "A", "B", P))
    assert "Payoff" in text and "Expectancy" in text and "t (days)" in text
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_report.py tests/test_compare.py -q`
Expected: assertion failures on the missing strings.

- [ ] **Step 3: Implement**

In `format_summary`, build the evidence text before `lines`:

```python
    if s.trades < 30 or s.evidence_days < 20:
        evidence = (f"{s.evidence_days} days, mean {s.evidence_mean:,.2f} per day   "
                    f"too few to judge (needs 30+ trades and 20+ days)")
    elif s.evidence_t is None:
        evidence = f"{s.evidence_days} days, mean {s.evidence_mean:,.2f} per day, t n/a"
    else:
        weak = "   not distinguishable from zero" if abs(s.evidence_t) < 2 else ""
        evidence = f"{s.evidence_days} days, mean {s.evidence_mean:,.2f} per day, t {s.evidence_t:.1f}{weak}"
```

and insert three lines directly after the `R on risk` line:

```python
        f"Avg win / avg loss    {s.avg_win:+,.2f} / {s.avg_loss:+,.2f}   payoff {s.payoff:.2f}",
        f"Expectancy            {s.expectancy:,.2f} per trade = {s.win_rate * 100:.1f}% x {s.avg_win:,.2f}"
        f" - {(1 - s.win_rate) * 100:.1f}% x {abs(s.avg_loss):,.2f}",
        f"Breakeven win rate    {s.breakeven_win_rate * 100:.1f}% at this payoff (actual {s.win_rate * 100:.1f}%)",
        f"Evidence              {evidence}",
```

In `compare.py`'s `_side_by_side`, add after the `R on risk` row:

```python
        ("Payoff", f"{a.payoff:.2f}", f"{b.payoff:.2f}"),
        ("Expectancy", f"{a.expectancy:,.2f}", f"{b.expectancy:,.2f}"),
        ("t (days)", "n/a" if a.evidence_t is None else f"{a.evidence_t:.1f}",
                      "n/a" if b.evidence_t is None else f"{b.evidence_t:.1f}"),
```

- [ ] **Step 4: Run the suite and look at a real run**

Run: `.venv/bin/pytest -q` → `550 passed`.
Run: `.venv/bin/tradebot report --run real-1 | sed -n 1,16p` (read-only) and paste the output in your report. Check the expectancy line's two sides agree with `Total PnL / Trades`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/report/summary.py src/tradebot/report/compare.py tests/test_report.py tests/test_compare.py
git commit -m "report: print expectancy, breakeven win rate and how much evidence the run rests on"
```

---

# Part 2 — every trade risks about the same amount

### Task 3: `min_risk_fraction` and the `risk_too_small` rejection

**Files:**
- Modify: `src/tradebot/config.py`, `src/tradebot/risk/engine.py`, `tests/helpers.py`
- Modify: `config.yaml`, `config-15m.yaml`, `config-orb.yaml`
- Test: `tests/test_risk.py`, `tests/test_config.py`

Why: `compute_quantity` returns `min(risk-based, margin-based)`. When margin is short the trade is still taken, risking a fraction of what was planned — 490 of `real-1`'s 884 trades risked under 25 rupees against a 1,000-rupee plan. Fixed-fractional sizing means every trade risks about the same amount or is not taken.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_risk.py` (its `CFG` is a `RiskConfig` built with keywords, so add the new field there too — see Step 3):

```python
def _cfg(**kw):
    """CFG with overrides; keeps the module's other tests untouched."""
    import dataclasses
    return dataclasses.replace(CFG, **kw)


def test_risk_too_small_when_margin_shrinks_the_position(_=None):
    """100,000 capital, 1% = 1,000 planned risk, stop 1 rupee away -> 1,000 shares wanted.
    Margin for only 300 shares means 300 rupees of risk, 30% of plan: below the 50% floor."""
    cfg = _cfg(min_risk_fraction=0.5)
    res = evaluate(_sig(entry=100.0, stop=99.0), _state(), cfg, 1, 30_000.0, OFF)
    assert isinstance(res, Rejection) and res.reason == "risk_too_small"


def test_risk_at_or_above_the_floor_is_approved(_=None):
    cfg = _cfg(min_risk_fraction=0.5)
    # margin for 600 shares -> 600 rupees of risk, 60% of plan
    res = evaluate(_sig(entry=100.0, stop=99.0), _state(), cfg, 1, 60_000.0, OFF)
    assert isinstance(res, ApprovedOrder) and res.quantity == 600
    # exactly at the floor: 500 shares, 500 rupees, 50% -> approved (the check is <, not <=)
    res = evaluate(_sig(entry=100.0, stop=99.0), _state(), cfg, 1, 50_000.0, OFF)
    assert isinstance(res, ApprovedOrder) and res.quantity == 500


def test_min_risk_fraction_zero_disables_the_check(_=None):
    cfg = _cfg(min_risk_fraction=0.0)
    res = evaluate(_sig(entry=100.0, stop=99.0), _state(), cfg, 1, 3_000.0, OFF)
    assert isinstance(res, ApprovedOrder) and res.quantity == 30


def test_insufficient_size_still_wins_over_risk_too_small(_=None):
    """A quantity below one lot keeps its own reason: the two would otherwise both fire."""
    cfg = _cfg(min_risk_fraction=0.5)
    res = evaluate(_sig(entry=100.0, stop=99.0), _state(), cfg, 1, 50.0, OFF)
    assert isinstance(res, Rejection) and res.reason == "insufficient_size"
```

Append to `tests/test_config.py`:

```python
def test_min_risk_fraction_defaults_and_is_validated(tmp_path):
    assert make_config(tmp_path, risk={"min_risk_fraction": 0.5}).risk.min_risk_fraction == 0.5
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError, match="min_risk_fraction"):
            make_config(tmp_path, risk={"min_risk_fraction": bad})
    root = Path(__file__).resolve().parents[1]
    for name in ("config.yaml", "config-15m.yaml", "config-orb.yaml"):
        cfg = load_config(root / name, tmp_path / "nonexistent.env")
        assert cfg.risk.min_risk_fraction == 0.5, name
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_risk.py tests/test_config.py -q`
Expected: `TypeError: __init__() got an unexpected keyword argument 'min_risk_fraction'`.

- [ ] **Step 3: Implement**

`src/tradebot/config.py`, add to `RiskConfig` as its last field (it must come after the fields without defaults):

```python
    min_risk_fraction: float = 0.5   # reject a trade margin has shrunk below this fraction of the planned risk; 0 disables
```

and to `_validate`'s `checks` list, beside the other risk checks:

```python
        (0.0 <= r.min_risk_fraction <= 1.0, "risk.min_risk_fraction must be between 0 and 1"),
```

`src/tradebot/risk/engine.py`, after the `insufficient_size` check and before the `return ApprovedOrder(...)`:

```python
    # Fixed-fractional sizing only works if every trade really risks the planned amount. When margin
    # is short, compute_quantity silently returns a smaller position, so the trade goes ahead risking
    # a fraction of the plan; a handful of rupees at risk cannot pay for its own brokerage, and a mix
    # of full-size and scrap-size losses makes a run's average R meaningless. Skip it instead.
    if cfg.min_risk_fraction > 0:
        planned_risk = state.capital * cfg.per_trade_pct / 100.0
        actual_risk = qty * abs(signal.entry_price - signal.stop_price)
        if actual_risk < cfg.min_risk_fraction * planned_risk:
            return Rejection(signal, "risk_too_small")
```

`tests/helpers.py`, in `BASE_CONFIG["risk"]`, add `"min_risk_fraction": 0.0,` with the comment `# off by default in tests: existing expectations and the golden-trades fixture predate the rule`.

In all three shipped configs, under `risk:`, after `per_trade_pct`:

```yaml
  min_risk_fraction: 0.5      # skip a trade margin has shrunk below half its planned risk; 0 disables
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q` → `555 passed`. The golden-trades fixture must not change (`git status --short` must not list `tests/fixtures/golden_trades.json`).

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py src/tradebot/risk/engine.py tests/helpers.py tests/test_risk.py tests/test_config.py config.yaml config-15m.yaml config-orb.yaml
git commit -m "risk: skip a trade whose risk margin has shrunk below half the plan"
```

### Task 4: measure what the rule changes

**Files:** none (two backtests and a report).

This is a measurement, not a tuning step: no parameter changes in response to it.

- [ ] **Step 1: Back up the database**

```bash
cd "/Users/toothless/AI-Trading Bot"
ls -la data/tradebot.db-wal          # must be 0 bytes or absent; if not, STOP and report
cp data/tradebot.db data/tradebot.pre-minrisk.bak.db
ls -la data/tradebot.db data/tradebot.pre-minrisk.bak.db   # sizes must match
```

- [ ] **Step 2: Re-run the two 5-minute strategies with the rule on**

`real-1` covered 2026-06-17..2026-09-11 and `real-conf-2` the same window; use those dates so the comparison is like-for-like.

```bash
.venv/bin/tradebot backtest --strategy ema_rsi --start 2026-06-17 --end 2026-09-11 --run-id cr-ema-rsi --ai stub
.venv/bin/tradebot backtest --strategy confluence --start 2026-06-17 --end 2026-09-11 --run-id cr-confluence --ai stub
```

- [ ] **Step 3: Report both pairs**

```bash
for r in real-1 cr-ema-rsi real-conf-2 cr-confluence; do .venv/bin/tradebot report --run $r | sed -n 1,16p; echo; done
```

Record, for each pair: trades, net, R on risk, expectancy, payoff, the evidence line, and the `risk_too_small` count from the new runs' Risk rejects. Report them in a table. Do not draw a conclusion beyond what the numbers say.

- [ ] **Step 4: Commit**

Nothing to commit (runs live in the database). Say so in your report.

---

# Part 3 — the daily systems screen

`scripts/daily_screen.py` is built in four layers, each with its own tests, then run. It reads the
database READ-ONLY and imports nothing from `tradebot.engine`. Keep it 3.9-safe: no `X | None`
annotations (the file has no `from __future__ import annotations`), use `Optional[...]`.

`tests/test_daily_screen.py` loads the script by path, the way `tests/test_orb_experiment.py` does:

```python
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "daily_screen", Path(__file__).resolve().parents[1] / "scripts" / "daily_screen.py")
ds = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ds)
```

### Task 5: data, the split-gap mask and the cost model

**Files:**
- Create: `scripts/daily_screen.py`
- Test: `tests/test_daily_screen.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_daily_screen.py` with the loader block above, then:

```python
import sqlite3
from datetime import date

import pytest


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
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q`
Expected: `FileNotFoundError` / `ModuleNotFoundError` for `scripts/daily_screen.py`.

- [ ] **Step 3: Implement**

Create `scripts/daily_screen.py`:

```python
"""Screen three classic DAILY systems for expectancy in excess of a same-holding-period baseline.

    .venv/bin/python scripts/daily_screen.py fetch      # pull daily candles (needs the Groww key approved)
    .venv/bin/python scripts/daily_screen.py insample   # 2020-01-01..2023-12-31
    .venv/bin/python scripts/daily_screen.py holdout    # 2024-01-01..2025-11-21, ONCE

Rules for the three systems, the cost schedule and the pass bar are fixed in
docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md and were written down
before any daily candle was stored. They are the standard published forms of mean reversion, trend
following and breakout; they are NOT any particular author's exact rules.

Long only: a cash (CNC) short cannot be held overnight. One open trade per system per symbol; a
signal while a trade is open is ignored. A signal on day t's close enters at day t+1's OPEN; an exit
condition met on day u's close exits at day u+1's OPEN. Trade-level expectancy only: no capital,
no portfolio, no overlap limit, so these numbers say whether an edge exists, not what an account
would have made.

This reads the database read-only and writes nothing to it.
"""
import argparse
import math
import sqlite3
import sys
from collections import defaultdict, namedtuple
from datetime import date, timedelta

DB = "data/tradebot.db"
INTERVAL = 1440
IST_OFFSET = 19800                      # daily bars are stamped 00:00 IST
INSAMPLE = (date(2020, 1, 1), date(2023, 12, 31))
HOLDOUT = (date(2024, 1, 1), date(2025, 11, 21))
HOLDOUT_FILE = "docs/superpowers/notes/daily-screen-holdout.txt"
WARMUP_BARS = 200                       # SMA(200) plus a bar to cross on
GAP_THRESHOLD = 0.20                    # an overnight move this large is an unadjusted corporate action
GAP_MASK_DAYS = 200                     # ... and the 200-day average is unusable for this long after it

# Groww DELIVERY (CNC) schedule on an assumed position value, plus slippage. CHECK THESE against
# groww.in/pricing before trusting any figure computed from them.
POSITION_VALUE = 25_000.0
BROKERAGE_PCT, BROKERAGE_MIN, BROKERAGE_MAX = 0.1, 5.0, 20.0
STT_PCT = 0.1                           # both sides on delivery
EXCHANGE_PCT = 0.00297
SEBI_PCT = 0.0001
STAMP_BUY_PCT = 0.015
GST_PCT = 18.0
DP_CHARGE = 15.34                       # depository, per sell
SLIPPAGE_PCT = 0.05                     # each side, against the trade

Bar = namedtuple("Bar", "date open high low close")


def round_trip_cost(position_value=POSITION_VALUE):
    """Round-trip cost as a FRACTION of position value: charges plus slippage on both opens."""
    def brokerage(v):
        return min(max(v * BROKERAGE_PCT / 100.0, BROKERAGE_MIN), BROKERAGE_MAX)

    v = position_value
    brok = brokerage(v) * 2
    stt = v * STT_PCT / 100.0 * 2
    exch = v * EXCHANGE_PCT / 100.0 * 2
    sebi = v * SEBI_PCT / 100.0 * 2
    stamp = v * STAMP_BUY_PCT / 100.0
    gst = (brok + exch + sebi) * GST_PCT / 100.0
    charges = brok + stt + exch + sebi + stamp + gst + DP_CHARGE
    return charges / v + 2 * SLIPPAGE_PCT / 100.0


def load_series(conn, symbols):
    """{symbol: [Bar, ...]} ascending by date, daily candles only. A symbol with no rows is absent."""
    out = {}
    for sym in symbols:
        rows = conn.execute(
            "SELECT ts, o, h, l, c FROM candles WHERE symbol=? AND interval=? ORDER BY ts",
            (sym, INTERVAL)).fetchall()
        if rows:
            out[sym] = [Bar(date.fromtimestamp(r[0] + IST_OFFSET), r[1], r[2], r[3], r[4]) for r in rows]
    return out


def gap_mask(bars, threshold=GAP_THRESHOLD, mask_days=GAP_MASK_DAYS):
    """Dates unusable because of an unadjusted corporate action: any bar whose open is more than
    `threshold` away from the previous close, and the `mask_days` trading days after it. An
    unadjusted split reads as a crash, which would manufacture mean-reversion entries, and it
    poisons every long average for as long as it stays in the window."""
    masked = set()
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        if prev_close > 0 and abs(bars[i].open / prev_close - 1.0) > threshold:
            for k in range(i, min(i + mask_days + 1, len(bars))):
                masked.add(bars[k].date)
    return masked


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["fetch", "insample", "holdout"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
```

`date.fromtimestamp(ts + IST_OFFSET)` reads the UTC epoch as an IST calendar date without a
timezone dependency; daily bars are stamped 00:00 IST, so this is exact. Note this differs from the
engine's `date_of`, which is correct for intraday bars; do not swap one for the other.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q` → `4 passed`. Then `.venv/bin/pytest -q` → `559 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "scripts(daily): load daily candles, mask unadjusted corporate actions, price a round trip"
```

### Task 6: indicators

**Files:**
- Modify: `scripts/daily_screen.py`
- Test: `tests/test_daily_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daily_screen.py`:

```python
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
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q` → `AttributeError: module 'daily_screen' has no attribute 'sma'`.

- [ ] **Step 3: Implement**

Add to `scripts/daily_screen.py`, after `gap_mask`:

```python
# -- indicators. Each returns a list aligned with the bars, None until warm; no lookahead: index i
# uses only bars 0..i.
def sma(values, n):
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi_wilder(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr_wilder(bars, n):
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = [0.0]
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)))
    atr = sum(trs[1:n + 1]) / n
    out[n] = atr
    for i in range(n + 1, len(bars)):
        atr = (atr * (n - 1) + trs[i]) / n
        out[i] = atr
    return out


def rolling_max_prev(values, n):
    """max of the n values BEFORE i (i excluded), None until there are n of them."""
    return [max(values[i - n:i]) if i >= n else None for i in range(len(values))]


def rolling_min_prev(values, n):
    return [min(values[i - n:i]) if i >= n else None for i in range(len(values))]


class Indicators(object):
    """Everything the three systems read, computed once per symbol."""

    def __init__(self, bars):
        self.bars = bars
        self.closes = [b.close for b in bars]
        self.sma5 = sma(self.closes, 5)
        self.sma50 = sma(self.closes, 50)
        self.sma200 = sma(self.closes, 200)
        self.rsi2 = rsi_wilder(self.closes, 2)
        self.atr20 = atr_wilder(bars, 20)
        self.max100_prev = rolling_max_prev(self.closes, 100)
        self.min50_prev = rolling_min_prev(self.closes, 50)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `564 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "scripts(daily): SMA, Wilder RSI and ATR, and rolling extremes that exclude today"
```

### Task 7: the three systems and the trade simulation

**Files:**
- Modify: `scripts/daily_screen.py`
- Test: `tests/test_daily_screen.py`

The rules come from the spec and must be typed in as written. Do not adjust a threshold to make a test pass; fix the test's data instead.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daily_screen.py`:

```python
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
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q` → `AttributeError: ... has no attribute 'mr_entry'`.

- [ ] **Step 3: Implement**

Add to `scripts/daily_screen.py`, after `Indicators`:

```python
# -- the three systems. Long only. Entry is judged on day i's close and filled at day i+1's open;
# an exit condition met on day u's close (u strictly after the entry day) is filled at u+1's open.
def mr_entry(ind, i):
    """Mean reversion: an oversold dip inside an uptrend."""
    return (ind.sma200[i] is not None and ind.rsi2[i] is not None
            and ind.closes[i] > ind.sma200[i] and ind.rsi2[i] < 10.0)


def mr_exit(ind, i, entry_i, run_high):
    """Out on the bounce, or out anyway after 10 trading days: the edge is short-lived, and a dip
    that has not bounced by then is a downtrend, not a pullback."""
    if ind.sma5[i] is not None and ind.closes[i] > ind.sma5[i]:
        return "sma5"
    if i - entry_i >= 10:
        return "time"
    return None


def tf_entry(ind, i):
    """Trend following: the bar the 50-day average crosses above the 200-day, price above the 200."""
    if i == 0:
        return False
    prev_fast, prev_slow = ind.sma50[i - 1], ind.sma200[i - 1]
    fast, slow = ind.sma50[i], ind.sma200[i]
    if prev_fast is None or prev_slow is None or fast is None or slow is None:
        return False
    return prev_fast <= prev_slow and fast > slow and ind.closes[i] > slow


def tf_exit(ind, i, entry_i, run_high):
    """A trailing stop, not a target: the whole point is to let one winner run."""
    if ind.atr20[i] is None:
        return None
    return "trail" if ind.closes[i] < run_high - 3.0 * ind.atr20[i] else None


def bo_entry(ind, i):
    """Breakout: a new 100-day closing high, in an uptrend."""
    return (ind.max100_prev[i] is not None and ind.sma200[i] is not None
            and ind.closes[i] > ind.max100_prev[i] and ind.closes[i] > ind.sma200[i])


def bo_exit(ind, i, entry_i, run_high):
    return "min50" if ind.min50_prev[i] is not None and ind.closes[i] < ind.min50_prev[i] else None


SYSTEMS = {"mr": (mr_entry, mr_exit), "tf": (tf_entry, tf_exit), "bo": (bo_entry, bo_exit)}
SYSTEM_TITLES = {
    "mr": "mean reversion (close > SMA200, RSI(2) < 10; exit close > SMA5 or 10 days)",
    "tf": "trend following (SMA50 crosses above SMA200, close > SMA200; exit 3 x ATR(20) trail)",
    "bo": "breakout (100-day closing high, close > SMA200; exit below the 50-day closing low)",
}

Trade = namedtuple("Trade", "symbol system entry_date exit_date entry_price exit_price days "
                            "gross net reason excess")
Trade.__new__.__defaults__ = (0.0,)


def simulate(symbol, name, bars, ind, masked, window, cost, system=None, warmup=WARMUP_BARS):
    """(trades, excluded) for one system on one symbol inside one window.

    One trade at a time: a signal while a trade is open is ignored, so the count is not inflated by
    a rule that fires every day of a dip. A trade still open at the window's last bar is closed at
    that bar's open, so an in-sample trade can never reach into the holdout. A trade any of whose
    days is masked by a corporate action is dropped and counted in `excluded` rather than silently
    scoring a split as a 50% loss."""
    entry_fn, exit_fn = system if system is not None else SYSTEMS[name]
    lo, hi = window
    in_window = [i for i in range(len(bars)) if lo <= bars[i].date <= hi]
    if not in_window:
        return [], 0
    first_i, last_i = in_window[0], in_window[-1]
    trades, excluded = [], 0
    i = max(first_i, warmup)
    while i < last_i:
        if bars[i].date in masked or not entry_fn(ind, i):
            i += 1
            continue
        entry_i = i + 1
        if entry_i >= last_i:
            break
        run_high = bars[entry_i].high
        u, reason = entry_i + 1, None
        while u < last_i:
            run_high = max(run_high, bars[u].high)
            reason = exit_fn(ind, u, entry_i, run_high)
            if reason:
                break
            u += 1
        exit_i = min(u + 1, last_i)
        if reason is None:
            reason = "window_end"
        if any(bars[k].date in masked for k in range(entry_i, exit_i + 1)):
            excluded += 1
        else:
            entry_px, exit_px = bars[entry_i].open, bars[exit_i].open
            gross = (exit_px / entry_px - 1.0) if entry_px > 0 else 0.0
            trades.append(Trade(symbol, name, bars[entry_i].date, bars[exit_i].date, entry_px,
                                exit_px, exit_i - entry_i, gross, gross - cost, reason))
        i = exit_i
    return trades, excluded
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `577 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "scripts(daily): the three systems as specified, and a one-trade-at-a-time simulation"
```

### Task 8: the baseline, the statistics and the table

**Files:**
- Modify: `scripts/daily_screen.py`
- Test: `tests/test_daily_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daily_screen.py`:

```python
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
```

Add `import math` to the test file's imports.

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q` → `AttributeError: ... 'baseline_return'`.

- [ ] **Step 3: Implement**

Add to `scripts/daily_screen.py`, after `simulate`:

```python
def baseline_return(bars, window, h, cost):
    """Mean net return of simply holding this symbol for h trading days, entered at the open of
    every date in the window whose exit also lands in it. None when no such hold fits.

    Indian large caps rose over this period, so any rule that buys shows a positive return from the
    drift alone; only the excess over this baseline is evidence of an edge."""
    lo, hi = window
    rs = []
    for i in range(len(bars)):
        if not (lo <= bars[i].date <= hi) or bars[i].open <= 0:
            continue
        j = i + h
        if j < len(bars) and bars[j].date <= hi:
            rs.append(bars[j].open / bars[i].open - 1.0 - cost)
    return (sum(rs) / len(rs)) if rs else None


def attach_excess(trades, bars, window, cost):
    """Each trade's net return less what holding the same symbol the same number of days paid on
    average. The baseline is cached per holding length: a system's trades repeat a few lengths."""
    cache = {}
    out = []
    for t in trades:
        if t.days not in cache:
            cache[t.days] = baseline_return(bars, window, t.days, cost)
        base = cache[t.days]
        out.append(t._replace(excess=t.net - (base if base is not None else 0.0)))
    return out


def t_across_dates(trades, attr="excess"):
    """t of the mean, averaging trades entered on the same date first. Signals cluster: one
    market-wide dip fires mean reversion across forty names at once, and counting those as forty
    independent observations would inflate t by roughly the square root of the cluster size."""
    by_date = defaultdict(list)
    for tr in trades:
        by_date[tr.entry_date].append(getattr(tr, attr))
    means = [sum(v) / len(v) for v in by_date.values()]
    n = len(means)
    if n < 2:
        return None
    mean = sum(means) / n
    var = sum((m - mean) ** 2 for m in means) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def describe(trades):
    """Rayner's block: win rate, the two averages, payoff, expectancy, the win rate this payoff
    would need to break even, plus the excess over the baseline and its t."""
    n = len(trades)
    if n == 0:
        return dict(trades=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0, payoff=0.0, expectancy=0.0,
                    breakeven=0.0, median_days=0, excess=0.0, t=None, dates=0, exits={})
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = avg_win / abs(avg_loss) if avg_loss < 0 and avg_win > 0 else 0.0
    days = sorted(t.days for t in trades)
    return dict(
        trades=n,
        win_rate=len(wins) / n,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=payoff,
        expectancy=sum(t.net for t in trades) / n,
        breakeven=(1.0 / (1.0 + payoff)) if payoff > 0 else 0.0,
        median_days=days[n // 2],
        excess=sum(t.excess for t in trades) / n,
        t=t_across_dates(trades),
        dates=len(set(t.entry_date for t in trades)),
        exits=dict(Counter(t.reason for t in trades)),
    )


def format_system(name, label, window, s, excluded, cost):
    t = "n/a" if s["t"] is None else "%.2f" % s["t"]
    return "\n".join([
        "%s   %s %s..%s" % (SYSTEM_TITLES[name], label, window[0], window[1]),
        "  trades %-6d win %5.1f%%   avg win %+.2f%%   avg loss %+.2f%%   payoff %.2f   median hold %dd"
        % (s["trades"], s["win_rate"] * 100, s["avg_win"] * 100, s["avg_loss"] * 100,
           s["payoff"], s["median_days"]),
        "  expectancy %+.3f%% per trade (net of %.3f%% round trip)   breakeven win %.1f%%"
        % (s["expectancy"] * 100, cost * 100, s["breakeven"] * 100),
        "  excess over baseline %+.3f%% per trade   t %s across %d entry dates"
        % (s["excess"] * 100, t, s["dates"]),
        "  exits %s   excluded by the gap mask %d"
        % (", ".join("%s %d" % kv for kv in sorted(s["exits"].items())) or "none", excluded),
    ])
```

Add `Counter` to the `collections` import line.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `582 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "scripts(daily): baseline for the same holding period, expectancy block, t across entry dates"
```

### Task 9: the phases, and the in-sample run

**Files:**
- Modify: `scripts/daily_screen.py`
- Test: `tests/test_daily_screen.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daily_screen.py`:

```python
def test_holdout_refuses_when_its_file_already_exists(tmp_path, monkeypatch, capsys):
    f = tmp_path / "daily-screen-holdout.txt"
    f.write_text("an earlier run\n")
    monkeypatch.setattr(ds, "HOLDOUT_FILE", str(f))
    with pytest.raises(SystemExit) as e:
        ds.guard_holdout()
    assert "already been run" in str(e.value)
    assert f.read_text() == "an earlier run\n"          # untouched


def test_holdout_guard_passes_when_the_file_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "HOLDOUT_FILE", str(tmp_path / "nope.txt"))
    ds.guard_holdout()                                   # must not raise


def test_run_phase_prints_a_block_per_system(tmp_path):
    """A tiny two-symbol database: the point is the shape of the output, not the numbers."""
    import io
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL, h REAL,"
                 " l REAL, c REAL, v INTEGER, source TEXT)")
    base = 1577836800                                    # 2020-01-01 00:00 UTC
    for sym in ("A", "B"):
        for k in range(300):
            c = 100.0 + k * 0.5
            conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,0,'official')",
                         (sym, base + k * 86400, 1440, c, c + 1, c - 1, c))
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    buf = io.StringIO()
    ds.run_phase(ro, ["A", "B"], (date(2020, 1, 1), date(2020, 10, 26)), "test", out=buf)
    text = buf.getvalue()
    for title in ds.SYSTEM_TITLES.values():
        assert title in text
    assert "2 symbols with daily candles" in text and "survivorship" in text
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q` → `AttributeError: ... 'guard_holdout'`.

- [ ] **Step 3: Implement**

Add to `scripts/daily_screen.py` (and `import io`, `import os` at the top):

```python
CAVEATS = (
    "Caveats: universe.yaml is today's constituent list, so six years of it is survivorship-biased;\n"
    "trade-level expectancy ignores capital, overlapping positions and position sizing;\n"
    "one parameter set per system, fixed in the spec before any daily candle was stored;\n"
    "the charge rates are from the spec and have not been checked against Groww's pricing page."
)


def run_phase(conn, symbols, window, label, out=sys.stdout):
    cost = round_trip_cost()
    series = load_series(conn, symbols)
    missing = [s for s in symbols if s not in series]
    print("%d symbols with daily candles%s"
          % (len(series), ("; no candles for " + ", ".join(missing)) if missing else ""), file=out)
    prepared = {}
    gaps = {}
    for sym, bars in series.items():
        masked = gap_mask(bars)
        prepared[sym] = (bars, Indicators(bars), masked)
        if masked:
            gaps[sym] = len(masked)
    print("corporate-action gaps masked: %s"
          % (", ".join("%s %d dates" % kv for kv in sorted(gaps.items())) if gaps else "none"), file=out)
    print("", file=out)
    for name in ("mr", "tf", "bo"):
        trades, excluded = [], 0
        for sym in sorted(prepared):
            bars, ind, masked = prepared[sym]
            got, ex = simulate(sym, name, bars, ind, masked, window, cost)
            trades.extend(attach_excess(got, bars, window, cost))
            excluded += ex
        print(format_system(name, label, window, describe(trades), excluded, cost), file=out)
        print("", file=out)
    print(CAVEATS, file=out)


def guard_holdout():
    """The holdout is worth one look. A second look at the same window, after seeing the first, is
    tuning with extra steps."""
    if os.path.exists(HOLDOUT_FILE):
        sys.exit("%s exists: the holdout has already been run and is meant to be looked at once. "
                 "Read that file instead." % HOLDOUT_FILE)


def fetch_daily(db_path):
    """Pull daily candles from 2020-01-01 for the universe. Needs the Groww key approved today.
    No session filter: daily bars are stamped 00:00 IST and SessionClock would drop every one."""
    import time
    from tradebot.config import load_config
    from tradebot.data.historical import fetch_incremental
    from tradebot.data.universe import load_universe
    from tradebot.execution.groww_adapter import GrowwAdapter
    from tradebot.store.db import connect
    from tradebot.store.repo import Repo

    cfg = load_config("config.yaml")
    uni = load_universe(cfg.paths.universe)
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret,
                           cfg.secrets.groww_api_secret)
    adapter.client
    repo = Repo(connect(db_path))
    now = int(time.time())
    lookback = (now - int((date(2020, 1, 1) - date(1970, 1, 1)).total_seconds() // 1)) // 86400 + 1

    def throttled(sym, exch, start, end, interval):
        try:
            return adapter.fetch_candles(sym, exch, start, end, interval)
        finally:
            time.sleep(0.4)

    total, failed = 0, []
    for i, sym in enumerate(uni.symbols, 1):
        try:
            n = fetch_incremental(repo, throttled, [sym], uni.exchange, INTERVAL, lookback, now,
                                  keep=None)[sym]
            total += n
            print("[%d/%d] %s: +%d" % (i, len(uni.symbols), sym, n))
        except Exception as e:                      # noqa: BLE001 - isolate per symbol
            failed.append(sym)
            print("[%d/%d] %s: FAILED %s: %s" % (i, len(uni.symbols), sym, type(e).__name__, str(e)[:160]))
    print("done: %d daily candles inserted; failed: %s" % (total, failed or "none"))
```

`lookback` above is awkward; replace that line with a plain day count:

```python
    lookback = (date.today() - date(2020, 1, 1)).days + 1
```

Replace `main` with:

```python
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["fetch", "insample", "holdout"])
    ap.add_argument("--db", default=DB, help="read-only for insample and holdout")
    a = ap.parse_args()
    if a.phase == "fetch":
        fetch_daily(a.db)
        return
    if a.phase == "holdout":
        guard_holdout()
    from tradebot.config import load_config
    from tradebot.data.universe import load_universe
    symbols = list(load_universe(load_config("config.yaml").paths.universe).symbols)
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    try:
        if a.phase == "insample":
            run_phase(conn, symbols, INSAMPLE, "in-sample")
        else:
            buf = io.StringIO()
            run_phase(conn, symbols, HOLDOUT, "holdout", out=buf)
            with open(HOLDOUT_FILE, "w") as fh:
                fh.write(buf.getvalue())
            print(buf.getvalue())
            print("written to %s; this window is now spent" % HOLDOUT_FILE)
    finally:
        conn.close()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q` → `585 passed`.

- [ ] **Step 5: Run the in-sample screen and STOP**

The daily candles are already loaded (49 symbols, 2020-01-01 to about 2025-11-21); do NOT run `fetch`.

Run: `.venv/bin/python scripts/daily_screen.py insample`

Paste the whole output in your report. Then sanity-check it and report each answer:
- Does every system have at least a few hundred trades? A system with almost none means an entry rule never fires; say which and show the count.
- Is any symbol missing, and how many dates did the gap mask remove?
- Do the median holds look like the rules (mean reversion a few days, trend following weeks or months)?
- Do the exit reasons cover more than one kind per system?

Do NOT run `holdout`. Do NOT change any rule or threshold in response to these numbers; if something looks structurally broken (not merely unprofitable), report it and stop.

- [ ] **Step 6: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "scripts(daily): fetch, in-sample and once-only holdout phases"
```

### Task 10: the holdout, once

**Files:** none (one run, plus the file it writes).

Do this only after the in-sample output has been reviewed and nothing structural is wrong.

- [ ] **Step 1: Run it**

Run: `.venv/bin/python scripts/daily_screen.py holdout`
Paste the output. Confirm `docs/superpowers/notes/daily-screen-holdout.txt` now exists.

- [ ] **Step 2: Confirm the guard**

Run it a second time: it must refuse, naming the file, and must not overwrite it (check the file's contents are unchanged).

- [ ] **Step 3: Commit the record**

```bash
git add docs/superpowers/notes/daily-screen-holdout.txt
git commit -m "notes(daily): the holdout table, run once"
```

### Task 11: results notes and README

**Files:**
- Create: `docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`
- Modify: `README.md`

- [ ] **Step 1: Write the notes**

Read the two most recent files in `docs/superpowers/notes/` for house style: plain sentences, tables, conclusions stated flatly, no hype. EVERY number must come from a command you run now or from output recorded earlier in this plan; never from memory.

Sections:

1. **What changed** (5-8 lines): the expectancy block, `min_risk_fraction`, the daily screen; where each lives.
2. **Expectancy in reports.** What the four lines mean, with `real-1`'s actual block as the worked example. State plainly what the breakeven line shows: at that run's payoff, the win rate it would have needed.
3. **The consistent-risk rule.** The before-and-after table from Task 4 (`real-1` vs `cr-ema-rsi`, `real-conf-2` vs `cr-confluence`): trades, net, R on risk, expectancy, payoff, `risk_too_small` count. Say what it changed and what it did not. The rule removes a measurement artefact; it is not an edge.
4. **The daily screen.** The three systems as one-line rules; the window, the symbol count and the cost per round trip; the in-sample table; the holdout table; the pass bar from the spec and, for each system, whether it passed. State that the rules were committed before the data was pulled, and give the spec commit's date.
5. **Limits** (bullets): survivorship over six years; trade-level only, no capital or overlap; one parameter set; Groww daily history starts about 2020-01 and stops about 2025-11-21, so this is one long bull market plus two corrections; costs assume a fixed position value; charge rates unverified; the gap mask is a proxy for unadjusted corporate actions rather than a corporate-actions feed.
6. **Conclusion.** What is supported and what is not. If nothing passed, say so plainly and note what that does and does not rule out (these three rule sets on these symbols over this period, not "daily systems").

- [ ] **Step 2: README**

Add a short section after the existing ones:

```markdown
## Expectancy and risk consistency

Reports print the expectancy block: average win and loss, payoff, expectancy per trade, the win rate
that payoff would need to break even, and how many days of evidence the number rests on. `R on risk`
(net PnL over rupees at risk, pooled) stays the decision metric.

`risk.min_risk_fraction` (default 0.5) skips a trade whose size margin has cut below half its planned
risk, instead of taking it at a fraction of the intended stake. Set it to 0 to reproduce older runs.

## Daily systems screen

    .venv/bin/python scripts/daily_screen.py fetch      # daily candles from 2020 (Groww key must be approved)
    .venv/bin/python scripts/daily_screen.py insample   # 2020-01-01..2023-12-31
    .venv/bin/python scripts/daily_screen.py holdout    # 2024-01-01..2025-11-21, refuses a second run

Three classic daily systems (mean reversion, trend following, breakout), long only, entered at the
next day's open, net of delivery charges, scored on expectancy in excess of holding the same stock
for the same number of days. Rules are fixed in
`docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md`. Results:
`docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`.
```

- [ ] **Step 3: Verify and commit**

Run: `.venv/bin/pytest -q` → `585 passed`. Run `git status --short` (expect only the two doc files).

```bash
git add docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md README.md
git commit -m "notes: expectancy block, the consistent-risk rule measured, and the daily screen's verdict"
```
