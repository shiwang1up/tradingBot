# Plan 3: Paper Trading on REST Bars Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `tradebot paper` command that runs today's session on live 5-minute REST candles through the existing strategy, risk, AI-filter and simulated-broker pipeline, resumes after a restart, and places no real orders (spec `docs/superpowers/specs/2026-09-15-paper-trading-design.md`).

**Architecture:** The per-bar cycle in `engine/loop.py` is split into a mode-independent `Engine` base and a `BacktestEngine` that keeps only its replay loop. A new `PaperEngine` (`engine/paper.py`) drives `process_bar` from the wall clock with injectable `now`/`sleep`, warms indicators from stored candles, and resumes a same-day run from SQLite. A new `LiveBarSource` (`data/live.py`) fetches the just-closed bar for every symbol over a small thread pool and stores it. The broker is the unchanged `BacktestBroker` plus a `restore` method. Stale bars (processed past the deadline) record their signals with risk reason `stale`.

**Tech Stack:** Python 3.9 (no `match`, no `X | Y` at runtime; annotations are strings via `from __future__ import annotations`), sqlite3, click, pytest, `concurrent.futures`. No network in tests: every Groww call goes through a fake fetcher.

**Hard rule (spec):** nothing in this plan calls a Groww order endpoint. The only Groww calls are login and `get_historical_candles`.

**Conventions:** as Plans 1 and 2. Run `.venv/bin/pytest -q` from the repo root (`/Users/toothless/AI-Trading Bot`); it must stay green (331 tests at the start). Every task ends with a commit. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Line width 120.

---

## File structure

```
src/tradebot/config.py              modify: DataConfig gains bar_grace_sec, warmup_bars; validation
config.yaml                         modify: the two new data keys
src/tradebot/store/schema.sql       modify: runs.last_bar_ts
src/tradebot/store/db.py            modify: SCHEMA_VERSION 3, MIGRATIONS[2]
src/tradebot/store/repo.py          modify: set_last_bar_ts, open_positions, pending_orders
src/tradebot/engine/clock.py        modify: last_bar_ts, latest_complete_bar
src/tradebot/execution/backtest.py  modify: BacktestBroker.restore
src/tradebot/engine/loop.py         modify: Engine base + BacktestEngine; process_bar(now_ts) stale rule; _observe; _write_daily_row
src/tradebot/data/live.py           new: LiveBarSource
src/tradebot/engine/paper.py        new: PaperEngine, position_from_row, order_from_row
src/tradebot/cli.py                 modify: paper command, _warm_fetch, _warm_candles
README.md                           modify: Paper trading section
tests/helpers.py                    modify: FakeTime
tests/test_config.py                modify
tests/test_store.py                 modify
tests/test_clock.py                 modify
tests/test_backtest_broker.py       modify
tests/test_engine.py                modify
tests/test_live_source.py           new
tests/test_paper.py                 new
tests/test_cli.py                   modify
```

---

### Task 1: Config keys `data.bar_grace_sec` and `data.warmup_bars`

**Files:**
- Modify: `src/tradebot/config.py` (DataConfig at ~line 72, `_validate` at ~line 158)
- Modify: `config.yaml` (`data:` section)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_config.py`)

```python
def test_data_section_defaults_and_validation(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.data.bar_grace_sec == 5
    assert cfg.data.warmup_bars == 300
    cfg2 = make_config(tmp_path, data={"official_fetch_concurrency": 5, "bar_grace_sec": 0, "warmup_bars": 50})
    assert cfg2.data.bar_grace_sec == 0 and cfg2.data.warmup_bars == 50
    with pytest.raises(ValueError, match="data.warmup_bars must be >= 1"):
        make_config(tmp_path, data={"official_fetch_concurrency": 5, "warmup_bars": 0})
    with pytest.raises(ValueError, match="data.bar_grace_sec must be >= 0"):
        make_config(tmp_path, data={"official_fetch_concurrency": 5, "bar_grace_sec": -1})
```

Make sure `tests/test_config.py` imports `pytest` and `make_config` from `tests.helpers` (it already does for other tests; add if missing).

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py::test_data_section_defaults_and_validation -q`
Expected: FAIL with `AttributeError: 'DataConfig' object has no attribute 'bar_grace_sec'`

- [ ] **Step 3: Implement**

In `src/tradebot/config.py` replace the `DataConfig` dataclass with:

```python
@dataclass(frozen=True)
class DataConfig:
    official_fetch_concurrency: int
    bar_grace_sec: int = 5      # paper: seconds to wait after a bar boundary before fetching the closed bar
    warmup_bars: int = 300      # paper: stored bars replayed per symbol before the first live bar (~4 days of 5m)
```

In `_validate`, after the `official_fetch_concurrency` check add:

```python
        (d.bar_grace_sec >= 0, "data.bar_grace_sec must be >= 0"),
        (d.warmup_bars >= 1, "data.warmup_bars must be >= 1"),
```

In `config.yaml` change the `data:` section to:

```yaml
data:
  official_fetch_concurrency: 5
  bar_grace_sec: 5              # paper: wait this long after the boundary before fetching the closed bar
  warmup_bars: 300              # paper: stored bars replayed per symbol before the first live bar (~4 days)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py config.yaml tests/test_config.py
git commit -m "feat(config): data.bar_grace_sec and data.warmup_bars for paper mode

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Store: `runs.last_bar_ts`, schema version 3, resume queries

**Files:**
- Modify: `src/tradebot/store/schema.sql` (runs table, lines 2-8)
- Modify: `src/tradebot/store/db.py` (SCHEMA_VERSION, MIGRATIONS)
- Modify: `src/tradebot/store/repo.py` (after `end_run`, after `list_positions`)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`; it already imports `sqlite3`, `connect`, `SCHEMA_VERSION`, and `Position`/`Signal` from `tradebot.types`; add any missing import)

```python
def test_v2_database_is_migrated_to_v3(tmp_path):
    path = tmp_path / "v2.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL);
        INSERT INTO runs VALUES ('r', 'backtest', 0, NULL, '{}');
        PRAGMA user_version = 2;
    """)
    raw.commit()
    raw.close()
    conn = connect(path)
    assert "last_bar_ts" in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert conn.execute("SELECT last_bar_ts FROM runs WHERE run_id='r'").fetchone()[0] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    conn.close()


def test_last_bar_ts_open_positions_and_pending_orders(repo):
    repo.create_run("p", "paper", 0, "{}")
    assert repo.get_run("p")["last_bar_ts"] is None
    repo.set_last_bar_ts("p", 1000)
    assert repo.get_run("p")["last_bar_ts"] == 1000

    sid = repo.insert_signal("p", Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", 900))
    repo.insert_order("p", "c1", sid, "ENTRY", "BUY", 10, 100.0, "PENDING", 900)
    sid2 = repo.insert_signal("p", Signal("ema_rsi", "B", "SHORT", 50.0, 51.0, 48.0, "MIS", 900))
    repo.insert_order("p", "c2", sid2, "ENTRY", "SELL", 5, 50.0, "PENDING", 900)
    repo.update_order("p", "c2", "FILLED", 1200)
    pend = repo.pending_orders("p")
    assert [(r["client_id"], r["strategy"], r["symbol"], r["direction"], r["qty"], r["entry"], r["stop"],
             r["target"], r["product"], r["bar_ts"]) for r in pend] == [
        ("c1", "ema_rsi", "A", "LONG", 10, 100.0, 99.0, 102.0, "MIS", 900)]

    open_id = repo.insert_position("p", Position("B", "MIS", "SHORT", 5, 50.0, 51.0, 48.0, 1200, "c2", "ema_rsi"))
    done_id = repo.insert_position("p", Position("C", "MIS", "LONG", 1, 10.0, 9.0, 12.0, 600, "c3", "ema_rsi"))
    repo.close_position(done_id, 900, 9.0, "STOP", -1.0)
    assert [r["id"] for r in repo.open_positions("p")] == [open_id]
```

Also change the existing assertion in `test_v1_database_is_migrated_to_v2` from `== SCHEMA_VERSION == 2` to `== SCHEMA_VERSION == 3` and rename that test to `test_v1_database_is_migrated_to_current`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_store.py -q`
Expected: the two new tests FAIL (`no such column: last_bar_ts` / `AttributeError: 'Repo' object has no attribute 'set_last_bar_ts'`), and the renamed test FAILS on the version assertion.

- [ ] **Step 3: Implement**

`src/tradebot/store/schema.sql`, runs table:

```sql
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  mode        TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  ended_at    INTEGER,
  config_json TEXT NOT NULL,
  last_bar_ts INTEGER              -- paper: open time of the last bar processed (resume point); NULL for backtests
);
```

`src/tradebot/store/db.py`:

```python
SCHEMA_VERSION = 3
```

and in `MIGRATIONS` add after the `1:` entry:

```python
    2: [
        "ALTER TABLE runs ADD COLUMN last_bar_ts INTEGER",
    ],
```

`src/tradebot/store/repo.py`, after `end_run`:

```python
    def set_last_bar_ts(self, run_id: str, ts: int) -> None:
        self.conn.execute("UPDATE runs SET last_bar_ts=? WHERE run_id=?", (ts, run_id))
        self.conn.commit()
```

after `list_positions`:

```python
    def open_positions(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE run_id=? AND closed_at IS NULL ORDER BY opened_at, symbol", (run_id,)
        ).fetchall()

    def pending_orders(self, run_id: str) -> list[sqlite3.Row]:
        """ENTRY orders still PENDING, joined to the signal they came from, so a paper resume can
        rebuild the ApprovedOrder the broker was holding."""
        return self.conn.execute(
            "SELECT o.client_id, o.qty, s.strategy, s.symbol, s.direction, s.entry, s.stop, s.target, s.product, s.bar_ts "
            "FROM orders o JOIN signals s ON s.id = o.signal_id "
            "WHERE o.run_id=? AND o.kind='ENTRY' AND o.status='PENDING' ORDER BY o.id", (run_id,)
        ).fetchall()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_store.py -q`
Expected: all PASS. Then `.venv/bin/pytest -q` to confirm nothing else asserted the old version number.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/store/schema.sql src/tradebot/store/db.py src/tradebot/store/repo.py tests/test_store.py
git commit -m "feat(store): runs.last_bar_ts (schema v3) and resume queries for paper mode

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Clock: `last_bar_ts` and `latest_complete_bar`

**Files:**
- Modify: `src/tradebot/engine/clock.py` (SessionClock, after `entries_allowed`)
- Modify: `docs/superpowers/specs/2026-09-15-paper-trading-design.md` (clock section)
- Test: `tests/test_clock.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_clock.py`)

```python
def test_last_bar_and_latest_complete_bar():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.last_bar_ts(D) == ist_epoch(D, "15:25")
    o = ist_epoch(D, "09:15")
    assert clk.latest_complete_bar(o + 299, 0) is None            # first bar still open
    assert clk.latest_complete_bar(o + 300, 0) == o                # closed exactly now
    assert clk.latest_complete_bar(o + 304, grace_sec=5) is None   # closed, but inside the grace period
    assert clk.latest_complete_bar(o + 305, grace_sec=5) == o
    assert clk.latest_complete_bar(ist_epoch(D, "12:00"), 0) == ist_epoch(D, "11:55")
    assert clk.latest_complete_bar(ist_epoch(D, "12:00") + 3, grace_sec=5) == ist_epoch(D, "11:50")
    assert clk.latest_complete_bar(ist_epoch(D, "16:00"), 0) == ist_epoch(D, "15:25")  # capped at the last bar
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_clock.py::test_last_bar_and_latest_complete_bar -q`
Expected: FAIL with `AttributeError: 'SessionClock' object has no attribute 'last_bar_ts'`

- [ ] **Step 3: Implement** (append to `SessionClock`)

```python
    def last_bar_ts(self, d: date) -> int:
        """Open time of the session's last bar (the one that ends at the close)."""
        return self.close_ts(d) - self.interval_sec

    def latest_complete_bar(self, now_ts: int, grace_sec: int = 0) -> Optional[int]:
        """Open time of the most recent bar of now_ts's day whose close plus grace is at or before
        now_ts, capped at the session's last bar. None before the first bar has completed. Paper
        mode uses it to decide which bars are ready to fetch."""
        d = date_of(now_ts)
        o = self.open_ts(d)
        closed_bars = (now_ts - grace_sec - o) // self.interval_sec
        if closed_bars < 1:
            return None
        return min(o + (closed_bars - 1) * self.interval_sec, self.last_bar_ts(d))
```

Add `Optional` to the `typing` import at the top of `clock.py` if it is not already imported.

In the spec, replace the paragraph under `### engine/clock.py: wall-clock scheduling` with:

```
`SessionClock` already maps session times to epochs. Add `last_bar_ts(d)` (open time of the bar that
ends at the close) and `latest_complete_bar(now_ts, grace_sec)` (open time of the most recent bar whose
close plus grace is at or before `now_ts`, capped at the last bar, `None` before the first bar closes).
Sleeping stays out of the clock: `PaperEngine` takes `now` and `sleep` callables so tests drive time.
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_clock.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/clock.py tests/test_clock.py docs/superpowers/specs/2026-09-15-paper-trading-design.md
git commit -m "feat(clock): last_bar_ts and latest_complete_bar for the paper scheduler

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `BacktestBroker.restore`

**Files:**
- Modify: `src/tradebot/execution/backtest.py` (after `open_positions`)
- Test: `tests/test_backtest_broker.py`

- [ ] **Step 1: Write the failing test** (append; the file already imports `BacktestBroker`, `Candle`; add `Closed` from `tradebot.execution.broker` and `ApprovedOrder`, `Position`, `Signal` from `tradebot.types`, and `pytest`, as needed)

```python
def test_restore_then_bar_fills_pending_and_exits_positions():
    b = BacktestBroker(100_000, slippage_pct=0.0, mis_leverage=5.0, entry_buffer_pct=None)
    pos = Position("A", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 900, "c1", "s", db_id=7)
    sig = Signal("s", "B", "LONG", 50.0, 49.0, 52.0, "MIS", 900)
    b.restore([pos], [ApprovedOrder(sig, 4, "c2")], cash=99_000.0)
    assert b.cash == 99_000.0
    assert b.open_positions() == {"A": pos} and b.pending_symbols() == {"B"}

    events = b.on_bar(1200, {"A": Candle("A", 1200, 101.0, 103.0, 100.5, 102.5, 1),
                             "B": Candle("B", 1200, 50.0, 51.0, 49.5, 50.5, 1)})
    assert {type(e).__name__ for e in events} == {"Filled", "Closed"}
    closed = next(e for e in events if isinstance(e, Closed)).position
    assert closed.db_id == 7 and closed.exit_reason == "TARGET" and closed.pnl == pytest.approx(20.0)
    assert set(b.open_positions()) == {"B"} and b.pending_symbols() == set()
    assert b.cash == pytest.approx(99_020.0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_backtest_broker.py::test_restore_then_bar_fills_pending_and_exits_positions -q`
Expected: FAIL with `AttributeError: 'BacktestBroker' object has no attribute 'restore'`

- [ ] **Step 3: Implement** (in `BacktestBroker`, after `open_positions`)

```python
    def restore(self, positions: list[Position], pending: list[ApprovedOrder], cash: float) -> None:
        """Load the state an earlier process left in SQLite (paper resume). Replaces whatever is
        held; `closed` starts empty because the earlier process already recorded its closes."""
        self.cash = float(cash)
        self._positions = {p.symbol: p for p in positions}
        self._pending = {o.signal.symbol: o for o in pending}
        self.closed = []
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_backtest_broker.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/execution/backtest.py tests/test_backtest_broker.py
git commit -m "feat(broker): restore positions, pending entries and cash for a paper resume

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Engine split and the stale-bar rule

**Files:**
- Modify: `src/tradebot/engine/loop.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_engine.py`; it already imports `BacktestEngine`, `BacktestBroker`, `EmaRsiStrategy`, `HistoricalSource`, `SessionClock`, `StubFilter`, `date_of`, `make_config`, and `DAYS`/`_candles`)

```python
def test_stale_bars_record_signals_but_place_nothing(repo, tmp_path):
    cfg = make_config(tmp_path)
    src = HistoricalSource(_candles())
    clock = SessionClock(cfg.session, 5)

    def engine(run_id):
        strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
        broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
        e = BacktestEngine(cfg, repo, src, [strat], broker, StubFilter(), clock, {"A": 1, "B": 1}, run_id)
        repo.create_run(run_id, "backtest", 0, "{}")
        e._start_day()
        return e

    fresh, late = engine("fresh"), engine("late")
    for ts in src.bar_timestamps():
        if date_of(ts) != DAYS[1]:
            continue
        fresh.process_bar(ts, src.candles_at(ts), now_ts=ts + 300 + 5)     # 5 s after the close: fresh
        late.process_bar(ts, src.candles_at(ts), now_ts=ts + 300 + 61)     # past the 60 s deadline
    assert repo.rejection_counts("fresh").get("stale", 0) == 0
    assert repo.rejection_counts("late")["stale"] > 0
    assert repo.list_positions("late") == []
    assert len(repo.list_positions("fresh")) >= 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_engine.py::test_stale_bars_record_signals_but_place_nothing -q`
Expected: FAIL with `TypeError: process_bar() got an unexpected keyword argument 'now_ts'`

- [ ] **Step 3: Implement**

In `src/tradebot/engine/loop.py`:

1. Change the module docstring's first line to: `"""The per-bar cycle (spec sections 4-8). Mode-independent given a broker and clock; subclasses supply the loop.` and add after the order list: `A bar handed in with now_ts past the deadline (paper) records its signals as `stale` and places nothing (spec 8.6).`

2. Replace `class BacktestEngine:` and its `__init__` with:

```python
class Engine:
    """Mode-independent per-bar cycle. BacktestEngine replays stored bars; PaperEngine (engine/paper.py)
    feeds live bars on the wall clock. Everything below `run` is shared."""

    def __init__(self, cfg: Config, repo: Repo, strategies: list[Strategy], broker: Broker, ai_filter: AIFilter,
                 clock: SessionClock, lot_sizes: dict[str, int], run_id: str, mode: str):
        self.cfg = cfg
        self.repo = repo
        self.strategies = strategies
        self.broker = broker
        self.ai_filter = ai_filter
        self.clock = clock
        self.lot_sizes = lot_sizes
        self.run_id = run_id
        self.mode = mode
        self.interval_sec = cfg.execution.interval_minutes * 60
        self._history: dict[str, deque[Candle]] = {}
        self._indicators: dict[str, IndicatorSet] = {}  # shared technical context, one set per symbol
        self._indicator_params = validate_indicator_params(cfg.strategy.get("indicators"))  # fail at start, not mid-run
        self._last_close: dict[str, float] = {}
        self._cooldown_until: dict[str, int] = {}
        self.disabled_strategies: set[str] = set()
        self._day = _DayCounters()
```

3. Move `run` out of the base into a new subclass placed at the end of the file:

```python
class BacktestEngine(Engine):
    def __init__(self, cfg: Config, repo: Repo, source: HistoricalSource, strategies: list[Strategy],
                 broker: Broker, ai_filter: AIFilter, clock: SessionClock, lot_sizes: dict[str, int],
                 run_id: str, mode: str = "backtest"):
        super().__init__(cfg, repo, strategies, broker, ai_filter, clock, lot_sizes, run_id, mode)
        self.source = source

    # -- lifecycle -------------------------------------------------------------
    def run(self) -> str:
        ... (the existing body of run(), unchanged)
```

4. In the base, replace `_end_day`'s last statement (`self.repo.upsert_daily_pnl(...)`) with `self._write_daily_row(d)` and add:

```python
    def _write_daily_row(self, d: date) -> None:
        self.repo.upsert_daily_pnl(self.run_id, d.isoformat(), self._day.realised, self._unrealised_today(),
                                   self._day.fills, self._day.entries_placed)
```

5. Replace the head of `process_bar` (the `for sym, c in candles.items():` loop) with a call to a new method, and add the stale rule:

```python
    def _observe(self, candles: dict[str, Candle]) -> None:
        """Update last closes, the AI context history and the shared indicators. Also used by the
        paper warm-up, which must not touch the broker."""
        for sym, c in candles.items():
            self._last_close[sym] = c.close
            self._history.setdefault(sym, deque(maxlen=self.cfg.ai.candles_in_context)).append(c)
            if sym not in self._indicators:  # not setdefault: that would build a throwaway set every bar
                self._indicators[sym] = IndicatorSet(self._indicator_params)
            self._indicators[sym].update(c)

    def process_bar(self, ts: int, candles: dict[str, Candle], now_ts: Optional[int] = None) -> None:
        """`now_ts` is the wall clock when the bar is handed in (paper); None means the bar is on time."""
        self._observe(candles)

        self._record(self.broker.on_bar(ts, candles))
        ... (square-off, daily cap, kill switch: unchanged) ...

        signals = self._run_strategies(candles)
        if not signals:
            return
        late_by = 0 if now_ts is None else now_ts - (ts + self.interval_sec)
        if late_by > self.cfg.execution.bar_deadline_sec:
            for _, sig in signals:
                sid = self.repo.insert_signal(self.run_id, sig)
                self.repo.insert_risk_decision(self.run_id, sid, False, "stale", 0)
            log.warning("bar %s handed in %ds after its close; %d signal(s) dropped as stale",
                        iso_ist(ts), late_by, len(signals))
            return
        if not self.clock.entries_allowed(ts):
            ... (unchanged) ...
        self._place(ts, signals, kill)
```

Add `from typing import Optional` to the imports.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_engine.py tests/test_cli.py -q`
Expected: all PASS (the golden-trades test proves the split changed nothing). Then `.venv/bin/pytest -q`: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/loop.py tests/test_engine.py
git commit -m "refactor(engine): Engine base with BacktestEngine replay loop; stale-bar rule; _observe and _write_daily_row for paper

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `LiveBarSource`

**Files:**
- Create: `src/tradebot/data/live.py`
- Test: `tests/test_live_source.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_source.py
import logging
from datetime import date

from tests.helpers import make_config, synth_candles
from tradebot.data.live import LiveBarSource
from tradebot.engine.clock import SessionClock, ist_epoch

D = date(2026, 9, 14)


def _source(repo, tmp_path, fetcher, symbols=("A", "B")):
    cfg = make_config(tmp_path)
    clock = SessionClock(cfg.session, 5)
    return LiveBarSource(fetcher, repo, list(symbols), "NSE", 5, 2, clock), clock


def test_fetch_bar_returns_the_closed_bar_and_stores_only_completed_bars(repo, tmp_path):
    a, b = synth_candles("A", [D]), synth_candles("B", [D], phase=4.0, seed=99)
    calls = []

    def fetcher(sym, exch, start, end, interval):
        calls.append((sym, exch, start, end, interval))
        return [c for c in (a if sym == "A" else b) if start <= c.ts <= end]  # `end` is the bar in progress

    src, clock = _source(repo, tmp_path, fetcher)
    bar = ist_epoch(D, "09:30")
    got = src.fetch_bar(bar)
    assert set(got) == {"A", "B"}
    assert got["A"] == a[3] and got["B"].ts == bar
    assert sorted(c[0] for c in calls) == ["A", "B"]
    assert all(c[1] == "NSE" and c[2] == clock.open_ts(D) and c[3] == bar + 300 and c[4] == 5 for c in calls)
    stored = repo.load_candles(["A"], 5, 0, 2_000_000_000)
    assert [c.ts for c in stored] == [c.ts for c in a[:4]]   # 09:15..09:30; the 09:35 bar in progress is not stored


def test_symbol_failure_is_skipped_and_total_failure_is_empty(repo, tmp_path, caplog):
    a = synth_candles("A", [D])

    def flaky(sym, exch, start, end, interval):
        if sym == "B":
            raise RuntimeError("boom")
        return [c for c in a if start <= c.ts <= end]

    src, _ = _source(repo, tmp_path, flaky)
    with caplog.at_level(logging.WARNING, logger="tradebot.live"):
        got = src.fetch_bar(ist_epoch(D, "09:20"))
    assert set(got) == {"A"}
    assert "candle fetch failed for B" in caplog.text

    def dead(sym, exch, start, end, interval):
        raise RuntimeError("down")

    src2, _ = _source(repo, tmp_path, dead)
    with caplog.at_level(logging.ERROR, logger="tradebot.live"):
        assert src2.fetch_bar(ist_epoch(D, "09:20")) == {}
    assert "every candle fetch failed" in caplog.text


def test_fetch_range_groups_bars_by_open_time(repo, tmp_path):
    a = synth_candles("A", [D])
    src, _ = _source(repo, tmp_path, lambda s, e, st, en, i: [c for c in a if st <= c.ts <= en], symbols=("A",))
    first, last = ist_epoch(D, "10:00"), ist_epoch(D, "10:10")
    got = src.fetch_range(first, last)
    assert sorted(got) == [first, first + 300, last]
    assert all(got[t]["A"].ts == t for t in got)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_live_source.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.data.live'`

- [ ] **Step 3: Implement**

```python
# src/tradebot/data/live.py
"""Live bars for paper mode: the just-closed official candle for every universe symbol, fetched over
REST at each bar boundary (paper spec, 'Bar source'). Nothing here places orders."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from tradebot.data.historical import Fetcher
from tradebot.engine.clock import SessionClock, date_of, iso_ist
from tradebot.store.repo import Repo
from tradebot.types import Candle

log = logging.getLogger("tradebot.live")


class LiveBarSource:
    """`fetcher` has the historical.Fetcher shape (symbol, exchange, start_ts, end_ts, interval) so the
    CLI passes GrowwAdapter.fetch_candles and tests pass a fake."""

    def __init__(self, fetcher: Fetcher, repo: Repo, symbols: list[str], exchange: str, interval: int,
                 concurrency: int, clock: SessionClock):
        self.fetcher = fetcher
        self.repo = repo
        self.symbols = list(symbols)
        self.exchange = exchange
        self.interval = interval
        self.interval_sec = interval * 60
        self.concurrency = max(1, concurrency)
        self.clock = clock

    def fetch_range(self, start_ts: int, end_ts: int) -> dict[int, dict[str, Candle]]:
        """Bars with start_ts <= ts <= end_ts (bar open times), keyed by ts then symbol. Requests one
        window per symbol from the session open of start_ts's day to the close of the end bar; every
        completed in-session bar that comes back is stored, so the day grows the candle cache. A
        symbol whose fetch raises is absent from the result and logged; the bar in progress
        (ts > end_ts) is neither stored nor returned."""
        win_start = self.clock.open_ts(date_of(start_ts))
        win_end = end_ts + self.interval_sec

        def one(sym: str) -> tuple:
            try:
                return sym, self.fetcher(sym, self.exchange, win_start, win_end, self.interval)
            except Exception as e:  # noqa: BLE001 - one symbol's failure must not sink the bar
                log.warning("candle fetch failed for %s: %s: %s", sym, type(e).__name__, e, extra={"symbol": sym})
                return sym, None

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            results = list(pool.map(one, self.symbols))

        out: dict[int, dict[str, Candle]] = {}
        failed = 0
        for sym, candles in results:  # SQLite writes stay on this thread
            if candles is None:
                failed += 1
                continue
            completed = [c for c in candles if c.ts <= end_ts and self.clock.in_session(c.ts)]
            self.repo.insert_candles(completed, self.interval)
            for c in completed:
                if c.ts >= start_ts:
                    out.setdefault(c.ts, {})[sym] = c
        if self.symbols and failed == len(self.symbols):
            log.error("every candle fetch failed for bars %s..%s", iso_ist(start_ts), iso_ist(end_ts))
        return out

    def fetch_bar(self, bar_ts: int) -> dict[str, Candle]:
        return self.fetch_range(bar_ts, bar_ts).get(bar_ts, {})
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_live_source.py -q`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/data/live.py tests/test_live_source.py
git commit -m "feat(data): LiveBarSource fetches and stores the closed REST bar per symbol for paper mode

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `PaperEngine`

**Files:**
- Create: `src/tradebot/engine/paper.py`
- Modify: `tests/helpers.py` (add `FakeTime`)
- Test: `tests/test_paper.py`

- [ ] **Step 1: Add `FakeTime` to `tests/helpers.py`** (append)

```python
class FakeTime:
    """Deterministic wall clock for the paper engine: `sleep` advances `now`."""

    def __init__(self, start_ts: int):
        self.t = float(start_ts)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_paper.py
import logging
from datetime import date

import pytest

from tests.helpers import FakeTime, make_config, synth_candles
from tradebot.ai.filter import StubFilter
from tradebot.data.historical import HistoricalSource
from tradebot.data.live import LiveBarSource
from tradebot.engine.clock import SessionClock, date_of, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.engine.paper import PaperEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.strategy.ema_rsi import EmaRsiStrategy

PRIOR = [date(2026, 9, 10), date(2026, 9, 11)]   # Thu, Fri: warm-up days
TODAY = date(2026, 9, 14)                         # Monday
OPEN = ist_epoch(TODAY, "09:15")


def _market():
    return {"A": synth_candles("A", PRIOR + [TODAY]), "B": synth_candles("B", PRIOR + [TODAY], phase=4.0, seed=99)}


def _fetcher(market, calls=None):
    def fetch(sym, exch, start, end, interval):
        if calls is not None:
            calls.append((sym, start, end))
        return [c for c in market[sym] if start <= c.ts <= end]
    return fetch


def _engine(repo, cfg, market, clock_time, run_id="paper-2026-09-14", calls=None):
    clock = SessionClock(cfg.session, 5)
    src = LiveBarSource(_fetcher(market, calls), repo, ["A", "B"], "NSE", 5, 2, clock)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    return PaperEngine(cfg, repo, src, [strat], broker, StubFilter(), clock, {"A": 1, "B": 1}, run_id,
                       now=clock_time.now, sleep=clock_time.sleep)


def _warm(market):
    return [c for cs in market.values() for c in cs if c.ts < OPEN]


def _trades(repo, rid, day=None):
    rows = repo.list_positions(rid)
    return [(r["symbol"], r["direction"], r["qty"], r["opened_at"], r["closed_at"], r["exit_reason"], r["pnl"])
            for r in rows if day is None or date_of(r["opened_at"]) == day]


def test_full_day_matches_the_backtester_bar_for_bar(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"cooldown_bars": 0})
    market = _market()
    calls = []
    t = FakeTime(ist_epoch(TODAY, "09:00"))
    rid = _engine(repo, cfg, market, t, calls=calls).run(_warm(market))
    assert rid == "paper-2026-09-14"
    run = repo.get_run(rid)
    assert run["mode"] == "paper" and run["ended_at"] is not None
    assert run["last_bar_ts"] == ist_epoch(TODAY, "15:25")
    assert len([c for c in calls if c[0] == "A"]) == 75                     # one fetch per bar per symbol
    assert calls[0][1] == OPEN and calls[0][2] == OPEN + 300
    assert [d["date"] for d in repo.daily_pnl(rid)] == ["2026-09-14"]
    assert repo.rejection_counts(rid).get("stale", 0) == 0
    assert t.t >= ist_epoch(TODAY, "15:30") + cfg.data.bar_grace_sec

    # The same day through the backtester, warmed by the same prior days, must produce the same trades.
    all_candles = market["A"] + market["B"]
    repo.insert_candles(all_candles, interval=5)
    src = HistoricalSource(all_candles)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    BacktestEngine(cfg, repo, src, [strat], broker, StubFilter(), SessionClock(cfg.session, 5), {"A": 1, "B": 1}, "bt").run()
    assert _trades(repo, rid) == _trades(repo, "bt", day=TODAY)
    assert _trades(repo, rid), "synthetic data must produce trades"


def test_late_start_catches_up_in_one_fetch_and_drops_stale_signals(repo, tmp_path):
    cfg = make_config(tmp_path)
    market = _market()
    calls = []
    t = FakeTime(ist_epoch(TODAY, "12:00") + 10)
    eng = _engine(repo, cfg, market, t, calls=calls)
    eng.run(_warm(market))
    assert calls[0][1] == OPEN and calls[0][2] == ist_epoch(TODAY, "12:00")   # 09:15..11:55 in one window
    cutoff = ist_epoch(TODAY, "11:55")
    n = repo.conn.execute("SELECT COUNT(*) FROM signals WHERE run_id=? AND bar_ts < ?", (eng.run_id, cutoff)).fetchone()[0]
    assert n > 0
    assert repo.rejection_counts(eng.run_id)["stale"] == n
    assert repo.get_run(eng.run_id)["ended_at"] is not None


def test_stop_and_resume_matches_a_continuous_run(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"cooldown_bars": 0})   # cooldowns are not persisted across a restart
    market = _market()
    warm = _warm(market)
    _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="cont").run(warm)

    t = FakeTime(ist_epoch(TODAY, "09:00"))
    first = _engine(repo, cfg, market, t, run_id="split")
    stop_at = ist_epoch(TODAY, "11:30")

    def sleep_then_stop(seconds):
        t.sleep(seconds)
        if t.t >= stop_at:
            first.request_stop()

    first.sleep = sleep_then_stop
    first.run(warm)
    run = repo.get_run("split")
    assert run["ended_at"] is None and run["last_bar_ts"] is not None
    assert repo.daily_pnl("split"), "the day's row is written on suspend"

    stored_today = [c for c in repo.load_candles(["A", "B"], 5, OPEN, 2_000_000_000) if c.ts <= run["last_bar_ts"]]
    second = _engine(repo, cfg, market, t, run_id="split")
    second.run(warm + stored_today)
    assert repo.get_run("split")["ended_at"] is not None
    assert _trades(repo, "split") == _trades(repo, "cont")
    assert _trades(repo, "cont")
    a, b = repo.daily_pnl("cont")[0], repo.daily_pnl("split")[0]
    assert (a["realised"], a["fills"], a["entries_placed"]) == (b["realised"], b["fills"], b["entries_placed"])


def test_outside_session_creates_no_run(repo, tmp_path):
    cfg = make_config(tmp_path)
    market = _market()
    saturday = FakeTime(ist_epoch(date(2026, 9, 12), "10:00"))
    assert _engine(repo, cfg, market, saturday, run_id="sat").run([]) is None
    late = FakeTime(ist_epoch(TODAY, "15:31"))
    assert _engine(repo, cfg, market, late, run_id="late").run([]) is None
    assert repo.get_run("sat") is None and repo.get_run("late") is None


def test_resuming_an_ended_run_is_refused(repo, tmp_path):
    cfg = make_config(tmp_path)
    market = _market()
    _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="done").run(_warm(market))
    again = _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "15:00")), run_id="done")
    with pytest.raises(ValueError, match="already ended"):
        again.run([])


def test_a_failing_bar_is_logged_and_the_loop_continues(repo, tmp_path, caplog, monkeypatch):
    cfg = make_config(tmp_path)
    market = _market()
    eng = _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="boom")
    real = eng.process_bar
    hits = []

    def flaky(ts, candles, now_ts=None):
        if not hits:
            hits.append(ts)
            raise RuntimeError("kaboom")
        return real(ts, candles, now_ts=now_ts)

    monkeypatch.setattr(eng, "process_bar", flaky)
    with caplog.at_level(logging.ERROR, logger="tradebot.paper"):
        eng.run(_warm(market))
    assert "failed; continuing" in caplog.text
    assert hits == [OPEN]
    assert repo.get_run("boom")["last_bar_ts"] == ist_epoch(TODAY, "15:25")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_paper.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.engine.paper'`

- [ ] **Step 4: Implement**

```python
# src/tradebot/engine/paper.py
"""Paper mode: the wall-clock loop over live REST bars (paper spec, 'Scheduling'). The broker is the
backtest simulator driven by official candles, so nothing here places a real order.

Per iteration: find the latest bar whose close plus grace has passed, fetch every bar from the last
processed one up to it in one window, process them in order, then sleep until the next bar's close
plus grace. A bar handed to process_bar later than the deadline drops its signals as `stale`, so a
catch-up after a late wake or a restart only settles fills and exits. Stop (SIGINT/SIGTERM via
request_stop) finishes the current bar, writes the day's row and leaves the run open so a restart the
same day resumes it; the session end squares off, writes the row and ends the run."""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Callable, Optional

from tradebot.config import resolved_config
from tradebot.data.live import LiveBarSource
from tradebot.engine.clock import date_of, iso_ist
from tradebot.engine.loop import Engine, _DayCounters
from tradebot.types import ApprovedOrder, Candle, Position, Signal

log = logging.getLogger("tradebot.paper")


def position_from_row(r) -> Position:
    return Position(r["symbol"], r["product"], r["direction"], r["qty"], r["avg_price"], r["stop"], r["target"],
                    r["opened_at"], r["client_id"], r["strategy"], fill_status=r["fill_status"],
                    adopted=bool(r["adopted"]), db_id=r["id"])


def order_from_row(r) -> ApprovedOrder:
    sig = Signal(r["strategy"], r["symbol"], r["direction"], r["entry"], r["stop"], r["target"], r["product"], r["bar_ts"])
    return ApprovedOrder(sig, r["qty"], r["client_id"])


class PaperEngine(Engine):
    def __init__(self, cfg, repo, source: LiveBarSource, strategies, broker, ai_filter, clock, lot_sizes: dict,
                 run_id: str, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        super().__init__(cfg, repo, strategies, broker, ai_filter, clock, lot_sizes, run_id, mode="paper")
        self.source = source
        self.now = now
        self.sleep = sleep
        self.grace = cfg.data.bar_grace_sec
        self.stop_requested = False

    def request_stop(self) -> None:
        """Signal-handler entry point: finish the current bar, write the day's row, leave positions open."""
        self.stop_requested = True

    # -- start-up -----------------------------------------------------------------------------
    def warm(self, candles: list[Candle]) -> Optional[int]:
        """Feed stored candles, oldest first, to the indicators and strategies without touching the
        broker. Returns the latest warmed bar ts, or None when there was nothing to warm."""
        by_ts: dict = {}
        for c in candles:
            by_ts.setdefault(c.ts, {})[c.symbol] = c
        last = None
        for ts in sorted(by_ts):
            self._observe(by_ts[ts])
            self._run_strategies(by_ts[ts])  # signals discarded: warm-up never places
            last = ts
        return last

    def resume(self, today: date) -> bool:
        """Reload today's run from SQLite into the broker and day counters. False when there is no
        such run; raises when it already ended (one run per day unless --run-id says otherwise)."""
        run = self.repo.get_run(self.run_id)
        if run is None:
            return False
        if run["ended_at"] is not None:
            raise ValueError(f"run {self.run_id} already ended today; pass a different --run-id to start another session")
        closed = [r for r in self.repo.list_positions(self.run_id) if r["closed_at"] is not None]
        positions = [position_from_row(r) for r in self.repo.open_positions(self.run_id)]
        pending = [order_from_row(r) for r in self.repo.pending_orders(self.run_id)]
        self.broker.restore(positions, pending, self.cfg.capital + sum(r["pnl"] or 0.0 for r in closed))
        for p in positions:  # unrealised needs a price for every open symbol; a gap falls back to cost
            self._last_close.setdefault(p.symbol, p.avg_price)
        rows = {r["date"]: r for r in self.repo.daily_pnl(self.run_id)}
        row = rows.get(today.isoformat())
        self._day = _DayCounters(realised=sum(r["pnl"] or 0.0 for r in closed if date_of(r["closed_at"]) == today),
                                 entries_placed=row["entries_placed"] if row else 0,
                                 fills=row["fills"] if row else 0)
        log.info("resumed run %s: %d open position(s), %d pending entr%s, last bar %s", self.run_id,
                 len(positions), len(pending), "y" if len(pending) == 1 else "ies",
                 iso_ist(run["last_bar_ts"]) if run["last_bar_ts"] is not None else "none")
        return True

    # -- loop ---------------------------------------------------------------------------------
    def run(self, warm_candles: list[Candle]) -> Optional[str]:
        """Returns the run id, or None when there is no session to trade right now."""
        now = int(self.now())
        today = date_of(now)
        if not self.clock.is_trading_day(today):
            log.info("%s is not a trading day; nothing to do", today)
            return None
        if now >= self.clock.close_ts(today):
            log.info("the %s session has already closed; nothing to do", today)
            return None
        warmed = self.warm(warm_candles)
        resumed = self.resume(today)
        if not resumed:
            self.repo.create_run(self.run_id, self.mode, now, json.dumps(resolved_config(self.cfg), default=str))
            self._start_day()
        open_ts = self.clock.open_ts(today)
        last = open_ts - self.interval_sec
        if warmed is not None and warmed >= open_ts:
            last = warmed  # today's stored bars were replayed by warm(); they are not fetched again
        stored_last = self.repo.get_run(self.run_id)["last_bar_ts"]
        if stored_last is not None:
            last = max(last, stored_last)
        end = self.clock.last_bar_ts(today)
        log.info("paper run %s (%s) %s: next bar %s, strategies=%s, %d symbols", self.run_id,
                 "resumed" if resumed else "new", today, iso_ist(last + self.interval_sec),
                 [s.name for s in self.strategies], len(self.source.symbols))
        try:
            while last < end and not self.stop_requested:
                latest = self.clock.latest_complete_bar(int(self.now()), self.grace)
                if latest is None or latest <= last:
                    self._sleep_until(last + 2 * self.interval_sec + self.grace)  # close of the next bar + grace
                    continue
                first = last + self.interval_sec
                bars = self.source.fetch_range(first, latest)
                for ts in range(first, latest + 1, self.interval_sec):
                    self._bar(ts, bars.get(ts, {}))
                    last = ts
                    if self.stop_requested:
                        break
        finally:
            if self.stop_requested and last < end:
                self._write_daily_row(today)
                log.info("paper run %s suspended after bar %s; start again today to resume", self.run_id,
                         iso_ist(last) if last >= open_ts else "none")
            else:
                self._end_day(today, max(last, open_ts))
                self.repo.end_run(self.run_id, int(self.now()))
                log.info("paper run %s ended: realised %.2f, %d fills / %d entries", self.run_id,
                         self._day.realised, self._day.fills, self._day.entries_placed)
        return self.run_id

    def _bar(self, ts: int, candles: dict) -> None:
        try:
            self.process_bar(ts, candles, now_ts=int(self.now()))
        except Exception:  # noqa: BLE001 - spec: log and keep the loop alive; nothing sits at a real broker
            log.exception("bar %s failed; continuing with the next bar", iso_ist(ts))
        self.repo.set_last_bar_ts(self.run_id, ts)
        self._write_daily_row(date_of(ts))

    def _sleep_until(self, wake_ts: float) -> None:
        """Sleep in short slices so a stop request is honoured within a second."""
        while not self.stop_requested:
            remaining = wake_ts - self.now()
            if remaining <= 0:
                return
            self.sleep(min(1.0, remaining))
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_paper.py -q -x`
Expected: 6 PASS. If `test_late_start_catches_up_in_one_fetch_and_drops_stale_signals` fails only on `n > 0` (the synthetic morning produced no EMA/RSI signal), move the fake start to `ist_epoch(TODAY, "14:00") + 10` and the cutoff to `"13:55"`; the assertion on the window end becomes `ist_epoch(TODAY, "14:00")`.

- [ ] **Step 6: Run everything**

Run: `.venv/bin/pytest -q`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/tradebot/engine/paper.py tests/test_paper.py tests/helpers.py
git commit -m "feat(engine): PaperEngine — wall-clock loop over live bars, warm-up, same-day resume, stop without flatten

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `tradebot paper` command

**Files:**
- Modify: `src/tradebot/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`; it already has `_invoke(tmp_path, *args)`, `make_config`, `synth_candles`, `CliRunner`, `cli`, `ist_epoch`, `connect`, `Repo`, `Candle`; add `import yaml`, `from tests.helpers import FakeTime`, and `from datetime import date` if missing)

```python
PRIOR = [date(2026, 9, 10), date(2026, 9, 11)]
TODAY = date(2026, 9, 14)


def _paper_setup(tmp_path, monkeypatch, fetch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B]\n")
    hdr = "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
    (tmp_path / "instruments.csv").write_text(hdr + "".join(f"NSE,{i},{s},CASH,EQ,1,0.05,1,1\n" for i, s in enumerate("AB")))
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Fake:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            return fetch(symbol, exchange, start_ts, end_ts, interval)

    monkeypatch.setattr(cli, "GrowwAdapter", Fake)


def test_paper_refuses_cnc_strategies(tmp_path, monkeypatch):
    _paper_setup(tmp_path, monkeypatch, lambda *a: [])
    p = tmp_path / "config.yaml"
    raw = yaml.safe_load(p.read_text())
    raw["strategy"]["ema_rsi"]["product"] = "CNC"
    p.write_text(yaml.safe_dump(raw))
    res = _invoke(tmp_path, "paper")
    assert res.exit_code != 0
    assert "MIS" in res.output


def test_paper_outside_session_exits_cleanly_without_a_run(tmp_path, monkeypatch):
    _paper_setup(tmp_path, monkeypatch, lambda *a: [])
    saturday = FakeTime(ist_epoch(date(2026, 9, 12), "10:00"))
    monkeypatch.setattr(cli.time, "time", saturday.now)
    res = _invoke(tmp_path, "paper", "--run-id", "p1")
    assert res.exit_code == 0, res.output
    assert "nothing to trade" in res.output
    assert Repo(connect(make_config(tmp_path).paths.db)).get_run("p1") is None


def test_paper_full_day_end_to_end(tmp_path, monkeypatch):
    market = {"A": synth_candles("A", PRIOR + [TODAY]), "B": synth_candles("B", PRIOR + [TODAY], phase=4.0, seed=99)}
    _paper_setup(tmp_path, monkeypatch, lambda sym, ex, s, e, i: [c for c in market[sym] if s <= c.ts <= e])
    t = FakeTime(ist_epoch(TODAY, "09:00"))
    monkeypatch.setattr(cli.time, "time", t.now)
    monkeypatch.setattr(cli.time, "sleep", t.sleep)
    res = _invoke(tmp_path, "paper", "--run-id", "p2", "--ai", "stub")
    assert res.exit_code == 0, res.output
    assert "Run p2 (paper)" in res.output
    repo = Repo(connect(make_config(tmp_path).paths.db))
    run = repo.get_run("p2")
    assert run["mode"] == "paper" and run["ended_at"] is not None
    assert run["last_bar_ts"] == ist_epoch(TODAY, "15:25")
    assert repo.latest_candle_ts("A", 5) == ist_epoch(TODAY, "15:25")     # the day's bars grew the cache
    assert repo.list_positions("p2"), "the warm-up from the prior days must make trades possible today"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -q -k paper`
Expected: FAIL with `Error: No such command 'paper'` (exit code 2 / assertion on output)

- [ ] **Step 3: Implement**

In `src/tradebot/cli.py`:

1. Imports: add `import signal as os_signal`, `from tradebot.data.live import LiveBarSource`, `from tradebot.engine.paper import PaperEngine`. Update the module docstring to `"""Command-line entry point: fetch-data, backtest, report, estimate-ai, paper. Live trading arrives in the live plan."""`.

2. Add after `_prices`:

```python
def _warm_fetch(cfg: Config, adapter, repo: Repo, symbols: list, exchange: str, clock: SessionClock,
                now_ts: int) -> list:
    """Bring the candle cache up to the last completed bar before starting paper. One symbol's failure
    does not stop the others; auth failures are fatal. Returns the symbols that failed."""
    failed = []
    interval = cfg.execution.interval_minutes
    for sym in symbols:
        try:
            fetch_incremental(repo, adapter.fetch_candles, [sym], exchange, interval, 10, now_ts,
                              keep=lambda cd: clock.in_session(cd.ts))
        except Exception as e:  # noqa: BLE001 - isolate per symbol; auth errors are fatal
            if _is_fatal_auth(e):
                raise
            failed.append(sym)
            click.echo(f"warm-up fetch failed for {sym}: {type(e).__name__}: {e}", err=True)
    return failed


def _warm_candles(repo: Repo, symbols: list, interval: int, now_ts: int, n: int) -> list:
    """The last `n` stored bars per symbol, up to now. Includes today's completed bars, which
    PaperEngine.warm treats as already processed."""
    out = []
    for sym in symbols:
        out.extend(repo.load_candles([sym], interval, now_ts - 30 * 86400, now_ts)[-n:])
    return out


@main.command()
@click.option("--strategy", "strategy_name", default="ema_rsi", show_default=True)
@click.option("--run-id", default=None, help="Defaults to paper-<today>; a run resumes if it exists and has not ended")
@click.option("--ai", "ai_filter", default=None, type=click.Choice(["stub", "claude", "claude_cached"]),
              help="Override ai.filter from config for this session")
@click.pass_obj
def paper(cfg: Config, strategy_name: str, run_id: Optional[str], ai_filter: Optional[str]) -> None:
    """Paper-trade today's session on live 5-minute REST bars. Places no real orders: fills and exits
    are simulated exactly as in backtest. Ctrl-C finishes the current bar and leaves the run resumable."""
    if strategy_name not in cfg.strategy:
        raise click.ClickException(f"no config for strategy '{strategy_name}'")
    params = strategy_params(cfg.strategy, strategy_name)
    if params.get("product", "MIS") != "MIS":
        raise click.ClickException("paper mode supports MIS (intraday) strategies only; set product: MIS")
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret, cfg.secrets.groww_api_secret)
    interval = cfg.execution.interval_minutes
    clock = SessionClock(cfg.session, interval)
    now = int(time.time())
    today = date_of(now)
    run_id = run_id or f"paper-{today.isoformat()}"
    if not clock.is_trading_day(today) or now >= clock.close_ts(today):
        click.echo(f"nothing to trade: {today} is not a trading day or the session has closed")
        return
    log_path = setup_logging(cfg.paths.logs, run_id=run_id)
    path = Path(cfg.paths.instruments)
    if not _instruments_fresh(path):
        click.echo("downloading instrument master")
        download_instruments(path)
    symbols, lots, exchange = _symbols_and_lots(cfg, require_instruments=True)
    adapter.client  # log in once, here, so a credential problem aborts before anything else
    click.echo(f"logged in to Groww ({adapter.flow} flow); paper mode places no orders")
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is not None and repo.get_run(run_id)["mode"] != "paper":
        raise click.ClickException(f"run '{run_id}' exists and is not a paper run; pick another --run-id")
    failed = _warm_fetch(cfg, adapter, repo, symbols, exchange, clock, now)
    if failed:
        click.echo(f"warm-up fetch failed for {len(failed)} symbol(s); they warm up live", err=True)
    warm = _warm_candles(repo, symbols, interval, now, cfg.data.warmup_bars)
    source = LiveBarSource(adapter.fetch_candles, repo, symbols, exchange, interval,
                           cfg.data.official_fetch_concurrency, clock)
    strategy = build_strategy(strategy_name, params)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    ai_cfg = dc_replace(cfg.ai, filter=ai_filter) if ai_filter else cfg.ai
    ai = build_filter(ai_cfg, cfg.secrets.anthropic_api_key, repo=repo, clock=clock)
    engine = PaperEngine(dc_replace(cfg, ai=ai_cfg), repo, source, [strategy], broker, ai, clock, lots, run_id,
                         now=time.time, sleep=time.sleep)

    def _stop(signum, frame):
        click.echo("stop requested; finishing the current bar (positions stay open on the books)", err=True)
        engine.request_stop()

    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        os_signal.signal(sig, _stop)
    rid = engine.run(warm)
    if rid is None:
        click.echo("nothing to trade: the session closed while warming up")
        return
    click.echo(format_summary(build_summary(repo, rid)))
    if log_path:
        click.echo(f"log: {log_path}")
```

`date_of` is already imported from `tradebot.engine.clock` in `cli.py`; `fetch_incremental` too.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_cli.py -q`
Expected: all PASS. If `test_paper_full_day_end_to_end` fails on `"Run p2 (paper)"`, check the exact header `format_summary` prints (`tests/test_report.py` shows it) and match it.

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/cli.py tests/test_cli.py
git commit -m "feat(cli): tradebot paper — live REST bars through the simulated broker, warm-up, resume, no real orders

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: README and a dry run of the command

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a section after "Backtest"**

```markdown
## Paper trading

    .venv/bin/tradebot paper --strategy ema_rsi --ai stub

Runs today's session on live 5-minute candles fetched over REST at each bar boundary, through the
same strategy, risk, AI filter and simulated broker as the backtester. It places no real orders.
Start it before 09:15 IST (it waits) or any time during the session (it warms up from the cache and
catches up). One run per day, id `paper-YYYY-MM-DD`; `report --run paper-YYYY-MM-DD` prints the
summary and the daily fill rate. Ctrl-C finishes the current bar and leaves the run resumable: start
the command again the same day and it reloads open positions and pending entries from SQLite. The
approval-flow Groww key must be approved on the API keys page before starting each day.

`--ai stub` keeps Claude out of the loop; drop it (or pass `--ai claude_cached`) to pay for the
filter on live signals. Intraday (MIS) strategies only.
```

- [ ] **Step 2: Dry-run the command outside market hours**

Run (needs `.env` with Groww credentials): `.venv/bin/tradebot paper --run-id dry-1`
Expected: prints `nothing to trade: ... not a trading day or the session has closed` if run outside 09:15-15:30 IST, and exit code 0. Inside the session it would start a real paper run; do not leave it running unattended in this task. If `.env` is missing the command fails with the existing credentials message, which is also acceptable here.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest -q`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: paper trading section

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review against the spec

- Hard rule: no task imports or calls an order endpoint; `GrowwAdapter` is used only for `client` (login) and `fetch_candles`. Covered.
- Bar source (fetch per boundary, storage, per-symbol skip, total-failure log): Task 6.
- Engine split, `warm`, `resume`, `run`, stale rule: Tasks 5 and 7.
- Clock helpers: Task 3 (spec text updated to match the two helpers actually built).
- Scheduling steps 1-5 and the stop flag: Task 7 (`run`, `_bar`, `_sleep_until`); late wake replays a range: `test_late_start_catches_up_in_one_fetch_and_drops_stale_signals`.
- Warm-up: `_warm_fetch` + `_warm_candles` (Task 8) feeding `PaperEngine.warm` (Task 7). The spec says "bars ending before today's open"; the plan warms with all stored bars up to now and treats today's warmed bars as processed, which is what makes a mid-session fresh start and a resume behave the same. Spec table row "Warm-up" still reads correctly.
- Store: Task 2. Config: Task 1. Broker restore: Task 4. CLI: Task 8. CNC refusal, outside-session exit, ended-run refusal: Tasks 7 and 8.
- Error table: auth fatal (adapter raises, CLI does not catch), per-symbol fetch skip (Task 6), strategy disable (existing), exception inside a bar (Task 7 `_bar`), ended run refused (Task 7), late wake (Task 7).
- Testing list in the spec: every item has a test in Tasks 2-8; the resume-equivalence test pins cooldowns to 0 and documents why.
- Signatures are consistent across tasks: `process_bar(ts, candles, now_ts=None)`, `latest_complete_bar(now_ts, grace_sec)`, `last_bar_ts(d)`, `restore(positions, pending, cash)`, `set_last_bar_ts(run_id, ts)`, `open_positions(run_id)`, `pending_orders(run_id)`, `LiveBarSource(fetcher, repo, symbols, exchange, interval, concurrency, clock)`, `PaperEngine(cfg, repo, source, strategies, broker, ai_filter, clock, lot_sizes, run_id, now, sleep)`, `run(warm_candles)`.
