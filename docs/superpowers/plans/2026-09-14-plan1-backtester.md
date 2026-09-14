# Plan 1: Backtester Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable backtester: fetch Groww candles into SQLite, run the EMA/RSI strategy through the risk engine and a stub AI filter against a simulated broker, and print a report.

**Architecture:** Event-driven loop. A `HistoricalSource` emits one bar at a time for every symbol; strategies produce `Signal`s; `risk.evaluate` sizes or rejects them; an `AIFilter` approves; `BacktestBroker` fills at next open and simulates broker-side exits. Everything is written to SQLite through `Repo`. Spec: `docs/superpowers/specs/2026-09-14-nse-bse-trading-bot-design.md`. This plan covers spec build-order milestones 1–4. Milestone 5 (Claude filter) and 6–7 (paper, live) are separate plans.

**Tech Stack:** Python 3.9+ (the Mac ships 3.9.6; growwapi needs >=3.9), sqlite3 (stdlib), click, pyyaml, python-dotenv, requests, growwapi, pyotp, pytest.

**Conventions used throughout:**
- All timestamps are integer UTC epoch seconds. Only `engine/clock.py` touches IST.
- Percent config values are human percents (`1.0` means 1%). Code divides by 100 at the point of use.
- Prices are rounded to the 0.05 tick with `round_tick`.
- Every task ends with a commit. Run tests with `.venv/bin/pytest`.

---

## File structure

```
pyproject.toml
config.yaml
universe.yaml
.env.example
scripts/update_universe.py
src/tradebot/__init__.py
src/tradebot/types.py            # Candle, Signal, ApprovedOrder, Rejection, Position, Candidate, Decision, make_client_id, round_tick
src/tradebot/config.py           # Config dataclasses, load_config
src/tradebot/store/__init__.py
src/tradebot/store/schema.sql
src/tradebot/store/db.py         # connect()
src/tradebot/store/repo.py       # Repo: all SQL
src/tradebot/engine/__init__.py
src/tradebot/engine/clock.py     # IST helpers, SessionClock
src/tradebot/engine/loop.py      # BacktestEngine
src/tradebot/data/__init__.py
src/tradebot/data/universe.py    # load_universe
src/tradebot/data/instruments.py # download/load instrument master, resolve_universe
src/tradebot/data/historical.py  # fetch_incremental, HistoricalSource
src/tradebot/strategy/__init__.py
src/tradebot/strategy/indicators.py  # EMA, RSI, ATR
src/tradebot/strategy/base.py        # Strategy ABC
src/tradebot/strategy/ema_rsi.py     # EmaRsiStrategy
src/tradebot/risk/__init__.py
src/tradebot/risk/killswitch.py
src/tradebot/risk/sizing.py
src/tradebot/risk/engine.py      # PortfolioState, evaluate
src/tradebot/ai/__init__.py
src/tradebot/ai/filter.py        # AIFilter protocol, StubFilter
src/tradebot/execution/__init__.py
src/tradebot/execution/broker.py # Broker protocol, events
src/tradebot/execution/backtest.py   # BacktestBroker
src/tradebot/execution/groww_adapter.py  # only module importing growwapi
src/tradebot/report/__init__.py
src/tradebot/report/summary.py
src/tradebot/cli.py
tests/conftest.py
tests/test_types.py
tests/test_config.py
tests/test_store.py
tests/test_clock.py
tests/test_universe_instruments.py
tests/test_historical.py
tests/test_indicators.py
tests/test_ema_rsi.py
tests/test_risk.py
tests/test_backtest_broker.py
tests/test_engine.py
tests/test_report.py
tests/test_cli.py
tests/fixtures/instrument_sample.csv
tests/fixtures/golden_trades.json
```

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/tradebot/__init__.py`
- Create: `src/tradebot/{store,engine,data,strategy,risk,ai,execution,report}/__init__.py`
- Create: `tests/conftest.py`
- Create: `.env.example`

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "tradebot"
version = "0.1.0"
description = "NSE/BSE trading bot on the Groww Trade API"
requires-python = ">=3.9"
dependencies = [
    "growwapi>=1.5.0,<2",
    "pyotp>=2.9",
    "anthropic>=0.40,<1",
    "pyyaml>=6.0",
    "click>=8.1",
    "python-dotenv>=1.0",
    "requests>=2.31",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
tradebot = "tradebot.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
tradebot = ["store/schema.sql"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create package directories and empty `__init__.py` files**

```bash
mkdir -p src/tradebot/{store,engine,data,strategy,risk,ai,execution,report} tests/fixtures scripts
for d in src/tradebot src/tradebot/{store,engine,data,strategy,risk,ai,execution,report}; do : > "$d/__init__.py"; done
```

- [ ] **Step 3: Write `.env.example`**

```
GROWW_API_KEY=
GROWW_TOTP_SECRET=
ANTHROPIC_API_KEY=
```

- [ ] **Step 4: Write `tests/conftest.py`**

```python
import pytest


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch):
    """No test may observe credentials leaked into the process by another test."""
    for name in ("GROWW_API_KEY", "GROWW_TOTP_SECRET", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo():
    # Imported lazily: store modules arrive in Task 4, and no earlier test uses this fixture.
    from tradebot.store.db import connect
    from tradebot.store.repo import Repo

    conn = connect(":memory:")
    try:
        yield Repo(conn)
    finally:
        conn.close()
```

- [ ] **Step 5: Create venv and install**

Run:
```bash
python3 -m venv .venv && .venv/bin/pip install -q -U pip setuptools && .venv/bin/pip install -q -e ".[dev]" && .venv/bin/python -c "import tradebot; print('ok')"
```
Expected: `ok`. The pip/setuptools upgrade is required: the system Python 3.9 ships pip 21.2, which cannot do editable installs of a pyproject-only package.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src tests .env.example scripts
git commit -m "chore: project scaffold"
```

---

### Task 2: Core types

**Files:**
- Create: `src/tradebot/types.py`
- Test: `tests/test_types.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_types.py
from tradebot.types import Position, Signal, make_client_id, round_tick


def _sig(**kw):
    base = dict(strategy="ema_rsi", symbol="RELIANCE", direction="LONG",
                entry_price=100.0, stop_price=99.0, target_price=102.0,
                product="MIS", bar_ts=1_700_000_000)
    base.update(kw)
    return Signal(**base)


def test_client_id_is_deterministic_and_16_hex():
    a = make_client_id("ema_rsi", "RELIANCE", 1_700_000_000)
    b = make_client_id("ema_rsi", "RELIANCE", 1_700_000_000)
    assert a == b
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_client_id_changes_with_any_component():
    base = make_client_id("ema_rsi", "RELIANCE", 1)
    assert make_client_id("other", "RELIANCE", 1) != base
    assert make_client_id("ema_rsi", "TCS", 1) != base
    assert make_client_id("ema_rsi", "RELIANCE", 2) != base


def test_round_tick():
    assert round_tick(100.03) == 100.05
    assert round_tick(100.02) == 100.0
    assert round_tick(99.98) == 100.0


def test_position_unrealised_signs():
    long = Position("X", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "cid", "s")
    short = Position("X", "MIS", "SHORT", 10, 100.0, 101.0, None, 1, "cid", "s")
    assert long.unrealised(102.0) == 20.0
    assert long.unrealised(98.0) == -20.0
    assert short.unrealised(98.0) == 20.0
    assert short.unrealised(102.0) == -20.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.types'`

- [ ] **Step 3: Write `src/tradebot/types.py`**

```python
"""Core value types shared by every package. No I/O here.

Annotations use PEP 604/585 syntax and are strings under Python 3.9 thanks to the
`from __future__ import annotations` import; never resolve them with typing.get_type_hints()
until the project drops 3.9.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Literal

Direction = Literal["LONG", "SHORT"]
Product = Literal["MIS", "CNC"]

TICK = 0.05


def round_tick(price: float, tick: float = TICK) -> float:
    return round(round(price / tick) * tick, 2)


def round_tick_down(price: float, tick: float = TICK) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 2)


def round_tick_up(price: float, tick: float = TICK) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 2)


def make_client_id(strategy: str, symbol: str, bar_ts: int) -> str:
    """Idempotent order id: same (strategy, symbol, bar) always yields the same id.

    16 hex chars satisfies Groww's 8-20 alphanumeric order_reference_id rule.
    """
    raw = f"{strategy}|{symbol}|{bar_ts}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Candle:
    symbol: str
    ts: int  # UTC epoch seconds, bar open time
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str = "official"  # "official" | "tick_built"


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float
    target_price: float | None
    product: Product
    bar_ts: int


@dataclass(frozen=True)
class ApprovedOrder:
    signal: Signal
    quantity: int
    client_id: str


@dataclass(frozen=True)
class Rejection:
    signal: Signal
    reason: str


@dataclass
class Position:
    symbol: str
    product: Product
    direction: Direction
    quantity: int
    avg_price: float
    stop_price: float
    target_price: float | None
    opened_ts: int
    client_id: str
    strategy: str
    closed_ts: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # "STOP" | "TARGET" | "SQUARE_OFF" | "FLATTEN"
    pnl: float | None = None
    fill_status: str = "full"  # "full" | "partial"
    adopted: bool = False
    db_id: int | None = None

    def unrealised(self, last_price: float) -> float:
        if self.direction == "LONG":
            return (last_price - self.avg_price) * self.quantity
        return (self.avg_price - last_price) * self.quantity


@dataclass(frozen=True)
class Candidate:
    """What the AI filter sees for one signal."""
    signal: Signal
    quantity: int
    indicators: dict[str, float]
    candles: tuple[Candle, ...]


@dataclass(frozen=True)
class Decision:
    signal: Signal
    approved: bool
    reason: str
    confidence: float
    filter_kind: str
    latency_ms: int = 0
    failure: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_types.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/types.py tests/test_types.py
git commit -m "feat: core value types"
```

---

### Task 3: Configuration loading

**Files:**
- Create: `src/tradebot/config.py`
- Create: `config.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import textwrap

import pytest

from tradebot.config import load_config

YAML = textwrap.dedent("""
capital: 100000
risk:
  per_trade_pct: 1.0
  daily_loss_cap_pct: 3.0
  flatten_on_daily_cap: false
  max_entries_per_day: 20
  max_open_positions: 5
  cooldown_bars: 3
  mis_leverage: 5.0
  adopted_stop_pct: 1.5
strategy:
  ema_rsi:
    fast: 9
    slow: 21
    rsi_period: 14
    rsi_long_min: 55
    rsi_short_max: 45
    atr_period: 14
    atr_stop_mult: 1.5
    reward_risk: 2.0
    product: MIS
ai:
  filter: stub
  model: claude-sonnet-5
  candles_in_context: 30
  on_failure: reject
execution:
  slippage_pct: 0.05
  entry_buffer_pct: 0.1
  bar_deadline_sec: 60
  interval_minutes: 5
session:
  open: "09:15"
  close: "15:30"
  square_off: "15:10"
  no_new_entries_after: "14:45"
  holidays: ["2026-10-02"]
data:
  official_fetch_concurrency: 5
paths:
  db: data/tradebot.db
  logs: data/logs
  instruments: data/instruments.csv
  kill_switch: KILL
  universe: universe.yaml
""")


def _write(tmp_path, text=YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_load_config_reads_all_sections(tmp_path):
    cfg_path = _write(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\nANTHROPIC_API_KEY=a\n")

    cfg = load_config(cfg_path, env_path)

    assert cfg.capital == 100000
    assert cfg.risk.per_trade_pct == 1.0
    assert cfg.risk.cooldown_bars == 3
    assert cfg.strategy["ema_rsi"]["fast"] == 9
    assert cfg.ai.filter == "stub"
    assert cfg.execution.interval_minutes == 5
    assert cfg.session.holidays == ("2026-10-02",)
    assert cfg.paths.kill_switch == "KILL"
    assert cfg.data.official_fetch_concurrency == 5
    assert cfg.secrets.groww_api_key == "k"
    assert cfg.raw["capital"] == 100000


def test_missing_env_yields_empty_secrets(tmp_path):
    cfg = load_config(_write(tmp_path), tmp_path / "missing.env")
    assert cfg.secrets.groww_api_key == ""


def test_process_env_beats_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("GROWW_API_KEY", "from-process")
    env_path = tmp_path / ".env"
    env_path.write_text("GROWW_API_KEY=from-file\n")
    cfg = load_config(_write(tmp_path), env_path)
    assert cfg.secrets.groww_api_key == "from-process"


def test_unquoted_holiday_dates_are_normalised_to_iso_strings(tmp_path):
    cfg = load_config(_write(tmp_path, YAML.replace('["2026-10-02"]', "[2026-10-02]")), tmp_path / "x.env")
    assert cfg.session.holidays == ("2026-10-02",)


@pytest.mark.parametrize("broken, fragment", [
    (YAML.replace("cooldown_bars: 3", "cooldown_bar: 3"), "risk"),
    (YAML.replace("  square_off: \"15:10\"\n", ""), "session"),
    (YAML.replace("capital: 100000\n", ""), "capital"),
    (YAML.replace("per_trade_pct: 1.0", "per_trade_pct: one"), "risk.per_trade_pct"),
    (YAML.replace("max_open_positions: 5", "max_open_positions: 2.5"), "risk.max_open_positions"),
    (YAML.replace("flatten_on_daily_cap: false", "flatten_on_daily_cap: nope"), "risk.flatten_on_daily_cap"),
    (YAML.replace("mis_leverage: 5.0", "mis_leverage: 0.5"), "mis_leverage"),
    (YAML.replace("capital: 100000", "capital: true"), "capital"),
    (YAML.replace('close: "15:30"', "close: 15:30"), "session.close"),        # PyYAML sexagesimal -> 930
    (YAML.replace('square_off: "15:10"', 'square_off: "3:10pm"'), "session.square_off"),
    (YAML.replace('holidays: ["2026-10-02"]', 'holidays: "2026-10-02"'), "session.holidays"),
    (YAML.replace("interval_minutes: 5", "interval_minutes: 0"), "interval_minutes"),
    (YAML.replace('holidays: ["2026-10-02"]', "holidays: [null]"), "session.holidays"),
    (YAML[:YAML.index("session:")] + YAML[YAML.index("data:"):], "missing section: session"),
    (YAML.replace("on_failure: reject", "on_failure: maybe"), "ai.on_failure"),
    (YAML.replace("filter: stub", "filter: gpt"), "ai.filter"),
    ("", "capital"),
])
def test_bad_config_fails_at_load_with_key_named(tmp_path, broken, fragment):
    with pytest.raises(ValueError) as e:
        load_config(_write(tmp_path, broken), tmp_path / "x.env")
    assert fragment in str(e.value)


def test_strategy_params_are_not_aliased_to_raw(tmp_path):
    cfg = load_config(_write(tmp_path), tmp_path / "x.env")
    cfg.strategy["ema_rsi"]["fast"] = 999
    assert cfg.raw["strategy"]["ema_rsi"]["fast"] == 9
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.config'`

- [ ] **Step 3: Write `src/tradebot/config.py`**

```python
"""Typed config loaded from config.yaml plus secrets from .env.

Conventions:
- Every ``*_pct`` field is a human percent: ``1.0`` means 1%. Divide by 100 at the point of use.
- Values are type-checked and coerced at load time so a bad config fails here, not mid-session.
- Process environment wins over ``.env`` so an operator can override credentials per run.
"""
from __future__ import annotations

import copy
import dataclasses
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class RiskConfig:
    """``*_pct`` fields are human percents (1.0 == 1%)."""
    per_trade_pct: float
    daily_loss_cap_pct: float
    flatten_on_daily_cap: bool
    max_entries_per_day: int
    max_open_positions: int
    cooldown_bars: int
    mis_leverage: float
    adopted_stop_pct: float


@dataclass(frozen=True)
class AIConfig:
    filter: str
    model: str
    candles_in_context: int
    on_failure: str


@dataclass(frozen=True)
class ExecutionConfig:
    """``*_pct`` fields are human percents (0.05 == 0.05% == 5 bps)."""
    slippage_pct: float
    entry_buffer_pct: float
    bar_deadline_sec: int
    interval_minutes: int


@dataclass(frozen=True)
class SessionConfig:
    open: str
    close: str
    square_off: str
    no_new_entries_after: str
    holidays: tuple  # of ISO date strings


@dataclass(frozen=True)
class DataConfig:
    official_fetch_concurrency: int


@dataclass(frozen=True)
class PathsConfig:
    db: str
    logs: str
    instruments: str
    kill_switch: str
    universe: str


@dataclass(frozen=True)
class Secrets:
    groww_api_key: str
    groww_totp_secret: str
    anthropic_api_key: str


@dataclass(frozen=True)
class Config:
    capital: float
    risk: RiskConfig
    strategy: dict  # strategy name -> params; each strategy validates its own keys
    ai: AIConfig
    execution: ExecutionConfig
    session: SessionConfig
    data: DataConfig
    paths: PathsConfig
    secrets: Secrets
    raw: dict


_COERCE = {"float": float, "int": int, "bool": bool, "str": str, "tuple": tuple}


def _coerce(section: str, name: str, type_name: str, value: Any) -> Any:
    """Coerce a YAML scalar to the dataclass field type, or raise a config error naming the key."""
    where = f"config.yaml {section}.{name}"
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{where}: expected true/false, got {value!r}")
    if type_name == "str":
        # Strict: PyYAML turns an unquoted 15:30 into the integer 930 and an empty value into None.
        if not isinstance(value, str):
            raise ValueError(f"{where}: expected a quoted string, got {value!r}")
        return value
    if type_name == "tuple":
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{where}: expected a list, got {value!r}")
        return tuple(value)
    if isinstance(value, bool):  # int and float must not accept true/false
        raise ValueError(f"{where}: expected {type_name}, got {value!r}")
    if type_name == "int" and isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{where}: expected an integer, got {value!r}")
    try:
        return _COERCE[type_name](value)
    except (TypeError, ValueError, KeyError) as e:
        raise ValueError(f"{where}: expected {type_name}, got {value!r}") from e


def _section(raw: dict, name: str, cls):
    if name not in raw or not isinstance(raw[name], dict):
        raise ValueError(f"config.yaml missing section: {name}")
    given = dict(raw[name])
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(given) - set(fields)
    if unknown:
        raise ValueError(f"config.yaml section '{name}' has unknown keys: {sorted(unknown)}")
    missing = set(fields) - set(given)
    if missing:
        raise ValueError(f"config.yaml section '{name}' missing keys: {sorted(missing)}")
    return cls(**{k: _coerce(name, k, fields[k].type, v) for k, v in given.items()})


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
AI_FILTERS = ("stub", "claude", "claude_cached")
AI_ON_FAILURE = ("reject", "pass_through")


def _validate(cfg: "Config") -> None:
    r, e, s, a, d = cfg.risk, cfg.execution, cfg.session, cfg.ai, cfg.data
    checks = [
        (r.per_trade_pct > 0, "risk.per_trade_pct must be > 0"),
        (r.daily_loss_cap_pct > 0, "risk.daily_loss_cap_pct must be > 0"),
        (r.adopted_stop_pct > 0, "risk.adopted_stop_pct must be > 0"),
        (r.mis_leverage >= 1, "risk.mis_leverage must be >= 1"),
        (r.max_open_positions >= 1, "risk.max_open_positions must be >= 1"),
        (r.max_entries_per_day >= 1, "risk.max_entries_per_day must be >= 1"),
        (r.cooldown_bars >= 0, "risk.cooldown_bars must be >= 0"),
        (e.slippage_pct >= 0, "execution.slippage_pct must be >= 0"),
        (e.entry_buffer_pct >= 0, "execution.entry_buffer_pct must be >= 0"),
        (e.bar_deadline_sec > 0, "execution.bar_deadline_sec must be > 0"),
        (e.interval_minutes > 0, "execution.interval_minutes must be > 0"),
        (d.official_fetch_concurrency >= 1, "data.official_fetch_concurrency must be >= 1"),
        (a.candles_in_context >= 1, "ai.candles_in_context must be >= 1"),
        (a.filter in AI_FILTERS, f"ai.filter must be one of {AI_FILTERS}"),
        (a.on_failure in AI_ON_FAILURE, f"ai.on_failure must be one of {AI_ON_FAILURE}"),
    ]
    for name in ("open", "close", "square_off", "no_new_entries_after"):
        checks.append((bool(_HHMM.match(getattr(s, name))), f'session.{name} must be a quoted "HH:MM" time'))
    checks.append((all(isinstance(h, str) for h in s.holidays), "session.holidays must be a list of ISO date strings"))
    for ok, msg in checks:
        if not ok:
            raise ValueError(f"config.yaml {msg}")


def load_config(path: Union[str, Path] = "config.yaml", env_path: Union[str, Path] = ".env") -> Config:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if Path(env_path).exists():
        load_dotenv(env_path)  # process env wins; .env only fills gaps
    if "capital" not in raw:
        raise ValueError("config.yaml missing 'capital'")
    capital = _coerce("(root)", "capital", "float", raw["capital"])
    if capital <= 0:
        raise ValueError("config.yaml capital must be > 0")
    raw_for_session = raw
    if isinstance(raw.get("session"), dict) and isinstance(raw["session"].get("holidays"), (list, tuple)):
        session_raw = dict(raw["session"])
        session_raw["holidays"] = tuple(h if h is None else str(h) for h in session_raw["holidays"])  # dates -> ISO
        raw_for_session = {**raw, "session": session_raw}
    cfg = Config(
        capital=capital,
        risk=_section(raw, "risk", RiskConfig),
        strategy=copy.deepcopy(raw.get("strategy") or {}),
        ai=_section(raw, "ai", AIConfig),
        execution=_section(raw, "execution", ExecutionConfig),
        session=_section(raw_for_session, "session", SessionConfig),
        data=_section(raw, "data", DataConfig),
        paths=_section(raw, "paths", PathsConfig),
        secrets=Secrets(
            groww_api_key=os.environ.get("GROWW_API_KEY", ""),
            groww_totp_secret=os.environ.get("GROWW_TOTP_SECRET", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
        raw=raw,
    )
    _validate(cfg)
    return cfg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 22 passed

- [ ] **Step 5: Write the real `config.yaml` at project root**

```yaml
capital: 100000

risk:
  per_trade_pct: 1.0          # max loss per trade as % of capital
  daily_loss_cap_pct: 3.0     # realised + unrealised; stop new entries when breached
  flatten_on_daily_cap: false
  max_entries_per_day: 20     # entry orders only; exits are never blocked
  max_open_positions: 5
  cooldown_bars: 3            # bars to wait on a symbol after a stop-out
  mis_leverage: 5.0           # simulated intraday leverage in backtest/paper
  adopted_stop_pct: 1.5       # fallback stop for orphaned positions (live)

strategy:
  ema_rsi:
    fast: 9
    slow: 21
    rsi_period: 14
    rsi_long_min: 55
    rsi_short_max: 45
    atr_period: 14
    atr_stop_mult: 1.5
    reward_risk: 2.0
    min_stop_pct: 0.1         # skip signals whose stop is closer than this % of price
    product: MIS

ai:
  filter: stub                # stub | claude | claude_cached (claude in Plan 2)
  model: claude-sonnet-5
  candles_in_context: 30
  on_failure: reject          # reject | pass_through

execution:
  slippage_pct: 0.05           # percent: 0.05 = 5 bps
  entry_buffer_pct: 0.1       # marketable limit buffer (live)
  bar_deadline_sec: 60
  interval_minutes: 5

session:
  open: "09:15"
  close: "15:30"
  square_off: "15:10"
  no_new_entries_after: "14:45"
  holidays: []                # ISO dates, e.g. "2026-10-02"

data:
  official_fetch_concurrency: 5

paths:
  db: data/tradebot.db
  logs: data/logs
  instruments: data/instruments.csv
  kill_switch: KILL
  universe: universe.yaml
```

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/config.py tests/test_config.py config.yaml
git commit -m "feat: typed config loader"
```

---

### Task 4: SQLite store

**Files:**
- Create: `src/tradebot/store/schema.sql`
- Create: `src/tradebot/store/db.py`
- Create: `src/tradebot/store/repo.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

```python
import json
import sqlite3

import pytest

from tradebot.store.db import SCHEMA_VERSION, SchemaVersionError, connect
from tradebot.types import Candle, Position, Signal


def _candle(sym="RELIANCE", ts=1_700_000_000, c=100.0):
    return Candle(sym, ts, c, c + 1, c - 1, c, 1000)


def _signal(sym="RELIANCE", ts=1_700_000_000):
    return Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", ts)


def test_run_lifecycle(repo):
    repo.create_run("r1", "backtest", 10, json.dumps({"a": 1}))
    run = repo.get_run("r1")
    assert run["mode"] == "backtest"
    assert run["ended_at"] is None
    repo.end_run("r1", 20)
    assert repo.get_run("r1")["ended_at"] == 20


def test_candles_insert_is_idempotent(repo):
    cs = [_candle(ts=1_700_000_000), _candle(ts=1_700_000_300)]
    assert repo.insert_candles(cs, interval=5) == 2
    assert repo.insert_candles(cs, interval=5) == 0
    assert repo.insert_candles(cs + [_candle(ts=1_700_000_600)], interval=5) == 1  # mixed batch
    assert repo.latest_candle_ts("RELIANCE", 5) == 1_700_000_600
    assert repo.latest_candle_ts("TCS", 5) is None


def test_load_candles_filters_and_orders(repo):
    repo.insert_candles([_candle(ts=3), _candle(ts=1), _candle(ts=2), _candle("TCS", ts=2)], interval=5)
    out = repo.load_candles(["RELIANCE"], interval=5, start_ts=1, end_ts=2)
    assert [c.ts for c in out] == [1, 2]
    both = repo.load_candles(["RELIANCE", "TCS"], interval=5, start_ts=0, end_ts=10)
    assert [(c.ts, c.symbol) for c in both] == [(1, "RELIANCE"), (2, "RELIANCE"), (2, "TCS"), (3, "RELIANCE")]


def test_signal_and_decisions(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    sid = repo.insert_signal("r1", _signal())
    repo.insert_risk_decision("r1", sid, approved=False, reason="max_open_positions", quantity=0)
    repo.insert_risk_decision("r1", repo.insert_signal("r1", _signal("TCS")), approved=True, reason="ok", quantity=10)
    repo.insert_ai_decision("r1", sid, "stub", True, "stub", 1.0, 0, None)
    assert repo.rejection_counts("r1") == {"max_open_positions": 1}


def test_orders_and_fills(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    sid = repo.insert_signal("r1", _signal())
    oid = repo.insert_order("r1", "abc123abc123abc1", sid, "ENTRY", "BUY", 10, 100.0, "PENDING", 5)
    repo.update_order("r1", "abc123abc123abc1", "FILLED", 6)
    repo.insert_fill(oid, 10, 100.05, 6)
    assert repo.order_status("r1", "abc123abc123abc1") == "FILLED"


def test_positions_roundtrip(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    p = Position("RELIANCE", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 5, "cid", "ema_rsi")
    p.db_id = repo.insert_position("r1", p)
    repo.close_position(p.db_id, closed_ts=9, exit_price=102.0, exit_reason="TARGET", pnl=20.0)
    rows = repo.list_positions("r1")
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "TARGET"
    assert rows[0]["pnl"] == 20.0


def test_daily_pnl_upsert(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    repo.upsert_daily_pnl("r1", "2026-09-14", realised=10.0, unrealised=0.0, fills=1, entries_placed=2)
    repo.upsert_daily_pnl("r1", "2026-09-14", realised=15.0, unrealised=1.0, fills=2, entries_placed=2)
    rows = repo.daily_pnl("r1")
    assert len(rows) == 1
    assert rows[0]["realised"] == 15.0
    assert rows[0]["fill_rate"] == 1.0


def test_duplicate_client_id_is_rejected_and_original_survives(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    repo.insert_order("r1", "dupdupdupdupdup1", None, "ENTRY", "BUY", 10, 100.0, "PENDING", 5)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_order("r1", "dupdupdupdupdup1", None, "ENTRY", "BUY", 99, 1.0, "PENDING", 6)
    assert repo.order_status("r1", "dupdupdupdupdup1") == "PENDING"
    assert repo.order_id("r1", "nope") is None
    assert repo.order_status("r1", "nope") is None


def test_foreign_keys_enforced(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_fill(999_999, 1, 1.0, 1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_signal("no-such-run", _signal())


def test_close_position_accepts_null_pnl(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    pid = repo.insert_position("r1", Position("X", "MIS", "LONG", 1, 1.0, 0.5, None, 1, "c", "s"))
    repo.close_position(pid, closed_ts=2, exit_price=None, exit_reason=None, pnl=None)
    assert repo.list_positions("r1")[0]["pnl"] is None


def test_file_backed_connect_uses_wal_persists_and_versions(tmp_path):
    path = tmp_path / "nested" / "dir" / "t.db"
    conn = connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.execute("INSERT INTO runs(run_id, mode, started_at, config_json) VALUES ('r','backtest',0,'{}')")
    conn.commit()
    conn.close()
    conn2 = connect(path)  # schema re-applied idempotently, data intact
    assert conn2.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn2.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn2.commit()
    conn2.close()
    with pytest.raises(SchemaVersionError):
        connect(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.store.db'`

- [ ] **Step 3: Write `src/tradebot/store/schema.sql`**

```sql
-- Schema version: bump SCHEMA_VERSION in db.py and add a migration whenever this file changes.
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  mode        TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  ended_at    INTEGER,
  config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
  symbol   TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  interval INTEGER NOT NULL,
  o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
  v INTEGER NOT NULL,
  source   TEXT NOT NULL DEFAULT 'official',
  PRIMARY KEY (symbol, ts, interval)
);

CREATE TABLE IF NOT EXISTS signals (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT NOT NULL REFERENCES runs(run_id),
  strategy  TEXT NOT NULL,
  symbol    TEXT NOT NULL,
  bar_ts    INTEGER NOT NULL,
  direction TEXT NOT NULL,
  entry     REAL NOT NULL,
  stop      REAL NOT NULL,
  target    REAL,
  product   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    TEXT NOT NULL REFERENCES runs(run_id),
  signal_id INTEGER NOT NULL REFERENCES signals(id),
  approved  INTEGER NOT NULL,
  reason    TEXT NOT NULL,
  quantity  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_decisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  signal_id   INTEGER NOT NULL REFERENCES signals(id),
  filter_kind TEXT NOT NULL,
  approved    INTEGER NOT NULL,
  reason      TEXT NOT NULL,
  confidence  REAL NOT NULL,
  latency_ms  INTEGER NOT NULL,
  failure     TEXT
);

CREATE TABLE IF NOT EXISTS ai_cache (
  symbol        TEXT NOT NULL,
  bar_ts        INTEGER NOT NULL,
  prompt_hash   TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  PRIMARY KEY (symbol, bar_ts, prompt_hash)
);

CREATE TABLE IF NOT EXISTS orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  client_id       TEXT NOT NULL,
  signal_id       INTEGER REFERENCES signals(id),
  broker_order_id TEXT,
  kind            TEXT NOT NULL,   -- ENTRY | EXIT | SQUARE_OFF
  side            TEXT NOT NULL,   -- BUY | SELL
  qty             INTEGER NOT NULL,
  limit_price     REAL,
  status          TEXT NOT NULL,   -- PENDING | FILLED | PARTIAL | UNFILLED | CANCELLED | FAILED
  placed_at       INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  UNIQUE (run_id, client_id)
);

CREATE TABLE IF NOT EXISTS fills (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  qty      INTEGER NOT NULL,
  price    REAL NOT NULL,
  ts       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  symbol        TEXT NOT NULL,
  product       TEXT NOT NULL,
  direction     TEXT NOT NULL,
  strategy      TEXT NOT NULL,   -- 'adopted' sentinel for positions with no known signal (live)
  client_id     TEXT NOT NULL,   -- '' for adopted positions
  qty           INTEGER NOT NULL,
  avg_price     REAL NOT NULL,
  stop          REAL NOT NULL,
  target        REAL,
  exit_ids_json TEXT NOT NULL DEFAULT '[]',
  opened_at     INTEGER NOT NULL,
  closed_at     INTEGER,
  exit_price    REAL,
  exit_reason   TEXT,
  pnl           REAL,
  fill_status   TEXT NOT NULL DEFAULT 'full',
  adopted       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_pnl (
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  date           TEXT NOT NULL,
  realised       REAL NOT NULL,
  unrealised     REAL NOT NULL,
  fills          INTEGER NOT NULL,
  entries_placed INTEGER NOT NULL,
  fill_rate      REAL NOT NULL,
  PRIMARY KEY (run_id, date)
);

CREATE INDEX IF NOT EXISTS idx_signals_run   ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_risk_run      ON risk_decisions(run_id, approved);
CREATE INDEX IF NOT EXISTS idx_ai_run        ON ai_decisions(run_id, approved);
CREATE INDEX IF NOT EXISTS idx_fills_order   ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_positions_run ON positions(run_id);
```

- [ ] **Step 4: Write `src/tradebot/store/db.py`**

```python
"""SQLite connection factory. Applies schema.sql on every connect (idempotent) and
refuses to open a database written by a different schema version."""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    pass


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    is_file = str(path) != ":memory:"
    if is_file:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if is_file:
        conn.execute("PRAGMA journal_mode = WAL")
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found not in (0, SCHEMA_VERSION):
        conn.close()
        raise SchemaVersionError(
            f"{path} has schema version {found}, code expects {SCHEMA_VERSION}; migrate or delete the file"
        )
    schema = resources.files("tradebot.store").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
```

- [ ] **Step 5: Write `src/tradebot/store/repo.py`**

```python
"""All SQL lives here. Every method takes and returns plain types or sqlite3.Row."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from tradebot.types import Candle, Position, Signal


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- runs --------------------------------------------------------------
    def create_run(self, run_id: str, mode: str, started_at: int, config_json: str) -> None:
        self.conn.execute(
            "INSERT INTO runs(run_id, mode, started_at, config_json) VALUES (?,?,?,?)",
            (run_id, mode, started_at, config_json),
        )
        self.conn.commit()

    def end_run(self, run_id: str, ended_at: int) -> None:
        self.conn.execute("UPDATE runs SET ended_at=? WHERE run_id=?", (ended_at, run_id))
        self.conn.commit()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    # -- candles -----------------------------------------------------------
    def insert_candles(self, candles: Iterable[Candle], interval: int) -> int:
        rows = [(c.symbol, c.ts, interval, c.open, c.high, c.low, c.close, c.volume, c.source) for c in candles]
        # total_changes is connection-wide; correct here because Repo is single-threaded and
        # nothing else runs between the two reads. Cursor.rowcount would count ignored rows too.
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO candles(symbol, ts, interval, o, h, l, c, v, source) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def delete_candles(self, symbol: str, interval: int, start_ts: int, end_ts: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM candles WHERE symbol=? AND interval=? AND ts BETWEEN ? AND ?",
            (symbol, interval, start_ts, end_ts),
        )
        self.conn.commit()
        return cur.rowcount

    def latest_candle_ts(self, symbol: str, interval: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM candles WHERE symbol=? AND interval=?", (symbol, interval)
        ).fetchone()
        return row["ts"]

    def load_candles(self, symbols: list[str], interval: int, start_ts: int, end_ts: int) -> list[Candle]:
        # One bind variable per symbol; SQLite >= 3.32 allows 32766, older builds 999. NIFTY 200 fits either.
        if len(symbols) > 900:
            raise ValueError("load_candles: more than 900 symbols; chunk the universe")
        marks = ",".join("?" * len(symbols))
        rows = self.conn.execute(
            f"SELECT * FROM candles WHERE symbol IN ({marks}) AND interval=? AND ts BETWEEN ? AND ? "
            "ORDER BY ts, symbol",
            (*symbols, interval, start_ts, end_ts),
        ).fetchall()
        return [Candle(r["symbol"], r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"], r["source"]) for r in rows]

    # -- signals & decisions ----------------------------------------------
    def insert_signal(self, run_id: str, s: Signal) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals(run_id, strategy, symbol, bar_ts, direction, entry, stop, target, product) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, s.strategy, s.symbol, s.bar_ts, s.direction, s.entry_price, s.stop_price, s.target_price, s.product),
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_risk_decision(self, run_id: str, signal_id: int, approved: bool, reason: str, quantity: int) -> None:
        self.conn.execute(
            "INSERT INTO risk_decisions(run_id, signal_id, approved, reason, quantity) VALUES (?,?,?,?,?)",
            (run_id, signal_id, int(approved), reason, quantity),
        )
        self.conn.commit()

    def insert_ai_decision(self, run_id: str, signal_id: int, filter_kind: str, approved: bool, reason: str,
                           confidence: float, latency_ms: int, failure: str | None) -> None:
        self.conn.execute(
            "INSERT INTO ai_decisions(run_id, signal_id, filter_kind, approved, reason, confidence, latency_ms, failure) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, signal_id, filter_kind, int(approved), reason, confidence, latency_ms, failure),
        )
        self.conn.commit()

    def rejection_counts(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS n FROM risk_decisions WHERE run_id=? AND approved=0 GROUP BY reason",
            (run_id,),
        ).fetchall()
        return {r["reason"]: r["n"] for r in rows}

    def ai_rejection_count(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM ai_decisions WHERE run_id=? AND approved=0", (run_id,)
        ).fetchone()
        return row["n"]

    # -- orders & fills ----------------------------------------------------
    def insert_order(self, run_id: str, client_id: str, signal_id: int | None, kind: str, side: str, qty: int,
                     limit_price: float | None, status: str, placed_at: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO orders(run_id, client_id, signal_id, kind, side, qty, limit_price, status, placed_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, client_id, signal_id, kind, side, qty, limit_price, status, placed_at, placed_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_order(self, run_id: str, client_id: str, status: str, updated_at: int) -> None:
        self.conn.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE run_id=? AND client_id=?",
            (status, updated_at, run_id, client_id),
        )
        self.conn.commit()

    def order_status(self, run_id: str, client_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM orders WHERE run_id=? AND client_id=?", (run_id, client_id)
        ).fetchone()
        return row["status"] if row else None

    def order_id(self, run_id: str, client_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM orders WHERE run_id=? AND client_id=?", (run_id, client_id)
        ).fetchone()
        return row["id"] if row else None

    def insert_fill(self, order_id: int, qty: int, price: float, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO fills(order_id, qty, price, ts) VALUES (?,?,?,?)", (order_id, qty, price, ts)
        )
        self.conn.commit()

    # -- positions ---------------------------------------------------------
    def insert_position(self, run_id: str, p: Position) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions(run_id, symbol, product, direction, strategy, client_id, qty, avg_price, stop, target, "
            "opened_at, fill_status, adopted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, p.symbol, p.product, p.direction, p.strategy, p.client_id, p.quantity, p.avg_price,
             p.stop_price, p.target_price, p.opened_ts, p.fill_status, int(p.adopted)),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_position(self, position_id: int, closed_ts: int, exit_price: float, exit_reason: str, pnl: float) -> None:
        self.conn.execute(
            "UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl=? WHERE id=?",
            (closed_ts, exit_price, exit_reason, pnl, position_id),
        )
        self.conn.commit()

    def list_positions(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE run_id=? ORDER BY opened_at, symbol", (run_id,)
        ).fetchall()

    # -- daily pnl ---------------------------------------------------------
    def upsert_daily_pnl(self, run_id: str, date: str, realised: float, unrealised: float,
                         fills: int, entries_placed: int) -> None:
        fill_rate = fills / entries_placed if entries_placed else 0.0
        self.conn.execute(
            "INSERT INTO daily_pnl(run_id, date, realised, unrealised, fills, entries_placed, fill_rate) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id, date) DO UPDATE SET realised=excluded.realised, "
            "unrealised=excluded.unrealised, fills=excluded.fills, entries_placed=excluded.entries_placed, "
            "fill_rate=excluded.fill_rate",
            (run_id, date, realised, unrealised, fills, entries_placed, fill_rate),
        )
        self.conn.commit()

    def daily_pnl(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM daily_pnl WHERE run_id=? ORDER BY date", (run_id,)
        ).fetchall()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: 11 passed

- [ ] **Step 7: Commit**

```bash
git add src/tradebot/store tests/test_store.py
git commit -m "feat: sqlite schema and repo"
```

---

### Task 5: Clock and IST session logic

**Files:**
- Create: `src/tradebot/engine/clock.py`
- Test: `tests/test_clock.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_clock.py
from dataclasses import replace
from datetime import date

import pytest

from tradebot.config import SessionConfig
from tradebot.engine.clock import SessionClock, date_of, iso_ist, ist_epoch, to_ist

SESSION = SessionConfig(open="09:15", close="15:30", square_off="15:10",
                        no_new_entries_after="14:45", holidays=("2026-10-02", "2026-09-12"))
D = date(2026, 9, 14)  # Monday


def test_ist_epoch_known_values():
    assert ist_epoch(D, "09:15") == 1789357500          # 2026-09-14 03:45 UTC
    assert ist_epoch(date(2026, 1, 5), "09:15") == 1767584700  # no DST in IST: same offset in January


def test_to_ist_date_of_and_iso_roundtrip():
    ts = ist_epoch(D, "15:29")
    dt = to_ist(ts)
    assert (dt.hour, dt.minute) == (15, 29)
    assert date_of(ts) == D
    assert iso_ist(ts) == "2026-09-14T15:29:00+05:30"


def test_trading_day_excludes_weekends_and_holidays():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.is_trading_day(D)
    assert not clk.is_trading_day(date(2026, 9, 13))   # Sunday
    assert not clk.is_trading_day(date(2026, 10, 2))   # holiday on a Friday
    assert not clk.is_trading_day(date(2026, 9, 12))   # holiday that is also a Saturday


def test_square_off_bar_is_last_bar_ending_at_or_before_square_off():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.square_off_bar_ts(D) == ist_epoch(D, "15:05")
    assert clk.is_square_off_bar(ist_epoch(D, "15:05"))
    assert not clk.is_square_off_bar(ist_epoch(D, "15:00"))
    assert not clk.is_square_off_bar(ist_epoch(D, "15:10"))  # a bar opening at 15:10 ends after square-off


@pytest.mark.parametrize("interval, expected", [(1, "15:09"), (5, "15:05"), (15, "14:45")])
def test_square_off_bar_other_intervals(interval, expected):
    clk = SessionClock(SESSION, interval_minutes=interval)
    assert clk.square_off_bar_ts(D) == ist_epoch(D, expected)


def test_square_off_due_is_true_from_the_bar_onward():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert not clk.square_off_due(ist_epoch(D, "15:00"))
    assert clk.square_off_due(ist_epoch(D, "15:05"))
    assert clk.square_off_due(ist_epoch(D, "15:20"))


def test_entries_allowed_cutoff():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.entries_allowed(ist_epoch(D, "14:40"))
    assert not clk.entries_allowed(ist_epoch(D, "14:45"))
    assert not clk.entries_allowed(ist_epoch(D, "09:10"))  # before open


def test_in_session():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.in_session(ist_epoch(D, "09:15"))
    assert clk.in_session(ist_epoch(D, "15:25"))
    assert not clk.in_session(ist_epoch(D, "15:30"))


def test_rejects_interval_that_does_not_fit():
    with pytest.raises(ValueError, match="does not fit"):
        SessionClock(SESSION, interval_minutes=360)


def test_rejects_square_off_bar_before_entry_cutoff():
    # 30m bars: square-off bar would open 14:15, before the 14:45 cutoff
    with pytest.raises(ValueError, match="before no_new_entries_after"):
        SessionClock(SESSION, interval_minutes=30)


def test_rejects_unordered_session_times():
    with pytest.raises(ValueError, match="open < no_new_entries_after"):
        SessionClock(replace(SESSION, square_off="14:00"), interval_minutes=5)


def test_rejects_malformed_holiday_and_time():
    with pytest.raises(ValueError):
        SessionClock(replace(SESSION, holidays=("2026-10-02 00:00:00",)), interval_minutes=5)
    with pytest.raises(ValueError, match="HH:MM"):
        ist_epoch(D, "09:15:00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_clock.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.engine.clock'`

- [ ] **Step 3: Write `src/tradebot/engine/clock.py`**

```python
"""The only module that knows about IST. Everything else uses UTC epoch seconds.

Bar-time convention: every ``ts`` handed to this class is a bar's OPEN time. So
``entries_allowed`` is true for the bar that opens strictly before the cutoff, and
``square_off_bar_ts`` is the open of the last bar that CLOSES at or before square-off.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from tradebot.config import SessionConfig

IST = ZoneInfo("Asia/Kolkata")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(s: str) -> time:
    m = _HHMM.match(s)
    if not m:
        raise ValueError(f"expected a time in HH:MM form, got {s!r}")
    return time(int(m.group(1)), int(m.group(2)))


def _minute_of_day(s: str) -> int:
    t = _parse_hhmm(s)
    return t.hour * 60 + t.minute


def ist_epoch(d: date, hhmm: str) -> int:
    """Epoch seconds for the given IST wall-clock time on date d."""
    return int(datetime.combine(d, _parse_hhmm(hhmm), tzinfo=IST).timestamp())


def to_ist(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=IST)


def date_of(ts: int) -> date:
    return to_ist(ts).date()


def iso_ist(ts: int) -> str:
    return to_ist(ts).isoformat()


class SessionClock:
    """Session boundaries for a given bar interval. Pure; no wall-clock access.

    Validates at construction that the session times are ordered and that at least one
    bar fits between open and square-off, and that the square-off bar does not precede
    the entry cutoff (otherwise an entry could be opened after square-off already ran).
    """

    def __init__(self, session: SessionConfig, interval_minutes: int):
        self.session = session
        self.interval_sec = interval_minutes * 60
        self._holidays = {date.fromisoformat(h) for h in session.holidays}
        o, cut, sq, c = (_minute_of_day(x) for x in
                         (session.open, session.no_new_entries_after, session.square_off, session.close))
        if not (o < cut <= sq <= c):
            raise ValueError("session times must satisfy open < no_new_entries_after <= square_off <= close")
        n_bars = (sq - o) // interval_minutes
        if n_bars < 1:
            raise ValueError(f"interval {interval_minutes}m does not fit between {session.open} and {session.square_off}")
        self._square_off_offset_sec = (n_bars - 1) * self.interval_sec
        if o + (n_bars - 1) * interval_minutes < cut:
            raise ValueError("square-off bar would open before no_new_entries_after; shorten the interval or move the cutoff")

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._holidays

    def open_ts(self, d: date) -> int:
        return ist_epoch(d, self.session.open)

    def close_ts(self, d: date) -> int:
        return ist_epoch(d, self.session.close)

    def in_session(self, ts: int) -> bool:
        d = date_of(ts)
        return self.open_ts(d) <= ts < self.close_ts(d)

    def square_off_bar_ts(self, d: date) -> int:
        """Open time of the last bar whose end is at or before the square-off time."""
        return self.open_ts(d) + self._square_off_offset_sec

    def is_square_off_bar(self, ts: int) -> bool:
        return ts == self.square_off_bar_ts(date_of(ts))

    def square_off_due(self, ts: int) -> bool:
        """True from the square-off bar onward. Callers latch once per day so a missing
        bar at exactly the square-off time cannot skip the square-off."""
        return ts >= self.square_off_bar_ts(date_of(ts))

    def entries_allowed(self, ts: int) -> bool:
        d = date_of(ts)
        cutoff = ist_epoch(d, self.session.no_new_entries_after)
        return self.open_ts(d) <= ts < cutoff
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clock.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/clock.py tests/test_clock.py
git commit -m "feat: IST session clock"
```

---

### Task 6: Universe and instrument master

**Files:**
- Create: `src/tradebot/data/universe.py`
- Create: `src/tradebot/data/instruments.py`
- Create: `universe.yaml`
- Create: `scripts/update_universe.py`
- Create: `tests/fixtures/instrument_sample.csv`
- Test: `tests/test_universe_instruments.py`

- [ ] **Step 1: Write the fixture CSV**

`tests/fixtures/instrument_sample.csv` (header must match Groww's real column names):

```csv
exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,sell_allowed,feed_key
NSE,2885,RELIANCE,NSE-RELIANCE,Reliance Industries,EQ,CASH,EQ,INE002A01018,,,,,1,0.05,,0,1,1,NSE_CASH_2885
NSE,11536,TCS,NSE-TCS,Tata Consultancy,EQ,CASH,EQ,INE467B01029,,,,,1,0.05,,0,1,1,NSE_CASH_11536
NSE,99999,SUSPENDED,NSE-SUSPENDED,Suspended Co,EQ,CASH,EQ,INE000000000,,,,,1,0.05,,0,0,0,NSE_CASH_99999
NSE,35000,NIFTY26SEPFUT,NSE-NIFTY26SEPFUT,Nifty Fut,FUT,FNO,,,NIFTY,,2026-09-24,,75,0.05,,0,1,1,NSE_FNO_35000
BSE,500325,RELIANCE,BSE-RELIANCE,Reliance Industries,EQ,CASH,A,INE002A01018,,,,,1,0.05,,0,1,1,BSE_CASH_500325
```

- [ ] **Step 2: Write the failing tests**

```python
from datetime import date
from pathlib import Path

import pytest

from tradebot.data import instruments as instruments_mod
from tradebot.data.instruments import download_instruments, load_instruments, resolve_universe
from tradebot.data.universe import Universe, load_universe

FIXTURE = Path(__file__).parent / "fixtures" / "instrument_sample.csv"


def test_load_universe(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - RELIANCE\n  - TCS\n")
    u = load_universe(p)
    assert u == Universe(exchange="NSE", symbols=("RELIANCE", "TCS"))


def test_load_instruments_keys_by_exchange_and_symbol():
    inst = load_instruments(FIXTURE)
    r = inst[("NSE", "RELIANCE")]
    assert r.exchange_token == "2885"
    assert r.lot_size == 1
    assert r.tick_size == 0.05
    assert r.buy_allowed and r.sell_allowed
    assert inst[("BSE", "RELIANCE")].exchange_token == "500325"


def test_resolve_universe_drops_missing_and_untradeable():
    inst = load_instruments(FIXTURE)
    u = Universe(exchange="NSE", symbols=("RELIANCE", "TCS", "SUSPENDED", "NOSUCH", "NIFTY26SEPFUT"))
    resolved, dropped = resolve_universe(u, inst)
    assert sorted(resolved) == ["RELIANCE", "TCS"]
    assert dict(dropped) == {
        "SUSPENDED": "not_tradeable",
        "NOSUCH": "not_found",
        "NIFTY26SEPFUT": "not_cash_equity",
    }


HEADER = FIXTURE.read_text().splitlines()[0]


def test_load_universe_normalises_and_dedupes(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: nse\nsymbols:\n  - reliance\n  - RELIANCE\n  - ' TCS '\n  - RELIANCE\n")
    assert load_universe(p) == Universe(exchange="NSE", symbols=("RELIANCE", "TCS"))


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "exchange: NSE\n", "exchange: NSE\nsymbols: []\n"])
def test_load_universe_rejects_malformed(tmp_path, text):
    p = tmp_path / "universe.yaml"
    p.write_text(text)
    with pytest.raises(ValueError):
        load_universe(p)


def test_load_universe_as_of_is_not_silently_ignored(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    with pytest.raises(NotImplementedError):
        load_universe(p, as_of=date(2024, 1, 1))


def test_load_instruments_missing_column_is_clear_error(tmp_path):
    p = tmp_path / "i.csv"
    p.write_text(HEADER.replace("buy_allowed,", "is_buy_allowed,") + "\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_instruments(p)


def test_load_instruments_tolerates_bom_and_empty_numeric_cells(tmp_path):
    p = tmp_path / "i.csv"
    row = "NSE,1,X,NSE-X,X Co,EQ,CASH,EQ,IN,,,,,,,,0,1,1,K"  # empty lot_size and tick_size
    p.write_bytes(("\ufeff" + HEADER + "\n" + row + "\n").encode("utf-8"))
    inst = load_instruments(p)[("NSE", "X")]
    assert inst.lot_size == 1 and inst.tick_size == 0.05


def test_load_instruments_bad_numeric_names_symbol(tmp_path):
    p = tmp_path / "i.csv"
    row = "NSE,1,BADLOT,NSE-BADLOT,X Co,EQ,CASH,EQ,IN,,,,,N/A,0.05,,0,1,1,K"
    p.write_text(HEADER + "\n" + row + "\n")
    with pytest.raises(ValueError, match="BADLOT"):
        load_instruments(p)


def test_empty_tradeability_cell_fails_closed(tmp_path):
    p = tmp_path / "i.csv"
    row = "NSE,1,X,NSE-X,X Co,EQ,CASH,EQ,IN,,,,,1,0.05,,0,,1,K"
    p.write_text(HEADER + "\n" + row + "\n")
    _, dropped = resolve_universe(Universe("NSE", ("X",)), load_instruments(p))
    assert dropped == [("X", "not_tradeable")]


def test_download_is_atomic_and_validated(tmp_path, monkeypatch):
    dest = tmp_path / "data" / "instruments.csv"
    dest.parent.mkdir()
    dest.write_text("old-good-cache")

    class Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(instruments_mod.requests, "get", lambda url, timeout: Resp(b"<html>blocked</html>"))
    with pytest.raises(ValueError, match="does not look like"):
        download_instruments(dest)
    assert dest.read_text() == "old-good-cache"
    assert not dest.with_suffix(".csv.tmp").exists()

    monkeypatch.setattr(instruments_mod.requests, "get", lambda url, timeout: Resp(FIXTURE.read_bytes()))
    download_instruments(dest)
    assert ("NSE", "RELIANCE") in load_instruments(dest)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_universe_instruments.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Write `src/tradebot/data/universe.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Union

import yaml


@dataclass(frozen=True)
class Universe:
    exchange: str
    symbols: tuple  # of upper-case trading symbols, de-duplicated, order preserved


def load_universe(path: Union[str, Path], as_of: Optional[date] = None) -> Universe:
    """Load universe.yaml.

    `as_of` is the hook for point-in-time constituents (spec 4.1). It is not implemented,
    so passing it raises rather than silently returning today's list.
    """
    if as_of is not None:
        raise NotImplementedError("point-in-time constituents are not implemented; omit as_of")
    p = Path(path)
    if not p.exists():
        raise ValueError(f"universe file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict) or "exchange" not in raw or "symbols" not in raw:
        raise ValueError(f"{p}: expected a mapping with 'exchange' and 'symbols' keys")
    if not isinstance(raw["symbols"], list) or not raw["symbols"]:
        raise ValueError(f"{p}: 'symbols' must be a non-empty list")
    seen: dict = {}
    for s in raw["symbols"]:
        sym = str(s).strip().upper()
        if sym:
            seen.setdefault(sym, None)
    return Universe(exchange=str(raw["exchange"]).strip().upper(), symbols=tuple(seen))
```

- [ ] **Step 5: Write `src/tradebot/data/instruments.py`**

```python
"""Groww instrument master: download, parse, and resolve universe symbols."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import requests

from tradebot.data.universe import Universe

INSTRUMENTS_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
REQUIRED_COLUMNS = frozenset({
    "exchange", "exchange_token", "trading_symbol", "segment", "instrument_type",
    "lot_size", "tick_size", "buy_allowed", "sell_allowed",
})


@dataclass(frozen=True)
class Instrument:
    exchange: str
    trading_symbol: str
    exchange_token: str
    segment: str
    instrument_type: str
    lot_size: int
    tick_size: float
    buy_allowed: bool
    sell_allowed: bool


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "y", "yes")


def download_instruments(dest: Union[str, Path], url: str = INSTRUMENTS_URL) -> Path:
    """Download the master atomically: a failed or truncated download never replaces a good cache."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    head = resp.content[:4096].decode("utf-8-sig", errors="replace").splitlines()[:1]
    if not head or "trading_symbol" not in head[0]:
        raise ValueError(f"instrument download from {url} does not look like the instrument CSV")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    os.replace(tmp, dest)
    return dest


def _num(row: dict, col: str, cast, default, path: Path):
    raw = (row.get(col) or "").strip()
    if raw == "":
        return default
    try:
        return cast(float(raw))
    except ValueError as e:
        raise ValueError(f"{path}: bad {col}={raw!r} for {row.get('trading_symbol')!r}") from e


def load_instruments(path: Union[str, Path]) -> dict:
    """Return {(exchange, trading_symbol): Instrument}. Fails loudly on schema drift."""
    path = Path(path)
    out: dict = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: instrument CSV missing columns {sorted(missing)}")
        for row in reader:
            inst = Instrument(
                exchange=row["exchange"].strip().upper(),
                trading_symbol=row["trading_symbol"].strip().upper(),
                exchange_token=row["exchange_token"].strip(),
                segment=row["segment"].strip().upper(),
                instrument_type=row["instrument_type"].strip().upper(),
                lot_size=_num(row, "lot_size", int, 1, path),
                tick_size=_num(row, "tick_size", float, 0.05, path),
                buy_allowed=_truthy(row["buy_allowed"]),    # empty cell => not tradeable (fail closed)
                sell_allowed=_truthy(row["sell_allowed"]),
            )
            out[(inst.exchange, inst.trading_symbol)] = inst
    return out


def resolve_universe(universe: Universe, instruments: dict) -> tuple:
    """Return (resolved symbol -> Instrument, [(symbol, drop_reason)])."""
    resolved: dict = {}
    dropped: list = []
    for sym in universe.symbols:
        inst = instruments.get((universe.exchange, sym))
        if inst is None:
            dropped.append((sym, "not_found"))
        elif inst.segment != "CASH" or inst.instrument_type != "EQ":
            dropped.append((sym, "not_cash_equity"))
        elif not (inst.buy_allowed and inst.sell_allowed):
            dropped.append((sym, "not_tradeable"))
        else:
            resolved[sym] = inst
    return resolved, dropped
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_universe_instruments.py -v`
Expected: 14 passed

- [ ] **Step 7: Write `universe.yaml` (NIFTY 50 starter; run the script in Step 8 to expand to NIFTY 200)**

```yaml
# Survivorship bias: this is today's constituent list. Backtests over past
# months overstate results. See spec section 4.1.
exchange: NSE
symbols:
  - ADANIENT
  - ADANIPORTS
  - APOLLOHOSP
  - ASIANPAINT
  - AXISBANK
  - BAJAJ-AUTO
  - BAJFINANCE
  - BAJAJFINSV
  - BEL
  - BPCL
  - BHARTIARTL
  - BRITANNIA
  - CIPLA
  - COALINDIA
  - DRREDDY
  - EICHERMOT
  - GRASIM
  - HCLTECH
  - HDFCBANK
  - HDFCLIFE
  - HEROMOTOCO
  - HINDALCO
  - HINDUNILVR
  - ICICIBANK
  - ITC
  - INDUSINDBK
  - INFY
  - JSWSTEEL
  - KOTAKBANK
  - LT
  - M&M
  - MARUTI
  - NTPC
  - NESTLEIND
  - ONGC
  - POWERGRID
  - RELIANCE
  - SBILIFE
  - SHRIRAMFIN
  - SBIN
  - SUNPHARMA
  - TCS
  - TATACONSUM
  - TATAMOTORS
  - TATASTEEL
  - TECHM
  - TITAN
  - TRENT
  - ULTRACEMCO
  - WIPRO
```

- [ ] **Step 8: Write `scripts/update_universe.py`**

```python
"""Rewrite universe.yaml from NSE's official NIFTY 200 constituent CSV.

Usage: .venv/bin/python scripts/update_universe.py [--index nifty200]
"""
import argparse
import csv
import io
import os
import sys

import requests
import yaml

URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="nifty200", choices=URLS)
    ap.add_argument("--out", default="universe.yaml")
    args = ap.parse_args()
    try:
        resp = requests.get(URLS[args.index], headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"download failed: {e}", file=sys.stderr)
        return 1
    resp.encoding = "utf-8-sig"
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    symbols = sorted(r["Symbol"].strip() for r in rows if r.get("Symbol"))
    if not symbols:
        print("no symbols parsed; NSE may have blocked the request", file=sys.stderr)
        return 1
    header = ("# Survivorship bias: this is today's constituent list. Backtests over past\n"
              "# months overstate results. See spec section 4.1.\n")
    body = header + yaml.safe_dump({"exchange": "NSE", "symbols": symbols}, sort_keys=False)
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, args.out)  # never leave universe.yaml half-written
    print(f"wrote {len(symbols)} symbols to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 9: Commit**

```bash
git add src/tradebot/data/universe.py src/tradebot/data/instruments.py universe.yaml scripts/update_universe.py tests/fixtures/instrument_sample.csv tests/test_universe_instruments.py
git commit -m "feat: universe loading and instrument master resolution"
```

---

### Task 7: Groww adapter (historical) and incremental fetch

**Files:**
- Create: `src/tradebot/execution/groww_adapter.py`
- Create: `src/tradebot/data/historical.py`
- Test: `tests/test_historical.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_historical.py
import pytest

from tradebot.data.historical import CHUNK_DAYS, HistoricalSource, fetch_incremental
from tradebot.execution.groww_adapter import (GrowwAdapter, candle_interval_name, groww_symbol, parse_candles,
                                              with_retry)
from tradebot.types import Candle

DAY = 86400


def test_parse_candles_accepts_epoch_seconds_millis_and_iso():
    resp = {"candles": [
        [1789357500, 100, 101, 99, 100.5, 1000],
        [1789357800000, "100.5", "102", "100", "101", "2000"],
        ["2026-09-14T09:25:00+05:30", 101, 101, 101, 101, 0],
    ]}
    out = parse_candles("RELIANCE", resp)
    assert [c.ts for c in out] == [1789357500, 1789357800, 1789358100]
    assert out[1].volume == 2000
    assert out[1].close == 101.0
    assert all(c.symbol == "RELIANCE" and c.source == "official" for c in out)


def test_parse_candles_respects_non_ist_offsets_and_z_suffix():
    resp = {"candles": [
        ["2026-09-14T03:45:00+00:00", 1, 1, 1, 1, 1],   # 09:15 IST expressed in UTC
        ["2026-09-14T03:45:00Z", 1, 1, 1, 1, 1],
        ["2026-09-14T09:15:00", 1, 1, 1, 1, 1],         # naive => IST
    ]}
    assert [c.ts for c in parse_candles("X", resp)] == [1789357500] * 3


def test_parse_candles_v2_rows_with_open_interest():
    resp = {"candles": [["2026-09-14T09:15:00", 100, 101, 99, 100.5, 1000, None]]}
    out = parse_candles("RELIANCE", resp)
    assert out[0].ts == 1789357500 and out[0].volume == 1000


def test_parse_candles_malformed_row_names_symbol():
    with pytest.raises(ValueError, match="RELIANCE"):
        parse_candles("RELIANCE", {"candles": [[1789357500, 1, 2]]})
    with pytest.raises(ValueError, match="RELIANCE"):
        parse_candles("RELIANCE", {"candles": [{"ts": 1}]})


def test_parse_candles_rejects_non_finite_or_inconsistent_ohlc():
    with pytest.raises(ValueError, match="bad OHLC"):
        parse_candles("X", {"candles": [[1, float("nan"), 2, 0.5, 1.5, 10]]})
    with pytest.raises(ValueError, match="bad OHLC"):
        parse_candles("X", {"candles": [[1, 1, 2, 0.5, 5.0, 10]]})  # close above high


def test_parse_candles_empty():
    assert parse_candles("X", {}) == []
    assert parse_candles("X", {"candles": None}) == []


def test_interval_names_and_groww_symbol():
    assert candle_interval_name(5) == "5minute"
    assert candle_interval_name(1440) == "1day"
    assert groww_symbol("NSE", "RELIANCE") == "NSE-RELIANCE"
    with pytest.raises(ValueError):
        candle_interval_name(7)


def test_with_retry_retries_with_backoff_then_succeeds():
    calls, delays = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("rate limit")
        return "ok"

    assert with_retry(fn, attempts=3, sleep=delays.append) == "ok"
    assert len(calls) == 3
    assert delays == [1.0, 2.0]


def test_with_retry_gives_up_after_attempts():
    def fn():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        with_retry(fn, attempts=2, sleep=lambda s: None)
    with pytest.raises(ValueError):
        with_retry(fn, attempts=0)


@pytest.mark.parametrize("exc_factory", [
    lambda: type("GrowwAPIAuthenticationException", (Exception,), {})("bad totp"),
    lambda: type("GrowwAPINotFoundException", (Exception,), {})("no such symbol"),
    # The SDK raises the generic class with a code for {"status": "FAILURE"} bodies.
    lambda: type("GrowwAPIException", (Exception,), {"code": "401"})("unauthorised"),
    lambda: type("GrowwAPIException", (Exception,), {"code": 403})("forbidden"),
])
def test_with_retry_never_retries_client_errors(exc_factory):
    calls = []

    def fn():
        calls.append(1)
        raise exc_factory()

    with pytest.raises(Exception):
        with_retry(fn, attempts=3, sleep=lambda s: None)
    assert len(calls) == 1


def test_with_retry_does_retry_generic_server_errors():
    calls = []

    def fn():
        calls.append(1)
        raise type("GrowwAPIException", (Exception,), {"code": "500"})("server")

    with pytest.raises(Exception):
        with_retry(fn, attempts=2, sleep=lambda s: None)
    assert len(calls) == 2


def test_fetch_candles_calls_v2_endpoint_with_ist_strings():
    calls = []

    class FakeClient:
        SEGMENT_CASH = "CASH"

        def get_historical_candles(self, **kw):
            calls.append(kw)
            return {"candles": [["2026-09-14T09:15:00", 1, 2, 0.5, 1.5, 10, None]]}

    adapter = GrowwAdapter("key", "secret")
    adapter._client = FakeClient()  # bypass TOTP auth
    out = adapter.fetch_candles("RELIANCE", "NSE", 1789357500, 1789357500 + 3600, 5)
    assert calls[0]["groww_symbol"] == "NSE-RELIANCE"
    assert calls[0]["candle_interval"] == "5minute"
    assert calls[0]["start_time"] == "2026-09-14 09:15:00"
    assert calls[0]["end_time"] == "2026-09-14 10:15:00"
    assert calls[0]["segment"] == "CASH"
    assert out[0].ts == 1789357500


def test_fetch_candles_validates_interval_before_any_call():
    adapter = GrowwAdapter("key", "secret")
    adapter._client = object()  # would explode if called
    with pytest.raises(ValueError):
        adapter.fetch_candles("RELIANCE", "NSE", 0, 1, 7)


def test_adapter_requires_credentials():
    with pytest.raises(ValueError):
        GrowwAdapter("", "")


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, symbol, exchange, start_ts, end_ts, interval):
        self.calls.append((symbol, start_ts, end_ts))
        # one candle per day boundary inside the window (both ends inclusive)
        first_day = (start_ts // DAY) * DAY
        return [Candle(symbol, t, 1, 2, 0.5, 1.5, 10)
                for t in range(first_day, end_ts + 1, DAY) if start_ts <= t <= end_ts]


def test_fetch_incremental_first_run_chunks_the_lookback(repo):
    now = 100 * DAY  # exactly on a bar boundary: the bar opening now is in progress, excluded
    f = FakeFetcher()
    result = fetch_incremental(repo, f, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now)
    starts = [c[1] for c in f.calls]
    assert starts[0] == now - 30 * DAY
    assert len(f.calls) == 2  # 30-day lookback at 15-day chunks
    assert f.calls[0][2] == starts[0] + CHUNK_DAYS[5] * DAY
    assert f.calls[1][1] == f.calls[0][2]  # windows share the boundary instant: no gap either way
    assert result["RELIANCE"] == 30  # days 70..99; day 100 belongs to the in-progress bar
    assert repo.latest_candle_ts("RELIANCE", 5) == now - DAY


def test_fetch_incremental_second_run_resumes_from_latest_and_is_idempotent(repo):
    now = 100 * DAY
    f = FakeFetcher()
    fetch_incremental(repo, f, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now)
    f2 = FakeFetcher()
    result = fetch_incremental(repo, f2, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now + 2 * DAY)
    assert f2.calls[0][1] == (now - DAY) + 1
    assert result["RELIANCE"] == 2
    f3 = FakeFetcher()
    assert fetch_incremental(repo, f3, ["RELIANCE"], "NSE", 5, 30, now + 2 * DAY)["RELIANCE"] == 0


def test_fetch_incremental_full_refetches_and_repairs(repo):
    now = 100 * DAY
    fetch_incremental(repo, FakeFetcher(), ["RELIANCE"], "NSE", 5, 30, now)
    repo.conn.execute("UPDATE candles SET c = 999 WHERE symbol='RELIANCE' AND ts=?", (90 * DAY,))
    repo.conn.commit()
    f = FakeFetcher()
    result = fetch_incremental(repo, f, ["RELIANCE"], "NSE", 5, 30, now, full=True)
    assert f.calls[0][1] == now - 30 * DAY
    assert result["RELIANCE"] == 30
    assert repo.load_candles(["RELIANCE"], 5, 90 * DAY, 90 * DAY)[0].close == 1.5


def test_fetch_incremental_ignores_out_of_window_candles_and_empty_symbols(repo):
    def stray(symbol, exchange, start_ts, end_ts, interval):
        return [Candle(symbol, end_ts + 5 * DAY, 1, 1, 1, 1, 1)] if symbol == "A" else []

    result = fetch_incremental(repo, stray, ["A", "B"], "NSE", 5, 30, 100 * DAY)
    assert result == {"A": 0, "B": 0}
    assert repo.latest_candle_ts("A", 5) is None


def test_fetch_incremental_rejects_unknown_interval(repo):
    with pytest.raises(ValueError):
        fetch_incremental(repo, FakeFetcher(), ["A"], "NSE", 7, 30, 100 * DAY)


def test_historical_source_groups_by_bar(repo):
    repo.insert_candles([
        Candle("A", 100, 1, 1, 1, 1, 1), Candle("B", 100, 2, 2, 2, 2, 1),
        Candle("A", 400, 1, 1, 1, 1, 1),
    ], interval=5)
    src = HistoricalSource.from_repo(repo, ["A", "B"], interval=5, start_ts=0, end_ts=1000)
    assert src.bar_timestamps() == [100, 400]
    assert set(src.candles_at(100)) == {"A", "B"}
    assert set(src.candles_at(400)) == {"A"}
    assert src.candles_at(999) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_historical.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/execution/groww_adapter.py`**

```python
"""The only module that imports growwapi. Plan 1 needs auth + historical candles only.
Order, feed, position, and margin methods are added in Plan 3."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tradebot.engine.clock import IST, to_ist
from tradebot.types import Candle

# Errors that must never be retried (spec 11): auth, authorisation, bad request, not found.
# The SDK raises the generic GrowwAPIException for Groww's {"status": "FAILURE"} bodies, so
# we match on the HTTP-style error code as well as the exception class name.
NO_RETRY_MARKERS = ("Authentication", "Authorisation", "Authorization", "BadRequest", "NotFound")
NO_RETRY_CODES = {"400", "401", "403", "404"}


def is_non_retryable(e: BaseException) -> bool:
    name = type(e).__name__
    if any(m in name for m in NO_RETRY_MARKERS):
        return True
    code = getattr(e, "code", None)
    return code is not None and str(code) in NO_RETRY_CODES


def with_retry(fn: Callable[[], Any], attempts: int = 3, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> Any:
    """Exponential backoff (1s, 2s, ...). Non-retryable errors propagate on the first attempt."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - SDK exceptions vary; classified by is_non_retryable
            if is_non_retryable(e) or i == attempts - 1:
                raise
            sleep(base_delay * (2 ** i))
    raise AssertionError("unreachable")


def _to_epoch(v: Any) -> int:
    if isinstance(v, (int, float)):
        v = int(v)
        return v // 1000 if v > 10_000_000_000 else v
    s = str(v).strip()
    if s.isdigit():
        return _to_epoch(int(s))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"  # 3.9's fromisoformat does not accept a Z suffix
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # Groww returns naive IST wall-clock strings
    return int(dt.timestamp())


def parse_candles(symbol: str, resp: dict | None) -> list[Candle]:
    """Rows are [ts, o, h, l, c, volume, ...]; V2 appends open interest, which is ignored."""
    rows = (resp or {}).get("candles") or []
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            raise ValueError(f"{symbol}: malformed candle row {r!r}")
        o, h, l, c = (float(r[i]) for i in (1, 2, 3, 4))
        if not all(math.isfinite(x) for x in (o, h, l, c)) or not (l <= min(o, c) and max(o, c) <= h):
            raise ValueError(f"{symbol}: bad OHLC in candle row {r!r}")
        out.append(Candle(symbol, _to_epoch(r[0]), o, h, l, c, int(float(r[5] or 0))))
    return out


def _fmt_ist(ts: int) -> str:
    return to_ist(ts).strftime("%Y-%m-%d %H:%M:%S")


# Interval in minutes -> Groww V2 candle_interval string (matches GrowwAPI.CANDLE_INTERVAL_* constants).
_INTERVAL_NAMES = {1: "1minute", 2: "2minute", 3: "3minute", 5: "5minute", 10: "10minute", 15: "15minute",
                   30: "30minute", 60: "1hour", 240: "4hour", 1440: "1day", 10080: "1week"}


def candle_interval_name(interval_minutes: int) -> str:
    try:
        return _INTERVAL_NAMES[interval_minutes]
    except KeyError as e:
        raise ValueError(f"unsupported candle interval: {interval_minutes} minutes") from e


def groww_symbol(exchange: str, trading_symbol: str) -> str:
    """Groww's symbol form for cash equities, e.g. NSE-RELIANCE."""
    return f"{exchange}-{trading_symbol}"


class GrowwAdapter:
    def __init__(self, api_key: str, totp_secret: str):
        if not api_key or not totp_secret:
            raise ValueError("GROWW_API_KEY and GROWW_TOTP_SECRET must be set in .env")
        self._api_key = api_key
        self._totp_secret = totp_secret
        self._client = None

    @property
    def client(self):
        """Authenticate lazily, once. A failed auth raises; never loop on it."""
        if self._client is None:
            import pyotp
            from growwapi import GrowwAPI

            totp = pyotp.TOTP(self._totp_secret).now()
            token = GrowwAPI.get_access_token(api_key=self._api_key, totp=totp)
            self._client = GrowwAPI(token)
        return self._client

    def fetch_candles(self, symbol: str, exchange: str, start_ts: int, end_ts: int, interval: int) -> list[Candle]:
        """Cash-segment candles via the V2 endpoint (get_historical_candle_data is deprecated).

        V2 rows are [iso_timestamp, o, h, l, c, volume, open_interest]; parse_candles reads the
        first six and treats naive timestamps as IST.
        """
        client = self.client  # auth happens here, outside the retry loop, so it is never retried
        interval_name = candle_interval_name(interval)
        gsym = groww_symbol(exchange, symbol)

        def call():
            return client.get_historical_candles(
                exchange=exchange,
                segment=client.SEGMENT_CASH,
                groww_symbol=gsym,
                start_time=_fmt_ist(start_ts),
                end_time=_fmt_ist(end_ts),
                candle_interval=interval_name,
                timeout=30,
            )

        return parse_candles(symbol, with_retry(call))
```

- [ ] **Step 4: Write `src/tradebot/data/historical.py`**

```python
"""Incremental candle download into SQLite, and the backtest candle source."""
from __future__ import annotations

from collections.abc import Callable

from tradebot.store.repo import Repo
from tradebot.types import Candle

# Request window per interval in minutes: half of Groww's documented maximum for
# get_historical_candles (1-5m: 30 days, 10-30m: 90 days, 1h+: 180 days), so a
# window that is inclusive on both ends can never trip the limit (spec 4.2).
CHUNK_DAYS = {1: 15, 2: 15, 3: 15, 5: 15, 10: 45, 15: 45, 30: 45, 60: 90, 240: 90, 1440: 90}
DAY = 86400

Fetcher = Callable[[str, str, int, int, int], list[Candle]]  # (symbol, exchange, start, end, interval)


def fetch_incremental(repo: Repo, fetcher: Fetcher, symbols: list[str], exchange: str, interval: int,
                      lookback_days: int, now_ts: int, full: bool = False,
                      log: Callable[[str], None] = lambda s: None) -> dict[str, int]:
    """For each symbol, fetch from (latest stored ts + 1) or (now - lookback) up to the last
    COMPLETED bar before now_ts, so an in-progress candle is never stored.

    Idempotent: inserts use INSERT OR IGNORE on (symbol, ts, interval). `full` deletes the window
    first so a refetch repairs bad rows. Consecutive windows share their boundary instant, so the
    result is the same whether Groww treats end_time as inclusive or exclusive. Returns inserted counts.
    """
    if interval not in CHUNK_DAYS:
        raise ValueError(f"unsupported candle interval: {interval} minutes")
    chunk = CHUNK_DAYS[interval] * DAY
    interval_sec = interval * 60
    limit = now_ts - (now_ts % interval_sec) - 1  # last instant that belongs to a completed bar
    inserted: dict[str, int] = {}
    for sym in symbols:
        latest = None if full else repo.latest_candle_ts(sym, interval)
        start = now_ts - lookback_days * DAY if latest is None else latest + 1
        if full:
            repo.delete_candles(sym, interval, start, limit)
        n = 0
        while start < limit:
            end = min(start + chunk, limit)
            candles = [c for c in fetcher(sym, exchange, start, end, interval) if start <= c.ts <= end]
            n += repo.insert_candles(candles, interval)
            start = end
        inserted[sym] = n
        log(f"{sym}: +{n} candles")
    return inserted


class HistoricalSource:
    """Replays stored candles one bar at a time. bar_timestamps() is sorted ascending."""

    def __init__(self, candles: list[Candle]):
        self._by_ts: dict[int, dict[str, Candle]] = {}
        for c in candles:
            self._by_ts.setdefault(c.ts, {})[c.symbol] = c
        self._ts = sorted(self._by_ts)

    @classmethod
    def from_repo(cls, repo: Repo, symbols: list[str], interval: int, start_ts: int, end_ts: int) -> "HistoricalSource":
        return cls(repo.load_candles(symbols, interval, start_ts, end_ts))

    def bar_timestamps(self) -> list[int]:
        return list(self._ts)

    def candles_at(self, ts: int) -> dict[str, Candle]:
        return dict(self._by_ts.get(ts, {}))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_historical.py -v`
Expected: 23 passed

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/execution/groww_adapter.py src/tradebot/data/historical.py tests/test_historical.py
git commit -m "feat: groww historical adapter and incremental fetch"
```

---

### Task 8: Incremental indicators

**Files:**
- Create: `src/tradebot/strategy/indicators.py`
- Test: `tests/test_indicators.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from tradebot.strategy.indicators import ATR, EMA, RSI
from tradebot.types import Candle


def test_ema_seeds_with_sma_then_smooths():
    e = EMA(3)
    assert e.update(1) is None and not e.ready
    assert e.update(2) is None
    assert e.update(3) == pytest.approx(2.0) and e.ready
    assert e.update(4) == pytest.approx(3.0)  # k=0.5: 4*0.5 + 2*0.5


def test_rsi_all_gains_is_100():
    r = RSI(3)
    for x in (1, 2, 3, 4):
        v = r.update(x)
    assert v == pytest.approx(100.0) and r.ready


def test_rsi_alternating_is_50():
    r = RSI(4)
    for x in (10, 11, 10, 11, 10):
        v = r.update(x)
    assert v == pytest.approx(50.0)


def test_rsi_not_ready_before_period_changes():
    r = RSI(3)
    assert r.update(1) is None
    assert r.update(2) is None
    assert r.update(3) is None
    assert not r.ready
    assert r.update(4) is not None


def _c(o, h, l, c, ts=0):
    return Candle("X", ts, o, h, l, c, 1)


def test_atr_constant_range_no_gaps():
    a = ATR(2)
    assert a.update(_c(10, 11, 9, 10)) is None      # first TR = high - low; ATR(2) needs two of them
    assert a.update(_c(10, 11, 9, 10)) == pytest.approx(2.0)
    assert a.update(_c(10, 11, 9, 10)) == pytest.approx(2.0) and a.ready


def test_atr_uses_gap_from_prev_close():
    a = ATR(1)
    a.update(_c(10, 11, 9, 10))
    # gap up: prev close 10, low 12 => TR = max(13-12, |13-10|, |12-10|) = 3
    assert a.update(_c(12, 13, 12, 13)) == pytest.approx(3.0)


# Wilder's worked example (StockCharts): 33 closes, RSI(14) first value 70.46.
STOCKCHARTS = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
               46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18,
               44.22, 44.57, 43.42, 42.66, 43.13]


def test_rsi_matches_wilder_reference_series():
    r = RSI(14)
    values = [v for v in (r.update(x) for x in STOCKCHARTS) if v is not None]
    assert values[:6] == pytest.approx([70.4641, 66.2496, 66.4809, 69.3469, 66.2947, 57.9150], abs=1e-3)


def test_rsi_flat_series_is_50_not_100():
    r = RSI(3)
    for _ in range(6):
        v = r.update(10.0)
    assert v == 50.0


@pytest.mark.parametrize("cls", [EMA, RSI, ATR])
@pytest.mark.parametrize("period", [0, -1, 2.5])
def test_invalid_period_rejected(cls, period):
    with pytest.raises(ValueError):
        cls(period)


def test_non_finite_input_fails_loud_instead_of_poisoning():
    e = EMA(2)
    e.update(1.0)
    with pytest.raises(ValueError):
        e.update(float("nan"))
    with pytest.raises(ValueError):
        RSI(2).update(float("inf"))
    with pytest.raises(ValueError):
        ATR(2).update(_c(1, float("nan"), 1, 1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_indicators.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/strategy/indicators.py`**

```python
"""Incremental (streaming) indicators. Each update() consumes one bar and returns the
current value, or None until warm-up completes. No lookahead: only past inputs are used.

Inputs must be finite: a NaN would otherwise poison the state forever while `ready`
still reports True, silently disabling the symbol. We fail loud instead."""
from __future__ import annotations

import math

from tradebot.types import Candle


def _check_period(period: int) -> int:
    if not isinstance(period, int) or period < 1:
        raise ValueError(f"period must be an integer >= 1, got {period!r}")
    return period


def _check_finite(name: str, x: float) -> float:
    if not math.isfinite(x):
        raise ValueError(f"{name}: non-finite input {x!r}")
    return x


class EMA:
    def __init__(self, period: int):
        self.period = _check_period(period)
        self.k = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        _check_finite("EMA", x)
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = x * self.k + self.value * (1 - self.k)
        return self.value


class RSI:
    """Wilder's RSI. Seeded with a simple average of the first `period` changes.
    A perfectly flat series (no gains, no losses) reads 50, not 100."""

    def __init__(self, period: int):
        self.period = _check_period(period)
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, close: float) -> float | None:
        _check_finite("RSI", close)
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self.avg_gain = sum(self._gains) / self.period
            self.avg_loss = sum(self._losses) / self.period
        else:
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        if self.avg_gain == 0 and self.avg_loss == 0:
            self.value = 50.0
        elif self.avg_loss == 0:
            self.value = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder's ATR. The first bar's true range is high-low (no previous close)."""

    def __init__(self, period: int):
        self.period = _check_period(period)
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, candle: Candle) -> float | None:
        for name, x in (("high", candle.high), ("low", candle.low), ("close", candle.close)):
            _check_finite(f"ATR.{name}", x)
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(candle.high - candle.low, abs(candle.high - self._prev_close), abs(candle.low - self._prev_close))
        self._prev_close = candle.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_indicators.py -v`
Expected: 17 passed. If `test_atr_constant_range_no_gaps` fails on the first assertion, the implementation is wrong, not the test: ATR(2) needs two true ranges before it is ready, and the first bar contributes `high - low`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/strategy/indicators.py tests/test_indicators.py
git commit -m "feat: streaming EMA, RSI, ATR"
```

---

### Task 9: Strategy base and EMA/RSI strategy

> Note from the Task 8 review: after a long run of identical closes RSI pins at exactly 100 or 0, so the first small tick afterwards can satisfy the 45/55 thresholds. Illiquid symbols will produce such runs from tick-built bars. A staleness guard (skip signals when the last N closes are identical) belongs in the live bar builder or a later strategy revision, not in this task.

**Files:**
- Create: `src/tradebot/strategy/base.py`
- Create: `src/tradebot/strategy/ema_rsi.py`
- Test: `tests/test_ema_rsi.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from tradebot.strategy.ema_rsi import EmaRsiStrategy, build_strategy
from tradebot.types import Candle

PARAMS = dict(fast=3, slow=5, rsi_period=3, rsi_long_min=55, rsi_short_max=45,
              atr_period=3, atr_stop_mult=1.0, reward_risk=2.0, product="MIS")


def _candles(closes, symbol="X"):
    return [Candle(symbol, 1000 + i * 300, c, c + 0.5, c - 0.5, c, 100) for i, c in enumerate(closes)]


def _run(strategy, candles):
    return [(c, strategy.on_candle(c)) for c in candles]


def test_warmup_yields_no_signals_and_not_ready():
    s = EmaRsiStrategy(PARAMS)
    out = _run(s, _candles([10, 9, 8, 7]))
    assert all(sig is None for _, sig in out)
    assert not s.is_ready("X")


def test_bullish_cross_with_strong_rsi_gives_one_long():
    s = EmaRsiStrategy(PARAMS)
    closes = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    sigs = [(c, sig) for c, sig in _run(s, _candles(closes)) if sig]
    assert len(sigs) == 1
    c, sig = sigs[0]
    assert sig.direction == "LONG"
    assert sig.symbol == "X" and sig.strategy == "ema_rsi" and sig.product == "MIS"
    assert sig.bar_ts == c.ts
    assert sig.entry_price == c.close
    assert sig.stop_price < sig.entry_price < sig.target_price
    risk = sig.entry_price - sig.stop_price
    assert sig.target_price == pytest.approx(sig.entry_price + 2 * risk, abs=0.05)
    assert s.is_ready("X")


def test_bearish_cross_with_weak_rsi_gives_one_short():
    s = EmaRsiStrategy(PARAMS)
    closes = [20, 21, 22, 23, 24, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16]
    sigs = [sig for _, sig in _run(s, _candles(closes)) if sig]
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.direction == "SHORT"
    assert sig.target_price < sig.entry_price < sig.stop_price


def test_symbols_are_independent():
    s = EmaRsiStrategy(PARAMS)
    closes = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12]
    a = [sig for _, sig in _run(s, _candles(closes, "A")) if sig]
    b = [sig for _, sig in _run(s, _candles(closes, "B")) if sig]
    assert len(a) == 1 and len(b) == 1
    assert a[0].symbol == "A" and b[0].symbol == "B"


def test_recompute_reproduces_state():
    closes = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10]
    live = EmaRsiStrategy(PARAMS)
    _run(live, _candles(closes))
    rebuilt = EmaRsiStrategy(PARAMS)
    rebuilt.recompute("X", _candles(closes))
    assert live.snapshot("X") == pytest.approx(rebuilt.snapshot("X"))
    assert set(live.snapshot("X")) == {"ema_fast", "ema_slow", "rsi", "atr"}


def test_rsi_filter_gates_both_directions():
    long_series = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    s = EmaRsiStrategy({**PARAMS, "rsi_long_min": 101, "rsi_short_max": 45})
    assert not [sig for _, sig in _run(s, _candles(long_series)) if sig]
    short_series = [20, 21, 22, 23, 24, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16]
    s = EmaRsiStrategy({**PARAMS, "rsi_long_min": 55, "rsi_short_max": -1})
    assert not [sig for _, sig in _run(s, _candles(short_series)) if sig]


def test_zero_or_sub_tick_atr_emits_nothing():
    # flat bars (high == low == close) then a bullish cross with a near-zero ATR
    flat = [Candle("X", 1000 + i * 300, 100.0, 100.0, 100.0, 100.0, 1) for i in range(8)]
    rise = [Candle("X", 1000 + (8 + i) * 300, c, c, c, c, 1) for i, c in enumerate([100.01, 100.02, 100.03, 100.04])]
    s = EmaRsiStrategy(PARAMS)
    assert not [sig for _, sig in _run(s, flat + rise) if sig]


def test_stop_and_target_are_on_the_correct_side_after_rounding():
    s = EmaRsiStrategy({**PARAMS, "atr_stop_mult": 0.3})
    closes = [10.03, 9.03, 8.03, 7.03, 6.03, 5.03, 6.03, 7.03, 8.03, 9.03, 10.03, 11.03, 12.03]
    sigs = [sig for _, sig in _run(s, _candles(closes)) if sig]
    assert sigs and all(sig.stop_price < sig.entry_price < sig.target_price for sig in sigs)
    for sig in sigs:
        assert round(sig.stop_price * 20) == pytest.approx(sig.stop_price * 20)  # on the 0.05 grid


@pytest.mark.parametrize("bad", [
    {"fast": 5, "slow": 3}, {"reward_risk": 0}, {"reward_risk": -1}, {"atr_stop_mult": 0},
    {"rsi_long_min": 45, "rsi_short_max": 55}, {"product": "NRML"}, {"fast": 2.5}, {"min_stop_pct": 0},
])
def test_invalid_params_rejected(bad):
    with pytest.raises(ValueError):
        EmaRsiStrategy({**PARAMS, **bad})


def test_build_strategy_registry():
    assert isinstance(build_strategy("ema_rsi", PARAMS), EmaRsiStrategy)
    with pytest.raises(ValueError):
        build_strategy("nope", PARAMS)


def test_reset_clears_symbol_state_only():
    s = EmaRsiStrategy(PARAMS)
    _run(s, _candles([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10], "A"))
    _run(s, _candles([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10], "B"))
    s.reset("A")
    assert not s.is_ready("A") and s.is_ready("B")


def test_recompute_rejects_wrong_symbol_candles():
    s = EmaRsiStrategy(PARAMS)
    with pytest.raises(ValueError):
        s.recompute("A", _candles([1, 2, 3], "B"))


def test_strategy_exception_is_not_swallowed():
    s = EmaRsiStrategy(PARAMS)
    with pytest.raises(AttributeError):
        s.on_candle(None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ema_rsi.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/strategy/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from tradebot.types import Candle, Product, Signal


class Strategy(ABC):
    """One instance handles every symbol; state is kept per symbol internally.

    Contract: on_candle is deterministic given the candle sequence for a symbol.
    Exceptions propagate; the engine decides what to do with them.
    """

    name: str
    product: Product

    @abstractmethod
    def on_candle(self, candle: Candle) -> Signal | None: ...

    @abstractmethod
    def is_ready(self, symbol: str) -> bool: ...

    @abstractmethod
    def snapshot(self, symbol: str) -> dict[str, float]:
        """Current indicator values for the AI filter. Empty dict if not ready."""

    @abstractmethod
    def reset(self, symbol: str) -> None: ...

    def recompute(self, symbol: str, candles: Iterable[Candle]) -> None:
        """Rebuild state for a symbol from scratch. Signals produced during replay are discarded."""
        self.reset(symbol)
        for c in candles:
            if c.symbol != symbol:
                raise ValueError(f"recompute({symbol!r}) given a candle for {c.symbol!r}")
            self.on_candle(c)
```

- [ ] **Step 4: Write `src/tradebot/strategy/ema_rsi.py`**

```python
"""EMA crossover confirmed by RSI. Stop = ATR multiple, target = reward:risk multiple."""
from __future__ import annotations

from dataclasses import dataclass

from tradebot.strategy.base import Strategy
from tradebot.strategy.indicators import ATR, EMA, RSI
from tradebot.types import Candle, Signal, round_tick_down, round_tick_up

PRODUCTS = ("MIS", "CNC")


@dataclass
class _State:
    fast: EMA
    slow: EMA
    rsi: RSI
    atr: ATR
    prev_diff: float | None = None


def _int(params: dict, key: str) -> int:
    v = params[key]
    if isinstance(v, bool) or int(v) != v:
        raise ValueError(f"ema_rsi.{key} must be an integer, got {v!r}")
    return int(v)


class EmaRsiStrategy(Strategy):
    name = "ema_rsi"

    def __init__(self, params: dict):
        self.fast_n = _int(params, "fast")
        self.slow_n = _int(params, "slow")
        self.rsi_n = _int(params, "rsi_period")
        self.rsi_long_min = float(params["rsi_long_min"])
        self.rsi_short_max = float(params["rsi_short_max"])
        self.atr_n = _int(params, "atr_period")
        self.atr_mult = float(params["atr_stop_mult"])
        self.rr = float(params["reward_risk"])
        self.min_stop_pct = float(params.get("min_stop_pct", 0.1))  # percent of entry
        self.product = params.get("product", "MIS")
        if self.fast_n >= self.slow_n:
            raise ValueError("ema_rsi.fast must be < ema_rsi.slow")
        if self.atr_mult <= 0 or self.rr <= 0 or self.min_stop_pct <= 0:
            raise ValueError("ema_rsi.atr_stop_mult, reward_risk and min_stop_pct must be > 0")
        if self.rsi_long_min <= self.rsi_short_max:
            raise ValueError("ema_rsi.rsi_long_min must be > ema_rsi.rsi_short_max")
        if self.product not in PRODUCTS:
            raise ValueError(f"ema_rsi.product must be one of {PRODUCTS}, got {self.product!r}")
        self._state: dict[str, _State] = {}

    def _st(self, symbol: str) -> _State:
        if symbol not in self._state:
            self.reset(symbol)
        return self._state[symbol]

    def reset(self, symbol: str) -> None:
        self._state[symbol] = _State(EMA(self.fast_n), EMA(self.slow_n), RSI(self.rsi_n), ATR(self.atr_n))

    def is_ready(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and st.fast.ready and st.slow.ready and st.rsi.ready and st.atr.ready and st.prev_diff is not None)

    def snapshot(self, symbol: str) -> dict[str, float]:
        st = self._state.get(symbol)
        if not st or not (st.fast.ready and st.slow.ready and st.rsi.ready and st.atr.ready):
            return {}
        return {"ema_fast": st.fast.value, "ema_slow": st.slow.value, "rsi": st.rsi.value, "atr": st.atr.value}

    def on_candle(self, candle: Candle) -> Signal | None:
        st = self._st(candle.symbol)
        fast = st.fast.update(candle.close)
        slow = st.slow.update(candle.close)
        rsi = st.rsi.update(candle.close)
        atr = st.atr.update(candle)
        if fast is None or slow is None or rsi is None or atr is None:
            st.prev_diff = None
            return None
        diff = fast - slow
        prev = st.prev_diff
        st.prev_diff = diff
        if prev is None:
            return None
        close = candle.close
        # A stop closer than min_stop_pct of price (or a zero ATR) is noise: slippage alone would
        # exceed it, and sizing would balloon to the margin limit. Emit nothing.
        if atr * self.atr_mult < close * self.min_stop_pct / 100.0:
            return None
        if prev <= 0 < diff and rsi >= self.rsi_long_min:
            stop = round_tick_down(close - atr * self.atr_mult)     # away from entry
            target = round_tick_down(close + (close - stop) * self.rr)  # toward entry
            if not stop < close < target:
                return None
            return Signal(self.name, candle.symbol, "LONG", close, stop, target, self.product, candle.ts)
        if prev >= 0 > diff and rsi <= self.rsi_short_max:
            stop = round_tick_up(close + atr * self.atr_mult)
            target = round_tick_up(close - (stop - close) * self.rr)
            if not target < close < stop:
                return None
            return Signal(self.name, candle.symbol, "SHORT", close, stop, target, self.product, candle.ts)
        return None


def build_strategy(name: str, params: dict) -> Strategy:
    if name == "ema_rsi":
        return EmaRsiStrategy(params)
    raise ValueError(f"unknown strategy: {name}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ema_rsi.py -v`
Expected: 20 passed

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/strategy/base.py src/tradebot/strategy/ema_rsi.py tests/test_ema_rsi.py
git commit -m "feat: strategy base and EMA/RSI strategy"
```

---

### Task 10: Risk engine

**Files:**
- Create: `src/tradebot/risk/killswitch.py`
- Create: `src/tradebot/risk/sizing.py`
- Create: `src/tradebot/risk/engine.py`
- Test: `tests/test_risk.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from tradebot.config import RiskConfig
from tradebot.risk.engine import PortfolioState, evaluate
from tradebot.risk.killswitch import KillState, read_kill_switch
from tradebot.risk.sizing import compute_quantity
from tradebot.types import ApprovedOrder, Rejection, Signal

CFG = RiskConfig(per_trade_pct=1.0, daily_loss_cap_pct=3.0, flatten_on_daily_cap=False,
                 max_entries_per_day=5, max_open_positions=2, cooldown_bars=3, mis_leverage=5.0,
                 adopted_stop_pct=1.5)
OFF = KillState(active=False, flatten=False)


def _sig(direction="LONG", entry=100.0, stop=99.0, product="MIS", symbol="RELIANCE", bar_ts=10_000):
    target = entry + 2 * (entry - stop) if direction == "LONG" else entry - 2 * (stop - entry)
    return Signal("ema_rsi", symbol, direction, entry, stop, target, product, bar_ts)


def _state(**kw):
    base = dict(capital=100_000.0, open_symbols=set(), pending_symbols=set(), realised_today=0.0,
                unrealised=0.0, entries_today=0, cooldown_until={})
    base.update(kw)
    return PortfolioState(**base)


# -- kill switch ---------------------------------------------------------
def test_kill_switch_absent(tmp_path):
    assert read_kill_switch(tmp_path / "KILL") == KillState(False, False)


def test_kill_switch_never_raises_and_fails_closed(tmp_path):
    d = tmp_path / "KILL"
    d.mkdir()
    assert read_kill_switch(d) == KillState(True, False)      # directory: block, don't flatten
    f = tmp_path / "K2"
    f.write_bytes(b"\xff\xfe not utf8")
    assert read_kill_switch(f) == KillState(True, False)      # undecodable: still active
    link = tmp_path / "K3"
    link.symlink_to(tmp_path / "gone")
    assert read_kill_switch(link) == KillState(False, False)  # dangling symlink == absent


def test_kill_switch_flatten_anywhere_in_text(tmp_path):
    (tmp_path / "KILL").write_text("FLATTEN everything now\n")
    assert read_kill_switch(tmp_path / "KILL").flatten


def test_kill_switch_empty_file_blocks_entries_only(tmp_path):
    (tmp_path / "KILL").write_text("")
    assert read_kill_switch(tmp_path / "KILL") == KillState(True, False)


def test_kill_switch_flatten(tmp_path):
    (tmp_path / "KILL").write_text(" Flatten \n")
    assert read_kill_switch(tmp_path / "KILL") == KillState(True, True)


# -- sizing ---------------------------------------------------------------
@pytest.mark.parametrize("capital,pct,entry,stop,margin,lot,expected", [
    (100_000, 1.0, 100.0, 99.0, 500_000, 1, 1000),   # risk-limited: 1000 rs / 1 rs
    (100_000, 1.0, 100.0, 99.0, 50_000, 1, 500),     # capital-limited: 50000 / 100
    (100_000, 1.0, 100.0, 99.0, 500_000, 75, 975),   # rounded down to lot
    (100_000, 1.0, 100.0, 99.0, 5_000, 75, 0),       # 50 < lot => 0
    (100_000, 1.0, 100.0, 100.0, 500_000, 1, 0),     # zero stop distance
    (100_000, 0.5, 250.0, 247.5, 1_000_000, 1, 200), # 500 / 2.5
    (100_000, 0.5, 100.0, 99.8, 1e9, 500, 2500),      # float-exact boundary must not floor to 2000
    (100_000, 1.0, 100.0, 99.0, 0, 1, 0),             # no margin
    (100_000, 1.0, 100.0, 99.0, -5, 1, 0),            # negative margin
    (100_000, 1.0, 100.0, 99.0, 500_000, 5000, 0),    # lot exceeds both candidates
    (100_000, 1.0, float("nan"), 99.0, 500_000, 1, 0),
    (100_000, 1.0, 100.0, 99.0, float("inf"), 1, 0),
])
def test_compute_quantity(capital, pct, entry, stop, margin, lot, expected):
    assert compute_quantity(capital, pct, entry, stop, margin, lot) == expected


# -- evaluate: rejections in spec order ----------------------------------
def test_kill_switch_rejects():
    r = evaluate(_sig(), _state(), CFG, 1, 500_000, KillState(True, False))
    assert isinstance(r, Rejection) and r.reason == "kill_switch"


def test_daily_loss_cap_includes_unrealised():
    r = evaluate(_sig(), _state(realised_today=-1000.0, unrealised=-2000.0), CFG, 1, 500_000, OFF)
    assert r.reason == "daily_loss_cap"
    ok = evaluate(_sig(), _state(realised_today=-1000.0, unrealised=-1999.0), CFG, 1, 500_000, OFF)
    assert isinstance(ok, ApprovedOrder)


def test_entry_cap():
    r = evaluate(_sig(), _state(entries_today=5), CFG, 1, 500_000, OFF)
    assert r.reason == "max_entries_per_day"


def test_max_open_positions_counts_pending():
    r = evaluate(_sig(), _state(open_symbols={"A"}, pending_symbols={"B"}), CFG, 1, 500_000, OFF)
    assert r.reason == "max_open_positions"


def test_symbol_already_open_or_pending():
    assert evaluate(_sig(), _state(open_symbols={"RELIANCE"}), CFG, 1, 500_000, OFF).reason == "symbol_already_open"
    assert evaluate(_sig(), _state(pending_symbols={"RELIANCE"}), CFG, 1, 500_000, OFF).reason == "symbol_already_open"


def test_cooldown_blocks_until_inclusive():
    st = _state(cooldown_until={"RELIANCE": 10_000})
    assert evaluate(_sig(bar_ts=10_000), st, CFG, 1, 500_000, OFF).reason == "cooldown"
    assert isinstance(evaluate(_sig(bar_ts=10_300), st, CFG, 1, 500_000, OFF), ApprovedOrder)


def test_invalid_stop_side():
    assert evaluate(_sig("LONG", 100.0, 101.0), _state(), CFG, 1, 500_000, OFF).reason == "invalid_stop"
    assert evaluate(_sig("SHORT", 100.0, 99.0), _state(), CFG, 1, 500_000, OFF).reason == "invalid_stop"


def test_short_requires_mis():
    r = evaluate(_sig("SHORT", 100.0, 101.0, product="CNC"), _state(), CFG, 1, 500_000, OFF)
    assert r.reason == "short_requires_mis"


def test_insufficient_size():
    r = evaluate(_sig(), _state(), CFG, lot_size=75, available_margin=5_000, kill=OFF)
    assert r.reason == "insufficient_size"


def test_approved_order_has_quantity_and_client_id():
    r = evaluate(_sig(), _state(), CFG, 1, 500_000, OFF)
    assert isinstance(r, ApprovedOrder)
    assert r.quantity == 1000
    assert len(r.client_id) == 16


def test_check_precedence_kill_switch_wins():
    st = _state(realised_today=-9999.0, entries_today=99, open_symbols={"RELIANCE"},
                cooldown_until={"RELIANCE": 99_999})
    r = evaluate(_sig(), st, CFG, 1, 0, KillState(True, False))
    assert r.reason == "kill_switch"
    assert evaluate(_sig(), st, CFG, 1, 0, OFF).reason == "daily_loss_cap"


def test_invalid_price_reason():
    assert evaluate(_sig(entry=0.0, stop=-1.0), _state(), CFG, 1, 500_000, OFF).reason == "invalid_price"


def test_evaluate_does_not_mutate_state():
    st = _state(open_symbols={"A"}, pending_symbols={"B"}, cooldown_until={"C": 5})
    before = (set(st.open_symbols), set(st.pending_symbols), dict(st.cooldown_until), st.entries_today)
    evaluate(_sig(), st, CFG, 1, 500_000, OFF)
    assert before == (st.open_symbols, st.pending_symbols, st.cooldown_until, st.entries_today)


def test_open_count_does_not_double_count_overlap():
    st = _state(open_symbols={"A"}, pending_symbols={"A"})
    assert st.open_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_risk.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/risk/killswitch.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class KillState:
    active: bool   # file exists (or is unreadable): no new entries
    flatten: bool  # file mentions "flatten": also square off everything


def read_kill_switch(path: Union[str, Path]) -> KillState:
    """Safety interlock: never raises, and fails closed.

    A single read avoids the exists/read race. Any OS error other than "not there"
    (directory, permissions, I/O) blocks new entries rather than being ignored.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return KillState(False, False)
    except OSError:
        return KillState(True, False)
    return KillState(True, "flatten" in text.lower())
```

- [ ] **Step 4: Write `src/tradebot/risk/sizing.py`**

```python
from __future__ import annotations

import math

_EPS = 1e-9  # absorbs float error on tick-aligned distances so exact integers are not floored down


def compute_quantity(capital: float, per_trade_pct: float, entry: float, stop: float,
                     available_margin: float, lot_size: int) -> int:
    """min(risk-based size, margin-based size), rounded down to lot size. 0 if below one lot."""
    dist = abs(entry - stop)
    if not all(math.isfinite(x) for x in (dist, entry, available_margin, capital)):
        return 0
    if dist <= 0 or entry <= 0 or lot_size <= 0 or available_margin <= 0:
        return 0
    risk_qty = math.floor(capital * per_trade_pct / 100.0 / dist + _EPS)
    margin_qty = math.floor(available_margin / entry + _EPS)
    qty = min(risk_qty, margin_qty)
    qty -= qty % lot_size
    return max(qty, 0)
```

- [ ] **Step 5: Write `src/tradebot/risk/engine.py`**

```python
"""Pure risk checks. Order of checks follows spec section 6."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from tradebot.config import RiskConfig
from tradebot.risk.killswitch import KillState
from tradebot.risk.sizing import compute_quantity
from tradebot.types import ApprovedOrder, Rejection, Signal, make_client_id


@dataclass
class PortfolioState:
    capital: float
    open_symbols: set[str] = field(default_factory=set)
    pending_symbols: set[str] = field(default_factory=set)
    realised_today: float = 0.0
    unrealised: float = 0.0
    entries_today: int = 0
    cooldown_until: dict[str, int] = field(default_factory=dict)  # symbol -> last blocked bar ts

    @property
    def open_count(self) -> int:
        return len(self.open_symbols | self.pending_symbols)

    def daily_loss_breached(self, cfg: RiskConfig) -> bool:
        return (self.realised_today + self.unrealised) <= -(cfg.daily_loss_cap_pct / 100.0) * self.capital


def evaluate(signal: Signal, state: PortfolioState, cfg: RiskConfig, lot_size: int,
             available_margin: float, kill: KillState) -> ApprovedOrder | Rejection:
    if kill.active:
        return Rejection(signal, "kill_switch")
    if state.daily_loss_breached(cfg):
        return Rejection(signal, "daily_loss_cap")
    if state.entries_today >= cfg.max_entries_per_day:
        return Rejection(signal, "max_entries_per_day")
    if state.open_count >= cfg.max_open_positions:
        return Rejection(signal, "max_open_positions")
    if signal.symbol in state.open_symbols or signal.symbol in state.pending_symbols:
        return Rejection(signal, "symbol_already_open")
    if signal.bar_ts <= state.cooldown_until.get(signal.symbol, -1):
        return Rejection(signal, "cooldown")
    if not (math.isfinite(signal.entry_price) and signal.entry_price > 0):
        return Rejection(signal, "invalid_price")
    if signal.direction == "LONG" and not signal.stop_price < signal.entry_price:
        return Rejection(signal, "invalid_stop")
    if signal.direction == "SHORT" and not signal.stop_price > signal.entry_price:
        return Rejection(signal, "invalid_stop")
    if signal.direction == "SHORT" and signal.product != "MIS":
        return Rejection(signal, "short_requires_mis")
    qty = compute_quantity(state.capital, cfg.per_trade_pct, signal.entry_price, signal.stop_price,
                           available_margin, lot_size)
    if qty < max(lot_size, 1):
        return Rejection(signal, "insufficient_size")
    return ApprovedOrder(signal, qty, make_client_id(signal.strategy, signal.symbol, signal.bar_ts))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_risk.py -v`
Expected: 31 passed

- [ ] **Step 7: Commit**

```bash
git add src/tradebot/risk tests/test_risk.py
git commit -m "feat: risk engine, sizing, kill switch"
```

---

### Task 11: AI filter interface and stub

**Files:**
- Create: `src/tradebot/ai/filter.py`
- Test: `tests/test_ai_filter.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from tradebot.ai.filter import StubFilter, build_filter
from tradebot.config import AIConfig
from tradebot.types import Candidate, Signal


def _cand(sym):
    sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", 1)
    return Candidate(sig, 10, {"rsi": 60.0}, ())


def test_stub_approves_everything_in_order():
    f = StubFilter()
    out = f.review([_cand("A"), _cand("B")])
    assert [d.signal.symbol for d in out] == ["A", "B"]
    assert all(d.approved and d.filter_kind == "stub" and d.confidence == 1.0 for d in out)


def test_stub_handles_empty():
    assert StubFilter().review([]) == []


def test_build_filter_stub():
    cfg = AIConfig(filter="stub", model="x", candles_in_context=30, on_failure="reject")
    assert isinstance(build_filter(cfg, api_key=""), StubFilter)


def test_build_filter_unknown_raises():
    cfg = AIConfig(filter="claude", model="x", candles_in_context=30, on_failure="reject")
    with pytest.raises(ValueError):
        build_filter(cfg, api_key="")


def check_filter_contract(flt, candidates):
    """Shared contract every AIFilter implementation must satisfy (reuse in Plan 2)."""
    out = flt.review(candidates)
    assert len(out) == len(candidates)
    for d, c in zip(out, candidates):
        assert d.signal is c.signal
        assert 0.0 <= d.confidence <= 1.0 and d.filter_kind == flt.kind and isinstance(d.approved, bool)


def test_stub_satisfies_filter_contract():
    check_filter_contract(StubFilter(), [_cand("A"), _cand("B"), _cand("C")])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ai_filter.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/ai/filter.py`**

```python
"""AI filter interface. Plan 1 ships only the stub; ClaudeFilter and the cache arrive in Plan 2."""
from __future__ import annotations

from typing import Protocol

from tradebot.config import AIConfig
from tradebot.types import Candidate, Decision


class AIFilter(Protocol):
    kind: str

    def review(self, candidates: list[Candidate]) -> list[Decision]:
        """Return exactly one Decision per candidate, in the same order, each carrying the
        candidate's own Signal. A length or order mismatch is a bug in the filter, never a
        way to express rejection: reject with approved=False instead. The engine checks this."""


class StubFilter:
    kind = "stub"

    def review(self, candidates: list[Candidate]) -> list[Decision]:
        return [Decision(c.signal, True, "stub", 1.0, self.kind) for c in candidates]


def build_filter(cfg: AIConfig, api_key: str) -> AIFilter:
    if cfg.filter == "stub":
        return StubFilter()
    raise ValueError(f"ai.filter '{cfg.filter}' is not available yet (Plan 2 adds claude filters)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ai_filter.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/ai/filter.py tests/test_ai_filter.py
git commit -m "feat: AI filter interface with stub"
```

---

### Task 12: Backtest broker

**Files:**
- Create: `src/tradebot/execution/broker.py`
- Create: `src/tradebot/execution/backtest.py`
- Test: `tests/test_backtest_broker.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from tradebot.execution.backtest import STOP_FIRST_ON_SAME_BAR, BacktestBroker, check_exit
from tradebot.execution.broker import Broker
from tradebot.types import Position
from tradebot.execution.broker import Closed, Filled, Unfilled
from tradebot.types import ApprovedOrder, Candle, Signal


def _order(direction="LONG", entry=100.0, stop=99.0, target=102.0, qty=10, sym="X", product="MIS"):
    return ApprovedOrder(Signal("ema_rsi", sym, direction, entry, stop, target, product, 1000), qty, "cid-" + sym)


def _c(o, h, l, c, ts=1300, sym="X"):
    return Candle(sym, ts, o, h, l, c, 1)


def _broker(slip=0.0, buffer=None):
    return BacktestBroker(capital=100_000.0, slippage_pct=slip, mis_leverage=5.0, entry_buffer_pct=buffer)


_: Broker = _broker()  # BacktestBroker must satisfy the Protocol (checked at import by type checkers)


def test_constant_documented():
    assert STOP_FIRST_ON_SAME_BAR is True


def test_entry_fills_at_next_open_with_slippage():
    b = _broker(slip=0.05)
    b.place_entry(_order())
    assert b.pending_symbols() == {"X"}
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    assert isinstance(ev[0], Filled)
    assert ev[0].position.avg_price == pytest.approx(100.05)
    assert ev[0].position.opened_ts == 1300
    assert b.pending_symbols() == set()
    assert set(b.open_positions()) == {"X"}


def test_short_entry_slippage_is_downward():
    b = _broker(slip=0.05)
    b.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.2)})
    assert ev[0].position.avg_price == pytest.approx(99.95)


def test_unfilled_when_no_candle_for_symbol():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"Y": _c(1, 1, 1, 1, sym="Y")})
    assert isinstance(ev[0], Unfilled) and ev[0].order.client_id == "cid-X" and ev[0].reason == "no_candle"
    assert b.pending_symbols() == set()


def test_unfilled_when_open_gaps_beyond_entry_buffer():
    b = _broker(buffer=0.1)  # 0.1% like the live marketable limit
    b.place_entry(_order(entry=100.0))
    ev = b.on_bar(1300, {"X": _c(100.5, 101.0, 100.4, 100.8)})  # opens 0.5% above the signal
    assert isinstance(ev[0], Unfilled) and ev[0].reason == "beyond_buffer"
    b2 = _broker(buffer=0.1)
    b2.place_entry(_order(entry=100.0))
    assert isinstance(b2.on_bar(1300, {"X": _c(100.05, 101.0, 99.9, 100.8)})[0], Filled)
    b3 = _broker(buffer=0.1)
    b3.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    assert b3.on_bar(1300, {"X": _c(99.5, 99.6, 99.0, 99.2)})[0].reason == "beyond_buffer"


def test_place_entry_refuses_duplicate_pending_or_open_symbol():
    b = _broker()
    b.place_entry(_order())
    with pytest.raises(ValueError):
        b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    with pytest.raises(ValueError):
        b.place_entry(_order())


def test_gap_through_stop_fills_at_open_not_stop():
    pos = Position("X", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c", "s")
    assert check_exit(pos, _c(95.0, 96.0, 94.0, 95.5)) == ("STOP", 95.0)
    short = Position("X", "MIS", "SHORT", 10, 100.0, 101.0, 98.0, 1, "c", "s")
    assert check_exit(short, _c(105.0, 106.0, 104.0, 105.0)) == ("STOP", 105.0)


def test_gap_through_target_fills_at_open():
    pos = Position("X", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c", "s")
    assert check_exit(pos, _c(103.0, 104.0, 102.5, 103.5)) == ("TARGET", 103.0)
    no_target = Position("X", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c", "s")
    assert check_exit(no_target, _c(103.0, 110.0, 102.5, 103.5)) is None


def test_entry_bar_gapping_through_stop_is_a_loss_not_a_profit():
    b = _broker()
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    ev = b.on_bar(1300, {"X": _c(95.0, 96.0, 94.0, 95.5)})
    assert [type(e) for e in ev] == [Filled, Closed]
    pos = ev[1].position
    assert pos.avg_price == 95.0 and pos.exit_price == 95.0 and pos.exit_reason == "STOP"
    assert pos.pnl == 0.0  # filled and stopped at the same gapped open; never +40


def test_cnc_position_carries_across_bars_then_stops():
    b = _broker()
    b.place_entry(_order(product="CNC"))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    assert b.on_bar(1600, {"X": _c(100.0, 100.5, 99.5, 100.2)}) == []
    assert b.square_off(1900, {"X": _c(100.0, 100.5, 99.5, 100.2, 1900)}) == []
    ev = b.on_bar(2200, {"X": _c(100.0, 100.1, 98.9, 99.0, 2200)})
    assert isinstance(ev[0], Closed) and ev[0].position.exit_reason == "STOP" and ev[0].position.closed_ts == 2200


def test_unrealised_requires_price_for_every_open_position():
    b = _broker()
    b.place_entry(_order())
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    with pytest.raises(KeyError):
        b.unrealised_pnl({})


def test_stop_hit_on_entry_bar():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"X": _c(100.0, 100.5, 98.5, 99.5)})
    assert [type(e) for e in ev] == [Filled, Closed]
    pos = ev[1].position
    assert pos.exit_reason == "STOP" and pos.exit_price == 99.0 and pos.pnl == pytest.approx(-10.0)
    assert b.open_positions() == {}


def test_target_hit_on_entry_bar():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"X": _c(100.0, 102.5, 99.5, 102.0)})
    assert ev[1].position.exit_reason == "TARGET" and ev[1].position.pnl == pytest.approx(20.0)


def test_both_hit_resolves_to_stop_first():
    b = _broker()
    b.place_entry(_order())
    ev = b.on_bar(1300, {"X": _c(100.0, 103.0, 98.0, 100.0)})
    assert ev[1].position.exit_reason == "STOP"


def test_stop_exit_applies_slippage_target_does_not():
    b = _broker(slip=0.05)
    b.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.1, 98.9, 99.0)})
    assert ev[0].position.exit_price == pytest.approx(98.95)   # 99 * (1 - 0.0005) rounded to tick
    b2 = _broker(slip=0.05)
    b2.place_entry(_order(entry=100.0, stop=99.0, target=102.0))
    b2.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    ev2 = b2.on_bar(1600, {"X": _c(100.0, 102.1, 99.9, 102.0)})
    assert ev2[0].position.exit_price == 102.0


def test_short_stop_and_target():
    b = _broker()
    b.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    b.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.0)})
    ev = b.on_bar(1600, {"X": _c(100.0, 100.5, 97.5, 98.0)})
    assert ev[0].position.exit_reason == "TARGET" and ev[0].position.pnl == pytest.approx(20.0)
    b2 = _broker()
    b2.place_entry(_order("SHORT", 100.0, 101.0, 98.0))
    b2.on_bar(1300, {"X": _c(100.0, 100.5, 99.5, 100.0)})
    ev2 = b2.on_bar(1600, {"X": _c(100.0, 101.5, 99.5, 101.0)})
    assert ev2[0].position.exit_reason == "STOP" and ev2[0].position.pnl == pytest.approx(-10.0)


def test_square_off_closes_mis_only_with_slippage():
    b = _broker(slip=0.05)
    b.place_entry(_order(sym="M", product="MIS"))
    b.place_entry(_order(sym="C", product="CNC"))
    b.on_bar(1300, {"M": _c(100.0, 100.1, 99.9, 100.0, sym="M"), "C": _c(100.0, 100.1, 99.9, 100.0, sym="C")})
    ev = b.square_off(1600, {"M": _c(101.0, 101.2, 100.8, 101.0, 1600, "M"), "C": _c(101.0, 101.2, 100.8, 101.0, 1600, "C")})
    assert [e.position.symbol for e in ev] == ["M"]
    assert ev[0].position.exit_reason == "SQUARE_OFF"
    assert ev[0].position.exit_price == pytest.approx(100.95)
    assert set(b.open_positions()) == {"C"}


def test_flatten_closes_everything():
    b = _broker()
    b.place_entry(_order(sym="M", product="MIS"))
    b.place_entry(_order(sym="C", product="CNC"))
    b.on_bar(1300, {"M": _c(100.0, 100.1, 99.9, 100.0, sym="M"), "C": _c(100.0, 100.1, 99.9, 100.0, sym="C")})
    ev = b.square_off(1600, {"M": _c(101, 101, 101, 101, 1600, "M"), "C": _c(101, 101, 101, 101, 1600, "C")},
                      products=("MIS", "CNC"), reason="FLATTEN")
    assert sorted(e.position.symbol for e in ev) == ["C", "M"]
    assert all(e.position.exit_reason == "FLATTEN" for e in ev)


def test_available_margin_reflects_leverage_positions_and_pending():
    b = _broker()
    assert b.available_margin("MIS") == pytest.approx(500_000.0)
    assert b.available_margin("CNC") == pytest.approx(100_000.0)
    b.place_entry(_order(qty=100))                 # 100 * 100 = 10,000 notional, 2,000 margin at 5x
    assert b.available_margin("MIS") == pytest.approx(490_000.0)
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    assert b.available_margin("MIS") == pytest.approx(490_000.0)
    assert b.available_margin("CNC") == pytest.approx(98_000.0)


def test_cash_and_unrealised():
    b = _broker()
    b.place_entry(_order(qty=10))
    b.on_bar(1300, {"X": _c(100.0, 100.1, 99.9, 100.0)})
    assert b.unrealised_pnl({"X": 101.0}) == pytest.approx(10.0)
    b.on_bar(1600, {"X": _c(101.0, 102.5, 100.9, 102.0)})
    assert b.cash == pytest.approx(100_020.0)
    assert b.unrealised_pnl({"X": 50.0}) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_backtest_broker.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/execution/broker.py`**

```python
"""Broker interface shared by backtest, paper, and live backends, plus the events they emit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union

from tradebot.types import ApprovedOrder, Candle, Position


@dataclass(frozen=True)
class Filled:
    position: Position
    order: ApprovedOrder
    ts: int


@dataclass(frozen=True)
class Unfilled:
    order: ApprovedOrder
    ts: int
    reason: str = "no_candle"  # no_candle | beyond_buffer


@dataclass(frozen=True)
class Closed:
    position: Position


BrokerEvent = Union[Filled, Unfilled, Closed]  # runtime union; `|` needs 3.10+


class Broker(Protocol):
    def place_entry(self, order: ApprovedOrder) -> None:
        """Queue an entry. One position or pending entry per symbol: raise ValueError otherwise."""
    def on_bar(self, ts: int, candles: dict[str, Candle]) -> list[BrokerEvent]: ...
    def cancel_pending(self, ts: int, reason: str = "cancelled") -> list[Unfilled]:
        """Withdraw every queued entry (day end, shutdown). Returns one Unfilled per order."""
    def square_off(self, ts: int, candles: dict[str, Candle], products: tuple[str, ...] = ("MIS",),
                   reason: str = "SQUARE_OFF") -> list[Closed]: ...
    def open_positions(self) -> dict[str, Position]: ...
    def pending_symbols(self) -> set[str]: ...
    def available_margin(self, product: str) -> float: ...
    def unrealised_pnl(self, last_prices: dict[str, float]) -> float: ...
```

- [ ] **Step 4: Write `src/tradebot/execution/backtest.py`**

```python
"""Simulated broker for backtests. Fills entries at the next bar's open; simulates
broker-side stop/target exits against each bar's high/low."""
from __future__ import annotations

import logging

from tradebot.execution.broker import BrokerEvent, Closed, Filled, Unfilled
from tradebot.types import ApprovedOrder, Candle, Position, round_tick

log = logging.getLogger("tradebot.backtest")

# Spec 8.1: if one bar touches both the stop and the target, assume the stop was hit first.
STOP_FIRST_ON_SAME_BAR = True


def check_exit(pos: Position, c: Candle) -> tuple[str, float] | None:
    """Return (reason, level) if the bar triggers an exit, else None.

    Levels are clamped to the bar's open: a bar that gaps through the stop fills at the
    open (worse than the stop), and one that gaps through the target fills at the open
    (better than the target). Without the clamp an entry bar gapping through the stop
    would book a profit on a losing trade.
    """
    long = pos.direction == "LONG"
    if long:
        stop_hit = c.low <= pos.stop_price
        target_hit = pos.target_price is not None and c.high >= pos.target_price
    else:
        stop_hit = c.high >= pos.stop_price
        target_hit = pos.target_price is not None and c.low <= pos.target_price
    if stop_hit and (STOP_FIRST_ON_SAME_BAR or not target_hit):
        return "STOP", (min(c.open, pos.stop_price) if long else max(c.open, pos.stop_price))
    if target_hit:
        return "TARGET", (max(c.open, pos.target_price) if long else min(c.open, pos.target_price))
    return None


class BacktestBroker:
    def __init__(self, capital: float, slippage_pct: float, mis_leverage: float,
                 entry_buffer_pct: float | None = None):
        """entry_buffer_pct mirrors the live marketable limit: an entry whose next open is beyond
        signal_price * (1 +/- buffer) is left unfilled, exactly as the live order would be.
        None disables the check."""
        self.cash = float(capital)
        self.slip = slippage_pct / 100.0
        self.lev = mis_leverage
        self.buffer = None if entry_buffer_pct is None else entry_buffer_pct / 100.0
        self._pending: dict[str, ApprovedOrder] = {}
        self._positions: dict[str, Position] = {}
        self.closed: list[Position] = []

    # -- interface -----------------------------------------------------------
    def place_entry(self, order: ApprovedOrder) -> None:
        sym = order.signal.symbol
        if sym in self._pending or sym in self._positions:
            raise ValueError(f"{sym} already has a pending entry or an open position")
        self._pending[sym] = order

    def pending_symbols(self) -> set[str]:
        return set(self._pending)

    def cancel_pending(self, ts: int, reason: str = "cancelled") -> list[Unfilled]:
        out = [Unfilled(order, ts, reason) for order in self._pending.values()]
        self._pending.clear()
        return out

    def open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def _beyond_buffer(self, sig, open_price: float) -> bool:
        if self.buffer is None:
            return False
        if sig.direction == "LONG":
            return open_price > sig.entry_price * (1 + self.buffer)
        return open_price < sig.entry_price * (1 - self.buffer)

    def on_bar(self, ts: int, candles: dict[str, Candle]) -> list[BrokerEvent]:
        events: list[BrokerEvent] = []
        for sym, order in list(self._pending.items()):
            del self._pending[sym]
            c = candles.get(sym)
            if c is None:
                events.append(Unfilled(order, ts, "no_candle"))
                continue
            sig = order.signal
            if self._beyond_buffer(sig, c.open):
                events.append(Unfilled(order, ts, "beyond_buffer"))
                continue
            price = self._entry_price(sig.direction, c.open)
            pos = Position(sym, sig.product, sig.direction, order.quantity, price, sig.stop_price,
                           sig.target_price, ts, order.client_id, sig.strategy)
            self._positions[sym] = pos
            events.append(Filled(pos, order, ts))
        for sym, pos in list(self._positions.items()):
            c = candles.get(sym)
            if c is None:
                continue
            hit = check_exit(pos, c)
            if hit is None:
                continue
            reason, level = hit
            price = self._exit_price(pos.direction, level) if reason == "STOP" else level
            self._close(pos, ts, price, reason)
            events.append(Closed(pos))
        return events

    def square_off(self, ts: int, candles: dict[str, Candle], products: tuple[str, ...] = ("MIS",),
                   reason: str = "SQUARE_OFF") -> list[Closed]:
        out: list[Closed] = []
        for sym, pos in list(self._positions.items()):
            c = candles.get(sym)
            if pos.product not in products or c is None:
                continue
            self._close(pos, ts, self._exit_price(pos.direction, c.close), reason)
            out.append(Closed(pos))
        return out

    def available_margin(self, product: str) -> float:
        """Free cash times leverage. Margin is charged on entry cost, not marked to market."""
        used = 0.0
        for pos in self._positions.values():
            used += pos.avg_price * pos.quantity / self._lev_for(pos.product)
        for order in self._pending.values():
            used += order.signal.entry_price * order.quantity / self._lev_for(order.signal.product)
        free = max(self.cash - used, 0.0)
        return free * self._lev_for(product)

    def unrealised_pnl(self, last_prices: dict[str, float]) -> float:
        """Sum over open positions. A position with no price in last_prices raises: the engine
        always has a last close for any symbol that has a position."""
        total = 0.0
        for s, p in self._positions.items():
            if s not in last_prices:
                raise KeyError(f"no last price for open position {s}")
            total += p.unrealised(last_prices[s])
        return total

    # -- internals ----------------------------------------------------------
    def _lev_for(self, product: str) -> float:
        return self.lev if product == "MIS" else 1.0

    def _entry_price(self, direction: str, ref: float) -> float:
        return round_tick(ref * (1 + self.slip)) if direction == "LONG" else round_tick(ref * (1 - self.slip))

    def _exit_price(self, direction: str, ref: float) -> float:
        # Exiting a long sells (worse = lower); exiting a short buys (worse = higher).
        return round_tick(ref * (1 - self.slip)) if direction == "LONG" else round_tick(ref * (1 + self.slip))

    def _close(self, pos: Position, ts: int, price: float, reason: str) -> None:
        pnl = (price - pos.avg_price) * pos.quantity if pos.direction == "LONG" else (pos.avg_price - price) * pos.quantity
        pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = ts, price, reason, round(pnl, 2)
        self.cash += pos.pnl
        if self.cash <= 0:
            log.warning("simulated cash is %.2f after closing %s: account is blown", self.cash, pos.symbol)
        del self._positions[pos.symbol]
        self.closed.append(pos)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_backtest_broker.py -v`
Expected: 21 passed

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/execution/broker.py src/tradebot/execution/backtest.py tests/test_backtest_broker.py
git commit -m "feat: backtest broker with stop-first exit simulation"
```

---

### Task 13: Engine loop

> Follow-up noted in Task 4 review: `Repo.load_candles` materialises the whole window. For a NIFTY 200 universe over a year of 5-minute bars this is millions of `Candle` objects. Acceptable for the 3-month windows Groww serves today; add a streaming `iter_candles` and a lazily-grouped `HistoricalSource` when the cache grows past that.

**Files:**
- Create: `src/tradebot/engine/loop.py`
- Create: `tests/helpers.py`
- Create: `tests/__init__.py` (empty; makes `from tests.helpers import ...` resolvable)
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write `tests/helpers.py`**

```python
# tests/helpers.py
"""Shared test builders: a config on disk and deterministic synthetic candles."""
import math
from datetime import date

import yaml

from tradebot.config import Config, load_config
from tradebot.engine.clock import ist_epoch
from tradebot.types import Candle

BASE_CONFIG = {
    "capital": 100000,
    "risk": {"per_trade_pct": 1.0, "daily_loss_cap_pct": 3.0, "flatten_on_daily_cap": False,
             "max_entries_per_day": 20, "max_open_positions": 5, "cooldown_bars": 3,
             "mis_leverage": 5.0, "adopted_stop_pct": 1.5},
    "strategy": {"ema_rsi": {"fast": 9, "slow": 21, "rsi_period": 14, "rsi_long_min": 55,
                             "rsi_short_max": 45, "atr_period": 14, "atr_stop_mult": 1.5,
                             "reward_risk": 2.0, "min_stop_pct": 0.1, "product": "MIS"}},
    "ai": {"filter": "stub", "model": "claude-sonnet-5", "candles_in_context": 30, "on_failure": "reject"},
    "execution": {"slippage_pct": 0.05, "entry_buffer_pct": 0.1, "bar_deadline_sec": 60, "interval_minutes": 5},
    "session": {"open": "09:15", "close": "15:30", "square_off": "15:10",
                "no_new_entries_after": "14:45", "holidays": []},
    "data": {"official_fetch_concurrency": 5},
    "paths": {"db": "data/tradebot.db", "logs": "data/logs", "instruments": "data/instruments.csv",
              "kill_switch": "KILL", "universe": "universe.yaml"},
}


def make_config(tmp_path, **overrides) -> Config:
    """Write a config.yaml under tmp_path with db/kill/universe paths pointing into tmp_path."""
    raw = yaml.safe_load(yaml.safe_dump(BASE_CONFIG))  # deep copy
    raw["paths"] = {
        "db": str(tmp_path / "tradebot.db"), "logs": str(tmp_path / "logs"),
        "instruments": str(tmp_path / "instruments.csv"), "kill_switch": str(tmp_path / "KILL"),
        "universe": str(tmp_path / "universe.yaml"),
    }
    for key, val in overrides.items():          # e.g. risk={"max_open_positions": 1}
        raw[key] = {**raw[key], **val} if isinstance(val, dict) else val
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p, tmp_path / "nonexistent.env")


def synth_candles(symbol: str, days: list[date], phase: float = 0.0, seed: int = 7,
                  bars_per_day: int = 75, noise: float = 1.0, gap: float = 0.08) -> list[Candle]:
    """Deterministic wavy price path with LCG noise so some trades stop out.
    5-minute bars from 09:15, `bars_per_day` per day. Each bar opens within +/- `gap` of the
    previous close, like real intraday bars, so the entry buffer does not reject every fill.
    Same inputs always give the same candles."""
    out, i, x, prev_close = [], 0, seed, None

    def rnd() -> float:  # linear congruential generator, uniform in [0, 1)
        nonlocal x
        x = (1103515245 * x + 12345) % (2 ** 31)
        return x / 2 ** 31

    for d in days:
        open_ts = ist_epoch(d, "09:15")
        for k in range(bars_per_day):
            base = 100.0 + 6.0 * math.sin((i + phase) / 7.0) + 0.02 * i
            c = round(base + (rnd() - 0.5) * 2 * noise, 2)
            o = round(c if prev_close is None else prev_close + (rnd() - 0.5) * 2 * gap, 2)
            h = round(max(o, c) + 0.2 + rnd() * noise, 2)
            l = round(min(o, c) - 0.2 - rnd() * noise, 2)
            out.append(Candle(symbol, open_ts + k * 300, o, h, l, c, 1000))
            prev_close = c
            i += 1
    return out
```

- [ ] **Step 2: Write the failing tests**

```python
import json
from datetime import date
from pathlib import Path

import pytest

from tests.helpers import make_config, synth_candles
from tradebot.ai.filter import StubFilter
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, date_of, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.strategy.ema_rsi import EmaRsiStrategy
from tradebot.types import Candle

GOLDEN = Path(__file__).parent / "fixtures" / "golden_trades.json"
DAYS = [date(2026, 9, 13), date(2026, 9, 14), date(2026, 9, 15)]  # Sunday, Mon, Tue


def _run(repo, cfg, candles, run_id="t1", ai=None):
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, sorted({c.symbol for c in candles}), 5, 0, 2_000_000_000)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    eng = BacktestEngine(cfg, repo, src, [strat], broker, ai or StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, run_id)
    eng.run()
    return eng, broker


def _candles():
    return synth_candles("A", DAYS, phase=0.0) + synth_candles("B", DAYS, phase=4.0, seed=99)


def test_engine_invariants(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert len(rows) >= 2, "synthetic data must produce trades"
    clock = SessionClock(cfg.session, 5)
    for r in rows:
        d = date_of(r["opened_at"])
        assert d.weekday() < 5, "no trades on Sunday"
        assert r["closed_at"] is not None, "MIS positions must be closed"
        assert date_of(r["closed_at"]) == d, "MIS closed same day"
        assert r["closed_at"] <= clock.square_off_bar_ts(d)
        assert r["opened_at"] <= ist_epoch(d, cfg.session.no_new_entries_after)
        assert r["exit_reason"] in ("STOP", "TARGET", "SQUARE_OFF")
        sign = 1 if r["direction"] == "LONG" else -1
        assert r["pnl"] == pytest.approx(sign * (r["exit_price"] - r["avg_price"]) * r["qty"], abs=0.01)
    assert repo.get_run("t1")["ended_at"] is not None
    days = repo.daily_pnl("t1")
    assert [d["date"] for d in days] == ["2026-09-14", "2026-09-15"]
    assert sum(d["realised"] for d in days) == pytest.approx(sum(r["pnl"] for r in rows), abs=0.01)


def test_engine_records_signals_decisions_and_orders(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    assert repo.rejection_counts("t1").get("entries_closed", 0) >= 0  # after-cutoff signals are rows, not silence
    n_signals = repo.conn.execute("SELECT COUNT(*) FROM signals WHERE run_id='t1'").fetchone()[0]
    n_risk = repo.conn.execute("SELECT COUNT(*) FROM risk_decisions WHERE run_id='t1'").fetchone()[0]
    n_ai = repo.conn.execute("SELECT COUNT(*) FROM ai_decisions WHERE run_id='t1'").fetchone()[0]
    n_orders = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE run_id='t1'").fetchone()[0]
    n_filled = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE run_id='t1' AND status='FILLED'").fetchone()[0]
    assert n_signals == n_risk >= n_ai >= n_orders >= 1
    assert n_filled == len(repo.list_positions("t1"))


def test_kill_switch_blocks_entries(repo, tmp_path):
    cfg = make_config(tmp_path)
    Path(cfg.paths.kill_switch).write_text("")
    _run(repo, cfg, _candles())
    assert repo.list_positions("t1") == []
    rej = repo.rejection_counts("t1")
    assert rej.get("kill_switch", 0) > 0 and set(rej) <= {"kill_switch", "entries_closed"}


def test_max_open_positions_one(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"max_open_positions": 1})
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    # no two positions overlap in time
    spans = sorted((r["opened_at"], r["closed_at"]) for r in rows)
    for (o1, c1), (o2, _) in zip(spans, spans[1:]):
        assert o2 > c1
    assert "max_open_positions" in repo.rejection_counts("t1")


def test_cooldown_after_stop(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    stops = [r for r in rows if r["exit_reason"] == "STOP"]
    if not stops:
        pytest.skip("synthetic data produced no stop-outs")
    for s in stops:
        later = [r for r in rows if r["symbol"] == s["symbol"] and r["opened_at"] > s["closed_at"]]
        for r in later:
            # entry fills one bar after the signal; signal bar must be > closed_ts + 3 bars
            assert r["opened_at"] - 300 > s["closed_at"] + 3 * 300


def test_strategy_exception_disables_strategy_for_day(repo, tmp_path):
    cfg = make_config(tmp_path)

    class Boom(EmaRsiStrategy):
        name = "boom"

        def on_candle(self, c):
            if date_of(c.ts) == date(2026, 9, 14) and c.ts >= ist_epoch(date(2026, 9, 14), "10:00"):
                raise RuntimeError("bad indicator")
            return super().on_candle(c)

    candles = _candles()
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, ["A", "B"], 5, 0, 2_000_000_000)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    eng = BacktestEngine(cfg, repo, src, [Boom(cfg.strategy["ema_rsi"])], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, "t1")
    eng.run()  # must not raise
    assert "boom" not in eng.disabled_strategies  # re-enabled on the next day
    rows = repo.conn.execute("SELECT bar_ts FROM signals WHERE run_id='t1'").fetchall()
    day1_after_10 = ist_epoch(date(2026, 9, 14), "10:00")
    day2_open = ist_epoch(date(2026, 9, 15), "09:15")
    assert not [r for r in rows if day1_after_10 <= r["bar_ts"] < day2_open], "disabled for the rest of day 1"
    assert [r for r in rows if r["bar_ts"] >= day2_open], "runs again on day 2"


def test_golden_trades(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    got = [dict(symbol=r["symbol"], direction=r["direction"], opened_at=r["opened_at"], closed_at=r["closed_at"],
                exit_reason=r["exit_reason"], qty=r["qty"], avg_price=r["avg_price"], exit_price=r["exit_price"],
                pnl=r["pnl"]) for r in repo.list_positions("t1")]
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(got, indent=2))
        pytest.fail(f"golden file written to {GOLDEN}; inspect it, commit it, and re-run")
    assert got == json.loads(GOLDEN.read_text())


# -- branches the golden run never reaches -----------------------------------------------------

def test_record_entry_bar_stop_inserts_then_closes_same_position(repo, tmp_path):
    from tradebot.execution.broker import Closed, Filled
    from tradebot.types import ApprovedOrder, Position, Signal
    cfg = make_config(tmp_path)
    src = HistoricalSource([])
    eng = BacktestEngine(cfg, repo, src, [], BacktestBroker(1e5, 0.0, 5.0), StubFilter(),
                         SessionClock(cfg.session, 5), {}, "t1")
    repo.create_run("t1", "backtest", 0, "{}")
    sig = Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", 1000)
    order = ApprovedOrder(sig, 10, "cidcidcidcidcid1")
    repo.insert_order("t1", order.client_id, repo.insert_signal("t1", sig), "ENTRY", "BUY", 10, 100.0, "PENDING", 1000)
    pos = Position("A", "MIS", "LONG", 10, 95.0, 99.0, 102.0, 1300, order.client_id, "ema_rsi")
    pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = 1300, 95.0, "STOP", 0.0
    eng._record([Filled(pos, order, 1300), Closed(pos)])
    row = repo.list_positions("t1")[0]
    assert row["closed_at"] == 1300 and row["exit_reason"] == "STOP"
    assert repo.order_status("t1", order.client_id) == "FILLED"
    assert repo.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert eng._cooldown_until["A"] == 1300 + 3 * 300


def test_tight_entry_buffer_produces_unfilled_orders(repo, tmp_path):
    cfg = make_config(tmp_path, execution={"entry_buffer_pct": 0.001})
    _run(repo, cfg, _candles())
    n_unfilled = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE status='UNFILLED'").fetchone()[0]
    n_pending = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0]
    assert n_unfilled > 0 and n_pending == 0
    days = repo.daily_pnl("t1")
    assert any(d["fill_rate"] < 1.0 for d in days)


def test_no_order_is_left_pending_at_run_end(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    assert repo.conn.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0] == 0


def test_ai_rejection_records_decision_and_places_nothing(repo, tmp_path):
    from tradebot.types import Decision

    class RejectAll:
        kind = "reject_all"

        def review(self, cands):
            return [Decision(c.signal, False, "no", 0.0, self.kind) for c in cands]

    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles(), ai=RejectAll())
    assert repo.list_positions("t1") == []
    assert repo.ai_rejection_count("t1") > 0


def test_ai_returning_wrong_count_is_an_error(repo, tmp_path):
    class Short:
        kind = "short"

        def review(self, cands):
            return []

    cfg = make_config(tmp_path)
    with pytest.raises(RuntimeError, match="returned 0 decisions"):
        _run(repo, cfg, _candles(), ai=Short())
    assert repo.get_run("t1")["ended_at"] is not None  # run is closed even on failure


def test_kill_switch_flatten_closes_open_positions(repo, tmp_path):
    cfg = make_config(tmp_path)
    kill = Path(cfg.paths.kill_switch)

    class ArmAfterFirstApproval(StubFilter):
        def review(self, cands):
            out = super().review(cands)
            kill.write_text("flatten")  # engine reads it on the next bar, after the entry fills
            return out

    _run(repo, cfg, _candles(), ai=ArmAfterFirstApproval())
    rows = repo.list_positions("t1")
    assert rows and all(r["exit_reason"] == "FLATTEN" for r in rows)


def test_daily_cap_flatten_fires_outside_entry_hours(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"daily_loss_cap_pct": 0.001, "flatten_on_daily_cap": True})
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert rows
    assert "daily_loss_cap" in repo.rejection_counts("t1")
    # once a loss breaches the tiny cap, anything still open that day is flattened, not stopped later
    by_day = {}
    for r in rows:
        by_day.setdefault(date_of(r["opened_at"]), []).append(r)
    for day_rows in by_day.values():
        losses = [r for r in day_rows if r["pnl"] < 0]
        if losses:
            first_loss_close = min(r["closed_at"] for r in losses)
            later_opens = [r for r in day_rows if r["opened_at"] > first_loss_close]
            assert not later_opens


def test_max_entries_per_day_is_enforced(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"max_entries_per_day": 1})
    _run(repo, cfg, _candles())
    assert all(d["entries_placed"] <= 1 for d in repo.daily_pnl("t1"))
    assert "max_entries_per_day" in repo.rejection_counts("t1")


def test_cnc_positions_carry_overnight_and_unrealised_is_day_scoped(repo, tmp_path):
    from tests.helpers import BASE_CONFIG
    params = {**BASE_CONFIG["strategy"]["ema_rsi"], "product": "CNC", "atr_stop_mult": 50.0, "reward_risk": 50.0}
    cfg = make_config(tmp_path, strategy={"ema_rsi": params})
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert rows and all(r["closed_at"] is None for r in rows), "nothing can exit: held to run end"
    days = repo.daily_pnl("t1")
    assert len(days) == 2 and days[0]["unrealised"] != 0.0
    # day 2's unrealised is today's move only, not the lifetime mark
    total_mark = sum(sig * (repo.conn.execute("SELECT c FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                                              (r["symbol"],)).fetchone()[0] - r["avg_price"]) * r["qty"]
                     for r in rows for sig in ([1] if r["direction"] == "LONG" else [-1]))
    assert days[0]["unrealised"] + days[1]["unrealised"] == pytest.approx(total_mark, abs=0.05)


def test_disabled_strategy_is_reset_before_next_day(repo, tmp_path):
    cfg = make_config(tmp_path)

    class Boom(EmaRsiStrategy):
        name = "boom"
        resets: list = []

        def reset(self, symbol):
            self.resets.append(symbol)
            super().reset(symbol)

        def on_candle(self, c):
            if date_of(c.ts) == date(2026, 9, 14) and c.ts >= ist_epoch(date(2026, 9, 14), "10:00"):
                raise RuntimeError("bad indicator")
            return super().on_candle(c)

    candles = _candles()
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, ["A", "B"], 5, 0, 2_000_000_000)
    strat = Boom(cfg.strategy["ema_rsi"])
    eng = BacktestEngine(cfg, repo, src, [strat], BacktestBroker(cfg.capital, 0.05, 5.0, 0.1), StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, "t1")
    eng.run()
    day2_open = ist_epoch(date(2026, 9, 15), "09:15")
    # resets happened for both symbols at the start of day 2, so early day-2 bars produce no signals
    assert {"A", "B"} <= set(strat.resets)
    early = [r for r in repo.conn.execute("SELECT bar_ts FROM signals WHERE run_id='t1'").fetchall()
             if day2_open <= r["bar_ts"] < day2_open + 20 * 300]
    assert not early


def test_run_survives_unquoted_holiday_dates_in_config(repo, tmp_path):
    cfg = make_config(tmp_path, session={"holidays": [date(2026, 10, 2)]})  # YAML date, not a string
    assert cfg.raw["session"]["holidays"][0].__class__.__name__ == "date"
    _run(repo, cfg, _candles())
    assert repo.get_run("t1")["ended_at"] is not None


def test_daily_cap_flatten_fires_after_entry_cutoff_unit(repo, tmp_path):
    from tradebot.types import ApprovedOrder, Signal
    cfg = make_config(tmp_path, risk={"flatten_on_daily_cap": True})
    broker = BacktestBroker(cfg.capital, 0.0, 5.0)
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {}, "t1")
    repo.create_run("t1", "backtest", 0, "{}")
    d = date(2026, 9, 14)
    sig = Signal("ema_rsi", "A", "LONG", 100.0, 90.0, 120.0, "MIS", ist_epoch(d, "14:40"))
    order = ApprovedOrder(sig, 10, "cidcidcidcidcid2")
    repo.insert_order("t1", order.client_id, repo.insert_signal("t1", sig), "ENTRY", "BUY", 10, 100.0, "PENDING", sig.bar_ts)
    broker.place_entry(order)
    eng._start_day()
    t1 = ist_epoch(d, "14:45")
    eng.process_bar(t1, {"A": Candle("A", t1, 100.0, 100.5, 99.5, 100.0, 1)})  # fills; after the cutoff
    assert set(broker.open_positions()) == {"A"}
    eng._day.realised = -0.5 * cfg.capital  # a huge loss booked earlier in the day
    t2 = ist_epoch(d, "14:50")
    eng.process_bar(t2, {"A": Candle("A", t2, 100.0, 100.5, 99.5, 100.0, 1)})
    assert broker.open_positions() == {}
    assert repo.list_positions("t1")[0]["exit_reason"] == "FLATTEN"


def test_end_run_is_written_even_if_day_end_bookkeeping_fails(repo, tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(repo, "upsert_daily_pnl", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError, match="disk full"):  # the mid-run day boundary propagates it
        _run(repo, cfg, _candles())
    assert repo.get_run("t1")["ended_at"] is not None  # but the run is still closed
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.engine.loop'`

- [ ] **Step 4: Write `src/tradebot/engine/loop.py`**

```python
"""The per-bar cycle (spec sections 4-8). Mode-independent given a source, broker, and clock.

Order inside a bar:
  1. broker.on_bar        fills pending entries at this bar's open, simulates exits
  2. square-off           once per day, from the square-off bar onward (latched)
  3. daily loss cap       flatten once per day if breached and configured to
  4. kill switch          flatten if requested
  5. strategies           every candle feeds every strategy (indicators stay warm)
  6. risk -> AI -> place  only if entries are allowed; a live kill switch rejects inside evaluate()

"PnL for the day" (spec 6.2) is realised today plus the change in unrealised since the day
opened, so a position carried overnight only charges today's move against today's cap.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date

from tradebot.ai.filter import AIFilter
from tradebot.config import Config
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, date_of
from tradebot.execution.broker import Broker, BrokerEvent, Closed, Filled, Unfilled
from tradebot.risk.engine import PortfolioState, evaluate
from tradebot.risk.killswitch import KillState, read_kill_switch
from tradebot.store.repo import Repo
from tradebot.strategy.base import Strategy
from tradebot.types import ApprovedOrder, Candidate, Candle, Rejection, Signal

log = logging.getLogger("tradebot.engine")


@dataclass
class _DayCounters:
    realised: float = 0.0
    entries_placed: int = 0
    fills: int = 0
    flattened: bool = False
    squared_off: bool = False
    unrealised_at_open: float = 0.0  # mark of positions carried in from earlier days


class BacktestEngine:
    def __init__(self, cfg: Config, repo: Repo, source: HistoricalSource, strategies: list[Strategy],
                 broker: Broker, ai_filter: AIFilter, clock: SessionClock, lot_sizes: dict[str, int],
                 run_id: str, mode: str = "backtest"):
        self.cfg = cfg
        self.repo = repo
        self.source = source
        self.strategies = strategies
        self.broker = broker
        self.ai_filter = ai_filter
        self.clock = clock
        self.lot_sizes = lot_sizes
        self.run_id = run_id
        self.mode = mode
        self.interval_sec = cfg.execution.interval_minutes * 60
        self._history: dict[str, deque[Candle]] = {}
        self._last_close: dict[str, float] = {}
        self._cooldown_until: dict[str, int] = {}
        self.disabled_strategies: set[str] = set()
        self._day = _DayCounters()

    # -- lifecycle -------------------------------------------------------------
    def run(self) -> str:
        self.repo.create_run(self.run_id, self.mode, int(time.time()), json.dumps(self.cfg.raw, default=str))
        current: date | None = None
        last_ts = 0
        try:
            for ts in self.source.bar_timestamps():
                d = date_of(ts)
                if not self.clock.is_trading_day(d):
                    continue
                if d != current:
                    if current is not None:
                        self._end_day(current, last_ts)
                    self._start_day()
                    current = d
                last_ts = ts  # set before processing so a failure mid-bar still stamps this bar
                self.process_bar(ts, self.source.candles_at(ts))
        finally:
            # A mid-run exception still leaves a closed run and, where possible, the last day's row.
            # Cleanup must never mask the original exception or skip end_run.
            try:
                if current is not None:
                    self._end_day(current, last_ts)
            except Exception:  # noqa: BLE001
                log.exception("end-of-day bookkeeping failed for run %s", self.run_id)
            finally:
                self.repo.end_run(self.run_id, int(time.time()))
        return self.run_id

    def _start_day(self) -> None:
        # A strategy disabled yesterday missed bars; its incremental indicators would silently
        # carry state across the hole. Reset so is_ready() gates signals until they are warm again.
        for strat in self.strategies:
            if strat.name in self.disabled_strategies:
                for sym in self._history:
                    strat.reset(sym)
        self.disabled_strategies.clear()
        self._day = _DayCounters(unrealised_at_open=self._unrealised_now())

    def _end_day(self, d: date, ts: int) -> None:
        # Entries queued on the last bar must not fill against tomorrow's open on a stale signal.
        self._record(self.broker.cancel_pending(ts, "day_end"))
        self.repo.upsert_daily_pnl(self.run_id, d.isoformat(), self._day.realised, self._unrealised_today(),
                                   self._day.fills, self._day.entries_placed)

    def _unrealised_now(self) -> float:
        return self.broker.unrealised_pnl(self._last_close)

    def _unrealised_today(self) -> float:
        return self._unrealised_now() - self._day.unrealised_at_open

    # -- per bar ----------------------------------------------------------------
    def process_bar(self, ts: int, candles: dict[str, Candle]) -> None:
        for sym, c in candles.items():
            self._last_close[sym] = c.close
            self._history.setdefault(sym, deque(maxlen=self.cfg.ai.candles_in_context)).append(c)

        self._record(self.broker.on_bar(ts, candles))
        if not self._day.squared_off and self.clock.square_off_due(ts):
            # Latched per day: a missing bar at exactly the square-off time cannot skip it.
            self._record(self.broker.square_off(ts, candles))
            self._day.squared_off = True

        # Both flatten triggers share one per-day latch. Safe: once the cap is breached every entry
        # is rejected for the day, so nothing can be opened after a cap flatten for a kill flatten to close.
        if (self.cfg.risk.flatten_on_daily_cap and not self._day.flattened
                and self._state().daily_loss_breached(self.cfg.risk)):
            log.warning("daily loss cap breached at %s; flattening", ts)
            self._flatten(ts, candles)

        kill = read_kill_switch(self.cfg.paths.kill_switch)
        if kill.flatten and not self._day.flattened:
            self._flatten(ts, candles)

        signals = self._run_strategies(candles)
        if not signals:
            return
        if not self.clock.entries_allowed(ts):
            # Audit trail: every dropped signal gets a row (spec 6), even outside entry hours.
            for _, sig in signals:
                sid = self.repo.insert_signal(self.run_id, sig)
                self.repo.insert_risk_decision(self.run_id, sid, False, "entries_closed", 0)
            return
        self._place(ts, signals, kill)

    def _run_strategies(self, candles: dict[str, Candle]) -> list[tuple[Strategy, Signal]]:
        out: list[tuple[Strategy, Signal]] = []
        for strat in self.strategies:
            if strat.name in self.disabled_strategies:
                continue
            for sym, c in candles.items():
                try:
                    sig = strat.on_candle(c)
                except Exception:  # noqa: BLE001 - spec 11: disable strategy for the day
                    log.exception("strategy %s failed on %s; disabled for the day", strat.name, sym)
                    self.disabled_strategies.add(strat.name)
                    break
                if sig is not None and strat.is_ready(sym):
                    out.append((strat, sig))
        return out

    def _lev(self, product: str) -> float:
        return self.cfg.risk.mis_leverage if product == "MIS" else 1.0

    def _state(self) -> PortfolioState:
        return PortfolioState(
            capital=self.cfg.capital,
            open_symbols=set(self.broker.open_positions()),
            pending_symbols=self.broker.pending_symbols(),
            realised_today=self._day.realised,
            unrealised=self._unrealised_today(),
            entries_today=self._day.entries_placed,
            cooldown_until=dict(self._cooldown_until),
        )

    def _place(self, ts: int, signals: list[tuple[Strategy, Signal]], kill: KillState) -> None:
        state = self._state()
        reserved_cash = 0.0  # margin claimed by approvals earlier in this same bar
        batch: list[tuple[int, ApprovedOrder, Candidate]] = []
        for strat, sig in signals:
            sid = self.repo.insert_signal(self.run_id, sig)
            margin = self.broker.available_margin(sig.product) - reserved_cash * self._lev(sig.product)
            res = evaluate(sig, state, self.cfg.risk, self.lot_sizes.get(sig.symbol, 1), margin, kill)
            if isinstance(res, Rejection):
                self.repo.insert_risk_decision(self.run_id, sid, False, res.reason, 0)
                continue
            self.repo.insert_risk_decision(self.run_id, sid, True, "ok", res.quantity)
            # Deliberately conservative within the bar: a risk-approved candidate holds its slot,
            # symbol and margin even if the AI then rejects it. The day counter counts placements.
            state.pending_symbols.add(sig.symbol)
            state.entries_today += 1
            reserved_cash += sig.entry_price * res.quantity / self._lev(sig.product)
            cand = Candidate(sig, res.quantity, strat.snapshot(sig.symbol), tuple(self._history.get(sig.symbol, ())))
            batch.append((sid, res, cand))
        if not batch:
            return
        decisions = self.ai_filter.review([c for _, _, c in batch])
        if len(decisions) != len(batch):  # zip would silently drop the tail; 3.9 has no strict=
            raise RuntimeError(f"{self.ai_filter.kind} returned {len(decisions)} decisions for {len(batch)} candidates")
        for (sid, order, _), dec in zip(batch, decisions):
            if dec.signal is not order.signal:
                raise RuntimeError(f"{self.ai_filter.kind} returned decisions out of order")
            self.repo.insert_ai_decision(self.run_id, sid, dec.filter_kind, dec.approved, dec.reason,
                                         dec.confidence, dec.latency_ms, dec.failure)
            if not dec.approved:
                continue
            side = "BUY" if order.signal.direction == "LONG" else "SELL"
            self.repo.insert_order(self.run_id, order.client_id, sid, "ENTRY", side, order.quantity,
                                   order.signal.entry_price, "PENDING", ts)
            self.broker.place_entry(order)
            self._day.entries_placed += 1

    def _flatten(self, ts: int, candles: dict[str, Candle]) -> None:
        self._record(self.broker.square_off(ts, candles, products=("MIS", "CNC"), reason="FLATTEN"))
        self._day.flattened = True

    # -- persistence of broker events ------------------------------------------
    def _record(self, events: list[BrokerEvent]) -> None:
        for ev in events:
            if isinstance(ev, Filled):
                ev.position.db_id = self.repo.insert_position(self.run_id, ev.position)
                self.repo.update_order(self.run_id, ev.order.client_id, "FILLED", ev.ts)
                oid = self.repo.order_id(self.run_id, ev.order.client_id)
                if oid is None:
                    log.error("fill for unknown order %s %s", ev.position.symbol, ev.order.client_id)
                else:
                    self.repo.insert_fill(oid, ev.position.quantity, ev.position.avg_price, ev.ts)
                self._day.fills += 1
            elif isinstance(ev, Unfilled):
                self.repo.update_order(self.run_id, ev.order.client_id, "UNFILLED", ev.ts)
                log.info("unfilled %s %s: %s", ev.order.signal.symbol, ev.order.client_id, ev.reason)
            elif isinstance(ev, Closed):
                p = ev.position
                if p.db_id is not None:
                    self.repo.close_position(p.db_id, p.closed_ts, p.exit_price, p.exit_reason, p.pnl)
                self._day.realised += p.pnl or 0.0
                if p.exit_reason == "STOP":
                    # Wall-clock cooldown: an overnight gap absorbs it, which is intended (intraday rule).
                    self._cooldown_until[p.symbol] = p.closed_ts + self.cfg.risk.cooldown_bars * self.interval_sec
```

- [ ] **Step 5: Run tests; first run of the golden test writes the fixture**

Run: `.venv/bin/pytest tests/test_engine.py -v`
Expected: 19 passed, 1 failed (`test_golden_trades` with "golden file written"). Open `tests/fixtures/golden_trades.json`, confirm it has 7 trades with STOP, TARGET and SQUARE_OFF exits and prices near 100, then:

Run: `.venv/bin/pytest tests/test_engine.py -v`
Expected: 20 passed. The fixture should show 7 trades: 4 STOP, 1 TARGET and 2 SQUARE_OFF exits.

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/engine/loop.py tests/__init__.py tests/helpers.py tests/test_engine.py tests/fixtures/golden_trades.json
git commit -m "feat: backtest engine loop with golden trade fixture"
```

---

### Task 14: Report

**Files:**
- Create: `src/tradebot/report/summary.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from tradebot.report.summary import build_summary, format_summary
from tradebot.types import Position, Signal


def _seed(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    trades = [  # (direction, entry, stop, qty, exit, reason, closed_at)
        ("LONG", 100.0, 99.0, 10, 102.0, "TARGET", 10),   # +20, R=2
        ("LONG", 100.0, 99.0, 10, 99.0, "STOP", 20),      # -10, R=-1
        ("SHORT", 100.0, 101.0, 10, 101.0, "STOP", 30),   # -10, R=-1
        ("LONG", 100.0, 98.0, 5, 101.0, "SQUARE_OFF", 40),  # +5, R=0.5
    ]
    for i, (d, e, s, q, x, why, ct) in enumerate(trades):
        p = Position("S%d" % i, "MIS", d, q, e, s, None, ct - 5, "cid%d" % i, "ema_rsi")
        pid = repo.insert_position("r1", p)
        pnl = (x - e) * q if d == "LONG" else (e - x) * q
        repo.close_position(pid, ct, x, why, pnl)
    adopted = Position("ADO", "MIS", "LONG", 1, 100.0, 98.5, None, 1, "cidA", "adopted", adopted=True)
    pid = repo.insert_position("r1", adopted)
    repo.close_position(pid, 50, 90.0, "STOP", -10.0)
    open_pos = Position("OPEN", "CNC", "LONG", 1, 100.0, 95.0, None, 60, "cidO", "ema_rsi")
    repo.insert_position("r1", open_pos)
    sid = repo.insert_signal("r1", Signal("ema_rsi", "X", "LONG", 1, 0.5, 2, "MIS", 1))
    repo.insert_risk_decision("r1", sid, False, "cooldown", 0)
    repo.insert_ai_decision("r1", sid, "stub", False, "n", 0.5, 1, None)
    repo.upsert_daily_pnl("r1", "2026-09-14", 5.0, 0.0, 4, 5)


def test_build_summary_metrics(repo):
    _seed(repo)
    s = build_summary(repo, "r1")
    assert s.trades == 4 and s.wins == 2 and s.losses == 2
    assert s.win_rate == pytest.approx(0.5)
    assert s.total_pnl == pytest.approx(5.0)
    assert s.avg_r == pytest.approx((2 - 1 - 1 + 0.5) / 4)
    assert s.max_drawdown == pytest.approx(20.0)  # peak 20 after t1, trough 0 after t3
    assert s.max_drawdown_equity == pytest.approx(0.0)  # single positive daily row
    assert s.r_trades == 4
    assert s.exit_reasons == {"TARGET": 1, "STOP": 2, "SQUARE_OFF": 1}
    assert s.risk_rejections == {"cooldown": 1}
    assert s.ai_rejections == 1
    assert s.open_positions == 1
    assert s.adopted_trades == 1 and s.adopted_pnl == pytest.approx(-10.0)
    assert [d["date"] for d in s.days] == ["2026-09-14"]


def test_format_summary_mentions_key_numbers(repo):
    _seed(repo)
    text = format_summary(build_summary(repo, "r1"))
    assert "Trades" in text and "4" in text
    assert "Win rate" in text and "50.0%" in text
    assert "Survivorship" in text


def test_unknown_run_raises(repo):
    with pytest.raises(ValueError):
        build_summary(repo, "nope")


def test_empty_run_reports_without_dividing_by_zero(repo):
    repo.create_run("empty", "backtest", 0, "{}")
    s = build_summary(repo, "empty")
    assert (s.trades, s.win_rate, s.avg_r, s.r_trades, s.max_drawdown, s.max_drawdown_equity) == (0, 0.0, 0.0, 0, 0.0, 0.0)
    text = format_summary(s)
    assert "n/a" in text and "none" in text


def test_zero_risk_trade_is_excluded_from_avg_r_and_null_pnl_tolerated(repo):
    repo.create_run("r2", "backtest", 0, "{}")
    pid = repo.insert_position("r2", Position("Z", "MIS", "LONG", 10, 100.0, 100.0, None, 1, "c", "s"))
    repo.close_position(pid, 2, 105.0, "TARGET", 50.0)
    pid2 = repo.insert_position("r2", Position("N", "MIS", "LONG", 1, 100.0, 99.0, None, 3, "c2", "s"))
    repo.close_position(pid2, 4, None, "SQUARE_OFF", None)
    s = build_summary(repo, "r2")
    assert s.trades == 2 and s.r_trades == 1 and s.avg_r == 0.0 and s.total_pnl == 50.0


def test_equity_drawdown_counts_open_excursions(repo):
    repo.create_run("r3", "backtest", 0, "{}")
    repo.upsert_daily_pnl("r3", "2026-09-14", realised=0.0, unrealised=-800.0, fills=1, entries_placed=1)
    repo.upsert_daily_pnl("r3", "2026-09-15", realised=0.0, unrealised=800.0, fills=0, entries_placed=0)
    s = build_summary(repo, "r3")
    assert s.max_drawdown == 0.0 and s.max_drawdown_equity == pytest.approx(800.0)


def test_daily_table_alignment_survives_large_numbers(repo):
    repo.create_run("r4", "backtest", 0, "{}")
    repo.upsert_daily_pnl("r4", "2026-09-14", realised=12_345_678.9, unrealised=-2_345.5, fills=4, entries_placed=5)
    repo.upsert_daily_pnl("r4", "2026-09-15", realised=5.0, unrealised=0.0, fills=12345, entries_placed=99999)
    lines = format_summary(build_summary(repo, "r4")).splitlines()
    table = [ln for ln in lines if ln[:4] in ("Date", "2026")]
    assert len({len(ln) for ln in table}) == 1, "header and every row have identical width"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `src/tradebot/report/summary.py`**

```python
"""Run summary: trade statistics, drawdown, rejection counts, daily PnL.

Conventions:
- Strategy statistics exclude adopted positions (spec 8.5); their PnL is reported separately.
- A trade with pnl == 0 counts as a loss so wins + losses == trades.
- "Max drawdown (closed)" is the peak-to-trough of cumulative realised PnL over closed trades.
  "Max drawdown (equity)" is over the daily rows' realised + unrealised, so open-position
  excursions count. Both are 2-dp rupee figures, not percentages.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from tradebot.store.repo import Repo


@dataclass
class Summary:
    run_id: str
    mode: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_r: float
    r_trades: int              # trades with non-zero risk that entered avg_r
    max_drawdown: float        # closed-trade, realised only
    max_drawdown_equity: float  # daily realised + unrealised
    exit_reasons: dict
    risk_rejections: dict
    ai_rejections: int
    open_positions: int
    adopted_trades: int
    adopted_pnl: float
    days: list = field(default_factory=list)


def _max_drawdown(increments) -> float:
    cum = peak = mdd = 0.0
    for p in increments:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def build_summary(repo: Repo, run_id: str) -> Summary:
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    rows = repo.list_positions(run_id)
    closed = sorted((r for r in rows if r["closed_at"] is not None and not r["adopted"]), key=lambda r: r["closed_at"])
    adopted_closed = [r for r in rows if r["closed_at"] is not None and r["adopted"]]
    pnls = [r["pnl"] or 0.0 for r in closed]
    rs = []
    for r in closed:
        risk = abs(r["avg_price"] - r["stop"]) * r["qty"]
        if risk > 0:
            rs.append((r["pnl"] or 0.0) / risk)
    days = [dict(d) for d in repo.daily_pnl(run_id)]
    wins = sum(1 for p in pnls if p > 0)
    return Summary(
        run_id=run_id,
        mode=run["mode"],
        trades=len(closed),
        wins=wins,
        losses=len(closed) - wins,
        win_rate=wins / len(closed) if closed else 0.0,
        total_pnl=sum(pnls),
        avg_r=sum(rs) / len(rs) if rs else 0.0,
        r_trades=len(rs),
        max_drawdown=_max_drawdown(pnls),
        max_drawdown_equity=_max_drawdown(d["realised"] + d["unrealised"] for d in days),
        exit_reasons=dict(Counter(r["exit_reason"] for r in closed)),
        risk_rejections=repo.rejection_counts(run_id),
        ai_rejections=repo.ai_rejection_count(run_id),
        open_positions=sum(1 for r in rows if r["closed_at"] is None),
        adopted_trades=len(adopted_closed),
        adopted_pnl=sum(r["pnl"] or 0.0 for r in adopted_closed),
        days=days,
    )


def _counts(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def format_summary(s: Summary) -> str:
    win_rate = f"{s.win_rate * 100:.1f}%" if s.trades else "n/a"
    lines = [
        f"Run {s.run_id} ({s.mode})",
        "Survivorship note: universe.yaml is today's constituent list; past-period results are overstated.",
        "",
        f"Trades                {s.trades}   (wins {s.wins}, losses {s.losses})",
        f"Win rate              {win_rate}",
        f"Total PnL             {s.total_pnl:,.2f}",
        f"Avg R                 {s.avg_r:.2f}   (over {s.r_trades} of {s.trades} trades with non-zero risk)",
        f"Max drawdown (closed) {s.max_drawdown:,.2f}   realised, closed trades only",
        f"Max drawdown (equity) {s.max_drawdown_equity:,.2f}   daily realised + unrealised",
        f"Exit reasons          {_counts(s.exit_reasons)}",
        f"Risk rejects          {_counts(s.risk_rejections)}",
        f"AI rejects            {s.ai_rejections}",
        f"Open now              {s.open_positions}",
    ]
    if s.adopted_trades:
        lines.append(f"Adopted               {s.adopted_trades} trades, PnL {s.adopted_pnl:,.2f} (excluded from stats above)")
    if s.days:
        lines += ["", f"{'Date':<10}  {'Realised':>14}  {'Unrealised':>14}  {'Fills/Entries':>14}  {'Fill rate':>9}"]
        for d in s.days:
            fe = f"{d['fills']}/{d['entries_placed']}"
            lines.append(f"{d['date']:<10}  {d['realised']:>14,.2f}  {d['unrealised']:>14,.2f}  {fe:>14}  "
                         f"{d['fill_rate'] * 100:>8.0f}%")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_report.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/report/summary.py tests/test_report.py
git commit -m "feat: run summary report"
```

---

### Task 15: CLI

**Files:**
- Create: `src/tradebot/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date

from click.testing import CliRunner

from tests.helpers import make_config, synth_candles
from tradebot import cli
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.types import Candle


def _setup(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B]\n")
    repo = Repo(connect(cfg.paths.db))
    days = [date(2026, 9, 14), date(2026, 9, 15)]
    repo.insert_candles(synth_candles("A", days) + synth_candles("B", days, phase=4.0, seed=99), interval=5)
    repo.conn.close()
    return cfg


def test_backtest_then_report(tmp_path):
    _setup(tmp_path)
    r = CliRunner()
    res = r.invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                              "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "cli1"])
    assert res.exit_code == 0, res.output
    assert "Trades" in res.output
    rep = r.invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "report", "--run", "cli1"])
    assert rep.exit_code == 0, rep.output
    assert "Run cli1" in rep.output


def test_backtest_without_data_fails_clearly(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                                        "--start", "2026-09-14", "--end", "2026-09-15"])
    assert res.exit_code != 0
    assert "fetch-data" in res.output


def _invoke(tmp_path, *args):
    return CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), *args])


def test_missing_credentials_is_a_clean_error(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,2885,RELIANCE,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = _invoke(tmp_path, "fetch-data")
    assert res.exit_code == 1 and "Error: GROWW_API_KEY and GROWW_TOTP_SECRET must be set" in res.output
    assert "Traceback" not in res.output


def test_groww_auth_failure_is_a_clean_error(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,2885,RELIANCE,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Auth:
        def __init__(self, k, s):
            pass

        def fetch_candles(self, *a):
            raise type("GrowwAPIAuthenticationException", (Exception,), {})("bad totp")

    monkeypatch.setattr(cli, "GrowwAdapter", Auth)
    res = _invoke(tmp_path, "fetch-data", "--sleep", "0")
    assert res.exit_code == 1 and "Error: GrowwAPIAuthenticationException: bad totp" in res.output


def test_generic_groww_exception_with_auth_code_aborts_on_first_symbol(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B, C]\n")
    hdr = "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
    (tmp_path / "instruments.csv").write_text(hdr + "".join(f"NSE,{i},{s},CASH,EQ,1,0.05,1,1\n" for i, s in enumerate("ABC")))
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    calls = []

    class Expired:
        def __init__(self, k, s):
            pass

        def fetch_candles(self, symbol, *a):
            calls.append(symbol)
            raise type("GrowwAPIException", (Exception,), {"code": "401"})("Invalid session")

    monkeypatch.setattr(cli, "GrowwAdapter", Expired)
    res = _invoke(tmp_path, "fetch-data", "--sleep", "0")
    assert res.exit_code == 1 and "Error: GrowwAPIException: Invalid session" in res.output
    assert calls == ["A"], "auth failure must abort before touching the next symbol"
    assert "symbol(s) failed" not in res.output


def test_fetch_data_isolates_symbol_failures(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B, C]\n")
    hdr = "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
    (tmp_path / "instruments.csv").write_text(hdr + "".join(f"NSE,{i},{s},CASH,EQ,1,0.05,1,1\n" for i, s in enumerate("ABC")))
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Flaky:
        def __init__(self, k, s):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            if symbol == "B":
                raise type("GrowwAPINotFoundException", (Exception,), {})("no such symbol")
            return [Candle(symbol, end_ts - (end_ts % 300), 1, 2, 0.5, 1.5, 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", Flaky)
    res = _invoke(tmp_path, "fetch-data", "--days", "5", "--sleep", "0")
    assert res.exit_code == 1
    assert "failed: B" in res.output and "1 symbol(s) failed: B" in res.output
    repo = Repo(connect(make_config(tmp_path).paths.db))
    assert repo.latest_candle_ts("A", 5) is not None and repo.latest_candle_ts("C", 5) is not None


def test_run_id_reuse_and_reversed_range_are_clean_errors(tmp_path):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "dup")
    assert ok.exit_code == 0, ok.output
    again = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "dup")
    assert again.exit_code == 1 and "already exists" in again.output
    rev = _invoke(tmp_path, "backtest", "--start", "2026-09-15", "--end", "2026-09-14")
    assert rev.exit_code == 1 and "--start must not be after --end" in rev.output


def test_bad_config_and_unknown_run_are_clean_errors(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "config.yaml").write_text((tmp_path / "config.yaml").read_text().replace("per_trade_pct: 1.0", "per_trade_pct: -1"))
    res = _invoke(tmp_path, "report", "--run", "x")
    assert res.exit_code == 1 and res.output.startswith("Error: config.yaml")
    make_config(tmp_path)
    res = _invoke(tmp_path, "report", "--run", "nope")
    assert res.exit_code == 1 and "no database" in res.output


def test_env_is_read_next_to_config_or_from_override(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfgs"
    cfg_dir.mkdir()
    make_config(cfg_dir)
    (cfg_dir / ".env").write_text("GROWW_API_KEY=beside-config\nGROWW_TOTP_SECRET=s\n")
    seen = {}

    class Spy:
        def __init__(self, k, s):
            seen["key"] = k

        def fetch_candles(self, *a):
            return []

    (cfg_dir / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    (cfg_dir / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,1,A,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    monkeypatch.setattr(cli, "GrowwAdapter", Spy)
    monkeypatch.chdir(tmp_path)  # CWD has no .env
    res = CliRunner().invoke(cli.main, ["--config", str(cfg_dir / "config.yaml"), "fetch-data", "--sleep", "0"])
    assert res.exit_code == 0, res.output
    assert seen["key"] == "beside-config"
    # A real CLI call is a fresh process; here the first invocation populated os.environ and
    # process env deliberately beats .env, so clear it before testing the override.
    for name in ("GROWW_API_KEY", "GROWW_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "other.env").write_text("GROWW_API_KEY=override\nGROWW_TOTP_SECRET=s\n")
    res = CliRunner().invoke(cli.main, ["--config", str(cfg_dir / "config.yaml"), "--env", str(tmp_path / "other.env"),
                                        "fetch-data", "--sleep", "0"])
    assert res.exit_code == 0 and seen["key"] == "override"


def test_fetch_data_uses_adapter_and_instruments(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE, NOSUCH]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
        "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
        "freeze_quantity,is_reserved,buy_allowed,sell_allowed,feed_key\n"
        "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,,,,,1,0.05,,0,1,1,NSE_CASH_2885\n")
    calls = []

    class FakeAdapter:
        def __init__(self, key, secret):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            calls.append((symbol, exchange, interval))
            return [Candle(symbol, end_ts - (end_ts % 300), 1, 2, 0.5, 1.5, 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "fetch-data", "--days", "20", "--sleep", "0"])
    assert res.exit_code == 0, res.output
    assert calls and all(c[0] == "RELIANCE" and c[1] == "NSE" and c[2] == 5 for c in calls)
    assert "dropping NOSUCH: not_found" in res.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.cli'`

- [ ] **Step 3: Write `src/tradebot/cli.py`**

```python
"""Command-line entry point: fetch-data, backtest, report. Paper/live/flatten arrive in Plan 3."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import click
import requests

from tradebot.ai.filter import build_filter
from tradebot.config import Config, load_config
from tradebot.data.historical import CHUNK_DAYS, HistoricalSource, fetch_incremental
from tradebot.data.instruments import download_instruments, load_instruments, resolve_universe
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.execution.groww_adapter import GrowwAdapter, is_non_retryable
from tradebot.report.summary import build_summary, format_summary
from tradebot.store.db import SchemaVersionError, connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy

# Operational failures that deserve a one-line message. sqlite3 programming errors (bad SQL) still traceback.
_FRIENDLY = (ValueError, SchemaVersionError, sqlite3.OperationalError, sqlite3.DatabaseError,
             requests.RequestException, NotImplementedError)
_FATAL_AUTH_CODES = {"401", "403"}


def _is_fatal_auth(e: BaseException) -> bool:
    """Auth/authorisation failures abort the whole run (spec 11). The SDK raises the generic
    GrowwAPIException with a code for Groww failure bodies, so match on the code as well as the name."""
    name = type(e).__name__
    if any(m in name for m in ("Authentication", "Authorisation", "Authorization")):
        return True
    return str(getattr(e, "code", "")) in _FATAL_AUTH_CODES


class _FriendlyGroup(click.Group):
    """Turn expected operational failures into one-line `Error: ...` messages with exit code 1.
    Programming errors still traceback. Groww SDK exceptions are matched by name so this module
    never imports growwapi."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except click.ClickException:
            raise
        except _FRIENDLY as e:
            raise click.ClickException(str(e)) from e
        except Exception as e:  # noqa: BLE001
            if type(e).__name__.startswith("Groww"):
                raise click.ClickException(f"{type(e).__name__}: {e}") from e
            raise


@click.group(cls=_FriendlyGroup)
@click.option("--config", "config_path", default="config.yaml", show_default=True,
              help="Path to config.yaml. Secrets are read from .env in the same directory unless --env is given.")
@click.option("--env", "env_path", default=None, help="Path to the .env file with GROWW_* and ANTHROPIC_* keys.")
@click.pass_context
def main(ctx: click.Context, config_path: str, env_path: Optional[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx.obj = load_config(config_path, env_path or Path(config_path).parent / ".env")


def _instruments_fresh(path: Path) -> bool:
    return path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() >= date.today()


def _symbols_and_lots(cfg: Config, require_instruments: bool) -> tuple:
    uni = load_universe(cfg.paths.universe)
    path = Path(cfg.paths.instruments)
    if not path.exists():
        if require_instruments:
            raise click.ClickException(f"instrument master missing at {path}")
        return list(uni.symbols), {s: 1 for s in uni.symbols}, uni.exchange
    resolved, dropped = resolve_universe(uni, load_instruments(path))
    for sym, why in dropped:
        click.echo(f"dropping {sym}: {why}")
    click.echo(f"resolved {len(resolved)} of {len(uni.symbols)} universe symbols")
    if not resolved:
        raise click.ClickException("no universe symbol resolved against the instrument master")
    return list(resolved), {s: i.lot_size for s, i in resolved.items()}, uni.exchange


@main.command("fetch-data")
@click.option("--days", default=90, show_default=True, help="Lookback for symbols with no stored candles")
@click.option("--full", is_flag=True, help="Ignore stored candles and refetch the whole window")
@click.option("--sleep", "pause", default=0.25, show_default=True, help="Seconds to pause between API requests")
@click.pass_obj
def fetch_data(cfg: Config, days: int, full: bool, pause: float) -> None:
    """Incrementally download candles for the universe into SQLite. Run weekly to grow the cache.

    One bad symbol does not stop the others: failures are listed at the end and the exit code is 1."""
    path = Path(cfg.paths.instruments)
    if not _instruments_fresh(path):
        click.echo("downloading instrument master")
        download_instruments(path)
    if cfg.execution.interval_minutes not in CHUNK_DAYS:
        raise click.ClickException(f"unsupported candle interval: {cfg.execution.interval_minutes} minutes")
    symbols, _, exchange = _symbols_and_lots(cfg, require_instruments=True)
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret)
    repo = Repo(connect(cfg.paths.db))
    failures: list = []

    def throttled(symbol, exch, start_ts, end_ts, interval):
        try:
            return adapter.fetch_candles(symbol, exch, start_ts, end_ts, interval)
        finally:
            if pause:
                time.sleep(pause)

    total = 0
    for sym in symbols:
        try:
            total += fetch_incremental(repo, throttled, [sym], exchange, cfg.execution.interval_minutes,
                                       days, int(time.time()), full=full, log=click.echo)[sym]
        except Exception as e:  # noqa: BLE001 - isolate per symbol; auth errors are fatal
            if _is_fatal_auth(e):
                raise
            if is_non_retryable(e) or isinstance(e, _FRIENDLY):
                failures.append((sym, f"{type(e).__name__}: {e}"))
                click.echo(f"failed: {sym} ({type(e).__name__}: {e})")
                continue
            raise
    click.echo(f"inserted {total} candles across {len(symbols) - len(failures)} symbols")
    if failures:
        raise click.ClickException(f"{len(failures)} symbol(s) failed: " + ", ".join(s for s, _ in failures))


@main.command()
@click.option("--start", required=True, type=click.DateTime(["%Y-%m-%d"]))
@click.option("--end", required=True, type=click.DateTime(["%Y-%m-%d"]))
@click.option("--strategy", "strategy_name", default="ema_rsi", show_default=True)
@click.option("--run-id", default=None, help="Defaults to bt-<timestamp>")
@click.pass_obj
def backtest(cfg: Config, start: datetime, end: datetime, strategy_name: str, run_id: Optional[str]) -> None:
    """Replay stored candles through the strategy, risk engine, AI filter, and simulated broker."""
    if start > end:
        raise click.ClickException("--start must not be after --end")
    symbols, lots, _ = _symbols_and_lots(cfg, require_instruments=False)
    repo = Repo(connect(cfg.paths.db))
    interval = cfg.execution.interval_minutes
    source = HistoricalSource.from_repo(repo, symbols, interval,
                                        ist_epoch(start.date(), "00:00"), ist_epoch(end.date(), "23:59"))
    if not source.bar_timestamps():
        raise click.ClickException("no candles in range; run `tradebot fetch-data` first")
    if strategy_name not in cfg.strategy:
        raise click.ClickException(f"no config for strategy '{strategy_name}'")
    run_id = run_id or f"bt-{datetime.now():%Y%m%d-%H%M%S}"
    if repo.get_run(run_id) is not None:
        raise click.ClickException(f"run '{run_id}' already exists; pick another --run-id")
    strategy = build_strategy(strategy_name, cfg.strategy[strategy_name])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    engine = BacktestEngine(cfg, repo, source, [strategy], broker,
                            build_filter(cfg.ai, cfg.secrets.anthropic_api_key),
                            SessionClock(cfg.session, interval), lots, run_id)
    rid = engine.run()
    click.echo(format_summary(build_summary(repo, rid)))


@main.command()
@click.option("--run", "run_id", required=True)
@click.pass_obj
def report(cfg: Config, run_id: str) -> None:
    """Print the summary for a stored run."""
    if not Path(cfg.paths.db).exists():
        raise click.ClickException(f"no database at {cfg.paths.db}")
    repo = Repo(connect(cfg.paths.db))
    click.echo(format_summary(build_summary(repo, run_id)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: 10 passed

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/cli.py tests/test_cli.py
git commit -m "feat: CLI with fetch-data, backtest, report"
```

---

### Task 16: First real run and README

**Files:**
- Create: `README.md`
- Modify: `.env` (local only, never committed)

- [ ] **Step 1: Write `README.md`**

```markdown
# tradebot

NSE/BSE trading bot on the Groww Trade API. Spec: `docs/superpowers/specs/2026-09-14-nse-bse-trading-bot-design.md`.

## Setup

    python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
    cp .env.example .env   # fill in GROWW_API_KEY, GROWW_TOTP_SECRET, ANTHROPIC_API_KEY

## Backtest (Plan 1)

    .venv/bin/python scripts/update_universe.py        # optional: NIFTY 200 from NSE
    .venv/bin/tradebot fetch-data --days 90            # run weekly to grow the cache
    .venv/bin/tradebot backtest --start 2026-06-15 --end 2026-09-12
    .venv/bin/tradebot report --run <run id printed above>

Survivorship bias: `universe.yaml` is today's constituent list, so backtests overstate results.

## Tests

    .venv/bin/pytest -q

## Kill switch

Create a file named `KILL` in the project root to stop new entries. Put the word `flatten`
in it to also square off everything on the next bar.
```

- [ ] **Step 2: Fill `.env` with real credentials and run the smoke sequence**

Run:
```bash
.venv/bin/tradebot fetch-data --days 30
```
Expected: "downloading instrument master", then one `SYMBOL: +N candles` line per symbol, then an `inserted ... candles` total. If it fails with an authentication error, the TOTP secret or API key is wrong; fix `.env` and rerun once. Do not loop on auth failures.

Run:
```bash
.venv/bin/tradebot backtest --start 2026-08-17 --end 2026-09-12
```
Expected: a summary block starting with `Run bt-...` and a daily table. Trades may be zero on a quiet window; that is not an error.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README with setup and backtest workflow"
```

---

## Self-review notes

- Spec coverage for milestones 1–4: store (Task 4), config (3), instruments and universe with `as_of` hook (6), incremental idempotent fetch with chunking (7), backtest broker with stop-first, entry-bar exits, square-off slippage, margin with leverage (12), indicators and strategy with `ready` and `recompute` (8, 9), every risk check in spec order including entries-only cap, cooldown, kill switch file semantics, short-requires-MIS (10), AI filter interface with stub and batched review (11), engine loop with strategy-disable-for-day, daily rollover, cooldown bookkeeping, flatten-on-cap (13), report with survivorship note, adopted exclusion, rejection counts (14), CLI (15).
- Deferred to Plan 2: `ClaudeFilter`, `CachedClaudeFilter`, `ai_cache` writes, `report --compare`.
- Deferred to Plan 3: live candle source, official-bar confirmation, paper and live brokers, marketable limits, OCO/GTT, partial fills, reconciliation, adoption, square-off cancel sequence, per-bar deadline, `flatten` command, JSON logging, signal handling.
