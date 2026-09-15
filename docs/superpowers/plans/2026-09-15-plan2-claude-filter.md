# Plan 2: Claude Filter and Compare Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put Claude in the loop as the signal filter (spec section 7), cache its decisions so replays are free, and build the stub-versus-Claude compare report (spec section 14) that decides whether the filter earns its cost.

**Architecture:** `ClaudeFilter` implements the existing `AIFilter` protocol: one structured-output request per bar covering every risk-approved candidate, deterministic prompt text so `(symbol, bar_ts, sha256(prompt))` is a stable cache key, and a failure policy from config. `CachedClaudeFilter` consults the `ai_cache` table first. `report --compare` joins the stub run's positions to the Claude run's rejections by the deterministic client id, so it can say what the rejected signals would have earned. Everything else (engine, risk, broker, store) is untouched except for token accounting columns.

**Tech Stack:** Python 3.9, `anthropic` 0.125 (the 0.x line is required on 3.9; `output_config` JSON-schema structured outputs are supported), sqlite3, click, pytest. Model default `claude-opus-5` with `effort: low`; switch `ai.model` to `claude-sonnet-5` to cut cost by about 60%.

**Conventions:** as Plan 1. Every task ends with a commit. `.venv/bin/pytest -q` must stay green (currently 234 tests). Never call the network from tests; `ClaudeClient` is replaced by a fake in every test.

**Cost note for the operator:** a full replay of the 62-day window makes one request per bar that had at least one risk-approved candidate. Task 8 adds `tradebot estimate-ai` to measure this before spending; the first Opus 5 replay of the 62-day window is likely in the range of one to two hundred US dollars (about 2,000 input tokens per call, adaptive thinking on output, roughly 4,650 calls at most); reruns cost zero thanks to the cache, and `claude-sonnet-5` cuts the first pass by about 60%.

---

## File structure

```
src/tradebot/types.py            # modify: Decision gains token fields
src/tradebot/config.py           # modify: AIConfig gains effort/max_tokens/timeout/max_calls/prices; _section honours defaults
src/tradebot/store/schema.sql    # modify: ai_decisions token columns
src/tradebot/store/db.py         # modify: SCHEMA_VERSION 2 + migration
src/tradebot/store/repo.py       # modify: ai cache get/put, ai usage, rejected-signal join helpers
src/tradebot/ai/prompt.py        # new: SYSTEM_PROMPT, RESPONSE_SCHEMA, render_candidates, prompt_hash
src/tradebot/ai/claude_client.py # new: ClaudeClient, ReviewResponse, ClaudeReviewError
src/tradebot/ai/cache.py         # new: AICache
src/tradebot/ai/filter.py        # modify: ClaudeFilter, CachedClaudeFilter, build_filter(cfg, api_key, repo)
src/tradebot/report/compare.py   # new: build_compare, format_compare
src/tradebot/cli.py              # modify: backtest --ai, report --compare, estimate-ai
config.yaml                      # modify: ai section
tests/test_config.py             # modify
tests/test_store.py              # modify: migration + cache tests
tests/test_ai_prompt.py          # new
tests/test_claude_client.py      # new
tests/test_ai_filter.py          # modify: Claude filters with a fake client
tests/test_compare.py            # new
tests/test_cli.py                # modify
```

---

### Task 1: Config and type additions

**Files:**
- Modify: `src/tradebot/config.py`
- Modify: `src/tradebot/types.py`
- Modify: `config.yaml`
- Test: `tests/test_config.py`, `tests/test_types.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_config.py`:

```python


def test_ai_section_defaults_and_validation(tmp_path):
    cfg = load_config(_write(tmp_path), tmp_path / "x.env")
    assert (cfg.ai.effort, cfg.ai.max_tokens, cfg.ai.timeout_sec, cfg.ai.max_calls_per_run) == ("low", 4000, 60, 10000)
    assert (cfg.ai.price_in_per_mtok, cfg.ai.price_out_per_mtok) == (5.0, 25.0)
    assert (cfg.ai.price_cache_read_per_mtok, cfg.ai.price_cache_write_per_mtok) == (0.5, 6.25)
    over = load_config(_write(tmp_path, YAML.replace("on_failure: reject", 'on_failure: reject\n  effort: max\n  max_tokens: "2500"')),
                       tmp_path / "x.env")
    assert (over.ai.effort, over.ai.max_tokens) == ("max", 2500)  # YAML overrides a default and is coerced
    for broken, frag in [
        (YAML.replace("on_failure: reject", "on_failure: reject\n  effort: turbo"), "ai.effort"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  max_calls_per_run: 0"), "ai.max_calls_per_run"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  max_tokens: 0"), "ai.max_tokens"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  timeout_sec: 0"), "ai.timeout_sec"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  price_out_per_mtok: -1"), "ai.price_out_per_mtok"),
    ]:
        with pytest.raises(ValueError) as e:
            load_config(_write(tmp_path, broken), tmp_path / "x.env")
        assert frag in str(e.value)
```

Append to `tests/test_types.py`:

```python


def test_decision_token_fields_default_to_zero():
    from tradebot.types import Decision
    d = Decision(_sig(), True, "ok", 1.0, "stub")
    assert (d.input_tokens, d.output_tokens, d.cache_read_tokens, d.cache_write_tokens) == (0, 0, 0, 0)
```

- [ ] **Step 2: Run them, expect failures**

Run: `.venv/bin/pytest tests/test_config.py tests/test_types.py -q`
Expected: 2 failed (AttributeError on `cfg.ai.effort`, TypeError on Decision kwargs).

- [ ] **Step 3: Implement**

In `src/tradebot/config.py`, replace the `AIConfig` dataclass with:

```python
@dataclass(frozen=True)
class AIConfig:
    filter: str
    model: str
    candles_in_context: int
    on_failure: str
    effort: str = "low"              # low | medium | high | xhigh | max
    max_tokens: int = 4000           # a backstop, not a cost knob: unused output is not billed
    timeout_sec: int = 60            # adaptive thinking can take a while; the SDK retries twice on top
    max_calls_per_run: int = 10000   # hard stop on spend per backtest; later bars use on_failure
    # USD per million tokens, used only for the cost line in reports (Opus 5 list prices).
    price_in_per_mtok: float = 5.0
    price_out_per_mtok: float = 25.0
    price_cache_read_per_mtok: float = 0.5     # prompt-cache hits bill ~0.1x input
    price_cache_write_per_mtok: float = 6.25   # prompt-cache writes bill ~1.25x input
```

In `_section`, make fields with defaults optional. Replace:

```python
    missing = set(fields) - set(given)
```

with:

```python
    required = {n for n, f in fields.items()
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    missing = required - set(given)
```

In `_validate`, add to `checks` after the `ai.on_failure` line:

```python
        (a.effort in AI_EFFORTS, f"ai.effort must be one of {AI_EFFORTS}"),
        (a.max_tokens >= 1, "ai.max_tokens must be >= 1"),
        (a.timeout_sec >= 1, "ai.timeout_sec must be >= 1"),
        (a.max_calls_per_run >= 1, "ai.max_calls_per_run must be >= 1"),
        (a.price_in_per_mtok >= 0, "ai.price_in_per_mtok must be >= 0"),
        (a.price_out_per_mtok >= 0, "ai.price_out_per_mtok must be >= 0"),
        (a.price_cache_read_per_mtok >= 0, "ai.price_cache_read_per_mtok must be >= 0"),
        (a.price_cache_write_per_mtok >= 0, "ai.price_cache_write_per_mtok must be >= 0"),
```

and next to `AI_ON_FAILURE` add:

```python
AI_EFFORTS = ("low", "medium", "high", "xhigh", "max")
```

In `src/tradebot/types.py`, extend `Decision`:

```python
@dataclass(frozen=True)
class Decision:
    signal: Signal
    approved: bool
    reason: str
    confidence: float
    filter_kind: str
    latency_ms: int = 0
    failure: str | None = None
    input_tokens: int = 0        # usage is attributed to the first decision of a batch
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
```

In `config.yaml`, replace the `ai:` section with:

```yaml
ai:
  filter: stub                # stub | claude | claude_cached
  model: claude-opus-5        # claude-sonnet-5 costs about 60% less (2 / 10 per MTok)
  candles_in_context: 30
  on_failure: reject          # reject | pass_through
  effort: low                 # classification-style task; raise if reasons look shallow
  max_tokens: 4000            # backstop only; thinking tokens count against it
  timeout_sec: 60
  max_calls_per_run: 10000    # spend guard per backtest; a 62-day replay is at most ~4650 calls
  price_in_per_mtok: 5.0      # list prices, used only for the report's cost line
  price_out_per_mtok: 25.0
  price_cache_read_per_mtok: 0.5
  price_cache_write_per_mtok: 6.25
```

Also update `BASE_CONFIG["ai"]` in `tests/helpers.py` to `{"filter": "stub", "model": "claude-opus-5", "candles_in_context": 30, "on_failure": "reject"}` (defaults cover the rest).

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (236).

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py src/tradebot/types.py config.yaml tests/helpers.py tests/test_config.py tests/test_types.py
git commit -m "feat(ai): config knobs for the Claude filter and token fields on Decision"
```

---

### Task 2: Store: token columns, schema migration, cache and compare queries

**Files:**
- Modify: `src/tradebot/store/schema.sql`, `src/tradebot/store/db.py`, `src/tradebot/store/repo.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_store.py`:

```python


def test_v1_database_is_migrated_to_v2(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL);
        CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, signal_id INTEGER NOT NULL,
                                   filter_kind TEXT NOT NULL, approved INTEGER NOT NULL, reason TEXT NOT NULL,
                                   confidence REAL NOT NULL, latency_ms INTEGER NOT NULL, failure TEXT);
        PRAGMA user_version = 1;
    """)
    raw.commit()
    raw.close()
    conn = connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    assert {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2


def test_ai_cache_roundtrip(repo):
    assert repo.get_ai_cache("A", 100, "h") is None
    repo.put_ai_cache("A", 100, "h", '{"approve": true}', created_at=5)
    repo.put_ai_cache("A", 100, "h", '{"approve": false}', created_at=6)  # replace
    assert repo.get_ai_cache("A", 100, "h") == '{"approve": false}'


def test_ai_usage_and_rejected_signals(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    s1 = repo.insert_signal("r1", _signal("A", 100))
    s2 = repo.insert_signal("r1", _signal("B", 100))
    repo.insert_ai_decision("r1", s1, "claude", False, "noise", 0.8, 900, None, input_tokens=1200, output_tokens=80)
    repo.insert_ai_decision("r1", s2, "claude", True, "fine", 0.6, 0, None)
    u = repo.ai_usage("r1")
    assert (u["calls"], u["input_tokens"], u["output_tokens"], u["decisions"]) == (1, 1200, 80, 2)
    assert u["avg_latency_ms"] == 900
    rej = repo.ai_rejected_signals("r1")
    assert [(r["symbol"], r["reason"]) for r in rej] == [("A", "noise")]


def test_positions_by_client_id(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 1, 100.0, 99.0, None, 5, "cid-a", "ema_rsi")
    pid = repo.insert_position("r1", p)
    repo.close_position(pid, 9, 101.0, "TARGET", 1.0)
    m = repo.positions_by_client_id("r1")
    assert m["cid-a"]["pnl"] == 1.0
```

- [ ] **Step 2: Run, expect failures**

Run: `.venv/bin/pytest tests/test_store.py -q`
Expected: 4 failed.

- [ ] **Step 3: Implement**

`src/tradebot/store/schema.sql`: in `ai_decisions`, after `failure     TEXT` add:

```sql
  failure     TEXT,
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0
```

(replace the existing `failure     TEXT` line, keeping the closing `);`).

`src/tradebot/store/db.py`: set `SCHEMA_VERSION = 2` and add, above `connect`:

```python
# Forward migrations keyed by the version they upgrade FROM. Each list runs inside one explicit
# transaction (Python 3.9's sqlite3 autocommits DDL otherwise), so a crash mid-way leaves the
# file at the old version rather than half-migrated. Every version below SCHEMA_VERSION must
# have an entry: a missing one means a column was added to schema.sql without a migration.
MIGRATIONS = {
    1: [
        "ALTER TABLE ai_decisions ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_decisions ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_decisions ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_decisions ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0",
    ],
}
```

and replace the version check block with:

```python
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found > SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"{path} has schema version {found}, code expects {SCHEMA_VERSION}; upgrade the code or delete the file"
        )
    if 0 < found < SCHEMA_VERSION:  # a fresh database (0) is created at the current version by schema.sql
        missing = [v for v in range(found, SCHEMA_VERSION) if v not in MIGRATIONS]
        if missing:
            conn.close()
            raise SchemaVersionError(f"no migration defined from schema version(s) {missing}; cannot open {path}")
        conn.execute("BEGIN")
        try:
            for v in range(found, SCHEMA_VERSION):
                for stmt in MIGRATIONS[v]:
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
    schema = resources.files("tradebot.store").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
```

Update the existing `test_file_backed_connect_uses_wal_persists_and_versions` test: the "refuse" case must now set `user_version` to `SCHEMA_VERSION + 1` (it already does) and the error message check is unchanged.

Also append the atomicity, missing-migration and failed-call-latency tests (see the repository's `tests/test_store.py` after this task: `_v1_db`, `test_migration_is_atomic_and_a_failed_one_leaves_the_old_version`, `test_missing_migration_entry_refuses_to_open`, `test_ai_usage_latency_includes_failed_calls`).

`src/tradebot/store/repo.py`: change `insert_ai_decision` to:

```python
    def insert_ai_decision(self, run_id: str, signal_id: int, filter_kind: str, approved: bool, reason: str,
                           confidence: float, latency_ms: int, failure: str | None,
                           input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                           cache_write_tokens: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO ai_decisions(run_id, signal_id, filter_kind, approved, reason, confidence, latency_ms, failure, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, signal_id, filter_kind, int(approved), reason, confidence, latency_ms, failure,
             input_tokens, output_tokens, cache_read_tokens, cache_write_tokens),
        )
        self.conn.commit()
```

and add these methods after `ai_rejection_count`:

```python
    def ai_usage(self, run_id: str) -> dict:
        """Token totals, call count (decisions carrying input tokens), failures, and mean latency over
        real calls including failed ones (a timeout is the slowest event and must not be excluded)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS decisions, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_write_tokens) AS cache_write_tokens, "
            "SUM(CASE WHEN input_tokens > 0 THEN 1 ELSE 0 END) AS calls, "
            "AVG(CASE WHEN input_tokens > 0 OR failure IS NOT NULL THEN latency_ms END) AS avg_latency_ms, "
            "SUM(CASE WHEN failure IS NOT NULL THEN 1 ELSE 0 END) AS failures "
            "FROM ai_decisions WHERE run_id=?", (run_id,)).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}

    def ai_rejected_signals(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.strategy, s.symbol, s.bar_ts, s.direction, d.reason, d.confidence "
            "FROM ai_decisions d JOIN signals s ON s.id = d.signal_id "
            "WHERE d.run_id=? AND d.approved=0 ORDER BY s.bar_ts, s.symbol", (run_id,)).fetchall()

    def positions_by_client_id(self, run_id: str) -> dict:
        """Backtest use only: adopted live positions share client_id '' and would collapse to one key."""
        return {r["client_id"]: r for r in self.list_positions(run_id)}

    # -- ai cache ----------------------------------------------------------
    def get_ai_cache(self, symbol: str, bar_ts: int, prompt_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT response_json FROM ai_cache WHERE symbol=? AND bar_ts=? AND prompt_hash=?",
            (symbol, bar_ts, prompt_hash)).fetchone()
        return row["response_json"] if row else None

    def put_ai_cache(self, symbol: str, bar_ts: int, prompt_hash: str, response_json: str, created_at: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_cache(symbol, bar_ts, prompt_hash, response_json, created_at) VALUES (?,?,?,?,?)",
            (symbol, bar_ts, prompt_hash, response_json, created_at))
        self.conn.commit()
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (243). Then open the real database once so it migrates: `.venv/bin/python -c "from tradebot.store.db import connect; c=connect('data/tradebot.db'); print(c.execute('PRAGMA user_version').fetchone()[0])"` prints `2`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/store tests/test_store.py
git commit -m "feat(store): schema v2 with AI token columns and migration; ai cache and compare queries"
```

---

### Task 3: Prompt rendering and hashing

**Files:**
- Create: `src/tradebot/ai/prompt.py`
- Test: `tests/test_ai_prompt.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ai_prompt.py
import hashlib
import json

from tradebot.ai.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, prompt_hash, render_candidates
from tradebot.types import Candidate, Candle, Signal

BAR = 1789357500  # 2026-09-14 09:15 IST


def _cand(sym="RELIANCE", bar_ts=BAR, direction="LONG", n=3):
    sig = Signal("ema_rsi", sym, direction, 1280.0, 1275.5, 1289.0, "MIS", bar_ts)
    candles = tuple(Candle(sym, bar_ts - 300 * (n - i), 1279.0 + i, 1281.0 + i, 1278.0 + i, 1280.0 + i, 1000 * (i + 1))
                    for i in range(n))
    return Candidate(sig, 222, {"rsi": 61.234567, "ema_fast": 1280.123456, "ema_slow": 1279.5, "atr": 3.0}, candles)


SESSION = {"square_off": "15:10", "entry_cutoff": "14:45", "close": "15:30", "bars_left": 70}


def test_render_is_deterministic_and_compact():
    a = render_candidates([_cand(), _cand("TCS")], SESSION)
    b = render_candidates([_cand(), _cand("TCS")], SESSION)
    assert a == b and ": " not in a and ", " not in a
    data = json.loads(a)
    assert data["bar_time"] == "2026-09-14T09:15:00+05:30" and data["session"] == SESSION
    c0 = data["candidates"][0]
    assert list(c0.keys())[:4] == ["index", "symbol", "direction", "product"] and list(c0.keys())[-1] == "candles"
    assert c0["index"] == 0 and c0["symbol"] == "RELIANCE" and c0["direction"] == "LONG"
    assert c0["entry"] == 1280.0 and c0["stop"] == 1275.5 and c0["target"] == 1289.0
    assert c0["stop_pct"] == 0.352 and c0["reward_risk"] == 2.0 and c0["quantity"] == 222 and c0["notional"] == 284160
    assert list(c0["indicators"]) == ["atr", "ema_fast", "ema_slow", "rsi"]  # sorted regardless of insertion order
    assert c0["indicators"]["rsi"] == 61.2346 and c0["indicators"]["ema_fast"] == 1280.1235
    assert c0["candles"][0] == ["09-14 09:00", 1279.0, 1281.0, 1278.0, 1280.0, 1000]
    assert len(c0["candles"]) == 3


def test_candle_rows_carry_the_date_so_overnight_gaps_are_visible():
    c = _cand(n=6)  # 09:15 with six prior 5-minute bars, but pretend the window spans a day boundary
    prev_day = tuple(Candle("RELIANCE", ts - 86400 * 3, 1, 2, 0.5, 1.5, 10) for ts in (BAR - 600, BAR - 300))
    c = Candidate(c.signal, c.quantity, c.indicators, prev_day + c.candles[-2:])
    rows = json.loads(render_candidates([c], SESSION))["candidates"][0]["candles"]
    assert rows[0][0].startswith("09-11") and rows[-1][0].startswith("09-14")


def test_render_order_follows_input_order():
    data = json.loads(render_candidates([_cand("TCS"), _cand("RELIANCE")], SESSION))
    assert [c["symbol"] for c in data["candidates"]] == ["TCS", "RELIANCE"]
    assert [c["index"] for c in data["candidates"]] == [0, 1]


def test_render_without_session_and_empty_batch():
    assert json.loads(render_candidates([_cand()]))["session"] is None
    assert render_candidates([]) == '{"bar_time":null,"session":null,"candidates":[]}'


def test_prompt_hash_is_sha256_of_system_and_user():
    user = render_candidates([_cand()], SESSION)
    h = prompt_hash(SYSTEM_PROMPT, user)
    assert h == hashlib.sha256((SYSTEM_PROMPT + "\n\n" + user).encode()).hexdigest()
    assert h != prompt_hash(SYSTEM_PROMPT, render_candidates([_cand("TCS")], SESSION))
    assert h != prompt_hash(SYSTEM_PROMPT + " ", user)  # a prompt edit misses the cache on purpose


def test_response_schema_shape():
    assert RESPONSE_SCHEMA["type"] == "object" and RESPONSE_SCHEMA["additionalProperties"] is False
    item = RESPONSE_SCHEMA["properties"]["decisions"]["items"]
    assert set(item["required"]) == {"index", "symbol", "approve", "confidence", "reason"}
    assert item["additionalProperties"] is False
    assert item["properties"]["confidence"] == {"type": "number", "minimum": 0, "maximum": 1}


def test_system_prompt_states_the_things_the_model_needs():
    for needle in ("[time, open, high, low, close, volume]", "MM-DD HH:MM", "overnight gap", "square-off",
                   "1 / (1 + R)", "between 0 and 1", "Approve a candidate unless"):
        assert needle in SYSTEM_PROMPT, needle
    assert len(SYSTEM_PROMPT.split()) >= 480, "must stay well above Opus 5's 512-token cacheable minimum"
```

- [ ] **Step 2: Run, expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/test_ai_prompt.py -q`

- [ ] **Step 3: Write `src/tradebot/ai/prompt.py`**

```python
"""Prompt text for the Claude filter. Byte-stable: the system prompt never changes between
calls (so it caches), and the user message is deterministic JSON so (symbol, bar_ts,
sha256(prompt)) is a reliable cache key (spec 7). Session facts (square-off time, bars left)
travel in the user message, never in the system prompt, so the cached prefix stays fixed."""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from tradebot.engine.clock import iso_ist, to_ist
from tradebot.types import Candidate

SYSTEM_PROMPT = """You review intraday equity trade candidates for an automated NSE strategy and decide, for each one, whether to approve or reject it. Your review is the last check before an order goes to the exchange, so both kinds of mistake cost money: a rejected winner is a real loss, and an approved loser is a real loss. Neither approving everything nor rejecting everything is useful.

How the candidates arise. A fast EMA crossing a slow EMA, confirmed by RSI, produces a candidate. Its stop is an ATR multiple from the entry and its target is a reward-to-risk multiple of that stop distance. Entries fill at the next bar's open. Intraday (MIS) positions that reach neither level are squared off at the session's square-off time, which the message states.

What you receive. For each candidate: the symbol, direction (LONG or SHORT), entry, stop and target prices, the stop distance as a percent of price, the reward-to-risk ratio, the planned quantity and notional, indicator values (ema_fast, ema_slow, rsi, atr), and recent 5-minute candles. Each candle row is [time, open, high, low, close, volume] with the time as MM-DD HH:MM in IST, oldest first; a change of date between rows is an overnight gap, not an intraday move. The message also gives the session facts: the current bar time, the square-off time, the entry cutoff, and how many 5-minute bars remain before square-off. You do not receive news or any data outside these candles.

Approve a candidate unless one of these clearly applies:
- the recent candles are flat or choppy, so the crossover is a wiggle rather than a change of direction
- the stop is inside the ordinary bar-to-bar range of the recent candles, so noise alone will hit it
- the candidate trades against the clear direction of the recent candles
- RSI reads as extreme after a stretch of near-identical closes, which means the indicator is stale rather than strong
- too few bars remain before square-off for the target to be reached at the pace the candles show

Two worked examples of the judgment. A LONG at 1280 with a stop at 1275.5 and a target at 1289, after eight candles whose closes drifted between 1278 and 1282 with ranges of 4 to 6 points each: the stop sits inside the ordinary bar range and the crossover is a wiggle, so reject with a confidence around 0.25. A SHORT at 842 with a stop at 847 and a target at 832, after a run of falling closes with widening ranges, a bounce that failed below the prior high, and 40 bars left: the direction is clear, the stop is beyond the failed bounce, and the target fits the pace, so approve with a confidence around 0.45.

Confidence is your probability, between 0 and 1, that the trade reaches its target before its stop or the square-off. A trade with reward-to-risk R is worth taking when that probability exceeds 1 / (1 + R): about 0.33 at R = 2. Let the approve decision follow from that comparison rather than from a 0.5 threshold. Give a one-sentence reason in plain words. Return one decision per candidate, keeping the candidate's index and symbol."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "symbol": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["index", "symbol", "approve", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}

_JSON = {"sort_keys": False, "separators": (",", ":")}


def _stamp(ts: int) -> str:
    """MM-DD HH:MM in IST: the date is what lets the model see an overnight gap in the window."""
    return to_ist(ts).strftime("%m-%d %H:%M")


def render_candidates(candidates: list[Candidate], session: Optional[dict] = None) -> str:
    """Deterministic JSON for one bar's batch. Key order is fixed by construction (trade parameters
    before the candle rows); indicators are sorted; floats are rounded so equal inputs give equal bytes.
    `session` is the dict of session facts (square_off, entry_cutoff, close, bars_left) or None."""
    if not candidates:
        return json.dumps({"bar_time": None, "session": session, "candidates": []}, **_JSON)
    bar_ts = candidates[0].signal.bar_ts
    items = []
    for i, c in enumerate(candidates):
        s = c.signal
        risk = abs(s.entry_price - s.stop_price)
        reward = abs(s.target_price - s.entry_price) if s.target_price is not None else None
        items.append({
            "index": i,
            "symbol": s.symbol,
            "direction": s.direction,
            "product": s.product,
            "entry": round(s.entry_price, 2),
            "stop": round(s.stop_price, 2),
            "target": None if s.target_price is None else round(s.target_price, 2),
            "stop_pct": round(risk / s.entry_price * 100, 3) if s.entry_price else None,
            "reward_risk": None if reward is None or risk == 0 else round(reward / risk, 2),
            "quantity": c.quantity,
            "notional": int(round(c.quantity * s.entry_price)),
            "indicators": {k: round(v, 4) for k, v in sorted(c.indicators.items())},
            "candles": [[_stamp(cd.ts), round(cd.open, 2), round(cd.high, 2), round(cd.low, 2), round(cd.close, 2), cd.volume]
                        for cd in c.candles],
        })
    return json.dumps({"bar_time": iso_ist(bar_ts), "session": session, "candidates": items}, **_JSON)


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n\n" + user).encode()).hexdigest()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_ai_prompt.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/ai/prompt.py tests/test_ai_prompt.py
git commit -m "feat(ai): deterministic prompt rendering, response schema and prompt hash"
```

---

### Task 4: Claude client wrapper

**Files:**
- Create: `src/tradebot/ai/claude_client.py`
- Test: `tests/test_claude_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_client.py
import types

import anthropic
import pytest

from tradebot.ai.claude_client import ClaudeClient, ClaudeReviewError, ReviewResponse


class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Usage:
    input_tokens = 1200
    output_tokens = 80
    cache_read_input_tokens = 900
    cache_creation_input_tokens = 300


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block("thinking"), _Block("text", text)]
        self.stop_reason = stop_reason
        self.usage = _Usage()
        self._request_id = "req_123"


class _Messages:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _client(outcome):
    c = ClaudeClient(api_key="k", model="claude-opus-5", effort="low", max_tokens=2000, timeout_sec=30)
    c._client = types.SimpleNamespace(messages=_Messages(outcome))
    return c


def test_review_sends_structured_output_request_and_parses():
    c = _client(_Resp('{"decisions": [{"index": 0, "symbol": "A", "approve": true, "confidence": 0.7, "reason": "ok"}]}'))
    r = c.review("SYS", "USER", {"type": "object"})
    assert isinstance(r, ReviewResponse)
    assert r.data["decisions"][0]["symbol"] == "A"
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.request_id) == (1200, 80, 900, 300, "req_123")
    assert r.latency_ms >= 0
    kw = c._client.messages.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["max_tokens"] == 2000
    assert kw["system"][0]["text"] == "SYS" and kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert "thinking" not in kw  # Opus 5 runs adaptive thinking by default


def _sdk_error(cls, status=None):
    """Instantiate an SDK exception without its initialiser: the wrapper only dispatches on class."""
    e = cls.__new__(cls)
    e.message = "boom"
    if status is not None:
        e.status_code = status
    return e


@pytest.mark.parametrize("exc", [
    _sdk_error(anthropic.APIConnectionError),
    _sdk_error(anthropic.RateLimitError, 429),
    _sdk_error(anthropic.APIStatusError, 500),
    _sdk_error(anthropic.APIResponseValidationError),
])
def test_transport_errors_become_review_errors(exc):
    with pytest.raises(ClaudeReviewError):
        _client(exc).review("S", "U", {})


def test_refusal_and_truncation_are_review_errors():
    with pytest.raises(ClaudeReviewError, match="refusal"):
        _client(_Resp("{}", stop_reason="refusal")).review("S", "U", {})
    with pytest.raises(ClaudeReviewError, match="max_tokens"):
        _client(_Resp('{"decisions": [', stop_reason="max_tokens")).review("S", "U", {})


def test_invalid_json_is_a_review_error():
    with pytest.raises(ClaudeReviewError, match="JSON"):
        _client(_Resp("not json")).review("S", "U", {})


def test_missing_key_is_rejected_early():
    with pytest.raises(ValueError):
        ClaudeClient(api_key="", model="claude-opus-5", effort="low", max_tokens=10, timeout_sec=1)
```

- [ ] **Step 2: Run, expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/test_claude_client.py -q`

- [ ] **Step 3: Write `src/tradebot/ai/claude_client.py`**

```python
"""Thin wrapper over the Anthropic SDK: one structured-output request, typed failure, usage numbers.
Nothing else in the project imports `anthropic`."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import anthropic


class ClaudeReviewError(RuntimeError):
    """The review could not produce a usable decision set. The filter applies ai.on_failure."""


@dataclass(frozen=True)
class ReviewResponse:
    data: dict
    latency_ms: int
    input_tokens: int          # uncached input tokens
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    request_id: Optional[str]


class ClaudeClient:
    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int, timeout_sec: int):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set in .env to use the Claude filter")
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # The SDK retries 429/5xx/connection errors twice with backoff on its own.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout_sec), max_retries=2)

    def review(self, system: str, user: str, schema: dict) -> ReviewResponse:
        t0 = time.monotonic()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.RateLimitError as e:
            raise ClaudeReviewError(f"rate limited after retries: {e}") from e
        except anthropic.APIStatusError as e:
            raise ClaudeReviewError(f"API error {getattr(e, 'status_code', '?')}: {getattr(e, 'message', e)}") from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ClaudeReviewError(f"connection error: {e}") from e
        except anthropic.APIError as e:  # response/webhook validation errors subclass APIError directly
            raise ClaudeReviewError(f"SDK error: {type(e).__name__}: {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise ClaudeReviewError("refusal: the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise ClaudeReviewError("max_tokens: response truncated; raise ai.max_tokens")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ClaudeReviewError(f"invalid JSON in response: {e}") from e
        usage = resp.usage
        return ReviewResponse(
            data=data,
            latency_ms=latency_ms,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            request_id=getattr(resp, "_request_id", None),
        )

    def count_tokens(self, system: str, user: str) -> int:
        """Exact input token count for a prompt, for the estimate-ai command."""
        r = self._client.messages.count_tokens(model=self.model, system=system,
                                               messages=[{"role": "user", "content": user}])
        return int(r.input_tokens)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_claude_client.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/ai/claude_client.py tests/test_claude_client.py
git commit -m "feat(ai): Anthropic client wrapper with structured outputs and typed failures"
```

---

### Task 5: Cache, ClaudeFilter, CachedClaudeFilter, build_filter

**Files:**
- Create: `src/tradebot/ai/cache.py`
- Modify: `src/tradebot/ai/filter.py`
- Test: `tests/test_ai_filter.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_ai_filter.py`:

```python


# -- Claude filters against a fake client --------------------------------------------------------
from tradebot.ai.cache import AICache
from tradebot.ai.claude_client import ClaudeReviewError, ReviewResponse
from tradebot.ai.filter import CachedClaudeFilter, ClaudeFilter
from tradebot.types import Candle


def _rich_cand(sym, bar_ts=1789357500):
    sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", bar_ts)
    candles = (Candle(sym, bar_ts - 300, 99.5, 100.2, 99.4, 100.0, 500),)
    return Candidate(sig, 10, {"rsi": 60.0, "ema_fast": 100.1, "ema_slow": 99.9, "atr": 1.0}, candles)


class FakeClaude:
    def __init__(self, decisions=None, error=None):
        self.decisions, self.error, self.calls = decisions, error, []

    def review(self, system, user, schema):
        self.calls.append(user)
        if self.error:
            raise self.error
        return ReviewResponse({"decisions": self.decisions}, 700, 1500, 90, 1000, 250, "req_x")


def _ai(**over):
    base = dict(filter="claude", model="claude-opus-5", candles_in_context=30, on_failure="reject")
    base.update(over)
    return AIConfig(**base)


def test_claude_filter_maps_decisions_by_index_and_symbol():
    fake = FakeClaude([
        {"index": 1, "symbol": "B", "approve": False, "confidence": 0.2, "reason": "chop"},
        {"index": 0, "symbol": "A", "approve": True, "confidence": 0.71, "reason": "clean break"},
    ])
    f = ClaudeFilter(_ai(), fake)
    out = f.review([_rich_cand("A"), _rich_cand("B")])
    check_filter_contract(f, [_rich_cand("A"), _rich_cand("B")])
    assert [d.approved for d in out] == [True, False]
    assert out[0].reason == "clean break" and out[0].confidence == 0.71 and out[0].filter_kind == "claude"
    assert (out[0].input_tokens, out[0].output_tokens, out[0].cache_read_tokens, out[0].cache_write_tokens) == (1500, 90, 1000, 250)
    assert (out[1].input_tokens, out[1].output_tokens) == (0, 0)  # usage attributed once per batch
    assert out[0].latency_ms == 700 and out[0].failure is None


def test_claude_filter_missing_or_mismatched_decision_uses_failure_policy():
    fake = FakeClaude([{"index": 0, "symbol": "WRONG", "approve": True, "confidence": 0.9, "reason": "x"}])
    out = ClaudeFilter(_ai(on_failure="reject"), fake).review([_rich_cand("A"), _rich_cand("B")])
    assert [d.approved for d in out] == [False, False]
    assert all(d.failure and d.filter_kind == "claude" for d in out)
    out2 = ClaudeFilter(_ai(on_failure="pass_through"), fake).review([_rich_cand("A")])
    assert out2[0].approved and out2[0].failure


def test_claude_filter_transport_failure_policy():
    fake = FakeClaude(error=ClaudeReviewError("rate limited"))
    out = ClaudeFilter(_ai(on_failure="reject"), fake).review([_rich_cand("A")])
    assert not out[0].approved and out[0].failure == "rate limited" and out[0].reason.startswith("ai_failure")
    out = ClaudeFilter(_ai(on_failure="pass_through"), fake).review([_rich_cand("A")])
    assert out[0].approved and out[0].failure == "rate limited"


def test_claude_filter_clamps_confidence_and_truncates_reason():
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 7.5, "reason": "r" * 500}])
    d = ClaudeFilter(_ai(), fake).review([_rich_cand("A")])[0]
    assert d.confidence == 1.0 and len(d.reason) == 200


def test_claude_filter_spend_guard():
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}])
    f = ClaudeFilter(_ai(max_calls_per_run=1), fake)
    assert f.review([_rich_cand("A")])[0].approved
    d = f.review([_rich_cand("A", bar_ts=1789357800)])[0]
    assert not d.approved and "max_calls_per_run" in d.failure and len(fake.calls) == 1


def test_cached_filter_hits_skip_the_call_and_misses_populate(repo):
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": False, "confidence": 0.3, "reason": "flat"}])
    f = CachedClaudeFilter(_ai(filter="claude_cached"), fake, AICache(repo))
    first = f.review([_rich_cand("A")])
    second = f.review([_rich_cand("A")])
    assert len(fake.calls) == 1
    assert first[0].approved is False and second[0].approved is False
    assert second[0].reason == "flat" and second[0].filter_kind == "claude_cached"
    assert second[0].input_tokens == 0 and second[0].latency_ms == 0  # cache hit costs nothing
    check_filter_contract(f, [_rich_cand("A")])


def test_cached_filter_partial_hit_calls_for_the_whole_batch(repo):
    fake = FakeClaude([
        {"index": 0, "symbol": "A", "approve": True, "confidence": 0.6, "reason": "a"},
        {"index": 1, "symbol": "B", "approve": True, "confidence": 0.6, "reason": "b"},
    ])
    f = CachedClaudeFilter(_ai(filter="claude_cached"), fake, AICache(repo))
    f.review([_rich_cand("A")])          # caches A alone under the single-candidate prompt
    f.review([_rich_cand("A"), _rich_cand("B")])  # different prompt bytes: call again, cache both
    assert len(fake.calls) == 2
    f.review([_rich_cand("A"), _rich_cand("B")])
    assert len(fake.calls) == 2


def test_cached_filter_does_not_cache_failures(repo):
    fake = FakeClaude(error=ClaudeReviewError("boom"))
    f = CachedClaudeFilter(_ai(filter="claude_cached"), fake, AICache(repo))
    f.review([_rich_cand("A")])
    fake.error = None
    fake.decisions = [{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}]
    assert f.review([_rich_cand("A")])[0].approved and len(fake.calls) == 2


def test_claude_filter_passes_session_facts_when_given_a_clock():
    import json
    from tradebot.config import SessionConfig
    from tradebot.engine.clock import SessionClock
    clock = SessionClock(SessionConfig("09:15", "15:30", "15:10", "14:45", ()), 5)
    fake = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}])
    ClaudeFilter(_ai(), fake, clock=clock).review([_rich_cand("A", bar_ts=1789357500)])  # 09:15 IST
    sent = json.loads(fake.calls[0])
    assert sent["session"] == {"square_off": "15:10", "entry_cutoff": "14:45", "close": "15:30", "bars_left": 70}
    fake2 = FakeClaude([{"index": 0, "symbol": "A", "approve": True, "confidence": 0.5, "reason": "ok"}])
    ClaudeFilter(_ai(), fake2).review([_rich_cand("A")])
    assert json.loads(fake2.calls[0])["session"] is None


def test_build_filter_claude_variants_need_key_and_repo(repo):
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_filter(_ai(filter="claude"), api_key="")
    assert isinstance(build_filter(_ai(filter="claude"), api_key="k"), ClaudeFilter)
    with pytest.raises(ValueError, match="repo"):
        build_filter(_ai(filter="claude_cached"), api_key="k")
    assert isinstance(build_filter(_ai(filter="claude_cached"), api_key="k", repo=repo), CachedClaudeFilter)
```

Also update the existing `test_build_filter_unknown_raises` in that file: `build_filter(cfg, api_key="")` with `filter="claude"` now raises for the missing key rather than for an unknown filter, so change its config to `filter="nope"` and keep the `pytest.raises(ValueError)`.

- [ ] **Step 2: Run, expect failures**

Run: `.venv/bin/pytest tests/test_ai_filter.py -q`

- [ ] **Step 3: Write `src/tradebot/ai/cache.py`**

```python
"""Decision cache keyed by (symbol, bar_ts, sha256(prompt)) so replays cost nothing (spec 7)."""
from __future__ import annotations

import json
import time
from typing import Optional

from tradebot.store.repo import Repo


class AICache:
    def __init__(self, repo: Repo):
        self.repo = repo

    def get(self, symbol: str, bar_ts: int, prompt_hash: str) -> Optional[dict]:
        raw = self.repo.get_ai_cache(symbol, bar_ts, prompt_hash)
        return json.loads(raw) if raw else None

    def put(self, symbol: str, bar_ts: int, prompt_hash: str, decision: dict) -> None:
        self.repo.put_ai_cache(symbol, bar_ts, prompt_hash, json.dumps(decision, sort_keys=True), int(time.time()))
```

- [ ] **Step 4: Rewrite `src/tradebot/ai/filter.py`**

```python
"""AI filter implementations behind one interface (spec 7).

StubFilter approves everything (backtest default). ClaudeFilter makes one structured-output
request per bar for all candidates. CachedClaudeFilter serves decisions from the ai_cache table
when the exact prompt was seen before, so AI-replay backtests are free after the first run."""
from __future__ import annotations

import logging
from typing import Optional, Protocol

from tradebot.ai.cache import AICache
from tradebot.ai.claude_client import ClaudeClient, ClaudeReviewError, ReviewResponse
from tradebot.ai.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, prompt_hash, render_candidates
from tradebot.config import AIConfig
from tradebot.engine.clock import SessionClock, date_of
from tradebot.types import Candidate, Decision

log = logging.getLogger("tradebot.ai")
REASON_MAX = 200


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


def _clamp(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, v))


class ClaudeFilter:
    kind = "claude"

    def __init__(self, cfg: AIConfig, client: ClaudeClient, cache: Optional[AICache] = None,
                 clock: Optional[SessionClock] = None):
        self.cfg = cfg
        self.client = client
        self.cache = cache
        self.clock = clock  # supplies the session facts the prompt's "bars left" rule needs
        self.calls = 0

    def _session(self, bar_ts: int) -> Optional[dict]:
        if self.clock is None:
            return None
        sq = self.clock.square_off_bar_ts(date_of(bar_ts))
        return {"square_off": self.clock.session.square_off, "entry_cutoff": self.clock.session.no_new_entries_after,
                "close": self.clock.session.close, "bars_left": max(0, (sq - bar_ts) // self.clock.interval_sec)}

    # -- interface --------------------------------------------------------------------------
    def review(self, candidates: list[Candidate]) -> list[Decision]:
        if not candidates:
            return []
        user = render_candidates(candidates, self._session(candidates[0].signal.bar_ts))
        h = prompt_hash(SYSTEM_PROMPT, user)
        if self.cache is not None:
            cached = [self.cache.get(c.signal.symbol, c.signal.bar_ts, h) for c in candidates]
            if all(d is not None for d in cached):
                return [self._from_dict(c, d, None, i) for i, (c, d) in enumerate(zip(candidates, cached))]
        if self.calls >= self.cfg.max_calls_per_run:
            return self._failed(candidates, f"max_calls_per_run ({self.cfg.max_calls_per_run}) reached")
        self.calls += 1
        try:
            resp = self.client.review(SYSTEM_PROMPT, user, RESPONSE_SCHEMA)
        except ClaudeReviewError as e:
            log.warning("claude review failed: %s", e)
            return self._failed(candidates, str(e))
        by_index = {}
        for d in resp.data.get("decisions", []) if isinstance(resp.data, dict) else []:
            if isinstance(d, dict) and isinstance(d.get("index"), int):
                by_index[d["index"]] = d
        out = []
        for i, c in enumerate(candidates):
            d = by_index.get(i)
            if d is None or d.get("symbol") != c.signal.symbol:
                out.append(self._failed([c], f"no decision for index {i} {c.signal.symbol}", resp, i)[0])
                continue
            out.append(self._from_dict(c, d, resp, i))
            if self.cache is not None:
                self.cache.put(c.signal.symbol, c.signal.bar_ts, h, d)
        return out

    # -- helpers ----------------------------------------------------------------------------
    def _from_dict(self, c: Candidate, d: dict, resp: Optional[ReviewResponse], i: int) -> Decision:
        usage = resp if (resp is not None and i == 0) else None  # batch usage attributed to decision 0
        return Decision(
            c.signal, bool(d.get("approve")), str(d.get("reason", ""))[:REASON_MAX], _clamp(d.get("confidence")),
            self.kind, latency_ms=resp.latency_ms if resp is not None else 0, failure=None,
            input_tokens=usage.input_tokens if usage else 0, output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
        )

    def _failed(self, candidates: list[Candidate], why: str, resp: Optional[ReviewResponse] = None,
                first_index: int = 0) -> list[Decision]:
        approved = self.cfg.on_failure == "pass_through"
        out = []
        for i, c in enumerate(candidates):
            usage = resp if (resp is not None and first_index + i == 0) else None
            out.append(Decision(
                c.signal, approved, f"ai_failure: {why}"[:REASON_MAX], 0.0, self.kind,
                latency_ms=resp.latency_ms if resp is not None else 0, failure=why[:REASON_MAX],
                input_tokens=usage.input_tokens if usage else 0, output_tokens=usage.output_tokens if usage else 0,
                cache_read_tokens=usage.cache_read_tokens if usage else 0,
                cache_write_tokens=usage.cache_write_tokens if usage else 0,
            ))
        return out


class CachedClaudeFilter(ClaudeFilter):
    kind = "claude_cached"

    def __init__(self, cfg: AIConfig, client: ClaudeClient, cache: AICache, clock: Optional[SessionClock] = None):
        super().__init__(cfg, client, cache, clock)


def build_filter(cfg: AIConfig, api_key: str, repo=None, clock: Optional[SessionClock] = None) -> AIFilter:
    if cfg.filter == "stub":
        return StubFilter()
    if cfg.filter not in ("claude", "claude_cached"):
        raise ValueError(f"unknown ai.filter '{cfg.filter}'")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY must be set in .env to use the Claude filter")
    client = ClaudeClient(api_key, cfg.model, cfg.effort, cfg.max_tokens, cfg.timeout_sec)
    if cfg.filter == "claude":
        return ClaudeFilter(cfg, client, clock=clock)
    if repo is None:
        raise ValueError("claude_cached needs a repo for the ai_cache table")
    return CachedClaudeFilter(cfg, client, AICache(repo), clock)
```

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: all pass. The engine's `_record` persists the new token fields only after Task 6; for now they are carried on the `Decision` object.

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/ai tests/test_ai_filter.py
git commit -m "feat(ai): ClaudeFilter with batch prompt, failure policy, spend guard, and cached replay"
```

---

### Task 6: Engine persists token usage

**Files:**
- Modify: `src/tradebot/engine/loop.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_engine.py`:

```python


def test_engine_persists_ai_token_usage(repo, tmp_path):
    from tradebot.types import Decision

    class Priced:
        kind = "priced"

        def review(self, cands):
            return [Decision(c.signal, True, "ok", 0.5, self.kind, latency_ms=300, input_tokens=1000 if i == 0 else 0,
                             output_tokens=50 if i == 0 else 0, cache_read_tokens=700 if i == 0 else 0)
                    for i, c in enumerate(cands)]

    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles(), ai=Priced())
    u = repo.ai_usage("t1")
    assert u["calls"] >= 1 and u["input_tokens"] == 1000 * u["calls"] and u["cache_read_tokens"] == 700 * u["calls"]
```

- [ ] **Step 2: Run, expect failure** (`input_tokens` stays 0 because `_place` does not pass them).

- [ ] **Step 3: Implement**

In `src/tradebot/engine/loop.py`, in `_place`, change the `insert_ai_decision` call to:

```python
            self.repo.insert_ai_decision(self.run_id, sid, dec.filter_kind, dec.approved, dec.reason,
                                         dec.confidence, dec.latency_ms, dec.failure,
                                         input_tokens=dec.input_tokens, output_tokens=dec.output_tokens,
                                         cache_read_tokens=dec.cache_read_tokens,
                                         cache_write_tokens=dec.cache_write_tokens)
```

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/loop.py tests/test_engine.py
git commit -m "feat(engine): persist AI token usage per decision"
```

---

### Task 7: Compare report

**Files:**
- Create: `src/tradebot/report/compare.py`
- Test: `tests/test_compare.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compare.py
import pytest

from tradebot.report.compare import Prices, build_compare, format_compare
from tradebot.types import Position, Signal, make_client_id

P = Prices(5.0, 25.0, 0.5, 6.25)


def _seed_pair(repo):
    """Run A (stub) took three trades; run B (claude) rejected two of them and approved one."""
    repo.create_run("A", "backtest", 0, "{}")
    repo.create_run("B", "backtest", 0, "{}")
    trades = [("X", 1000, 50.0), ("Y", 1300, -30.0), ("Z", 1600, 20.0)]  # symbol, bar_ts, pnl in A
    for sym, ts, pnl in trades:
        sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", ts)
        sa = repo.insert_signal("A", sig)
        repo.insert_ai_decision("A", sa, "stub", True, "stub", 1.0, 0, None)
        cid = make_client_id("ema_rsi", sym, ts)
        pid = repo.insert_position("A", Position(sym, "MIS", "LONG", 10, 100.0, 99.0, 102.0, ts + 300, cid, "ema_rsi"))
        repo.close_position(pid, ts + 900, 100.0 + pnl / 10, "TARGET" if pnl > 0 else "STOP", pnl)
        sb = repo.insert_signal("B", sig)
        approve = sym == "Z"
        repo.insert_ai_decision("B", sb, "claude", approve, "fine" if approve else "chop", 0.6, 800, None,
                                input_tokens=1000, output_tokens=60, cache_read_tokens=500, cache_write_tokens=100)
        if approve:
            pid = repo.insert_position("B", Position(sym, "MIS", "LONG", 10, 100.0, 99.0, 102.0, ts + 300, cid, "ema_rsi"))
            repo.close_position(pid, ts + 900, 102.0, "TARGET", pnl)
    # a signal Claude rejected that A never filled (unfilled in A): must not count as avoided PnL
    sig = Signal("ema_rsi", "W", "LONG", 100.0, 99.0, 102.0, "MIS", 1900)
    repo.insert_ai_decision("A", repo.insert_signal("A", sig), "stub", True, "stub", 1.0, 0, None)
    repo.insert_ai_decision("B", repo.insert_signal("B", sig), "claude", False, "late", 0.4, 0, None)
    repo.upsert_daily_pnl("A", "2026-09-14", 40.0, 0.0, 3, 3)
    repo.upsert_daily_pnl("B", "2026-09-14", 20.0, 0.0, 1, 1)


def test_build_compare_attributes_rejected_pnl(repo):
    _seed_pair(repo)
    c = build_compare(repo, "A", "B", P)
    assert c.a.run_id == "A" and c.b.run_id == "B"
    assert c.rejected == 3 and c.rejected_with_position_in_a == 2
    assert c.rejected_pnl_in_a == pytest.approx(20.0)      # +50 and -30 avoided: net +20 given up
    assert c.rejected_losses_avoided == pytest.approx(30.0) and c.rejected_wins_forgone == pytest.approx(50.0)
    assert c.total_pnl_delta == pytest.approx(20.0 - 40.0)
    assert c.calls == 3 and c.input_tokens == 3000 and c.output_tokens == 180
    assert (c.cache_read_tokens, c.cache_write_tokens) == (1500, 300)
    assert c.est_cost_usd == pytest.approx((3000 * 5.0 + 180 * 25.0 + 1500 * 0.5 + 300 * 6.25) / 1e6)
    assert [r["symbol"] for r in c.rows] == ["X", "Y", "W"]
    assert c.rows[2]["pnl_in_a"] is None


def test_format_compare_reads_sensibly(repo):
    _seed_pair(repo)
    text = format_compare(build_compare(repo, "A", "B", P))
    assert "Rejected by Claude" in text and "X" in text and "chop" in text
    assert "Estimated cost" in text and "$0.0" in text
    assert "Verdict" in text


def test_compare_requires_both_runs(repo):
    repo.create_run("A", "backtest", 0, "{}")
    with pytest.raises(ValueError):
        build_compare(repo, "A", "missing", P)
```

- [ ] **Step 2: Run, expect ModuleNotFoundError**

- [ ] **Step 3: Write `src/tradebot/report/compare.py`**

```python
"""Stub-versus-Claude comparison (spec 14): the same signals, one run with every signal taken and
one with Claude filtering, joined by the deterministic client id so the report can say what
the rejected signals earned in the unfiltered run."""
from __future__ import annotations

from dataclasses import dataclass, field

from tradebot.engine.clock import iso_ist
from tradebot.report.summary import Summary, build_summary
from tradebot.store.repo import Repo
from tradebot.types import make_client_id


@dataclass(frozen=True)
class Prices:
    """USD per million tokens by kind, from ai.price_* config."""
    input: float
    output: float
    cache_read: float
    cache_write: float

    def cost(self, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output
                + cache_read * self.cache_read + cache_write * self.cache_write) / 1e6


@dataclass
class Compare:
    a: Summary
    b: Summary
    rejected: int
    rejected_with_position_in_a: int
    rejected_pnl_in_a: float        # net PnL of rejected signals in run A (what the filter gave up or avoided)
    rejected_losses_avoided: float  # sum of losses among rejected signals (positive number)
    rejected_wins_forgone: float    # sum of wins among rejected signals
    total_pnl_delta: float          # b.total_pnl - a.total_pnl
    calls: int
    failures: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    avg_latency_ms: float
    est_cost_usd: float
    rows: list = field(default_factory=list)


def build_compare(repo: Repo, run_a: str, run_b: str, prices: Prices) -> Compare:
    a, b = build_summary(repo, run_a), build_summary(repo, run_b)  # raises ValueError for unknown runs
    positions_a = repo.positions_by_client_id(run_a)
    rows, with_pos, net, losses, wins = [], 0, 0.0, 0.0, 0.0
    for r in repo.ai_rejected_signals(run_b):
        cid = make_client_id(r["strategy"], r["symbol"], r["bar_ts"])
        pos = positions_a.get(cid)
        pnl = None if pos is None or pos["pnl"] is None else float(pos["pnl"])
        if pnl is not None:
            with_pos += 1
            net += pnl
            if pnl < 0:
                losses += -pnl
            else:
                wins += pnl
        rows.append({"symbol": r["symbol"], "bar": iso_ist(r["bar_ts"]), "direction": r["direction"],
                     "reason": r["reason"], "confidence": r["confidence"], "pnl_in_a": pnl,
                     "exit_in_a": pos["exit_reason"] if pos is not None else None})
    u = repo.ai_usage(run_b)
    cost = prices.cost(int(u["input_tokens"]), int(u["output_tokens"]), int(u["cache_read_tokens"]),
                       int(u["cache_write_tokens"]))
    return Compare(
        a=a, b=b, rejected=len(rows), rejected_with_position_in_a=with_pos, rejected_pnl_in_a=net,
        rejected_losses_avoided=losses, rejected_wins_forgone=wins, total_pnl_delta=b.total_pnl - a.total_pnl,
        calls=int(u["calls"]), failures=int(u["failures"]), input_tokens=int(u["input_tokens"]),
        output_tokens=int(u["output_tokens"]), cache_read_tokens=int(u["cache_read_tokens"]),
        cache_write_tokens=int(u["cache_write_tokens"]),
        avg_latency_ms=float(u["avg_latency_ms"]), est_cost_usd=cost, rows=rows,
    )


def _side_by_side(a: Summary, b: Summary) -> list:
    metrics = [
        ("Trades", f"{a.trades}", f"{b.trades}"),
        ("Win rate", f"{a.win_rate * 100:.1f}%", f"{b.win_rate * 100:.1f}%"),
        ("Total PnL", f"{a.total_pnl:,.2f}", f"{b.total_pnl:,.2f}"),
        ("Avg R", f"{a.avg_r:.2f}", f"{b.avg_r:.2f}"),
        ("Max DD (closed)", f"{a.max_drawdown:,.2f}", f"{b.max_drawdown:,.2f}"),
        ("Max DD (equity)", f"{a.max_drawdown_equity:,.2f}", f"{b.max_drawdown_equity:,.2f}"),
        ("AI rejects", f"{a.ai_rejections}", f"{b.ai_rejections}"),
    ]
    out = [f"{'Metric':<16} {'A: ' + a.run_id:>18} {'B: ' + b.run_id:>18}"]
    out += [f"{m:<16} {va:>18} {vb:>18}" for m, va, vb in metrics]
    return out


def format_compare(c: Compare) -> str:
    lines = [f"Compare  A={c.a.run_id} (unfiltered)  vs  B={c.b.run_id} (Claude filter)", ""]
    lines += _side_by_side(c.a, c.b)
    lines += ["",
              f"Rejected by Claude      {c.rejected} signals, {c.rejected_with_position_in_a} of which A actually traded",
              f"  losses avoided        {c.rejected_losses_avoided:,.2f}",
              f"  wins forgone          {c.rejected_wins_forgone:,.2f}",
              f"  net PnL given up      {c.rejected_pnl_in_a:,.2f}   (negative means the filter removed net losers)",
              f"Total PnL delta (B-A)   {c.total_pnl_delta:,.2f}",
              f"Calls / failures        {c.calls} / {c.failures}   avg latency {c.avg_latency_ms:.0f} ms",
              f"Tokens in/out/cached    {c.input_tokens:,} / {c.output_tokens:,} / {c.cache_read_tokens:,} (cache writes {c.cache_write_tokens:,})",
              f"Estimated cost          ${c.est_cost_usd:,.2f}"]
    verdict = ("filter helped" if c.total_pnl_delta > c.est_cost_usd * 0 and c.total_pnl_delta > 0
               else "filter did not help on this window")
    lines += ["", f"Verdict: {verdict} (PnL delta {c.total_pnl_delta:,.2f}, cost ${c.est_cost_usd:,.2f})"]
    if c.rows:
        lines += ["", f"{'Bar (IST)':<25} {'Symbol':<12} {'Dir':<5} {'PnL in A':>10} {'Exit A':<11} Reason"]
        for r in c.rows:
            pnl = "unfilled" if r["pnl_in_a"] is None else f"{r['pnl_in_a']:,.2f}"
            lines.append(f"{r['bar']:<25} {r['symbol']:<12} {r['direction']:<5} {pnl:>10} {str(r['exit_in_a'] or '-'):<11} "
                         f"{r['reason']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests** — Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/report/compare.py tests/test_compare.py
git commit -m "feat(report): stub-vs-Claude compare report"
```

---

### Task 8: CLI: --ai override, report --compare, estimate-ai

**Files:**
- Modify: `src/tradebot/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_cli.py`:

```python


def test_backtest_ai_override_and_compare_report(tmp_path, monkeypatch):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "stub-run")
    assert ok.exit_code == 0, ok.output

    from tradebot.ai import filter as filter_mod
    from tradebot.ai.claude_client import ReviewResponse

    class FakeClaude:
        def __init__(self, *a, **k):
            pass

        def review(self, system, user, schema):
            import json
            cands = json.loads(user)["candidates"]
            return ReviewResponse({"decisions": [{"index": c["index"], "symbol": c["symbol"], "approve": c["index"] % 2 == 0,
                                                  "confidence": 0.5, "reason": "test"} for c in cands]}, 10, 500, 20, 0, 0, None)

    monkeypatch.setattr(filter_mod, "ClaudeClient", FakeClaude)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "claude-run",
                  "--ai", "claude_cached")
    assert res.exit_code == 0, res.output
    assert "AI rejects" in res.output
    cmp_ = _invoke(tmp_path, "report", "--compare", "stub-run", "claude-run")
    assert cmp_.exit_code == 0, cmp_.output
    assert "Rejected by Claude" in cmp_.output and "Estimated cost" in cmp_.output
    # second claude_cached run makes zero calls: cost line shows 0 tokens
    res2 = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "claude-run-2",
                   "--ai", "claude_cached")
    assert res2.exit_code == 0, res2.output
    cmp2 = _invoke(tmp_path, "report", "--compare", "stub-run", "claude-run-2")
    assert "Tokens in/out/cached    0 / 0 / 0 (cache writes 0)" in cmp2.output


def test_backtest_claude_without_key_is_clean_error(tmp_path):
    _setup(tmp_path)
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--ai", "claude")
    assert res.exit_code == 1 and "ANTHROPIC_API_KEY" in res.output


def test_estimate_ai_reports_calls_and_cost(tmp_path, monkeypatch):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "stub-run")
    assert ok.exit_code == 0, ok.output
    from tradebot import cli as cli_mod

    class Counter:
        def __init__(self, *a, **k):
            pass

        def count_tokens(self, system, user):
            return 1500

    monkeypatch.setattr(cli_mod, "ClaudeClient", Counter)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "estimate-ai", "--run", "stub-run")
    assert res.exit_code == 0, res.output
    assert "bars with candidates" in res.output and "estimated cost" in res.output
```

- [ ] **Step 2: Run, expect failures**

- [ ] **Step 3: Implement in `src/tradebot/cli.py`**

Add imports:

```python
from dataclasses import replace as dc_replace

from tradebot.ai.claude_client import ClaudeClient
from tradebot.ai.prompt import SYSTEM_PROMPT, render_candidates
from tradebot.report.compare import Prices, build_compare, format_compare
from tradebot.types import Candidate, Candle, Signal
```

In `backtest`, add the option and wiring:

```python
@click.option("--ai", "ai_filter", default=None, type=click.Choice(["stub", "claude", "claude_cached"]),
              help="Override ai.filter from config for this run")
```

and the function signature gains `ai_filter: Optional[str]` as its last parameter. Then replace this exact block in `backtest`:

```python
    engine = BacktestEngine(cfg, repo, source, [strategy], broker,
                            build_filter(cfg.ai, cfg.secrets.anthropic_api_key),
                            SessionClock(cfg.session, interval), lots, run_id)
```

with:

```python
    ai_cfg = dc_replace(cfg.ai, filter=ai_filter) if ai_filter else cfg.ai
    clock = SessionClock(cfg.session, interval)
    ai = build_filter(ai_cfg, cfg.secrets.anthropic_api_key, repo=repo, clock=clock)
    # The engine stores the resolved config with the run, so record the effective filter there too.
    engine = BacktestEngine(dc_replace(cfg, ai=ai_cfg), repo, source, [strategy], broker, ai, clock, lots, run_id)
```

Replace the `report` command with:

```python
@main.command()
@click.option("--run", "run_id", default=None)
@click.option("--compare", "compare", nargs=2, default=None, metavar="RUN_A RUN_B",
              help="Side-by-side of an unfiltered run A and a Claude-filtered run B")
@click.pass_obj
def report(cfg: Config, run_id: Optional[str], compare) -> None:
    """Print the summary for a stored run, or compare two runs."""
    if not Path(cfg.paths.db).exists():
        raise click.ClickException(f"no database at {cfg.paths.db}")
    if not run_id and not compare:
        raise click.ClickException("give --run RUN or --compare RUN_A RUN_B")
    repo = Repo(connect(cfg.paths.db))
    if compare:
        click.echo(format_compare(build_compare(repo, compare[0], compare[1], _prices(cfg))))
        return
    click.echo(format_summary(build_summary(repo, run_id)))
```

Add the estimate command:

```python
def _prices(cfg: Config) -> Prices:
    return Prices(cfg.ai.price_in_per_mtok, cfg.ai.price_out_per_mtok,
                  cfg.ai.price_cache_read_per_mtok, cfg.ai.price_cache_write_per_mtok)


@main.command("estimate-ai")
@click.option("--run", "run_id", required=True, help="A completed stub run whose risk-approved signals define the workload")
@click.pass_obj
def estimate_ai(cfg: Config, run_id: str) -> None:
    """Estimate calls, tokens and cost of replaying a run through the Claude filter.

    Counts tokens once on the largest bar's prompt via the API, then scales by the number of
    bars that had at least one risk-approved candidate."""
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is None:
        raise click.ClickException(f"unknown run: {run_id}")
    rows = repo.conn.execute(
        "SELECT s.bar_ts, COUNT(*) AS n FROM risk_decisions r JOIN signals s ON s.id = r.signal_id "
        "WHERE r.run_id=? AND r.approved=1 GROUP BY s.bar_ts", (run_id,)).fetchall()
    if not rows:
        raise click.ClickException("that run has no risk-approved signals")
    bars = len(rows)
    candidates = sum(r["n"] for r in rows)
    biggest = max(r["n"] for r in rows)
    client = ClaudeClient(cfg.secrets.anthropic_api_key, cfg.ai.model, cfg.ai.effort, cfg.ai.max_tokens, cfg.ai.timeout_sec)
    sample = [Candidate(Signal("ema_rsi", f"SYM{i}", "LONG", 1280.0, 1275.5, 1289.0, "MIS", 1789357500), 200,
                        {"ema_fast": 1280.1, "ema_slow": 1279.9, "rsi": 61.2, "atr": 3.1},
                        tuple(Candle(f"SYM{i}", 1789357500 - 300 * k, 1279.0, 1281.0, 1278.0, 1280.0, 12345)
                              for k in range(cfg.ai.candles_in_context, 0, -1)))
              for i in range(biggest)]
    session = {"square_off": cfg.session.square_off, "entry_cutoff": cfg.session.no_new_entries_after,
               "close": cfg.session.close, "bars_left": 40}
    per_biggest = client.count_tokens(SYSTEM_PROMPT, render_candidates(sample, session))
    per_candidate = per_biggest / biggest
    input_tokens = int(candidates * per_candidate)
    output_tokens = candidates * 60
    cost = _prices(cfg).cost(input_tokens, output_tokens, 0, 0)
    click.echo(f"bars with candidates: {bars}   candidates: {candidates}   largest bar: {biggest}")
    click.echo(f"tokens per candidate: {per_candidate:.0f}   input total: {input_tokens:,}   output total ~{output_tokens:,}")
    click.echo(f"estimated cost ({cfg.ai.model}): ${cost:,.2f} as an upper bound; the shared system prompt caches, "
               f"and a cached replay costs $0")
```

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/cli.py tests/test_cli.py
git commit -m "feat(cli): --ai override, report --compare, estimate-ai"
```

---

### Task 9: The real replay

**Files:** none new; README section.

- [ ] **Step 1: Estimate**

Run: `.venv/bin/tradebot estimate-ai --run real-1`
Record the printed cost. Stop and ask the operator before Step 2 if it exceeds what they said they would spend.

- [ ] **Step 2: Replay with the cached filter**

Run: `.venv/bin/tradebot backtest --start 2026-06-17 --end 2026-09-12 --ai claude_cached --run-id real-1-claude`
Expected: a summary with a non-zero `AI rejects` line. The run takes roughly `calls × latency`; expect tens of minutes.

- [ ] **Step 3: Compare**

Run: `.venv/bin/tradebot report --compare real-1 real-1-claude`
Expected: the side-by-side, the rejected-signal table, and a cost line consistent with Step 1.

- [ ] **Step 4: Rerun to prove the cache**

Run: `.venv/bin/tradebot backtest --start 2026-06-17 --end 2026-09-12 --ai claude_cached --run-id real-1-claude-2 && .venv/bin/tradebot report --compare real-1 real-1-claude-2`
Expected: identical decisions, `Tokens in/out/cached 0 / 0 / 0`.

- [ ] **Step 5: README**

Add under "Backtest":

```markdown
## Claude filter

    .venv/bin/tradebot estimate-ai --run <stub run>                       # cost before you spend
    .venv/bin/tradebot backtest --start ... --end ... --ai claude_cached  # one paid pass, then free
    .venv/bin/tradebot report --compare <stub run> <claude run>

Decisions are cached by (symbol, bar, prompt hash) in `data/tradebot.db`, so reruns over the same
data cost nothing. Change `ai.model` or the prompt text and the cache misses on purpose.
```

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: Claude filter workflow"
```

---

## Self-review notes

- Spec 7: interface unchanged; batched one call per bar; StubFilter default; ClaudeFilter with structured JSON; CachedClaudeFilter keyed `(symbol, bar_ts, sha256(prompt))` in `ai_cache`; `ai.on_failure` reject/pass_through with the failure recorded; every decision stored with run id and, new, token usage.
- Spec 14: `report --compare` side by side plus per-signal table of rejected signals and what they earned in the unfiltered run; cost line from stored usage.
- Not in scope: Plan 3 (paper/live), any strategy tuning.
