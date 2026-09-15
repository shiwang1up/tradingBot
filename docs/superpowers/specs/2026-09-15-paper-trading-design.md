# Paper Trading on REST Bars — Design Spec

Date: 2026-09-15. Status: approved. Extends the 2026-09-14 trading bot spec (milestone 6 of its
build order) and replaces its section 4.3 for paper mode.

## Goal

Run the existing strategy, risk, AI-filter and simulated-broker pipeline against live market data,
bar by bar, during the trading session, with the same fill and exit rules as the backtester, so the
real-time loop, session timing, sizing, restart recovery and daily reporting are proven on live data
before any real order exists.

## Hard rule: no real money

Paper mode never calls a Groww order, smart-order, modify or cancel endpoint. The only Groww calls it
makes are authentication and historical candle fetches. The live backend (real orders, order-update
feed, reconciliation, `flatten`) is a separate plan and is gated on paper results the operator is
satisfied with. Nothing in this spec is a step toward placing an order.

## Decisions

| Topic | Decision |
|---|---|
| Scope | Paper only. Tick feed, live broker, reconciliation and `flatten` deferred to the live plan |
| Bar source | REST candles fetched at each 5-minute boundary for all universe symbols (49 names). The 2026-09-14 spec's tick-feed design applied to a 200-name universe; at 49 names the REST path is ~600 calls an hour, the same order as `fetch-data` |
| Broker | The existing `BacktestBroker`, unchanged: entry at the next bar's open plus slippage inside the marketable-limit buffer, exits against bar high/low with the stop-first rule, square-off at 15:10. Paper and backtest numbers are comparable by construction |
| Runs | One run per trading day, id `paper-YYYY-MM-DD` by default, mode `paper`. `report --run` works unchanged |
| Restart | Same-day restart resumes the run: positions, pending entries and day counters reload from SQLite; missed bars are fetched and replayed with the broker active |
| Products | MIS only. A strategy configured with product CNC is refused at start (multi-day carry-over is out of scope) |
| Warm-up | Incremental fetch to now, then replay the last `data.warmup_bars` stored bars through strategies and indicators with no placement |
| Deadline | A bar processed later than `execution.bar_deadline_sec` after its close still updates state, but its signals are dropped with reason `stale` (2026-09-14 spec 8.6) |
| Shutdown | SIGINT/SIGTERM finish the current bar, write the daily row, end the run, exit. No flatten; the `KILL` file with `flatten` still works per bar |

## Components

### `engine/loop.py`: split `Engine` from `BacktestEngine`

The per-bar logic moves unchanged into a base `Engine`: `process_bar`, `_start_day`, `_end_day`,
`_record`, `_place`, `_flatten`, `_run_strategies`, state accessors. `BacktestEngine(Engine)` keeps
only its replay loop over `HistoricalSource.bar_timestamps()`. `PaperEngine(Engine)` adds:

- `warm(candles)`: feeds candles to the shared `IndicatorSet`s, the strategies and the AI context
  history, and records last closes, without calling the broker or placing anything.
- `resume()`: if `repo.get_run(run_id)` exists and has no `ended_at`, restores broker state from
  the run's rows (see Store) and day counters; if it exists and has ended, raises
  `ValueError("run paper-... already ended for today")`.
- `run()`: the wall-clock loop (see Scheduling). Each iteration calls `process_bar(ts, candles)` and
  then `repo.upsert_daily_pnl` so the daily row is current after every bar, not only at day end.
- `stale` handling: `process_bar` gains an optional `now_ts` argument; when
  `now_ts - (ts + interval) > bar_deadline_sec`, signals are stored with risk reason `stale` instead
  of going to `_place`. Backtest passes nothing and is unaffected.

### `data/live.py`: `LiveBarSource`

```
LiveBarSource(fetcher, repo, symbols, exchange, interval, concurrency, clock, budget_sec=None)
  fetch_bar(bar_ts) -> dict[str, Candle]
  fetch_range(start_ts, end_ts) -> dict[int, dict[str, Candle]]
```

`fetch_bar` requests today's candles for every symbol from session open to `bar_ts + interval`
using the adapter's `fetch_candles` (already wrapped in `with_retry`), on a thread pool of
`concurrency` workers. Every returned bar inside the session is stored with `insert_candles`
(INSERT OR IGNORE), so the day grows the cache. It returns only bars whose `ts == bar_ts`; a symbol
that failed after retries or has no bar is absent, and the failure is logged with the symbol. If
every symbol fails on a bar, the source logs an error and returns an empty dict; the engine treats it
like a bar with no candles. `fetch_range` is the same over several bars, used for catch-up after a
restart or a late wake-up. `budget_sec` bounds one window's wall time: a symbol still fetching when it
runs out is skipped for that bar and logged, so one slow name cannot push every other symbol's signals
past the deadline. The CLI passes the bar deadline minus the grace. Each window logs one timing line.

`fetcher` is the same `Fetcher` callable type `historical.py` uses, so tests pass a fake and the
CLI passes `GrowwAdapter.fetch_candles`.

### `engine/clock.py`: wall-clock scheduling

`SessionClock` already maps session times to epochs. Add `last_bar_ts(d)` (open time of the bar that
ends at the close) and `latest_complete_bar(now_ts, grace_sec)` (open time of the most recent bar whose
close plus grace is at or before `now_ts`, capped at the last bar, `None` before the first bar closes).
Sleeping stays out of the clock: `PaperEngine` takes `now` and `sleep` callables so tests drive time.

### Scheduling (`PaperEngine.run()`)

1. Today is not a trading day, or `now >= close` and there is no open run for today: log and return
   without creating a run. An open run started today may be started after the close: it replays the
   missed bars (stale, so no entries), squares off and ends. A run from an earlier day is refused.
2. Create or resume the run. Warm up (Warm-up below).
3. If resuming, `last_ts` is `runs.last_bar_ts` (see Store). Fetch and replay every bar from
   `last_ts + interval` up to the last completed bar with the broker active. Signals from these bars
   are `stale` by the deadline rule, so catch-up only settles exits and fills.
4. Loop while the last processed bar is before `last_bar_ts(today)`: `latest =
   latest_complete_bar(now, bar_grace_sec)`; if that is not past the last processed bar, sleep until
   the next bar's close plus grace and re-check; otherwise `source.fetch_range(last + interval,
   latest)` in one window and `process_bar(ts, candles, now_ts=now())` for each bar in order, writing
   the daily row and `last_bar_ts` after each. A machine that slept simply finds several bars ready.
5. When the last bar of the session has been processed: `_end_day`, `end_run`, return.

A `stop` flag set by the SIGINT/SIGTERM handler is checked after each bar and inside the sleep; when set
the loop finishes the current bar, writes the daily row and returns without ending the run. Positions
and pending entries stay on the books, so a resume later the same day picks them up.

### Warm-up

`fetch_incremental` runs for the universe with the CLI's usual session filter (lookback 10 days if
the cache is empty, paced like `fetch-data`). Then the last `data.warmup_bars` stored bars for each
symbol, up to now and including today's completed bars, are replayed through `warm`. On a new run
today's warmed bars count as processed (nothing was at the broker); on a resume the engine trims the
warm-up to `runs.last_bar_ts` so every later bar is replayed with the broker. Readiness is whatever the
strategies report; a symbol with fewer stored bars than needed simply stays not-ready until it warms live.

### Store

- `runs` gains `last_bar_ts INTEGER` (schema version 3, additive migration). Updated after each
  processed bar in paper mode; NULL for backtests.
- `Repo.open_positions(run_id)` returns rows with `closed_at IS NULL`; `Repo.pending_orders(run_id)`
  returns ENTRY orders with status PENDING joined to their signal.
- `BacktestBroker.restore(positions, pending, cash)` loads state. Cash is recomputed as
  `capital + sum(closed pnl)`; pending orders are rebuilt as `ApprovedOrder`s from the joined rows.

### Config

Two new keys with defaults, both validated positive:

```
data:
  official_fetch_concurrency: 5   # existing
  bar_grace_sec: 5                # wait after the boundary before fetching the closed bar
  warmup_bars: 300                # stored bars replayed before the first live bar (~4 days)
```

### CLI

```
tradebot paper --strategy ema_rsi [--ai stub|claude|claude_cached] [--run-id paper-2026-09-16]
```

Authenticates the adapter (fatal on failure, no retry loop), resolves the universe against the
instrument master (required: lot sizes matter for sizing), refuses CNC strategies, builds
`LiveBarSource`, the broker, the AI filter and `PaperEngine`, installs the signal handlers, runs,
and prints `format_summary` at the end. The AI filter default is whatever `ai.filter` says; the
operator is low on Anthropic credit, so the README notes `--ai stub` for the first sessions.

## Error handling

| Failure | Policy |
|---|---|
| Groww auth at start | Fatal, exit |
| Candle fetch for a symbol | Retry per adapter, then skip that symbol for the bar, warn |
| Every symbol fails on a bar | Error log, empty bar, continue |
| Strategy exception | Existing per-strategy disable for the day |
| Exception inside a bar | Log with traceback, mark the bar processed, continue; nothing is at the broker |
| Run already ended today | Refuse to start; the operator passes a different `--run-id` to start a second session |
| Machine sleep / late wake | Fetch and replay missed bars in order; their signals are `stale` |

## Testing

No network. `LiveBarSource` tests use a fake fetcher with canned candles, per-symbol failure
injection, a total-failure bar, and assert storage and the `ts` filter. Scheduler tests drive
`PaperEngine.run()` with a fake `now`/`sleep` pair: bars fire at boundary plus grace, a late wake
replays the missed range, the session end writes the daily row and ends the run, a stop flag ends it
early. The resume test runs ten bars continuously and ten bars with a stop after bar five and a
fresh engine resuming, and asserts identical positions, orders, fills and daily counters. Also:
warm-up leaves indicators ready and places nothing; `stale` signals get a risk row and no order;
CNC strategy refused; started after close creates no run; schema migration to version 3 preserves
existing rows.

## Out of scope

Tick feed and tick-built bars, the live broker and order sequences, order-update subscription,
reconciliation, `flatten`, multi-day (CNC) paper positions, alerts.
