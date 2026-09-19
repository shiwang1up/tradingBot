# 15-Minute Pullback Strategy — Design Spec

Date: 2026-09-15. Status: approved. Extends the 2026-09-14 trading bot spec.

## Goal

Test one idea that the tuning note (`docs/superpowers/notes/2026-09-15-confluence-tuning.md`) says is
needed: a longer bar so the stop is several times the transaction cost, and an entry on a pullback
into a level rather than at the close that completed the move. The deliverable is a strategy that
runs through the existing backtester and paper engine on official 15-minute candles, plus the small
tooling change needed to judge it with the forward-return table before trusting a backtest.

## Decisions

| Topic | Decision |
|---|---|
| Bars | Official 15-minute candles fetched from Groww (about 90 days of history), stored in the existing candles table under interval 15. No resampling of the 5-minute cache |
| Config | A second file `config-15m.yaml`: `execution.interval_minutes: 15` and a `strategy.pullback` section; everything else identical to `config.yaml`. Same database, same universe |
| Trend | EMA20 above EMA50 is an uptrend, below is a downtrend, equal is none |
| Pullback | A bar whose low touches or crosses EMA20 during an uptrend (high touches or crosses EMA20 in a downtrend) |
| Entry | The first pullback bar that closes back above EMA20 with a low above the previous bar's low (mirror for shorts). Signal at that bar's close; the broker fills at the next open as today |
| Stop and target | Stop one tick below the pullback's lowest low (above the highest high for shorts); target at `reward_risk` times the stop distance |
| One trade per pullback | After a signal the symbol waits until a bar's high exceeds the swing high (the high before the pullback, raised by the confirmation bar's own high) before a new pullback can arm; mirror with the swing low for shorts |
| Guards | Skip a signal whose stop is closer than `min_stop_pct` of price or further than `max_stop_atr` ATRs. A pullback older than `max_pullback_bars` is a failing trend, not a dip: it is spent (phase done) so a fresh swing extreme is needed before another can arm. A trend flip resets to idle |
| Session boundary | The first bar of a new IST day starts a clean cycle (idle, swings re-seeded from that bar, no arming on that bar), so a pullback from the previous afternoon can never confirm against the opening gap |
| Product | MIS only, square-off as today (with 15-minute bars the square-off bar opens at 14:45) |
| Evaluation | Backtest on the fetched window, then `scripts/signal_forward_returns.py` on the run; the script reads the interval from the run's stored config instead of assuming 5 minutes |
| Out of scope | Parameter tuning, the AI filter, changes to the paper engine (it already takes the interval from config), resampling |

## `strategy/pullback.py`

`PullbackStrategy(params)` implements the `Strategy` interface (`on_candle`, `is_ready`, `snapshot`,
`reset`, `recompute`) with one state per symbol:

```
_State: ema_fast, ema_slow, atr (streaming, from strategy/indicators.py)
        phase: IDLE | PULLBACK | DONE
        direction: "LONG" | "SHORT" | None     # the trend the pullback belongs to
        swing_high, swing_low: float | None    # extremes tracked while IDLE, per trend
        pullback_low, pullback_high: float | None
        pullback_bars: int
        prev_low, prev_high: float | None      # previous bar, for the higher-low / lower-high test
        ready_bars: int                        # bars seen with every indicator warm
        day: date | None                       # IST date of the last bar
```

Per bar, after updating the three indicators:

1. Not ready (EMA50 or ATR is None): return None. The first ready bar only records state and never fires.
2. Determine `trend`: LONG if `ema_fast > ema_slow`, SHORT if `ema_fast < ema_slow`, else None. If the
   bar opens a new IST day, or `trend` differs from `direction`, reset the phase to IDLE with
   `direction = trend`, the swing extremes set to this bar's high and low and the pullback extremes
   cleared, and do nothing else on this bar (a flip bar or an opening bar never arms a pullback).
3. IDLE, trend LONG: `swing_high = max(swing_high, high)`. If `low <= ema_fast`: phase PULLBACK,
   `pullback_low = low`, `pullback_bars = 1`. (Mirror for SHORT with `swing_low`, `high >= ema_fast`,
   `pullback_high = high`.)
4. PULLBACK, trend LONG: `pullback_low = min(pullback_low, low)`, `pullback_bars += 1`. If
   `pullback_bars > max_pullback_bars`: phase DONE without a signal (spent). Else if
   `close > ema_fast and low > prev_low`: candidate signal, phase DONE, and
   `swing_high = max(swing_high, high)` so a strong confirmation bar raises the bar for re-arming.
   (Mirror for SHORT: `close < ema_fast and high < prev_high`.)
5. DONE, trend LONG: if `high > swing_high`: phase IDLE with `swing_high = high`. (Mirror for SHORT.)
6. Record `prev_low`, `prev_high` for the next bar.

A candidate LONG signal has `entry = close`, `stop = round_tick_down(pullback_low - TICK)`,
`target = round_tick_down(entry + (entry - stop) * reward_risk)`. It is dropped (phase still moves to
DONE) when `entry - stop < entry * min_stop_pct / 100` or `entry - stop > atr * max_stop_atr`, or when
`stop < entry < target` does not hold. Shorts mirror with `round_tick_up`.

Parameters (config section `strategy.pullback`), validated at construction with the same style as
the other strategies (numbers only, positive where stated, product in MIS/CNC):

```
ema_fast: 20
ema_slow: 50            # must be > ema_fast
atr_period: 14
max_pullback_bars: 8    # >= 1
reward_risk: 2.0
min_stop_pct: 0.1
max_stop_atr: 3.0
product: MIS
```

`snapshot(symbol)` returns `ema_fast, ema_slow, atr, trend (+1, -1, 0), phase (0 idle, 1 pullback,
2 done), swing_high, swing_low, pullback_low, pullback_high` as floats (None values as 0.0) so the AI
filter's context has the setup in it; it is empty until `is_ready` (two warm bars), per the base
contract. `reset` re-creates the state; `recompute` resets and replays the given candles.
`build_strategy("pullback", params)` returns it; `strategy_params` needs no change.

## `config-15m.yaml`

A copy of `config.yaml` with `execution.interval_minutes: 15` and the `strategy.pullback` block
added. `paths` are unchanged, so the file shares `data/tradebot.db`, the instrument master and the
universe. The session block is unchanged; `SessionClock` already validates that 25 bars fit and that
the square-off bar (14:45) does not open before the entry cutoff (14:45).

## `scripts/signal_forward_returns.py`

Reads `execution.interval_minutes` from the run's `config_json` and loads candles of that interval;
the horizons stay 1, 2, 3, 6, 12 and 24 bars. A run whose config has no interval falls back to 5.
Usage unchanged: `.venv/bin/python scripts/signal_forward_returns.py <run_id>`.

## Operator sequence

```
.venv/bin/tradebot fetch-data --config config-15m.yaml
.venv/bin/tradebot backtest --config config-15m.yaml --strategy pullback --start 2026-06-15 --end 2026-09-15 --run-id pb-15m-1
.venv/bin/python scripts/signal_forward_returns.py pb-15m-1
.venv/bin/tradebot report --run pb-15m-1
```

The forward-return table decides whether the backtest is worth reading: the h6 and h12 columns need
to sit clearly above about 15 bp (round-trip cost is about 10 bp) before the strategy is considered
to have an edge. `--config` is a group option and goes before the command name.

## Testing

Unit tests on crafted 15-minute bars for `PullbackStrategy`:

- the canonical long setup (warm trend, one bar dips to EMA20, next bar closes above it with a higher
  low) fires LONG at that bar with stop one tick under the pullback low and target at 2R;
- a confirmation bar without a higher low does not fire, and the next bar that has one does;
- after a signal nothing fires again until a bar exceeds the swing high (raised by the confirmation
  bar if it printed a new high), then a fresh pullback fires again;
- a pullback longer than `max_pullback_bars` is spent without firing and cannot re-arm until a new
  swing high, after which the next pullback trades;
- a trend flip mid-pullback resets without firing, and the flip bar itself does not arm;
- the first bar of a new day starts clean: a pullback armed on the last bar of the previous day does
  not confirm on an opening gap bar;
- a bar with equal EMAs has no trend and places nothing;
- the short mirror of the canonical setup fires SHORT with the mirrored stop and target;
- the `min_stop_pct` and `max_stop_atr` guards each drop the signal and still move the phase to DONE;
- the first ready bar never fires; `is_ready` is False until EMA50 and ATR are warm;
- parameter validation rejects `ema_slow <= ema_fast`, non-positive values and a bad product;
- `build_strategy("pullback", ...)` returns the class.

Also: `config-15m.yaml` loads through `load_config` and builds a `SessionClock` at 15 minutes; the
forward-return script uses the interval stored with the run (a run created with interval 15 reads
interval-15 candles). No network in tests.
