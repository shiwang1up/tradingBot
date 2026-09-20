# Real Charges, Opening-Range Breakout and NIFTY Regime Filter — Design Spec

Date: 2026-09-19. Status: approved. Extends the 2026-09-14
trading bot spec and the 2026-09-15 indicator, paper and pullback specs.

## Why

Three strategies (ema_rsi 5m, confluence 5m, pullback 15m) all lose 0.26-0.31R per trade over
2026-06-17..2026-09-15. Measured on the stored runs, the median stop is 0.34-0.46% of price, so the
modelled 5 bps slippage per side already costs 0.22-0.30R per round trip: nearly the whole loss is
friction. The backtest also models no brokerage or statutory charges, which roughly doubles the true
loss (about ₹35,000 of brokerage alone on 884 trades). Two strategies are slightly positive before
slippage. So the work is, in order: measure honestly, cut cost per R, then add a market filter.

## Decisions

| Topic | Decision |
|---|---|
| Order of work | Three phases in this order; phases 2 and 3 are judged net of phase 1 charges |
| Charge schedule | Groww intraday equity rates, all in `config.yaml`; the user verifies them against Groww's pricing page |
| Meaning of `pnl` | Unchanged: gross, after slippage. New `positions.charges`; net = `pnl - charges` |
| Old runs | `charges` is NULL; the report computes charges after the fact from quantity and prices |
| Holding period | Intraday only (MIS), squared off at 15:10 as now. No overnight, no CNC |
| Low-turnover strategy | Opening-range breakout on 15-minute bars, at most one signal per symbol per day |
| Entries per day | `max_entries_per_day: 2`, `max_open_positions: 2` in the variant's config |
| Competing signals | Optional `Signal.priority`; engine sorts same-bar signals by priority descending, then symbol |
| Overfitting guard | Tune on 2026-06-17..2026-08-15 only; 2026-08-16..2026-09-15 is run once as the holdout |
| Regime data | `NSE-NIFTY` index candles through the existing cash-segment fetcher; stored, never traded |
| Regime fallback | If Groww refuses index history: equal-weight mean of the universe's bar returns, no new data |
| Regime placement | In the engine before the risk check; a drop is a risk rejection with reason `regime` |
| Out of scope | Swing/CNC trading, live order placement, order-book or news inputs, a general ranking for other strategies beyond the `priority` hook |

## Phase 1: charges

### `execution/charges.py`

Pure, no I/O. `round_trip_charges(buy_value, sell_value, cfg) -> float`, rounded to paise. A short is
the same two orders with the sell first, so the function takes values by side, not by entry/exit.

    brokerage  = sum over the two orders of clamp(order_value * brokerage_pct/100, brokerage_min, brokerage_max)
    stt        = sell_value * stt_sell_pct/100
    txn        = (buy_value + sell_value) * exchange_txn_pct/100
    sebi       = (buy_value + sell_value) * sebi_pct/100
    stamp      = buy_value * stamp_buy_pct/100
    gst        = (brokerage + txn + sebi) * gst_pct/100
    total      = brokerage + stt + txn + sebi + stamp + gst

### Config

    charges:
      enabled: true
      brokerage_pct: 0.1        # per order, percent of order value ...
      brokerage_max: 20.0       # ... capped at this many rupees
      brokerage_min: 5.0        # ... and floored at this
      stt_sell_pct: 0.025       # intraday equity, sell side only
      exchange_txn_pct: 0.00297 # NSE
      sebi_pct: 0.0001
      stamp_buy_pct: 0.003      # buy side only
      gst_pct: 18.0             # on brokerage + exchange txn + SEBI

Validated at load like the other sections: all rates finite and >= 0, `brokerage_min <= brokerage_max`.
A config with no `charges:` section loads with these defaults. `enabled: false` yields zero charges
(used by tests that pin old numbers).

### Broker, engine, store

- `Position` gains `charges: float | None`. `BacktestBroker._close` computes it from the fill values,
  sets it, and moves cash by `pnl - charges`. `pnl` keeps its meaning.
- `Engine._record` adds `pnl - charges` to `_day.realised`, so the daily loss cap and `daily_pnl`
  are net. Unrealised PnL stays gross (charges are only known at close).
- Schema version bump with `ALTER TABLE positions ADD COLUMN charges REAL`, following the existing
  migration list in `store/db.py`. `repo.close_position` writes it; `restore` reads it.

### Report

`summary.py` prints `Gross PnL`, `Charges`, `Net PnL`; win rate, Avg R and both drawdowns are on
net. For a position whose `charges` is NULL the report computes it with the current config and marks
the run `charges: estimated after the fact`. `--compare` uses net on both sides.

## Phase 2: opening-range breakout

### `strategy/orb.py`, name `orb`

Per symbol and per session day, state is: range high, range low, mean volume of the range bars,
`done` flag. A bar belongs to the range when its start is before `session.open + range_minutes`.
The day is detected from the candle timestamp; a new day resets the state.

After the range is complete, on the first bar whose close is beyond the range:

- LONG when `close > range_high`: entry = close, stop = `round_tick_down(range_low)`.
- SHORT when `close < range_low`: entry = close, stop = `round_tick_up(range_high)`.
- target = entry ± `reward_risk` × |entry − stop|, tick-rounded toward entry as in
  `ema_rsi`; `reward_risk: null` means no target, exit by stop or square-off only.
- `priority` = breakout bar volume / mean range-bar volume (0 when the mean is 0).
- The symbol is `done` for the day after its first signal, traded or not.

No signal, and the symbol is `done`, when the range width as a percent of the range midpoint is
below `min_range_pct` or above `max_range_pct`, or when the range has fewer bars than
`range_minutes / interval` (late listing, missing data). `is_ready` is true once the range is
complete. `snapshot` returns range high, low, width percent and relative volume for the AI filter.

Entries stop at `session.no_new_entries_after`, which the variant sets to 13:00. The strategy counts
range bars itself, so its params carry `session_open` and `interval_minutes`; config load fails unless
they equal `session.open` and `execution.interval_minutes`. `product` must be MIS.

### `config-orb.yaml`

A copy of `config-15m.yaml` with `strategy.orb`, `execution.interval_minutes: 15`,
`session.no_new_entries_after: "13:00"`, `risk.max_entries_per_day: 2`, `risk.max_open_positions: 2`.

    orb:
      range_minutes: 30
      reward_risk: 2.0          # null = stop or square-off only
      min_range_pct: 0.4
      max_range_pct: 1.5
      session_open: "09:15"     # must equal session.open
      interval_minutes: 15      # must equal execution.interval_minutes
      product: MIS

### Priority ordering

`Signal` gains `priority: float = 0.0` (last field, so existing constructors are unchanged).
`Engine._place` and the `stale` and `entries_closed` paths iterate signals sorted by
`(-priority, symbol)`. Existing strategies emit 0.0, so their order becomes alphabetical in both
backtest and paper; this removes the present difference between the two modes (SQL order vs
`universe.yaml` order). Stored runs are not affected; re-runs of old strategies in paper may pick
different symbols on contended bars than before, which is the intended fix.

### Experiment

Six tuning runs on 2026-06-17..2026-08-15: `range_minutes` {30, 60} × `reward_risk` {1.5, 2.0, null},
AI filter `stub`. The best by R on risk is run once on 2026-08-16..2026-09-15. The
backtest CLI already takes `--start/--end`; run ids are `orb-t-<range>-<rr>` and `orb-holdout`.

The decision metric is **R on risk**: net PnL divided by rupees at risk (`|fill - stop| x quantity`), all
trades pooled. The unweighted per-trade mean R is not used: a position sized from leftover margin can risk
a few rupees, the per-order brokerage floor then costs it several R, and a few such trades dominate the
mean (on `real-1`: -3.96 unweighted against -0.61 on risk). R on risk has the sign of net PnL, so a
verdict can never contradict the money. The report prints both.

Success: R on risk > 0 on both windows with at least 30 holdout trades. Otherwise the result is
recorded as no edge. Fewer than 30 holdout trades is recorded as inconclusive, not as a pass.

## Phase 3: NIFTY regime filter

### Data

`data.index_symbol: NIFTY` (empty string disables). `fetch-data` fetches it at the run interval with
the universe; `groww_symbol("NSE", "NIFTY")` already yields `NSE-NIFTY`. Index bars have zero volume,
which the candle parser must accept. `HistoricalSource.from_repo` and `LiveBars` load the index with
the universe. The index never reaches strategies, the broker or the universe list.

If the fetch fails as unsupported, `regime.source: composite` builds the series instead: per bar, the
mean of `close/prev_close - 1` over universe symbols present in both bars, chained from 100.

### `risk/regime.py`

`RegimeFilter(ema_period)`: `update(close)`, `state` in {`UP`, `DOWN`, `NOT_READY`}. `UP` when the
index close is above its EMA, `DOWN` when at or below. It reuses the streaming `EMA` from
`strategy/indicators.py`.

### Engine

A helper `_split_index(candles)` removes the index candle and feeds it to the filter. It runs at the
top of `process_bar` and of the paper warm-up path, before `_observe`, so nothing downstream sees the
index. With no index bar at this timestamp the last state stands. In
`_place`, before `evaluate`: a LONG in `DOWN` or a SHORT in `UP` gets a `risk_decisions` row with
reason `regime`; any signal in `NOT_READY` gets `regime_not_ready`. Both appear under "Risk rejects"
in the report with no report change.

    regime:
      enabled: false
      source: index             # index | composite
      ema_period: 20

### Experiment

The chosen ORB config and the 5-minute confluence config, each with `regime.enabled` false and true,
on both windows, net of charges: eight runs. The filter is kept for a strategy only if it improves
R on risk on both windows.

## Errors

- Invalid `charges`, `orb` or `regime` config fails at load, not mid-run.
- `regime.enabled: true` with no index candles in the run window fails at start with a message that
  names `fetch-data`.
- A strategy exception keeps the existing behaviour: disabled for the day.

## Tests

Test-first, as in the repo. `test_charges.py`: a hand-computed long and a short, brokerage floor and
cap, `enabled: false`. `test_backtest_broker.py`: cash and `charges` on close. `test_engine.py`: daily
cap on net; priority ordering; index candle never reaches a strategy; regime rejections and reasons;
missing index bar keeps state. `test_orb.py`: range building, breakout both ways, one signal per day,
width filters, short range, day reset, `reward_risk: null`, priority value. `test_regime.py`: states
and warm-up. `test_report.py` and `test_compare.py`: gross/charges/net and the estimated path for NULL charges.
`test_store.py`: migration from the previous schema version. `test_cli.py`: index fetch, the start-up check. All code stays Python 3.9 compatible.

## Deliverables

Code and tests per phase, `config-orb.yaml`, and `docs/superpowers/notes/2026-09-19-charges-orb-regime-results.md`
with the re-stated old runs net of charges, the six tuning runs, the holdout and the eight regime runs.

## Amendments (2026-09-20, after the ORB tuning runs)

- **ORB tuning result:** all six parameter sets are negative on R on risk (-0.29 to -0.35) over 41 days;
  every position was re-derived independently from raw candles and matched. The holdout has not been run;
  running it is the user's decision, since it can only confirm a rejection and would stop the window
  being clean for other ORB variants.
- **Regime experiment uses the tuning window only.** The filter is worth validating out of sample for a
  strategy only if R on risk with the filter on is higher than off AND above zero. Filter-on and
  filter-off runs are scored on the same days (from the first session after the filter's EMA is warm).
- **Pre-registered alternative regime definition** (written down before any regime result was seen): UP
  when the index's last close is above TODAY'S SESSION OPEN (the first bar's open), otherwise DOWN. Reason:
  on 15-minute bars at 09:45 a 20-bar EMA is about 80% yesterday's prices, so "above the EMA" is mostly
  the overnight gap, and it is blind to a gap up that fades from the open, the case that matters most for
  ORB. The alternative needs no state across days and no warm-up and does not depend on the bar interval.
  It is the ONLY alternative that may be evaluated, on the tuning window only, and only after the EMA
  version has been measured. `ema_period` stays fixed at 20.
- **Engine ordering:** `_usable` runs before `_split_index`; the composite level never becomes non-finite
  and is not updated on a bar where fewer than half the known symbols have a return; a bar whose only
  candle is the index does not reach the broker.
- **Index fetch:** `fetch-data` fetches the index LAST and an index failure never blocks the universe; the
  paper live feed carries the index, first in its list, only when the filter is on with `source: index`;
  the warm-up fetch always includes it. An `index_symbol` that is also a universe symbol is refused.
- **Results:** `docs/superpowers/notes/2026-09-19-charges-orb-regime-results.md`. The regime filter improved
  R on risk slightly for ORB and confluence on the tuning window and left both clearly negative, so by the
  rule above it is not carried to the holdout. The ORB holdout remains unspent.
