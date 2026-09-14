# NSE/BSE Trading Bot — Design Spec

Date: 2026-09-14
Status: Approved design, pending implementation plan

## 1. Goal

A Python trading bot for Indian equities via the Groww Trade API. Rule-based
strategies generate signals, a risk engine sizes and vets them, Claude filters
them, and an execution backend places orders. One strategy interface runs
unchanged across three modes: backtest, paper, and live.

## 2. Decisions

| Topic | Decision |
|---|---|
| Broker | Groww Trade API, Python SDK `growwapi` 1.5.0 |
| Auth | TOTP flow (`GrowwAPI.get_access_token(api_key, totp)`); no token expiry |
| Instruments | NSE/BSE equity cash now; F&O later via the same interfaces |
| Holding period | Intraday (MIS) and swing (CNC), declared per strategy |
| Direction | Long and short; shorts intraday only |
| Universe | `universe.yaml`, shipped with NIFTY 200 constituents |
| First strategy | EMA crossover + RSI filter, 5-minute bars, intraday |
| AI role | Hybrid: rules generate, Claude approves/rejects |
| AI context | Signal, indicator snapshot, last 30 candles. No news, no breadth. |
| Capital | Paper first, then small real capital |
| Runtime | This Mac, CLI, logs to file. No alerts, no dashboard. |
| Storage | SQLite, plain SQL, no ORM |
| Backtester | Built in, event-driven, same code path as live |

## 3. Architecture

```
Candle source ──> Strategy(s) ──> Signal ──> Risk engine ──> AI filter ──> Execution backend ──> SQLite
   (backtest /        │                        (size, caps,    (rank/veto)    (backtest / paper / live)
    paper / live)     └── indicators            daily loss)
```

Single Python 3.11 process. A CLI mode flag selects the candle source and the
execution backend. Everything else is shared.

### 3.1 Packages

| Package | Responsibility | Depends on |
|---|---|---|
| `data/` | Candle sources (historical, live), instrument master | `store/`, Groww adapter |
| `strategy/` | `Strategy` base, indicators, `ema_rsi` | nothing external |
| `risk/` | Signal → approved order or rejection | portfolio state, config |
| `ai/` | Claude filter, prompt, response cache | `store/`, Anthropic SDK |
| `execution/` | `Broker` interface; backtest, paper, live backends; Groww adapter | `store/`, Groww SDK |
| `store/` | SQLite schema and repositories | sqlite3 |
| `engine/` | Clock, main loop, square-off scheduler, reconciliation | all of the above |
| `report/` | Run summaries and AI-stub vs AI-replay comparison | `store/` |
| `cli.py` | `backtest`, `paper`, `live`, `fetch-data`, `report`, `flatten` | `engine/` |

### 3.2 Core types

- `Candle(symbol, ts, open, high, low, close, volume, source)` where `source` is
  `official` or `tick_built`.
- `Signal(strategy, symbol, direction, entry_price, stop_price, target_price | None,
  product, bar_ts)`. `stop_price` is mandatory.
- `Order(client_id, signal, quantity, limit_price, status, broker_order_id | None)`.
- `Position(symbol, product, quantity, avg_price, stop_price, target_price,
  exit_order_ids, opened_at)`.

## 4. Data

### 4.1 Universe and instruments

On startup the engine downloads the Groww instrument master CSV
(`https://growwapi-assets.groww.in/instruments/instrument.csv`), caches it to
`data/instruments.csv` with a date stamp, and resolves every symbol in
`universe.yaml` to `exchange_token`, `lot_size`, `tick_size`, and tradeability
flags. Symbols that are missing or not tradeable are dropped and logged.

Survivorship bias: `universe.yaml` is today's NIFTY 200. Backtests therefore
overstate results. This is documented in the report header. The universe loader
takes an optional `as_of` date parameter; it is ignored for now and is the hook
for point-in-time constituents later.

### 4.2 Historical source (backtest)

`fetch-data` pulls candles from `get_historical_candle_data` in chunks that
respect Groww's per-interval max duration (5-minute: 15 days per call, 3 months
lookback) and writes them to the `candles` table. Backtests read only from
SQLite.

Groww only serves three months of intraday history, which is a thin backtest
window. `fetch-data` is therefore incremental and idempotent: for each symbol
and interval it reads the latest stored `ts`, fetches from there to now, and
inserts with `INSERT OR IGNORE` on the unique key. Running it weekly grows the
local cache past Groww's window indefinitely. A `--full` flag refetches the
whole available window for repairs.

### 4.3 Live source (paper and live)

Subscribes to `GrowwFeed.subscribe_ltp` for all universe tokens (limit 1000).
Aggregates ticks into 5-minute bars flagged `tick_built`. Strategies run on
these bars for every symbol.

Official-bar confirmation. Fetching ~200 REST candles every bar would exceed
Groww's rate limits and the per-bar deadline, so the official candle is fetched
only for symbols that need it:

- every symbol whose strategy produced a signal on the tick-built bar, and
- every symbol with an open position or pending entry.

Fetches run with bounded concurrency (`data.official_fetch_concurrency`,
default 5) and share the per-bar deadline. For each fetched symbol the engine
replaces the bar with the official one, calls `Strategy.recompute`, and re-runs
`on_candle`. Only a signal that survives the official bar proceeds to risk. If
the REST call fails after retries, the bar stays `tick_built`, the signal is
dropped with reason `unconfirmed_bar`, and the position's indicator state is
recomputed on the next successful fetch. All other symbols keep their
tick-built bar; because they produced no signal, the small drift is harmless
and self-corrects when they next need confirmation.

Entries are therefore only ever placed from signals confirmed against an
official bar. Exits are unaffected because they are broker-side.

### 4.4 Clock

`Clock` abstraction with two implementations. `BacktestClock` advances through
stored candle timestamps. `WallClock` uses real time. Both expose the NSE session
(09:15–15:30 IST), the square-off trigger time, and a holiday list from config.

### 4.5 Timestamps

Every `ts` in code, SQLite, and cache keys is an integer UTC epoch in seconds,
matching what Groww returns. A candle's `ts` is its open time. Only `Clock`
converts to and from IST, and only for session logic: market open, close,
square-off, and holiday checks. Config times are written in IST. Log lines show
both the epoch and an ISO-8601 IST string. No naive `datetime` objects anywhere;
tests assert that `Clock` maps 09:15 IST on a given date to the expected epoch.

## 5. Strategy

`Strategy` base class:

- `on_candle(candle) -> Signal | None`, called once per symbol per bar.
- `ready: bool`, false until the indicator warm-up is complete. The engine drops
  signals from strategies that are not ready.
- `product: "MIS" | "CNC"`, declared by the strategy.
- `recompute(symbol, candles)` rebuilds state from a candle list; used after a
  tick-built bar is replaced.

First strategy `ema_rsi`: fast EMA crosses slow EMA with RSI confirming
direction. Long on bullish cross with RSI above a threshold, short on bearish
cross with RSI below. Stop is a configurable ATR multiple; target is a
configurable reward-to-risk ratio. Parameters live in `config.yaml`.

Strategies must be deterministic given the candle sequence. Any exception inside
a strategy disables that strategy for the rest of the day; other strategies
continue.

## 6. Risk engine

A pure function of `(signal, portfolio_state, config) -> ApprovedOrder | Rejection`.
Every rejection is stored with a reason code. Checks in order:

1. Kill switch: if the file at `paths.kill_switch` (default `./KILL`) exists,
   reject. Checked once per bar. Empty file means no new entries. Contents
   `flatten` additionally triggers flatten-all.
2. Daily loss cap: realised plus unrealised PnL for the day must be above
   `-risk.daily_loss_cap_pct * capital`. Once breached, no new entries for the
   day and, if `risk.flatten_on_daily_cap` is true, square off everything.
3. Daily entry-count cap: `risk.max_entries_per_day`. Counts entry orders only.
   Exits, square-offs, and flatten are never blocked by this cap.
4. Max open positions: `risk.max_open_positions`.
5. Symbol already has an open position or pending entry: reject.
6. Symbol cooldown: after a stop-out, no entries on that symbol for
   `risk.cooldown_bars` bars (default 3).
7. Stop price present and on the correct side of entry.
8. Short signals with product CNC: reject. Shorts are intraday only.
9. Position size:
   - `risk_qty = floor(capital * risk.per_trade_pct / |entry - stop|)`
   - `capital_qty = floor(available_margin_for_product / entry)`
   - `qty = min(risk_qty, capital_qty)`, rounded down to `lot_size`.
   - Reject if `qty < lot_size`.

`available_margin_for_product` comes from the Groww margin API in live mode
(queried once per bar, cached). Paper and backtest track a simulated cash
balance with `risk.mis_leverage` applied for MIS.

## 7. AI filter

Interface `AIFilter.review(candidates: list[Candidate]) -> list[Decision]`.
`Candidate` carries the signal, the indicator snapshot, and the last
`ai.candles_in_context` candles (default 30). `Decision` is `approve | reject`, a one-line reason, and a confidence
in [0, 1].

Implementations:

- `StubFilter`: approves everything. Default in backtest.
- `ClaudeFilter`: one batched Claude call per bar covering all candidates.
  Structured JSON output. Model and max tokens from config.
- `CachedClaudeFilter`: wraps `ClaudeFilter`. Cache key is
  `(symbol, bar_ts, sha256(prompt))`, stored in the `ai_cache` table. Used for
  AI-replay backtests so repeated runs cost nothing.

Failure policy (`ai.on_failure`): `reject` (default) or `pass_through`. The
decision row records the failure reason either way.

Every decision, including stub approvals, is stored in `ai_decisions` with the
run ID so reports can attribute PnL to the filter.

## 8. Execution

`Broker` interface: `place_entry`, `place_exits`, `cancel`, `open_orders`,
`positions`, `square_off`. Three backends.

### 8.1 Constants

- `STOP_FIRST_ON_SAME_BAR = True`. In backtest and paper, if a bar's range
  touches both stop and target, the stop is assumed to have been hit first.
  Documented in `execution/backtest.py`.
- `ENTRY_BUFFER_PCT`: marketable limit buffer, default 0.1%. Longs at
  `signal_price * (1 + buffer)`, shorts at `signal_price * (1 - buffer)`.
- `BAR_DEADLINE_SEC`: default 60 seconds after bar close.
- `SQUARE_OFF_TIME`: default 15:10 IST, giving slack before the broker's own
  3:20 auto square-off.

### 8.2 Backtest backend

Fills entries at the next bar's open plus `slippage_pct`. Exits are simulated
each bar against high/low using the stop-first rule, including the entry bar
itself: after filling at that bar's open, the same bar's high and low are
checked against stop and target. Intraday positions are closed at the close of
the last bar ending at or before `SQUARE_OFF_TIME`, with `slippage_pct` applied
against the position so backtest square-off stays comparable to the live
market order. Tracks a simulated cash balance.

### 8.3 Paper backend

Same fill and exit simulation as backtest but driven by live LTP. Entry fills at
LTP plus slippage when LTP crosses the marketable limit. Runs against the live
candle source so it also exercises tick aggregation and REST reconciliation.

### 8.4 Live backend

Wraps the Groww SDK through `groww_adapter.py`, the only module that imports
`growwapi`.

Entry sequence per approved order:

1. Place a marketable limit order (`ORDER_TYPE_LIMIT`, `VALIDITY_DAY`) with
   `order_reference_id` set to the client ID.
2. Wait for fill via `subscribe_equity_order_updates`.
3. On the first fill event, full or partial, place exits for the filled
   quantity: OCO (`create_smart_order` with `SMART_ORDER_TYPE_OCO`, product
   MIS) for intraday; GTT stop (`SMART_ORDER_TYPE_GTT`, product CNC) for
   swing. Store exit IDs on the position. Further partial fills before the
   next bar call `modify_smart_order` to raise the exit quantity to match.
4. At the next bar, if any quantity remains unfilled, cancel the remainder.
   If nothing filled, record the order `unfilled`. If some filled, record the
   position at the final filled quantity with status `partial` and confirm the
   exit quantity matches it.

Fill rate is logged per day (`fills / entries_placed`) so it can be compared
against the backtest's fill-at-next-open assumption.

Square-off sequence at `SQUARE_OFF_TIME` for every intraday position:

1. Cancel both OCO legs.
2. Wait for cancellation confirmation on the order-update feed (timeout
   configurable, default 10 seconds; on timeout, re-query the smart order via
   REST before proceeding).
3. Only then send a market square-off order.

Square-off never runs while exit legs are live.

### 8.5 Idempotency and reconciliation

Client order ID = first 16 hex characters of `sha256(strategy|symbol|bar_ts)`,
which satisfies Groww's 8–20 alphanumeric limit. The full tuple is stored in
`orders` for lookup.

On startup and every 15 minutes, `reconcile.py`:

- Fetches open orders, smart orders, and positions from Groww.
- Never re-places an entry whose client ID already exists at the broker.
- Adopts orphaned open orders and positions into engine state, tagging them
  `adopted`. The signal that opened an orphan is usually unknown, so an adopted
  position without exits gets a stop at `risk.adopted_stop_pct` from its
  average price (default 1.5%) and no target, placed immediately as OCO with
  only the stop leg for MIS or as GTT for CNC. Reports exclude adopted
  positions from strategy statistics but include them in account PnL.
- Logs every discrepancy.

### 8.6 Per-bar time budget

Risk checks, the Claude call, and order placement must complete within
`BAR_DEADLINE_SEC` after bar close. Signals not placed by the deadline are
discarded and stored with reason `stale`.

## 9. Storage

SQLite at `data/tradebot.db`. Schema in `store/schema.sql`. Tables:

- `runs(run_id, mode, started_at, ended_at, config_json)`
- `candles(symbol, ts, o, h, l, c, v, interval, source)` unique on
  `(symbol, ts, interval)`
- `signals(id, run_id, strategy, symbol, bar_ts, direction, entry, stop, target, product)`
- `risk_decisions(id, run_id, signal_id, approved, reason, quantity)`
- `ai_decisions(id, run_id, signal_id, filter_kind, approved, reason, confidence, latency_ms, failure)`
- `ai_cache(symbol, bar_ts, prompt_hash, response_json, created_at)` primary key on the first three
- `orders(id, run_id, client_id, signal_id, broker_order_id, kind, side, qty, limit_price, status, placed_at, updated_at)`
- `fills(id, order_id, qty, price, ts)`
- `positions(id, run_id, symbol, product, qty, avg_price, stop, target, exit_ids_json, opened_at, closed_at, pnl, fill_status, adopted)`
- `daily_pnl(run_id, date, realised, unrealised, fills, entries_placed, fill_rate)`

Every run stores its resolved config so reports are reproducible.

## 10. Configuration and secrets

`config.yaml` sections: `capital`, `risk`, `strategy.ema_rsi`, `ai`,
`execution`, `session`, `paths`. `.env` holds `GROWW_API_KEY`,
`GROWW_TOTP_SECRET`, `ANTHROPIC_API_KEY`. `.env.example` ships with placeholders.
`.env`, `data/`, and `KILL` are gitignored.

## 11. Error handling

| Failure | Policy |
|---|---|
| Groww auth at startup | Fatal, exit. Never retry auth in a loop. |
| REST rate limit / timeout | Backoff, 3 attempts, then fail for that bar. Failed candle fetch flags the bar. Failed order placement discards the signal; reconciliation catches any order that actually went through. |
| WebSocket disconnect | Reconnect with backoff. No new entries while down. Resubscribe and reconcile on recovery. |
| Claude failure | Per `ai.on_failure`, default reject. Decision row records the failure. |
| Strategy exception | Caught per strategy per symbol. Strategy disabled for the day. |
| Engine loop exception | Safe state: no new entries, keep monitoring, keep square-off scheduler alive. Restart required. |

## 12. Shutdown

SIGINT/SIGTERM: finish or abandon the current bar's placement per the deadline,
flush SQLite, close the feed. Positions are not flattened; broker-side exits and
the 3:20 auto square-off cover intraday, and swing positions are meant to be
held. `tradebot flatten` closes everything on demand.

## 13. Logging

JSON lines to `data/logs/YYYY-MM-DD.jsonl`, daily rotation, plus human-readable
console output. Every line carries `run_id`; order-related lines carry `symbol`
and `client_id`.

## 14. Reporting

`tradebot report --run <id>` prints trades, win rate, average R, max drawdown,
daily PnL, fill rate, and rejection counts by reason.

`tradebot report --compare <stub_run> <replay_run>` prints the two side by side
plus a per-signal table showing which signals Claude rejected and what those
signals would have earned. This is the primary tool for deciding whether the
filter is worth its cost.

## 15. Testing

No test touches the network except one opt-in smoke test. `groww_adapter.py`
is the only module wrapping the SDK, and a `FakeBroker` replaces it in tests.

- Indicators/strategy: exact signals from fixed candle sequences; `ready`
  during warm-up; state after `recompute`.
- Risk: table-driven tests for every check in section 6, including both sizing
  limits and lot rounding.
- Backtest backend: next-open fills, slippage, stop-first rule, entry-bar
  exits (stop hit on the fill bar, target hit on the fill bar, both hit on the
  fill bar resolving to stop), end-of-day square-off with slippage.
- Live backend against `FakeBroker`: entry-then-OCO, partial fill then exit
  quantity modify then remainder cancel, marketable limit pricing,
  cancel-confirm-then-square-off ordering, client ID derivation, orphan adoption
  with fallback stop, deadline enforcement.
- Live data source: official-bar confirmation only for signal and position
  symbols, signal dropped when confirmation fails, bounded concurrency.
- `fetch-data`: incremental resume from latest stored `ts`, idempotent re-run
  inserts nothing.
- AI filter with fake Claude client: batch prompt shape, parsing, failure
  policy, cache hits.
- Engine: end-to-end backtest on a fixture dataset asserting the exact trade
  list.
- Smoke (opt-in, `TRADEBOT_SMOKE=1`): read-only Groww calls with real
  credentials. Never places orders.

## 16. Project layout

```
AI-Trading Bot/
  pyproject.toml            # growwapi, pyotp, anthropic, pyyaml, pandas, click, pytest
  config.yaml
  universe.yaml
  .env.example
  src/tradebot/
    cli.py
    engine/      clock.py, loop.py, squareoff.py, reconcile.py
    data/        source.py, historical.py, live.py, instruments.py
    strategy/    base.py, indicators.py, ema_rsi.py
    risk/        engine.py, sizing.py, killswitch.py
    ai/          filter.py, claude_client.py, prompt.py, cache.py
    execution/   broker.py, backtest.py, paper.py, live.py, groww_adapter.py
    store/       db.py, schema.sql, repo.py
    report/      summary.py, compare.py
  tests/
  data/           tradebot.db, logs/, instruments.csv   (gitignored)
  docs/superpowers/specs/
```

## 17. Build order

Each milestone is runnable on its own.

1. Store, config loading, instrument master, universe resolution.
2. Historical fetch and the backtest backend.
3. Indicators, `ema_rsi`, risk engine.
4. `backtest` and `report` CLI commands with the stub filter.
5. Claude filter, cache, AI-replay backtests, compare report.
6. Paper backend with the live candle source.
7. Live backend: Groww adapter, entry/exit sequences, reconciliation,
   square-off, kill switch flatten, `flatten` command.

Live mode is gated on paper results you are satisfied with.

## 18. Out of scope for this spec

F&O trading, alerts, dashboard, multiple brokers, point-in-time universe data,
news or breadth context for the AI filter.
