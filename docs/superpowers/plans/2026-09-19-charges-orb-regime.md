# Real Charges, Opening-Range Breakout and NIFTY Regime Filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backtest and paper results net of real Groww intraday charges, add a low-turnover opening-range breakout strategy with priority-ranked signals, add a NIFTY regime filter, and measure all of it with a tuning window and a holdout.

**Architecture:** Charges are a pure function called from the one place a trade closes (`BacktestBroker._close`); `pnl` stays gross and a new `positions.charges` column carries the cost. ORB is a new `Strategy` subclass; same-bar signals are sorted by a new optional `Signal.priority`. The regime filter is a small streaming class the engine feeds from an index candle that is split off before any strategy sees the bar.

**Tech Stack:** Python 3.9 (keep every annotation 3.9-safe: `from __future__ import annotations`, `Optional[...]` in runtime positions), SQLite, click, pytest. Run everything with `.venv/bin/...` from the repo root `/Users/toothless/AI-Trading Bot`.

**Spec:** `docs/superpowers/specs/2026-09-19-charges-orb-regime-design.md`. Branch: `charges-orb-regime` (already created).

**Baseline:** `.venv/bin/pytest -q` → `384 passed`. Every task ends with the full suite green.

**Commit trailer:** end every commit message with
`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`

## File map

| File | Change | Responsibility |
|---|---|---|
| `src/tradebot/config.py` | modify | `ChargesConfig`, `RegimeConfig`, `DataConfig.index_symbol`, optional sections, validation |
| `src/tradebot/execution/charges.py` | create | pure `round_trip_charges` |
| `src/tradebot/types.py` | modify | `Position.charges`, `Signal.priority` |
| `src/tradebot/execution/backtest.py` | modify | broker books charges, cash moves by net |
| `src/tradebot/store/schema.sql`, `store/db.py`, `store/repo.py` | modify | `positions.charges`, schema v4 |
| `src/tradebot/engine/loop.py` | modify | net realised, priority sort, `_split_index`, regime gate |
| `src/tradebot/engine/paper.py` | modify | resume on net, warm-up splits the index |
| `src/tradebot/report/summary.py`, `report/compare.py` | modify | gross / charges / net, after-the-fact estimate |
| `src/tradebot/strategy/orb.py` | create | opening-range breakout |
| `src/tradebot/strategy/ema_rsi.py` | modify | register `orb` in `build_strategy` |
| `src/tradebot/risk/regime.py` | create | `RegimeFilter` |
| `src/tradebot/cli.py` | modify | charges to broker and reports, index symbol in fetch/backtest/paper |
| `config.yaml`, `config-15m.yaml` | modify | `charges:` block |
| `config-orb.yaml` | create | ORB variant |
| `scripts/orb_experiment.py` | create | tuning, holdout and regime runs |
| `tests/helpers.py` | modify | charges off by default in tests, `FixedStrategy` |
| `tests/test_charges.py`, `tests/test_orb.py`, `tests/test_regime.py` | create | unit tests |
| `tests/test_config.py`, `test_backtest_broker.py`, `test_store.py`, `test_engine.py`, `test_paper.py`, `test_report.py`, `test_compare.py` | modify | new cases |
| `docs/superpowers/notes/2026-09-19-charges-orb-regime-results.md` | create | results |

---

# Phase 1 — charges

### Task 1: `ChargesConfig` and optional config sections

**Files:**
- Modify: `src/tradebot/config.py`
- Modify: `tests/helpers.py`
- Modify: `config.yaml`, `config-15m.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (add `ChargesConfig` to the existing `from tradebot.config import ...` line):

```python
def test_charges_section_missing_means_groww_defaults(tmp_path):
    cfg = make_config(tmp_path, charges=None)
    assert cfg.charges == ChargesConfig()
    assert cfg.charges.enabled is True
    assert (cfg.charges.brokerage_pct, cfg.charges.brokerage_min, cfg.charges.brokerage_max) == (0.1, 5.0, 20.0)
    assert (cfg.charges.stt_sell_pct, cfg.charges.exchange_txn_pct) == (0.025, 0.00297)
    assert (cfg.charges.sebi_pct, cfg.charges.stamp_buy_pct, cfg.charges.gst_pct) == (0.0001, 0.003, 18.0)


def test_charges_are_validated(tmp_path):
    with pytest.raises(ValueError, match="charges.stt_sell_pct"):
        make_config(tmp_path, charges={"stt_sell_pct": -1})
    with pytest.raises(ValueError, match="brokerage_min"):
        make_config(tmp_path, charges={"brokerage_min": 30.0})
    with pytest.raises(ValueError, match="unknown keys"):
        make_config(tmp_path, charges={"brokrage_pct": 1})


def test_shipped_configs_carry_the_charges_block(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in ("config.yaml", "config-15m.yaml"):
        cfg = load_config(root / name, tmp_path / "nonexistent.env")
        assert "charges" in cfg.raw, name
        assert cfg.charges == ChargesConfig(), name


def test_resolved_config_records_charges(tmp_path):
    from tradebot.config import resolved_config
    assert resolved_config(make_config(tmp_path, charges={"enabled": True}))["charges"]["enabled"] is True
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: collection error, `ImportError: cannot import name 'ChargesConfig'`.

- [ ] **Step 3: Implement**

In `src/tradebot/config.py` add `import math` beside the other stdlib imports, then add after `PathsConfig`:

```python
@dataclass(frozen=True)
class ChargesConfig:
    """Groww intraday equity schedule. ``*_pct`` fields are human percents of order value.
    Every field has a default, so the section may be left out of config.yaml."""
    enabled: bool = True
    brokerage_pct: float = 0.1         # per order ...
    brokerage_max: float = 20.0        # ... capped at this many rupees
    brokerage_min: float = 5.0         # ... and floored at this
    stt_sell_pct: float = 0.025        # sell side only
    exchange_txn_pct: float = 0.00297  # NSE, both sides
    sebi_pct: float = 0.0001           # both sides
    stamp_buy_pct: float = 0.003       # buy side only
    gst_pct: float = 18.0              # on brokerage + exchange txn + SEBI
```

Add to `Config`, after `raw: dict` (defaults must come last):

```python
    charges: ChargesConfig = ChargesConfig()
```

Add below `_section`:

```python
def _optional_section(raw: dict, name: str, cls):
    """For a section whose every field has a default: absent or empty in YAML means all defaults."""
    if raw.get(name) is None:
        return cls()
    return _section(raw, name, cls)
```

In `_validate`, before the `for name in ("open", ...)` loop:

```python
    ch = cfg.charges
    for f in dataclasses.fields(ch):
        if f.name != "enabled":
            v = getattr(ch, f.name)
            checks.append((math.isfinite(v) and v >= 0, f"charges.{f.name} must be a finite number >= 0"))
    checks.append((ch.brokerage_min <= ch.brokerage_max, "charges.brokerage_min must not exceed charges.brokerage_max"))
```

In `load_config`, pass `charges=_optional_section(raw, "charges", ChargesConfig),` to `Config(...)` after `raw=raw,`.

In `tests/helpers.py` add to `BASE_CONFIG` (tests pin pre-charges numbers; tests that want charges turn them on):

```python
    "charges": {"enabled": False},
```

and make the override loop tolerate a section that `BASE_CONFIG` lacks:

```python
        raw[key] = {**(raw.get(key) or {}), **val} if isinstance(val, dict) else val
```

In `config.yaml` and `config-15m.yaml`, add after the `execution:` block:

```yaml
charges:                        # Groww intraday equity; check against groww.in/pricing before trusting results
  enabled: true
  brokerage_pct: 0.1            # per order, percent of order value ...
  brokerage_max: 20.0           # ... capped at this many rupees
  brokerage_min: 5.0            # ... and floored at this
  stt_sell_pct: 0.025           # sell side only
  exchange_txn_pct: 0.00297     # NSE, both sides
  sebi_pct: 0.0001              # both sides
  stamp_buy_pct: 0.003          # buy side only
  gst_pct: 18.0                 # on brokerage + exchange txn + SEBI
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: `388 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py tests/helpers.py tests/test_config.py config.yaml config-15m.yaml
git commit -m "config: charges section with Groww intraday defaults; optional sections"
```

### Task 2: `round_trip_charges`

**Files:**
- Create: `src/tradebot/execution/charges.py`
- Test: `tests/test_charges.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_charges.py`. The expected values are worked by hand in the comments; do not change them to match the code.

```python
import pytest

from tradebot.config import ChargesConfig
from tradebot.execution.charges import round_trip_charges

CFG = ChargesConfig()


def test_long_round_trip_by_hand():
    # buy 100 x 100.00 = 10,000; sell 100 x 102.00 = 10,200
    # brokerage 10.00 + 10.20 = 20.20; STT 10,200 x 0.025% = 2.55; txn 20,200 x 0.00297% = 0.59994
    # SEBI 20,200 x 0.0001% = 0.0202; stamp 10,000 x 0.003% = 0.30
    # GST 18% x (20.20 + 0.59994 + 0.0202) = 3.7476252; total 27.4177652
    assert round_trip_charges(10_000.0, 10_200.0, CFG) == pytest.approx(27.42)


def test_short_round_trip_takes_values_by_side_not_by_order():
    # short 100 @ 100.00 (sell 10,000), cover @ 98.00 (buy 9,800)
    # brokerage 9.80 + 10.00 = 19.80; STT 2.50; txn 0.58806; SEBI 0.0198; stamp 0.294
    # GST 18% x 20.40786 = 3.6734148; total 26.8752748
    assert round_trip_charges(9_800.0, 10_000.0, CFG) == pytest.approx(26.88)


def test_brokerage_is_capped_per_order():
    # 65,000 each side: 0.1% would be 65 per order, capped at 20 -> 40
    # STT 16.25; txn 3.861; SEBI 0.13; stamp 1.95; GST 18% x 43.991 = 7.91838; total 70.10938
    assert round_trip_charges(65_000.0, 65_000.0, CFG) == pytest.approx(70.11)


def test_brokerage_is_floored_per_order():
    # 1,000 each side: 0.1% would be 1 per order, floored at 5 -> 10
    # STT 0.25; txn 0.0594; SEBI 0.002; stamp 0.03; GST 18% x 10.0614 = 1.811052; total 12.152452
    assert round_trip_charges(1_000.0, 1_000.0, CFG) == pytest.approx(12.15)


def test_disabled_or_absent_config_costs_nothing():
    assert round_trip_charges(10_000.0, 10_200.0, ChargesConfig(enabled=False)) == 0.0
    assert round_trip_charges(10_000.0, 10_200.0, None) == 0.0


def test_result_is_rounded_to_paise():
    v = round_trip_charges(12_345.67, 12_400.10, CFG)
    assert v == round(v, 2) and v > 0
```

- [ ] **Step 2: Run to see it fail**

Run: `.venv/bin/pytest tests/test_charges.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.execution.charges'`.

- [ ] **Step 3: Implement**

Create `src/tradebot/execution/charges.py`:

```python
"""Statutory and brokerage charges on one closed intraday trade. Pure: no I/O, no state.

Values are taken by side (what was bought, what was sold), not by entry and exit, because STT is
charged on the sell side and stamp duty on the buy side: a short sells first and buys second."""
from __future__ import annotations

from typing import Optional

from tradebot.config import ChargesConfig


def round_trip_charges(buy_value: float, sell_value: float, cfg: Optional[ChargesConfig]) -> float:
    """Rupees charged on a trade that bought `buy_value` and sold `sell_value`, rounded to paise.
    Zero when `cfg` is None or disabled."""
    if cfg is None or not cfg.enabled:
        return 0.0

    def brokerage(order_value: float) -> float:
        return min(max(order_value * cfg.brokerage_pct / 100.0, cfg.brokerage_min), cfg.brokerage_max)

    turnover = buy_value + sell_value
    brok = brokerage(buy_value) + brokerage(sell_value)
    stt = sell_value * cfg.stt_sell_pct / 100.0
    txn = turnover * cfg.exchange_txn_pct / 100.0
    sebi = turnover * cfg.sebi_pct / 100.0
    stamp = buy_value * cfg.stamp_buy_pct / 100.0
    gst = (brok + txn + sebi) * cfg.gst_pct / 100.0
    return round(brok + stt + txn + sebi + stamp + gst, 2)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/test_charges.py -q` → `6 passed`. Then `.venv/bin/pytest -q` → `394 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/execution/charges.py tests/test_charges.py
git commit -m "execution: round-trip charges as a pure function, checked against hand-worked trades"
```

### Task 3: the broker books charges and moves cash by net

**Files:**
- Modify: `src/tradebot/types.py` (`Position`)
- Modify: `src/tradebot/execution/backtest.py`
- Test: `tests/test_backtest_broker.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backtest_broker.py`:

```python
def test_close_books_charges_and_moves_cash_by_net():
    from tradebot.config import ChargesConfig
    b = BacktestBroker(100_000.0, 0.0, 5.0, None, charges=ChargesConfig())
    b.place_entry(_order(qty=100))                                   # LONG 100 @ 100, stop 99, target 102
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    ev = b.on_bar(1600, {"X": _c(101.0, 102.5, 100.8, 102.2, ts=1600)})
    pos = ev[0].position
    assert pos.exit_reason == "TARGET" and pos.pnl == pytest.approx(200.0)   # pnl stays gross
    assert pos.charges == pytest.approx(27.42)                       # bought 10,000, sold 10,200
    assert b.cash == pytest.approx(100_000.0 + 200.0 - 27.42)


def test_short_charges_put_the_entry_on_the_sell_side():
    from tradebot.config import ChargesConfig
    b = BacktestBroker(100_000.0, 0.0, 5.0, None, charges=ChargesConfig())
    b.place_entry(_order(direction="SHORT", entry=100.0, stop=101.0, target=98.0, qty=100))
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 99.8)})
    ev = b.on_bar(1600, {"X": _c(99.0, 99.2, 97.5, 97.8, ts=1600)})
    pos = ev[0].position
    assert pos.exit_reason == "TARGET" and pos.exit_price == pytest.approx(98.0)
    assert pos.charges == pytest.approx(26.88)                       # sold 10,000, bought 9,800


def test_without_a_charges_config_the_cost_is_zero_not_none():
    b = _broker()
    b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.2, 98.5, 98.8, ts=1600)})
    assert ev[0].position.exit_reason == "STOP" and ev[0].position.charges == 0.0
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_backtest_broker.py -q`
Expected: `TypeError: __init__() got an unexpected keyword argument 'charges'` and `AttributeError: 'Position' object has no attribute 'charges'`.

- [ ] **Step 3: Implement**

`src/tradebot/types.py`, in `Position`, directly after the `pnl` field:

```python
    charges: float | None = None  # brokerage + statutory, set at close; pnl stays gross, net = pnl - charges
```

`src/tradebot/execution/backtest.py`: import

```python
from typing import Optional

from tradebot.config import ChargesConfig
from tradebot.execution.charges import position_charges
```

Change the constructor signature and body:

```python
    def __init__(self, capital: float, slippage_pct: float, mis_leverage: float,
                 entry_buffer_pct: float | None = None, charges: Optional[ChargesConfig] = None):
```

adding `self.charges = charges` after `self.buffer = ...`, and extend the docstring with
`charges=None means a free broker (tests); the CLI always passes cfg.charges.`

Replace `_close`:

```python
    def _close(self, pos: Position, ts: int, price: float, reason: str) -> None:
        pnl = (price - pos.avg_price) * pos.quantity if pos.direction == "LONG" else (pos.avg_price - price) * pos.quantity
        pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = ts, price, reason, round(pnl, 2)
        pos.charges = position_charges(pos.direction, pos.avg_price, price, pos.quantity, self.charges)
        self.cash += pos.pnl - pos.charges
        if self.cash <= 0:
            log.warning("simulated cash is %.2f after closing %s: account is blown", self.cash, pos.symbol)
        del self._positions[pos.symbol]
        self.closed.append(pos)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `397 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/types.py src/tradebot/execution/backtest.py tests/test_backtest_broker.py
git commit -m "broker: book charges on close, move cash by net; pnl stays gross"
```

### Task 4: schema v4, `positions.charges`

**Files:**
- Modify: `src/tradebot/store/schema.sql`, `src/tradebot/store/db.py`, `src/tradebot/store/repo.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_store.py`, change the two existing assertions `== SCHEMA_VERSION == 3` to `== SCHEMA_VERSION == 4` and rename nothing. Then append:

```python
def test_v3_database_is_migrated_to_v4(tmp_path):
    path = tmp_path / "v3.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL, last_bar_ts INTEGER);
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                                product TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
                                client_id TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
                                stop REAL NOT NULL, target REAL, exit_ids_json TEXT NOT NULL DEFAULT '[]',
                                opened_at INTEGER NOT NULL, closed_at INTEGER, exit_price REAL, exit_reason TEXT,
                                pnl REAL, fill_status TEXT NOT NULL DEFAULT 'full', adopted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO runs VALUES ('r', 'backtest', 0, NULL, '{}', NULL);
        INSERT INTO positions(run_id, symbol, product, direction, strategy, client_id, qty, avg_price, stop,
                              opened_at, closed_at, exit_price, exit_reason, pnl)
               VALUES ('r', 'A', 'MIS', 'LONG', 'ema_rsi', 'c1', 10, 100.0, 99.0, 1, 2, 102.0, 'TARGET', 20.0);
        PRAGMA user_version = 3;
    """)
    raw.commit()
    raw.close()
    conn = connect(path)
    assert "charges" in {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    row = conn.execute("SELECT pnl, charges FROM positions WHERE run_id='r'").fetchone()
    assert row["pnl"] == 20.0 and row["charges"] is None      # old rows keep NULL: the report estimates them
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    conn.close()


def test_close_position_stores_charges(repo):
    repo.create_run("r", "backtest", 0, "{}")
    pid = repo.insert_position("r", Position("A", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c1", "ema_rsi"))
    repo.close_position(pid, 9, 102.0, "TARGET", 20.0, charges=3.21)
    assert repo.list_positions("r")[0]["charges"] == 3.21
    pid2 = repo.insert_position("r", Position("B", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c2", "ema_rsi"))
    repo.close_position(pid2, 9, 102.0, "TARGET", 20.0)     # older callers: charges stays NULL
    assert repo.list_positions("r")[1]["charges"] is None
```

If `Position` is not already imported at the top of `tests/test_store.py`, add `from tradebot.types import Position`.

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_store.py -q`
Expected: failures on `== 4` and `unexpected keyword argument 'charges'`.

- [ ] **Step 3: Implement**

`schema.sql`, in `positions`, after the `pnl` line:

```sql
  charges       REAL,            -- brokerage + statutory at close; NULL on rows written before schema v4
```

`db.py`: `SCHEMA_VERSION = 4` and add to `MIGRATIONS`:

```python
    3: [
        "ALTER TABLE positions ADD COLUMN charges REAL",
    ],
```

`repo.py`, replace `close_position`:

```python
    def close_position(self, position_id: int, closed_ts: int, exit_price: float, exit_reason: str, pnl: float,
                       charges: Optional[float] = None) -> None:
        self.conn.execute(
            "UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl=?, charges=? WHERE id=?",
            (closed_ts, exit_price, exit_reason, pnl, charges, position_id),
        )
        self.conn.commit()
```

(add `from typing import Optional` to `repo.py` if it is not imported).

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `399 passed`.

- [ ] **Step 5: Back up the real database, then let it migrate**

```bash
cp data/tradebot.db data/tradebot.v3.bak.db
.venv/bin/tradebot report --run real-1 | head -8
.venv/bin/python -c "import sqlite3; print(sqlite3.connect('data/tradebot.db').execute('PRAGMA user_version').fetchone())"
```
Expected: the report prints as before and the version is `(4,)`. (`data/` is gitignored; the backup stays local.)

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/store tests/test_store.py
git commit -m "store: schema v4 adds positions.charges with a forward migration"
```

### Task 5: engine, paper resume and CLI run on net

**Files:**
- Modify: `src/tradebot/engine/loop.py` (`_record`)
- Modify: `src/tradebot/engine/paper.py` (`resume`)
- Modify: `src/tradebot/cli.py` (both `BacktestBroker(...)` calls)
- Test: `tests/test_engine.py`, `tests/test_paper.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_engine.py` change the broker line inside `_run` to pass the config's charges:

```python
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
```

and append:

```python
def test_daily_realised_and_cash_are_net_of_charges(repo, tmp_path):
    cfg = make_config(tmp_path, charges={"enabled": True})
    _, broker = _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert len(rows) >= 2 and all(r["charges"] > 0 for r in rows)
    net = sum(r["pnl"] - r["charges"] for r in rows)
    assert sum(d["realised"] for d in repo.daily_pnl("t1")) == pytest.approx(net, abs=0.01)
    assert broker.cash == pytest.approx(cfg.capital + net, abs=0.01)
```

In `tests/test_paper.py` change the broker line inside `_engine` the same way (`charges=cfg.charges`) and append:

```python
def test_resume_restores_cash_and_realised_net_of_charges(repo, tmp_path):
    cfg = make_config(tmp_path, charges={"enabled": True})
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
    last = repo.get_run("split")["last_bar_ts"]
    stored_today = [c for c in repo.load_candles(["A", "B"], 5, OPEN, 2_000_000_000) if c.ts <= last]
    second = _engine(repo, cfg, market, t, run_id="split")
    second.run(warm + stored_today)
    assert _trades(repo, "split") == _trades(repo, "cont") and _trades(repo, "cont")
    a, b = repo.daily_pnl("cont")[0], repo.daily_pnl("split")[0]
    assert a["realised"] == pytest.approx(b["realised"], abs=0.01)
    net = sum(r["pnl"] - r["charges"] for r in repo.list_positions("split"))
    assert b["realised"] == pytest.approx(net, abs=0.01)
    assert second.broker.cash == pytest.approx(cfg.capital + net, abs=0.01)
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_engine.py tests/test_paper.py -q`
Expected: the two new tests fail (`charges` is `None` in the rows; realised equals gross).

- [ ] **Step 3: Implement**

`engine/loop.py`, in `_record`, the `Closed` branch becomes:

```python
            elif isinstance(ev, Closed):
                p = ev.position
                if p.db_id is not None:
                    self.repo.close_position(p.db_id, p.closed_ts, p.exit_price, p.exit_reason, p.pnl, p.charges)
                self._day.realised += (p.pnl or 0.0) - (p.charges or 0.0)  # the daily cap runs on net
                log.info("closed %s %s @ %.2f pnl %.2f charges %.2f", p.symbol, p.exit_reason, p.exit_price or 0.0,
                         p.pnl or 0.0, p.charges or 0.0, extra={"symbol": p.symbol, "client_id": p.client_id})
```

(keep the `if p.exit_reason == "STOP":` cooldown block below it unchanged). Also update the module
docstring's last paragraph: `"PnL for the day" (spec 6.2) is realised today, net of charges, plus ...`.

`engine/paper.py`: add above `class PaperEngine`:

```python
def _net(r) -> float:
    return (r["pnl"] or 0.0) - (r["charges"] or 0.0)
```

and in `resume` replace the two gross sums:

```python
        self.broker.restore(positions, pending, self.cfg.capital + sum(_net(r) for r in closed))
```
```python
        self._day = DayCounters(realised=sum(_net(r) for r in closed if date_of(r["closed_at"]) == today),
```

`cli.py`: in both `backtest` and `paper`, the broker line becomes:

```python
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `401 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine src/tradebot/cli.py tests/test_engine.py tests/test_paper.py
git commit -m "engine: daily cap, daily rows and paper resume run on PnL net of charges"
```

### Task 6: reports show gross, charges and net; old runs are estimated

**Files:**
- Modify: `src/tradebot/report/summary.py`, `src/tradebot/report/compare.py`, `src/tradebot/cli.py`
- Test: `tests/test_report.py`, `tests/test_compare.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_report.py`:

```python
def test_summary_is_net_of_stored_charges(repo):
    repo.create_run("n1", "backtest", 0, "{}")
    for i, (exit_, pnl, ch) in enumerate([(102.0, 20.0, 5.0), (100.2, 2.0, 5.0)]):
        p = Position("S%d" % i, "MIS", "LONG", 10, 100.0, 99.0, None, i, "c%d" % i, "ema_rsi")
        repo.close_position(repo.insert_position("n1", p), 10 + i, exit_, "TARGET", pnl, charges=ch)
    s = build_summary(repo, "n1")
    assert s.gross_pnl == pytest.approx(22.0) and s.charges == pytest.approx(10.0)
    assert s.total_pnl == pytest.approx(12.0) and s.charges_estimated is False
    assert s.wins == 1 and s.losses == 1          # +2 gross is -3 net: a loss
    assert s.avg_r == pytest.approx((15.0 / 10.0 + -3.0 / 10.0) / 2)
    text = format_summary(s)
    assert "Gross PnL" in text and "Charges" in text and "estimated" not in text


def test_old_rows_are_estimated_with_the_given_schedule(repo):
    from tradebot.config import ChargesConfig
    repo.create_run("o1", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 100, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("o1", p), 9, 102.0, "TARGET", 200.0)   # charges NULL
    bare = build_summary(repo, "o1")
    assert bare.charges == 0.0 and bare.total_pnl == pytest.approx(200.0)           # no schedule given: as before
    s = build_summary(repo, "o1", ChargesConfig())
    assert s.charges == pytest.approx(27.42) and s.total_pnl == pytest.approx(172.58)
    assert s.charges_estimated is True and "estimated" in format_summary(s)
```

Append to `tests/test_compare.py` (`_seed_pair` and `P` are defined at the top of that file):

```python
def test_compare_is_net_when_a_schedule_is_given(repo):
    from tradebot.config import ChargesConfig
    _seed_pair(repo)
    gross = build_compare(repo, "A", "B", P)
    net = build_compare(repo, "A", "B", P, ChargesConfig())
    assert gross.a.charges == 0.0
    assert net.a.charges > 0 and net.a.total_pnl == pytest.approx(gross.a.total_pnl - net.a.charges)
    assert net.rejected_pnl_in_a < gross.rejected_pnl_in_a     # the two rejected trades now carry their costs
    assert "Charges" in format_compare(net)
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_report.py tests/test_compare.py -q`
Expected: `AttributeError: 'Summary' object has no attribute 'gross_pnl'`, `TypeError` on the extra argument.

- [ ] **Step 3: Implement `summary.py`**

Imports:

```python
from typing import Optional

from tradebot.config import ChargesConfig
from tradebot.execution.charges import position_charges
```

Extend the module docstring conventions with:
`- PnL figures are net of charges. A row whose charges is NULL predates the charges model: with a schedule passed in, its charges are estimated from the fills and the summary is marked estimated.`

Add to `Summary`, directly above the `days` field (fields with defaults must come after every field without one). `total_pnl` is now net:

```python
    gross_pnl: float = 0.0           # after slippage, before charges
    charges: float = 0.0
    charges_estimated: bool = False  # some rows predate the charges model and were estimated here
```

Add:

```python
def row_charges(r, schedule: Optional[ChargesConfig]) -> tuple:
    """(charges, estimated) for one closed position row."""
    if r["charges"] is not None:
        return float(r["charges"]), False
    if schedule is None or not schedule.enabled or r["exit_price"] is None:
        return 0.0, False
    return position_charges(r["direction"], r["avg_price"], r["exit_price"], r["qty"], schedule), True
```

Change `build_summary`'s signature to `def build_summary(repo: Repo, run_id: str, schedule: Optional[ChargesConfig] = None) -> Summary:` and replace the `pnls`/`rs` block:

```python
    costs = [row_charges(r, schedule) for r in closed]
    gross = [r["pnl"] or 0.0 for r in closed]
    pnls = [g - c for g, (c, _) in zip(gross, costs)]
    rs = []
    for r, p in zip(closed, pnls):
        risk = abs(r["avg_price"] - r["stop"]) * r["qty"]
        if risk > 0:
            rs.append(p / risk)
```

and pass `gross_pnl=sum(gross), charges=sum(c for c, _ in costs), charges_estimated=any(e for _, e in costs),` to `Summary(...)`. `adopted_pnl` stays gross.

In `format_summary` replace the `Total PnL` line with:

```python
        f"Gross PnL             {s.gross_pnl:,.2f}   after slippage, before charges",
        f"Charges               {s.charges:,.2f}" + ("   estimated after the fact; the daily rows below predate charges and are gross"
                                                      if s.charges_estimated else ""),
        f"Total PnL             {s.total_pnl:,.2f}   net",
```

- [ ] **Step 4: Implement `compare.py` and the CLI**

`compare.py`: import `row_charges` and `ChargesConfig`/`Optional`; add `"charges"` to `COMPARABLE_KEYS`; change
`def build_compare(repo, run_a, run_b, prices, schedule: Optional[ChargesConfig] = None)`, build both summaries with
`build_summary(repo, run_x, schedule)`, and in the loop make the rejected PnL net:

```python
            status, pnl = "closed", float(pos["pnl"] or 0.0) - row_charges(pos, schedule)[0]
```

In `_side_by_side` add after the `Total PnL` row:

```python
        ("Charges", f"{a.charges:,.2f}", f"{b.charges:,.2f}"),
```

`cli.py`: `build_summary(repo, rid, cfg.charges)` in `backtest` and `paper`, `build_summary(repo, run_id, cfg.charges)` and
`build_compare(repo, compare[0], compare[1], _prices(cfg), cfg.charges)` in `report`.

- [ ] **Step 5: Run**

Run: `.venv/bin/pytest -q` → `404 passed`. If an existing `test_cli.py` assertion matched the old `Total PnL` line exactly, update it to the new text.

- [ ] **Step 6: Look at a real run**

Run: `.venv/bin/tradebot report --run real-1 | sed -n 1,12p`
Expected: `Gross PnL -84,699.25`, `Charges` about `62,000` marked estimated, `Total PnL` about `-146,800   net`.

- [ ] **Step 7: Commit**

```bash
git add src/tradebot/report src/tradebot/cli.py tests/test_report.py tests/test_compare.py
git commit -m "report: gross, charges and net; rows that predate charges are estimated and marked"
```

---

# Phase 2 — opening-range breakout

### Task 7: `Signal.priority` and ranked same-bar signals

**Files:**
- Modify: `src/tradebot/types.py` (`Signal`)
- Modify: `src/tradebot/engine/loop.py` (`_run_strategies`)
- Modify: `tests/helpers.py` (new `FixedStrategy`)
- Test: `tests/test_types.py`, `tests/test_engine.py`

- [ ] **Step 1: Add the test strategy to `tests/helpers.py`**

Add the imports `from tradebot.strategy.base import Strategy` and
`from tradebot.types import Candle, Signal, round_tick_down, round_tick_up` (extend the existing `types` import), then append:

```python
class FixedStrategy(Strategy):
    """Fires exactly the signals it is told to and records every symbol it was shown.
    `fires` maps (symbol, bar_ts) to (direction, priority)."""
    name = "fixed"
    product = "MIS"

    def __init__(self, fires: dict):
        self.fires = dict(fires)
        self.seen: set = set()

    def on_candle(self, candle: Candle):
        self.seen.add(candle.symbol)
        hit = self.fires.get((candle.symbol, candle.ts))
        if hit is None:
            return None
        direction, priority = hit
        stop = round_tick_down(candle.close * 0.99) if direction == "LONG" else round_tick_up(candle.close * 1.01)
        return Signal(self.name, candle.symbol, direction, candle.close, stop, None, "MIS", candle.ts, priority=priority)

    def is_ready(self, symbol: str) -> bool:
        return True

    def snapshot(self, symbol: str) -> dict:
        return {}

    def reset(self, symbol: str) -> None:
        pass
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_types.py` (import `Signal` if the file does not already):

```python
def test_signal_priority_defaults_to_zero_and_is_the_last_field():
    s = Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", 1000)
    assert s.priority == 0.0
    assert Signal("orb", "A", "LONG", 100.0, 99.0, None, "MIS", 1000, priority=2.5).priority == 2.5
```

Append to `tests/test_engine.py` (extend the helpers import to `from tests.helpers import FixedStrategy, make_config, synth_candles`):

```python
MON = date(2026, 9, 14)


def _run_fixed(repo, cfg, candles, fires, symbols, run_id="t1"):
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, symbols, 5, 0, 2_000_000_000)
    strat = FixedStrategy(fires)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    BacktestEngine(cfg, repo, src, [strat], broker, StubFilter(), SessionClock(cfg.session, 5),
                   {s: 1 for s in symbols}, run_id).run()
    return strat


def _decisions(repo, run_id="t1"):
    rows = repo.conn.execute(
        "SELECT s.symbol, s.direction, d.approved, d.reason FROM signals s JOIN risk_decisions d ON d.signal_id = s.id "
        "WHERE s.run_id=? ORDER BY s.id", (run_id,)).fetchall()
    return [(r["symbol"], r["direction"], r["approved"], r["reason"]) for r in rows]


def _two_symbols():
    return synth_candles("A", [MON]) + synth_candles("B", [MON], phase=4.0, seed=99)


def test_same_bar_signals_are_ranked_by_priority(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"max_open_positions": 1})
    ts = ist_epoch(MON, "10:00")
    _run_fixed(repo, cfg, _two_symbols(), {("A", ts): ("LONG", 1.0), ("B", ts): ("LONG", 2.5)}, ["A", "B"])
    assert _decisions(repo) == [("B", "LONG", 1, "ok"), ("A", "LONG", 0, "max_open_positions")]


def test_equal_priority_falls_back_to_symbol_order(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"max_open_positions": 1})
    ts = ist_epoch(MON, "10:00")
    _run_fixed(repo, cfg, _two_symbols(), {("B", ts): ("LONG", 0.0), ("A", ts): ("LONG", 0.0)}, ["A", "B"])
    assert _decisions(repo) == [("A", "LONG", 1, "ok"), ("B", "LONG", 0, "max_open_positions")]
```

- [ ] **Step 3: Run to see them fail**

Run: `.venv/bin/pytest tests/test_types.py tests/test_engine.py -q`
Expected: `TypeError: __init__() got an unexpected keyword argument 'priority'`.

- [ ] **Step 4: Implement**

`types.py`, last field of `Signal`:

```python
    priority: float = 0.0  # ranks same-bar signals when slots are short; higher first, then symbol
```

`engine/loop.py`, at the end of `_run_strategies`, replace `return out` with:

```python
        # Deterministic in every mode: backtest bars arrive in SQL order and paper bars in universe
        # order, so without this the two modes could hand the last free slot to different symbols.
        out.sort(key=lambda pair: (-pair[1].priority, pair[1].symbol))
        return out
```

Update the module docstring's step 6 to read `6. rank -> risk -> AI -> place`.

- [ ] **Step 5: Run**

Run: `.venv/bin/pytest -q` → `407 passed`. The golden-trades test must still pass unchanged (its symbols were already in alphabetical order).

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/types.py src/tradebot/engine/loop.py tests/helpers.py tests/test_types.py tests/test_engine.py
git commit -m "engine: rank same-bar signals by Signal.priority, then symbol, in every mode"
```

### Task 8: the `orb` strategy

**Files:**
- Create: `src/tradebot/strategy/orb.py`
- Modify: `src/tradebot/strategy/ema_rsi.py` (`build_strategy`)
- Test: `tests/test_orb.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orb.py`:

```python
from datetime import date

import pytest

from tradebot.engine.clock import ist_epoch
from tradebot.strategy.ema_rsi import build_strategy
from tradebot.strategy.orb import OrbStrategy
from tradebot.types import Candle

PARAMS = dict(range_minutes=30, reward_risk=2.0, min_range_pct=0.4, max_range_pct=1.5, product="MIS",
              session_open="09:15", interval_minutes=15)
D1, D2 = date(2026, 9, 14), date(2026, 9, 15)
# Two range bars: high 100.8, low 99.8, width 1.0 on a 100.3 midpoint = 0.997%; mean volume 2000.
RANGE = [(100.0, 100.6, 99.8, 100.4, 1000), (100.4, 100.8, 100.0, 100.2, 3000)]
INSIDE = (100.2, 100.7, 100.1, 100.5, 500)
UP = (100.5, 101.3, 100.4, 101.2, 5000)      # closes above 100.8
DOWN = (100.0, 100.1, 99.2, 99.4, 4000)      # closes below 99.8


def bars(rows, day=D1, start="09:15", symbol="X"):
    t0 = ist_epoch(day, start)
    return [Candle(symbol, t0 + i * 900, o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]


def run(candles, params=None):
    s = OrbStrategy(params or PARAMS)
    return s, [s.on_candle(c) for c in candles]


def test_long_breakout_fires_on_the_first_close_above_the_range():
    s, out = run(bars(RANGE + [INSIDE, UP]))
    assert out[:3] == [None, None, None]
    sig = out[3]
    assert (sig.strategy, sig.symbol, sig.direction, sig.product) == ("orb", "X", "LONG", "MIS")
    assert sig.entry_price == pytest.approx(101.2) and sig.stop_price == pytest.approx(99.8)
    assert sig.target_price == pytest.approx(104.0)          # 101.2 + 2 x (101.2 - 99.8)
    assert sig.priority == pytest.approx(2.5)                # 5000 / mean(1000, 3000)
    assert sig.bar_ts == ist_epoch(D1, "10:00")


def test_short_breakout_mirrors():
    _, out = run(bars(RANGE + [DOWN]))
    sig = out[2]
    assert sig.direction == "SHORT" and sig.entry_price == pytest.approx(99.4)
    assert sig.stop_price == pytest.approx(100.8) and sig.target_price == pytest.approx(96.6)
    assert sig.priority == pytest.approx(2.0)


def test_one_signal_per_symbol_per_day_and_a_new_day_starts_over():
    higher = (101.2, 102.0, 101.0, 101.9, 9000)
    s, out = run(bars(RANGE + [UP, higher, DOWN]) + bars(RANGE + [UP], day=D2))
    assert [o is not None for o in out] == [False, False, True, False, False, False, False, True]
    assert out[-1].bar_ts == ist_epoch(D2, "09:45")


def test_symbols_keep_separate_ranges():
    s = OrbStrategy(PARAMS)
    out = [s.on_candle(c) for pair in zip(bars(RANGE + [UP], symbol="X"), bars(RANGE + [INSIDE], symbol="Y")) for c in pair]
    assert [o.symbol for o in out if o] == ["X"]


def test_a_range_that_is_too_narrow_or_too_wide_trades_nothing_that_day():
    narrow = [(100.0, 100.2, 100.0, 100.1, 1000), (100.1, 100.3, 100.05, 100.2, 1000)]   # 0.30%
    wide = [(100.0, 102.0, 100.0, 101.5, 1000), (101.5, 101.8, 100.5, 101.0, 1000)]      # 1.98%
    breakout = (101.0, 103.5, 100.9, 103.0, 9000)
    for rng in (narrow, wide):
        s, out = run(bars(rng + [breakout, breakout]))
        assert out == [None] * 4
        assert s.is_ready("X") and s.snapshot("X")["range_width_pct"] > 0


def test_a_short_range_is_skipped():
    # The 09:15 bar is missing: one range bar where two are needed.
    _, out = run(bars([RANGE[1], UP], start="09:30"))
    assert out == [None, None]


def test_bars_before_the_open_are_ignored():
    pre = Candle("X", ist_epoch(D1, "09:00"), 90.0, 120.0, 80.0, 100.0, 1)
    s = OrbStrategy(PARAMS)
    assert s.on_candle(pre) is None
    out = [s.on_candle(c) for c in bars(RANGE + [UP])]
    assert out[2] is not None and out[2].stop_price == pytest.approx(99.8)    # 80.0 never entered the range


def test_no_target_when_reward_risk_is_null():
    _, out = run(bars(RANGE + [UP]), dict(PARAMS, reward_risk=None))
    assert out[2].target_price is None and out[2].stop_price == pytest.approx(99.8)


def test_zero_volume_range_gives_priority_zero():
    rng = [(o, h, l, c, 0) for o, h, l, c, _ in RANGE]
    _, out = run(bars(rng + [UP]))
    assert out[2].priority == 0.0


def test_ready_and_snapshot_follow_the_range():
    s = OrbStrategy(PARAMS)
    cs = bars(RANGE + [UP])
    s.on_candle(cs[0]); s.on_candle(cs[1])
    assert not s.is_ready("X") and s.snapshot("X") == {}
    s.on_candle(cs[2])
    snap = s.snapshot("X")
    assert s.is_ready("X")
    assert snap["range_high"] == pytest.approx(100.8) and snap["range_low"] == pytest.approx(99.8)
    assert snap["range_width_pct"] == pytest.approx(1.0 / 100.3 * 100) and snap["rel_volume"] == pytest.approx(2.5)
    s.reset("X")
    assert not s.is_ready("X")


def test_sixty_minute_range_needs_four_bars():
    four = RANGE + [(100.2, 100.7, 100.1, 100.5, 2000), (100.5, 100.75, 100.2, 100.6, 2000)]
    _, out = run(bars(four + [UP]), dict(PARAMS, range_minutes=60))
    assert [o is not None for o in out] == [False] * 4 + [True]


def test_params_are_validated():
    for bad, match in [(dict(range_minutes=20), "multiple"), (dict(range_minutes=0), "range_minutes"),
                       (dict(min_range_pct=2.0), "min_range_pct"), (dict(reward_risk=0), "reward_risk"),
                       (dict(product="CNC"), "MIS"), (dict(session_open="9:15"), "HH:MM")]:
        with pytest.raises(ValueError, match=match):
            OrbStrategy(dict(PARAMS, **bad))


def test_build_strategy_knows_orb():
    assert isinstance(build_strategy("orb", PARAMS), OrbStrategy)
```

- [ ] **Step 2: Run to see it fail**

Run: `.venv/bin/pytest tests/test_orb.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.strategy.orb'`.

- [ ] **Step 3: Implement**

Create `src/tradebot/strategy/orb.py`:

```python
"""Opening-range breakout (spec docs/superpowers/specs/2026-09-19-charges-orb-regime-design.md).

The range is the high and low of the bars that start before session_open + range_minutes. After it,
the first bar that closes beyond the range fires: LONG above with the stop at the range low, SHORT
below with the stop at the range high, target at reward_risk times the stop distance (none when
reward_risk is null: the trade then ends at its stop or the session square-off). One signal per symbol
per day, whether or not it is traded. A range narrower than min_range_pct of its midpoint (costs
dominate), wider than max_range_pct (the size becomes tiny) or built from fewer bars than expected
(missing data) trades nothing that day. priority is the breakout bar's volume relative to the mean
range-bar volume, so the engine gives scarce slots to the breakouts with the most participation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from tradebot.engine.clock import date_of, ist_epoch
from tradebot.strategy.base import Strategy
from tradebot.types import Candle, Signal, round_tick_down, round_tick_up


@dataclass
class _Day:
    day: Optional[date] = None
    open_ts: int = 0
    range_end: int = 0
    high: Optional[float] = None
    low: Optional[float] = None
    bars: int = 0
    volume: float = 0.0
    complete: bool = False   # the first bar after the range has been seen
    done: bool = False       # nothing more fires today
    rel_volume: float = 0.0  # of the latest post-range bar


def _int(params: dict, key: str) -> int:
    v = params[key]
    if isinstance(v, bool) or int(v) != v:
        raise ValueError(f"orb.{key} must be an integer, got {v!r}")
    return int(v)


class OrbStrategy(Strategy):
    name = "orb"

    def __init__(self, params: dict):
        self.range_minutes = _int(params, "range_minutes")
        self.interval = _int(params, "interval_minutes")
        self.session_open = str(params["session_open"])
        rr = params.get("reward_risk")
        self.rr = None if rr is None else float(rr)
        self.min_range_pct = float(params["min_range_pct"])
        self.max_range_pct = float(params["max_range_pct"])
        self.product = params.get("product", "MIS")
        if self.interval < 1 or self.range_minutes < 1:
            raise ValueError("orb.range_minutes and orb.interval_minutes must be >= 1")
        if self.range_minutes % self.interval:
            raise ValueError("orb.range_minutes must be a multiple of orb.interval_minutes")
        if not 0 < self.min_range_pct < self.max_range_pct:
            raise ValueError("orb.min_range_pct must be > 0 and below orb.max_range_pct")
        if self.rr is not None and self.rr <= 0:
            raise ValueError("orb.reward_risk must be > 0 or null")
        if self.product != "MIS":
            raise ValueError("orb is an intraday strategy: orb.product must be MIS")
        ist_epoch(date(2000, 1, 3), self.session_open)  # raises ValueError unless it is HH:MM
        self.need = self.range_minutes // self.interval
        self._state: dict[str, _Day] = {}

    # -- Strategy interface ----------------------------------------------------
    def reset(self, symbol: str) -> None:
        self._state[symbol] = _Day()

    def is_ready(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and st.complete)

    def snapshot(self, symbol: str) -> dict[str, float]:
        st = self._state.get(symbol)
        if not st or not st.complete or st.high is None or st.low is None:
            return {}
        return {"range_high": st.high, "range_low": st.low, "range_width_pct": self._width_pct(st),
                "rel_volume": st.rel_volume}

    def on_candle(self, candle: Candle) -> Signal | None:
        st = self._state.setdefault(candle.symbol, _Day())
        day = date_of(candle.ts)
        if day != st.day:
            open_ts = ist_epoch(day, self.session_open)
            st = self._state[candle.symbol] = _Day(day=day, open_ts=open_ts,
                                                   range_end=open_ts + self.range_minutes * 60)
        if candle.ts < st.open_ts:
            return None
        if candle.ts < st.range_end:
            st.high = candle.high if st.high is None else max(st.high, candle.high)
            st.low = candle.low if st.low is None else min(st.low, candle.low)
            st.bars += 1
            st.volume += candle.volume
            return None
        if not st.complete:
            st.complete = True
            if st.bars < self.need or st.high is None or st.low is None:
                st.done = True
            elif not self.min_range_pct <= self._width_pct(st) <= self.max_range_pct:
                st.done = True
        mean_volume = st.volume / st.bars if st.bars else 0.0
        st.rel_volume = candle.volume / mean_volume if mean_volume > 0 else 0.0
        if st.done:
            return None
        close = candle.close
        if close > st.high:
            stop = round_tick_down(st.low)
            target = None if self.rr is None else round_tick_down(close + (close - stop) * self.rr)
            ok = stop < close and (target is None or close < target)
            direction = "LONG"
        elif close < st.low:
            stop = round_tick_up(st.high)
            target = None if self.rr is None else round_tick_up(close - (stop - close) * self.rr)
            ok = close < stop and (target is None or target < close)
            direction = "SHORT"
        else:
            return None
        st.done = True  # the first breakout is the only one, traded or not
        if not ok:
            return None
        return Signal(self.name, candle.symbol, direction, close, stop, target, self.product, candle.ts,
                      priority=st.rel_volume)

    @staticmethod
    def _width_pct(st: _Day) -> float:
        mid = (st.high + st.low) / 2.0
        return (st.high - st.low) / mid * 100.0 if mid > 0 else 0.0
```

In `src/tradebot/strategy/ema_rsi.py`, `build_strategy`, before the final `raise`:

```python
    if name == "orb":
        from tradebot.strategy.orb import OrbStrategy  # local import, as for confluence
        return OrbStrategy(params)
```

`strategy_params` attaches an `indicators` key to every strategy's params; `OrbStrategy` ignores it, which is what the other strategies do with keys they do not use.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests/test_orb.py -q` → `13 passed`. Then `.venv/bin/pytest -q` → `420 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/strategy/orb.py src/tradebot/strategy/ema_rsi.py tests/test_orb.py
git commit -m "strategy(orb): opening-range breakout, one signal per symbol per day, volume-ranked"
```

### Task 9: `config-orb.yaml` and the config cross-check

**Files:**
- Modify: `src/tradebot/config.py` (`_validate`)
- Create: `config-orb.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
ORB = {"range_minutes": 30, "reward_risk": 2.0, "min_range_pct": 0.4, "max_range_pct": 1.5, "product": "MIS",
       "session_open": "09:15", "interval_minutes": 15}


def test_config_orb_loads_and_builds_the_strategy(tmp_path):
    from tradebot.strategy.ema_rsi import build_strategy, strategy_params
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config-orb.yaml", tmp_path / "nonexistent.env")
    assert cfg.execution.interval_minutes == 15 and cfg.strategy["orb"] == ORB
    assert (cfg.risk.max_entries_per_day, cfg.risk.max_open_positions) == (2, 2)
    assert cfg.session.no_new_entries_after == "13:00" and cfg.charges == ChargesConfig()
    assert cfg.paths.db == load_config(root / "config.yaml", tmp_path / "nonexistent.env").paths.db
    SessionClock(cfg.session, cfg.execution.interval_minutes)     # the 13:00 cutoff fits the session
    assert build_strategy("orb", strategy_params(cfg.strategy, "orb")).name == "orb"


def test_orb_keys_must_match_the_session_and_the_interval(tmp_path):
    with pytest.raises(ValueError, match="orb.interval_minutes"):
        make_config(tmp_path, strategy={"orb": ORB})                       # BASE_CONFIG runs 5-minute bars
    with pytest.raises(ValueError, match="orb.session_open"):
        make_config(tmp_path, strategy={"orb": dict(ORB, interval_minutes=5, session_open="09:30")})
    make_config(tmp_path, strategy={"orb": dict(ORB, interval_minutes=5, range_minutes=30)})
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: `config file not found: .../config-orb.yaml`, and the second test does not raise.

- [ ] **Step 3: Implement**

`config.py`, in `_validate` before the final `for ok, msg in checks:` loop:

```python
    orb = cfg.strategy.get("orb")
    if isinstance(orb, dict):  # the strategy counts range bars itself, so its copy of these must not drift
        checks.append((orb.get("interval_minutes") == e.interval_minutes,
                       "strategy.orb.interval_minutes must equal execution.interval_minutes"))
        checks.append((orb.get("session_open") == s.open, "strategy.orb.session_open must equal session.open"))
```

Create the variant:

```bash
cp config-15m.yaml config-orb.yaml
.venv/bin/python - <<'EOF'
import re
p = "config-orb.yaml"
s = open(p).read()
s = s.replace(s.splitlines()[0],
              "# Opening-range breakout variant: 15-minute bars, at most two entries a day, no entries after 13:00.")
s = re.sub(r"(\n  max_entries_per_day:) 20", r"\1 2 ", s)
s = re.sub(r"(\n  max_open_positions:) 5", r"\1 2", s)
s = s.replace('no_new_entries_after: "14:45"', 'no_new_entries_after: "13:00"')
orb = '''  orb:                        # opening-range breakout; see the 2026-09-19 charges-orb-regime spec
    range_minutes: 30         # 30 or 60; must be a multiple of the bar interval
    reward_risk: 2.0          # null = exit by stop or square-off only
    min_range_pct: 0.4        # narrower than this and costs dominate
    max_range_pct: 1.5        # wider than this and the size becomes tiny
    session_open: "09:15"     # must equal session.open
    interval_minutes: 15      # must equal execution.interval_minutes
    product: MIS
'''
s = s.replace("  indicators:", orb + "  indicators:", 1)
open(p, "w").write(s)
EOF
git diff --no-index config-15m.yaml config-orb.yaml
```

Expected diff: the header line, the two risk caps, the `orb:` block before `indicators:`, and the cutoff. If a `re.sub` did not match (the diff shows no change on that line), edit that line by hand to the value above.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `422 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py config-orb.yaml tests/test_config.py
git commit -m "config: opening-range breakout variant; orb's session and interval must match the config"
```

### Task 10: tuning runs and the holdout

**Files:**
- Create: `scripts/orb_experiment.py`

No unit tests: this is a thin driver over tested parts, like `scripts/sweep_confluence.py`. It writes deliberate, named runs into the project database.

- [ ] **Step 1: Write the script**

Create `scripts/orb_experiment.py`:

```python
"""ORB experiments against the project database (spec 2026-09-19-charges-orb-regime-design.md).

    .venv/bin/python scripts/orb_experiment.py tune
    .venv/bin/python scripts/orb_experiment.py holdout --range 30 --rr 2.0     # --rr none for no target

Tuning runs see 2026-06-17..2026-08-15 only. The holdout (2026-08-16..2026-09-15) has a fixed run id,
so it can be run once: choose the parameters from the tuning table first. The AI filter is the stub."""
import argparse
import logging
import sys
from dataclasses import replace as dc_replace
from datetime import date

from tradebot.ai.filter import build_filter
from tradebot.config import load_config
from tradebot.data.historical import HistoricalSource
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.report.summary import build_summary
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy, strategy_params

TUNE = (date(2026, 6, 17), date(2026, 8, 15))
HOLDOUT = (date(2026, 8, 16), date(2026, 9, 15))
GRID = [(r, rr) for r in (30, 60) for rr in (1.5, 2.0, None)]

logging.basicConfig(level=logging.WARNING)


def label(range_minutes, rr) -> str:
    return f"{range_minutes}-{'none' if rr is None else rr}"


def run(cfg, strategy_name: str, run_id: str, window) -> object:
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is not None:
        sys.exit(f"run '{run_id}' already exists: this experiment has been run; report it with "
                 f"`tradebot report --run {run_id}`")
    symbols = list(load_universe(cfg.paths.universe).symbols)
    interval = cfg.execution.interval_minutes
    source = HistoricalSource.from_repo(repo, symbols, interval,
                                        ist_epoch(window[0], "00:00"), ist_epoch(window[1], "23:59"))
    if not source.bar_timestamps():
        sys.exit(f"no {interval}-minute candles in {window[0]}..{window[1]}; run fetch-data with this config")
    clock = SessionClock(cfg.session, interval)
    ai_cfg = dc_replace(cfg.ai, filter="stub")
    cfg = dc_replace(cfg, ai=ai_cfg)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    engine = BacktestEngine(cfg, repo, source, [build_strategy(strategy_name, strategy_params(cfg.strategy, strategy_name))],
                            broker, build_filter(ai_cfg, "", repo=repo, clock=clock), clock,
                            {s: 1 for s in symbols}, run_id)
    engine.run()
    return build_summary(repo, run_id, cfg.charges)


def with_orb(cfg, range_minutes, rr):
    strat = dict(cfg.strategy)
    strat["orb"] = dict(strat["orb"], range_minutes=range_minutes, reward_risk=rr)
    return dc_replace(cfg, strategy=strat)


def row(s) -> str:
    return (f"{s.run_id:<22} {s.trades:>6} {s.win_rate * 100:>6.1f}% {s.gross_pnl:>12,.0f} {s.charges:>10,.0f} "
            f"{s.total_pnl:>12,.0f} {s.r_on_risk:>10.3f}")


# "R on risk" is net PnL over rupees at risk, all trades pooled. It is the decision metric: unlike the
# unweighted per-trade Avg R, scrap-sized trades cannot dominate it, and its sign is the sign of net PnL.
HEADER = f"{'run':<22} {'trades':>6} {'win':>7} {'gross':>12} {'charges':>10} {'net':>12} {'R on risk':>10}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["tune", "holdout"])
    ap.add_argument("--config", default="config-orb.yaml")
    ap.add_argument("--range", dest="range_minutes", type=int, choices=[30, 60])
    ap.add_argument("--rr", help="1.5, 2.0 or none")
    a = ap.parse_args()
    cfg = load_config(a.config)
    print(HEADER)
    if a.phase == "tune":
        for range_minutes, rr in GRID:
            print(row(run(with_orb(cfg, range_minutes, rr), "orb", f"orb-t-{label(range_minutes, rr)}", TUNE)), flush=True)
        return
    if a.range_minutes is None or a.rr is None:
        sys.exit("holdout needs --range and --rr, chosen from the tuning table")
    rr = None if a.rr.lower() == "none" else float(a.rr)
    s = run(with_orb(cfg, a.range_minutes, rr), "orb", "orb-holdout", HOLDOUT)
    print(row(s))
    verdict = ("inconclusive: fewer than 30 holdout trades" if s.trades < 30
               else "holds up out of sample" if s.r_on_risk > 0 else "no edge out of sample")
    print(f"holdout verdict: {verdict}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the six tuning runs**

Run: `.venv/bin/python scripts/orb_experiment.py tune`
Expected: a table of six rows `orb-t-30-1.5` … `orb-t-60-none`, each with well under 100 trades (two entries a day over about 42 sessions caps it at about 84). Save the output for the notes file.

Sanity checks before trusting it — run `.venv/bin/tradebot --config config-orb.yaml report --run orb-t-30-2.0 | sed -n 1,16p` and confirm: no day has more than 2 entries in the daily table; `Risk rejects` shows `max_entries_per_day` and `entries_closed`; `Charges` is not marked estimated.

- [ ] **Step 3: Choose, then run the holdout once**

Pick the row with the highest `R on risk` (net PnL divided by rupees at risk, all trades pooled; `Summary.r_on_risk`). If every row is negative, still run the holdout on the least bad one: the result to record is then "no edge", confirmed out of sample.

Run: `.venv/bin/python scripts/orb_experiment.py holdout --range <30|60> --rr <1.5|2.0|none>`
Expected: one row `orb-holdout` and a verdict line. Do not re-run with other parameters; a second look makes it a tuning run.

- [ ] **Step 4: Commit**

```bash
git add scripts/orb_experiment.py
git commit -m "scripts: ORB tuning grid on the tuning window and a run-once holdout"
```

---

# Phase 3 — NIFTY regime filter

### Task 11: `data.index_symbol` and the `regime` section

**Files:**
- Modify: `src/tradebot/config.py`
- Modify: `config.yaml`, `config-15m.yaml`, `config-orb.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (add `RegimeConfig` to the `tradebot.config` import):

```python
def test_regime_defaults_off_and_is_validated(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.regime == RegimeConfig() and cfg.regime.enabled is False
    assert (cfg.regime.source, cfg.regime.ema_period, cfg.data.index_symbol) == ("index", 20, "")
    with pytest.raises(ValueError, match="regime.source"):
        make_config(tmp_path, regime={"source": "vix"})
    with pytest.raises(ValueError, match="regime.ema_period"):
        make_config(tmp_path, regime={"ema_period": 0})
    with pytest.raises(ValueError, match="data.index_symbol"):
        make_config(tmp_path, regime={"enabled": True})                  # the index source needs a symbol
    assert make_config(tmp_path, regime={"enabled": True, "source": "composite"}).regime.enabled
    assert make_config(tmp_path, regime={"enabled": True}, data={"index_symbol": "NIFTY"}).data.index_symbol == "NIFTY"


def test_shipped_configs_name_the_index_and_keep_the_filter_off(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in ("config.yaml", "config-15m.yaml", "config-orb.yaml"):
        cfg = load_config(root / name, tmp_path / "nonexistent.env")
        assert cfg.data.index_symbol == "NIFTY", name
        assert cfg.regime == RegimeConfig(), name
        assert "regime" in cfg.raw, name
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_config.py -q` → `ImportError: cannot import name 'RegimeConfig'`.

- [ ] **Step 3: Implement**

`config.py`: add to `DataConfig`:

```python
    index_symbol: str = ""      # index fetched and stored with the universe for the regime filter; never traded
```

add after `ChargesConfig`:

```python
REGIME_SOURCES = ("index", "composite")


@dataclass(frozen=True)
class RegimeConfig:
    """Market-direction gate: longs only while the index is above its EMA, shorts only while at or below."""
    enabled: bool = False
    source: str = "index"    # index: data.index_symbol candles | composite: equal-weight mean of universe returns
    ema_period: int = 20     # bars of the run's interval
```

add `regime: RegimeConfig = RegimeConfig()` to `Config` after `charges`, pass
`regime=_optional_section(raw, "regime", RegimeConfig),` in `load_config`, and add to `_validate`:

```python
    g = cfg.regime
    checks += [
        (g.source in REGIME_SOURCES, f"regime.source must be one of {REGIME_SOURCES}"),
        (g.ema_period >= 1, "regime.ema_period must be >= 1"),
        (not (g.enabled and g.source == "index") or bool(d.index_symbol),
         "data.index_symbol must be set when regime.enabled uses source: index"),
    ]
```

In all three shipped configs add under `data:`:

```yaml
  index_symbol: NIFTY           # fetched and stored with the universe; feeds the regime filter, never traded
```

and after the `charges:` block:

```yaml
regime:                         # longs only while the index is above its EMA, shorts only while at or below
  enabled: false
  source: index                 # index | composite (equal-weight mean of universe returns; needs no index data)
  ema_period: 20                # bars of execution.interval_minutes
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `424 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py config.yaml config-15m.yaml config-orb.yaml tests/test_config.py
git commit -m "config: data.index_symbol and an off-by-default regime section"
```

### Task 12: `RegimeFilter`

**Files:**
- Create: `src/tradebot/risk/regime.py`
- Test: `tests/test_regime.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_regime.py`:

```python
import pytest

from tradebot.risk.regime import DOWN, NOT_READY, UP, RegimeFilter


def test_not_ready_until_the_ema_is_warm_and_everything_is_held_back():
    f = RegimeFilter(3)
    assert f.state == NOT_READY
    assert f.update(10.0) == NOT_READY and f.update(11.0) == NOT_READY
    assert f.rejection("LONG") == "regime_not_ready" and f.rejection("SHORT") == "regime_not_ready"


def test_up_above_the_ema_and_down_at_or_below_it():
    f = RegimeFilter(3)
    for x in (10.0, 11.0):
        f.update(x)
    assert f.update(12.0) == UP          # EMA seeds at the mean 11.0; 12 is above
    assert f.rejection("LONG") is None and f.rejection("SHORT") == "regime"
    assert f.update(9.0) == DOWN         # EMA moves to 10.0; 9 is below
    assert f.rejection("SHORT") is None and f.rejection("LONG") == "regime"


def test_a_close_equal_to_the_ema_counts_as_down():
    f = RegimeFilter(3)
    for x in (10.0, 10.0, 10.0):
        f.update(x)
    assert f.state == DOWN


def test_bad_input_is_loud():
    with pytest.raises(ValueError):
        RegimeFilter(0)
    with pytest.raises(ValueError):
        RegimeFilter(3).update(float("nan"))
```

- [ ] **Step 2: Run to see it fail**

Run: `.venv/bin/pytest tests/test_regime.py -q` → `ModuleNotFoundError: No module named 'tradebot.risk.regime'`.

- [ ] **Step 3: Implement**

Create `src/tradebot/risk/regime.py`:

```python
"""Market-direction gate: the index against its own EMA. The engine feeds it one index close per
bar and asks it, per signal, whether the direction fits the market."""
from __future__ import annotations

from typing import Optional

from tradebot.strategy.indicators import EMA

UP, DOWN, NOT_READY = "UP", "DOWN", "NOT_READY"


class RegimeFilter:
    def __init__(self, ema_period: int):
        self._ema = EMA(ema_period)
        self.state = NOT_READY

    def update(self, close: float) -> str:
        ema = self._ema.update(close)
        if ema is not None:
            self.state = UP if close > ema else DOWN
        return self.state

    def rejection(self, direction: str) -> Optional[str]:
        """The risk-decision reason that blocks `direction` right now, or None when it may trade."""
        if self.state == NOT_READY:
            return "regime_not_ready"
        if (direction == "LONG") != (self.state == UP):
            return "regime"
        return None
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `428 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/risk/regime.py tests/test_regime.py
git commit -m "risk: regime filter, the index against its EMA"
```

### Task 13: the engine splits off the index and gates entries

**Files:**
- Modify: `src/tradebot/engine/loop.py`
- Modify: `src/tradebot/engine/paper.py` (`warm`)
- Test: `tests/test_engine.py`, `tests/test_paper.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine.py`:

```python
REGIME = dict(regime={"enabled": True, "ema_period": 3}, data={"index_symbol": "NIFTY"})


def _index(day, closes):
    t0 = ist_epoch(day, "09:15")
    return [Candle("NIFTY", t0 + i * 300, c, c, c, c, 0) for i, c in enumerate(closes)]   # an index has no volume


def test_regime_blocks_longs_in_a_falling_index_and_lets_shorts_through(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    t_long, t_short = ist_epoch(MON, "10:00"), ist_epoch(MON, "10:30")
    strat = _run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 - 10 * i for i in range(75)]),
                       {("A", t_long): ("LONG", 0.0), ("A", t_short): ("SHORT", 0.0)}, ["A", "NIFTY"])
    assert _decisions(repo) == [("A", "LONG", 0, "regime"), ("A", "SHORT", 1, "ok")]
    assert strat.seen == {"A"}                     # the index never reaches a strategy
    assert repo.rejection_counts("t1") == {"regime": 1}
    assert [r["symbol"] for r in repo.list_positions("t1")] == ["A"]


def test_regime_blocks_shorts_in_a_rising_index(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    t_short, t_long = ist_epoch(MON, "10:00"), ist_epoch(MON, "10:30")
    _run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 + 10 * i for i in range(75)]),
               {("A", t_short): ("SHORT", 0.0), ("A", t_long): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert _decisions(repo) == [("A", "SHORT", 0, "regime"), ("A", "LONG", 1, "ok")]


def test_regime_holds_everything_back_until_the_ema_is_warm(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    _run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 + 10 * i for i in range(75)]),
               {("A", ist_epoch(MON, "09:20")): ("LONG", 0.0)}, ["A", "NIFTY"])    # second bar; EMA3 needs three
    assert _decisions(repo) == [("A", "LONG", 0, "regime_not_ready")]


def test_a_missing_index_bar_keeps_the_last_state(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    ts = ist_epoch(MON, "10:00")
    index = [c for c in _index(MON, [25000.0 - 10 * i for i in range(75)]) if c.ts != ts]
    _run_fixed(repo, cfg, synth_candles("A", [MON]) + index, {("A", ts): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert _decisions(repo) == [("A", "LONG", 0, "regime")]


def test_index_candles_never_reach_strategies_with_the_filter_off(repo, tmp_path):
    cfg = make_config(tmp_path, data={"index_symbol": "NIFTY"})
    strat = _run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 - 10 * i for i in range(75)]),
                       {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert _decisions(repo) == [("A", "LONG", 1, "ok")] and strat.seen == {"A"}
```

Append to `tests/test_paper.py` (add `from tradebot.types import Candle` to its imports):

```python
def test_warm_up_feeds_the_regime_filter_and_hides_the_index(repo, tmp_path):
    cfg = make_config(tmp_path, regime={"enabled": True, "ema_period": 3}, data={"index_symbol": "NIFTY"})
    market = _market()
    eng = _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")))
    index = [Candle("NIFTY", c.ts, 25000.0 + i, 25000.0 + i, 25000.0 + i, 25000.0 + i, 0)
             for i, c in enumerate(market["A"]) if c.ts < OPEN]
    eng.warm(_warm(market) + index)
    assert eng._regime.state == "UP"
    assert "NIFTY" not in eng._last_close and "NIFTY" not in eng._history
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_engine.py tests/test_paper.py -q`
Expected: the six new tests fail (`strat.seen` contains `NIFTY`, no `regime` reasons, no `_regime` attribute).

- [ ] **Step 3: Implement the engine**

`engine/loop.py`: import `from tradebot.risk.regime import RegimeFilter`. In `Engine.__init__`, after `self._day = DayCounters()`:

```python
        self._index_symbol = cfg.data.index_symbol
        self._regime = RegimeFilter(cfg.regime.ema_period) if cfg.regime.enabled else None
```

Add above `_observe`:

```python
    def _split_index(self, candles: dict[str, Candle]) -> dict[str, Candle]:
        """The tradable candles of a bar. The index candle is fed to the regime filter and removed, so
        no strategy, indicator, broker call or AI prompt ever sees it. With no index bar at this
        timestamp the filter keeps its last state. Must run before _observe."""
        idx = self._index_symbol
        index_candle = candles.get(idx) if idx else None
        tradable = {s: c for s, c in candles.items() if s != idx} if index_candle is not None else candles
        if self._regime is not None and index_candle is not None:
            self._regime.update(index_candle.close)
        return tradable
```

In `process_bar`, make the first statement `candles = self._split_index(candles)` (before `self._observe(candles)`).

In `_place`, directly after `sid = self.repo.insert_signal(self.run_id, sig)`:

```python
            blocked = self._regime.rejection(sig.direction) if self._regime is not None else None
            if blocked is not None:
                self.repo.insert_risk_decision(self.run_id, sid, False, blocked, 0)
                log.info("signal %s %s rejected: %s", sig.direction, sig.symbol, blocked, extra={"symbol": sig.symbol})
                continue
```

Extend the module docstring's bar order with a line before step 1: `  0. split off the index   the regime filter sees it; nothing else does`.

- [ ] **Step 4: Implement the paper warm-up**

`engine/paper.py`, in `warm`, replace the loop body:

```python
        for ts in sorted(by_ts):
            bar = self._split_index(by_ts[ts])
            self._observe(bar)
            self._run_strategies(bar)  # signals discarded: warm-up never places
            last = ts
```

- [ ] **Step 5: Run**

Run: `.venv/bin/pytest -q` → `434 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/engine tests/test_engine.py tests/test_paper.py
git commit -m "engine: index candle feeds the regime filter and nothing else; off-side signals are risk rejections"
```

### Task 14: the composite source

**Files:**
- Modify: `src/tradebot/engine/loop.py` (`_split_index`)
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine.py`:

```python
def _drift(sym, day, base, step):
    t0 = ist_epoch(day, "09:15")
    out, prev = [], base
    for i in range(75):
        p = round(base + step * (i + 1), 2)
        out.append(Candle(sym, t0 + i * 300, prev, max(prev, p) + 0.05, min(prev, p) - 0.05, p, 1000))
        prev = p
    return out


def test_composite_regime_needs_no_index_candles(repo, tmp_path):
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3})
    candles = _drift("A", MON, 100.0, -0.1) + _drift("B", MON, 200.0, -0.2)        # every symbol falls every bar
    _run_fixed(repo, cfg, candles, {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0),
                                    ("A", ist_epoch(MON, "10:30")): ("SHORT", 0.0)}, ["A", "B"])
    assert _decisions(repo) == [("A", "LONG", 0, "regime"), ("A", "SHORT", 1, "ok")]
```

- [ ] **Step 2: Run to see it fail**

Run: `.venv/bin/pytest tests/test_engine.py::test_composite_regime_needs_no_index_candles -q`
Expected: FAIL with `regime_not_ready` in place of `regime` (the filter is never fed).

- [ ] **Step 3: Implement**

In `Engine.__init__` add `self._composite_level = 100.0` after `self._regime = ...`. Replace `_split_index`:

```python
    def _split_index(self, candles: dict[str, Candle]) -> dict[str, Candle]:
        """The tradable candles of a bar. The index candle is fed to the regime filter and removed, so
        no strategy, indicator, broker call or AI prompt ever sees it. With no index bar at this
        timestamp the filter keeps its last state. Must run before _observe: the composite source
        reads the previous closes that _observe is about to overwrite."""
        idx = self._index_symbol
        index_candle = candles.get(idx) if idx else None
        tradable = {s: c for s, c in candles.items() if s != idx} if index_candle is not None else candles
        if self._regime is None:
            return tradable
        if self.cfg.regime.source == "index":
            if index_candle is not None:
                self._regime.update(index_candle.close)
            return tradable
        # composite: equal-weight mean of this bar's close-to-close returns, chained from 100
        returns = [c.close / self._last_close[s] - 1.0 for s, c in tradable.items() if self._last_close.get(s)]
        if returns:
            self._composite_level *= 1.0 + sum(returns) / len(returns)
            self._regime.update(self._composite_level)
        return tradable
```

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `435 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/loop.py tests/test_engine.py
git commit -m "engine: composite regime source from universe returns, for when index history is unavailable"
```

### Task 15: the CLI fetches, loads and checks the index

**Files:**
- Modify: `src/tradebot/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
_INSTRUMENTS = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
    "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
    "freeze_quantity,is_reserved,buy_allowed,sell_allowed,feed_key\n"
    "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,,,,,1,0.05,,0,1,1,NSE_CASH_2885\n")


def _fetch_with_index(tmp_path, monkeypatch, fail_index=False):
    make_config(tmp_path, data={"index_symbol": "NIFTY"})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(_INSTRUMENTS)
    calls = []

    class FakeAdapter:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            calls.append(symbol)
            if fail_index and symbol == "NIFTY":
                raise ValueError("index history not served")
            return [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 0 if symbol == "NIFTY" else 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    return _invoke(tmp_path, "fetch-data", "--days", "20", "--sleep", "0"), calls


def test_fetch_data_also_fetches_the_index(tmp_path, monkeypatch):
    res, calls = _fetch_with_index(tmp_path, monkeypatch)
    assert res.exit_code == 0, res.output
    assert set(calls) == {"RELIANCE", "NIFTY"}           # the index is not in the instrument master's EQ rows
    repo = Repo(connect(str(tmp_path / "tradebot.db")))
    assert repo.latest_candle_ts("NIFTY", 5) is not None


def test_a_failed_index_fetch_points_at_the_composite_source(tmp_path, monkeypatch):
    res, _ = _fetch_with_index(tmp_path, monkeypatch, fail_index=True)
    assert res.exit_code == 1 and "NIFTY" in res.output and "composite" in res.output


def test_backtest_with_the_regime_on_needs_index_candles(tmp_path):
    _setup(tmp_path)
    make_config(tmp_path, regime={"enabled": True}, data={"index_symbol": "NIFTY"})
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "rg0")
    assert res.exit_code == 1 and "NIFTY" in res.output and "fetch-data" in res.output
    assert "Traceback" not in res.output


def test_backtest_with_the_regime_on_runs_when_the_index_is_stored(tmp_path):
    cfg = _setup(tmp_path)
    make_config(tmp_path, regime={"enabled": True, "ema_period": 3}, data={"index_symbol": "NIFTY"})
    repo = Repo(connect(cfg.paths.db))
    t0 = ist_epoch(date(2026, 9, 14), "09:15")
    repo.insert_candles([Candle("NIFTY", t0 + i * 300, 25000.0, 25000.0, 25000.0, 25000.0, 0) for i in range(75)], interval=5)
    repo.conn.close()
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "rg1")
    assert res.exit_code == 0, res.output
    import json
    repo = Repo(connect(cfg.paths.db))
    assert json.loads(repo.get_run("rg1")["config_json"])["regime"]["enabled"] is True
    assert all(r["symbol"] != "NIFTY" for r in repo.list_positions("rg1"))     # gating itself is covered in test_engine
```

`_setup` writes the config first and `make_config` then rewrites it with the regime on; the database path is the same because both point into `tmp_path`.

Append to `tests/test_historical.py`, where `parse_candles` is tested (import it there if the file imports it under another name). Index rows carry a zero or null volume; the parser already accepts both, and this pins it:

```python
def test_index_rows_with_zero_or_null_volume_parse():
    from tradebot.execution.groww_adapter import parse_candles
    out = parse_candles("NIFTY", {"candles": [[1789450000, 25000.0, 25010.0, 24990.0, 25005.0, 0],
                                              [1789450300, 25005.0, 25020.0, 25000.0, 25015.0, None]]})
    assert [c.volume for c in out] == [0, 0] and out[0].symbol == "NIFTY"
```

This one passes before the implementation; it is a guard, not a driver.

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_historical.py -q`
Expected: four failures in `test_cli.py` (the index is never fetched; the regime backtest raises no error and shows no `regime` reason).

- [ ] **Step 3: Implement**

`cli.py`, add below `_symbols_and_lots`:

```python
def _with_index(cfg: Config, symbols: list) -> list:
    """The symbols to fetch and load: the universe plus the regime index. The index skips instrument
    resolution (it is not a tradable EQ row) and gets no lot size: it is never traded."""
    idx = cfg.data.index_symbol
    return list(symbols) + [idx] if idx and idx not in symbols else list(symbols)
```

`fetch_data`: after `symbols, _, exchange = _resolve_and_login(cfg, adapter)` add `symbols = _with_index(cfg, symbols)`. In the failure branch, after `click.echo(f"failed: {sym} ...")`, add:

```python
                if sym == cfg.data.index_symbol:
                    click.echo(f"hint: without {sym} history the regime filter can still run on the universe "
                               f"itself: set regime.source: composite", err=True)
```

`backtest`: load the index with the universe and fail early when the filter cannot be fed:

```python
    source = HistoricalSource.from_repo(repo, _with_index(cfg, symbols), interval,
                                        ist_epoch(start.date(), "00:00"), ist_epoch(end.date(), "23:59"))
    if not source.bar_timestamps():
        raise click.ClickException("no candles in range; run `tradebot fetch-data` first")
    idx = cfg.data.index_symbol
    if cfg.regime.enabled and cfg.regime.source == "index" and not repo.load_candles(
            [idx], interval, ist_epoch(start.date(), "00:00"), ist_epoch(end.date(), "23:59")):
        raise click.ClickException(f"regime.enabled needs {interval}-minute candles for {idx} in this window; run "
                                   f"`tradebot fetch-data` with this config, or set regime.source: composite")
```

`paper`: after `symbols, lots, exchange = _resolve_and_login(...)` add `feed = _with_index(cfg, symbols)` and pass `feed` in place of `symbols` to `_warm_fetch`, `_warm_candles` and `LiveBarSource`. `lots` stays as it is.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest -q` → `440 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/cli.py tests/test_cli.py tests/test_historical.py
git commit -m "cli: fetch, load and check the regime index; it is never resolved as an instrument or traded"
```

### Task 16: fetch the index and run the regime experiment

**Files:**
- Modify: `scripts/orb_experiment.py`

- [ ] **Step 1: Fetch index history at both intervals**

Needs the Groww credentials in `.env` (approval-flow keys: approve today's key first).

```bash
.venv/bin/tradebot fetch-data --days 95
.venv/bin/tradebot --config config-orb.yaml fetch-data --days 95
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('data/tradebot.db')
for r in c.execute(\"select interval, count(*), date(min(ts),'unixepoch'), date(max(ts),'unixepoch') from candles where symbol='NIFTY' group by interval\"): print(r)"
```
Expected: two rows, intervals 5 and 15, starting near 2026-06-17. Groww serves about three months of intraday history, so the first few June sessions may be missing; signals on those days become `regime_not_ready`, which the report counts. If the fetch fails for `NIFTY` on both intervals, run step 3 with `--source composite` and say so in the notes.

- [ ] **Step 2: Extend the script**

In `scripts/orb_experiment.py`: import `from tradebot.cli import _with_index`; in `run`, load the index too by replacing the `source = ...` statement with:

```python
    source = HistoricalSource.from_repo(repo, _with_index(cfg, symbols), interval,
                                        ist_epoch(window[0], "00:00"), ist_epoch(window[1], "23:59"))
```

add below `with_orb`:

```python
def with_regime(cfg, enabled: bool, source: str):
    return dc_replace(cfg, regime=dc_replace(cfg.regime, enabled=enabled, source=source))


def regime_phase(a) -> None:
    rr = None if a.rr.lower() == "none" else float(a.rr)
    variants = [("orb", with_orb(load_config("config-orb.yaml"), a.range_minutes, rr)),
                ("confluence", load_config("config.yaml"))]
    for strategy_name, base in variants:
        for enabled in (False, True):
            for tag, window in (("tune", TUNE), ("hold", HOLDOUT)):
                run_id = f"rg-{strategy_name}-{'on' if enabled else 'off'}-{tag}"
                print(row(run(with_regime(base, enabled, a.source), strategy_name, run_id, window)), flush=True)
```

and in `main`: `choices=["tune", "holdout", "regime"]`, a new argument
`ap.add_argument("--source", default="index", choices=["index", "composite"])`, the `--range/--rr` check moved above the phase branches for both `holdout` and `regime`, and before the holdout branch:

```python
    if a.phase == "regime":
        regime_phase(a)
        return
```

Update the module docstring with the third command:
`    .venv/bin/python scripts/orb_experiment.py regime --range 30 --rr 2.0 [--source composite]`
and the sentence: `The regime phase runs ORB (the chosen parameters) and 5-minute confluence, filter off and on, on both windows: eight runs. The two ORB filter-off runs repeat the tuning and holdout runs with the same parameters, so their rows must match those exactly.`

- [ ] **Step 3: Run the eight regime runs**

Run: `.venv/bin/python scripts/orb_experiment.py regime --range <chosen> --rr <chosen>`
Expected: eight rows `rg-orb-off-tune` … `rg-confluence-on-hold`; the confluence runs take a few minutes each. Check that `rg-orb-off-tune` equals the chosen `orb-t-…` row and `rg-orb-off-hold` equals `orb-holdout` (determinism), and that an `-on-` run's report shows `regime` under `Risk rejects`.

Decision rule from the spec: keep the filter for a strategy only if `R on risk` is higher with it on **both** windows.

- [ ] **Step 4: Commit**

```bash
git add scripts/orb_experiment.py
git commit -m "scripts: regime on/off runs for ORB and confluence on both windows"
```

---

# Wrap-up

### Task 17: results notes, README, final check

**Files:**
- Create: `docs/superpowers/notes/2026-09-19-charges-orb-regime-results.md`
- Modify: `README.md`

- [ ] **Step 1: Re-state the old runs net of charges**

```bash
for r in real-1 real-1-sonnet real-conf-2 pb-15m-1; do .venv/bin/tradebot report --run $r | sed -n 1,9p; echo; done
.venv/bin/tradebot --config config-15m.yaml report --run pb-15m-1 | sed -n 1,9p
```

- [ ] **Step 2: Write the notes**

Create the notes file in the style of `docs/superpowers/notes/2026-09-15-pullback-15m-results.md` (read it first) with these sections, every number copied from command output, none from memory:

1. **Charges** — the schedule used, and a table of the four old runs: trades, gross, charges (estimated), net, R on risk (net). Say in one sentence why the unweighted per-trade Avg R is not used: on `real-1` it reads -3.96 against an R on risk of -0.61, because 490 of 884 trades risk under 25 rupees and the per-order brokerage floor costs those more than 1R each.
2. **ORB tuning** — the six-row table from Task 10 step 2, the parameters chosen and why.
3. **ORB holdout** — the single row and the verdict line, plus the median stop distance as a percent of price and the cost per trade in R for `orb-holdout`, from:

```bash
.venv/bin/python -c "
import sqlite3, statistics as st
c = sqlite3.connect('data/tradebot.db'); c.row_factory = sqlite3.Row
rows = c.execute(\"select avg_price, stop, qty, charges from positions where run_id='orb-holdout' and pnl is not null\").fetchall()
print('median stop %:', round(st.median(abs(r['avg_price']-r['stop'])/r['avg_price']*100 for r in rows), 3))
print('median charges in R:', round(st.median(r['charges']/(abs(r['avg_price']-r['stop'])*r['qty']) for r in rows), 3))"
```

4. **Regime filter** — the eight-row table and, per strategy, keep or drop by the both-windows rule.
5. **Conclusion** — what is supported and what is not, including the standing caveats: survivorship bias, three months of data, and that the charge rates were taken from the config and must be checked against Groww's pricing page.

- [ ] **Step 3: README**

Add after the `## Backtest` section:

```markdown
## Charges

Backtest and paper results are net of brokerage and statutory charges (`charges:` in the config,
Groww intraday equity rates; check them against Groww's pricing page). `pnl` in the database stays
gross; `positions.charges` holds the cost. Reports print gross, charges and net. Runs stored before
the charges model are estimated after the fact and marked so.

## Opening-range breakout

    .venv/bin/tradebot --config config-orb.yaml backtest --strategy orb --start 2026-06-17 --end 2026-08-15
    .venv/bin/python scripts/orb_experiment.py tune

15-minute bars, one signal per symbol per day, at most two entries a day, no entries after 13:00.
When several symbols break out on one bar the engine takes the ones with the highest volume relative
to their opening range (`Signal.priority`), then alphabetical order, in backtest and paper alike.

## Regime filter

`data.index_symbol: NIFTY` is fetched and stored with the universe and never traded. With
`regime.enabled: true` longs are taken only while the index is above its EMA and shorts only while at
or below; blocked signals appear under "Risk rejects" as `regime` or `regime_not_ready`.
`regime.source: composite` builds the index from the universe's own returns when Groww serves no
index history.
```

- [ ] **Step 4: Final verification**

Run: `.venv/bin/pytest -q`
Expected: `440 passed`.

Run: `git status --short`
Expected: only the notes file and `README.md`.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/notes/2026-09-19-charges-orb-regime-results.md README.md
git commit -m "notes: results net of charges, ORB tuning and holdout, regime on/off; README"
```
