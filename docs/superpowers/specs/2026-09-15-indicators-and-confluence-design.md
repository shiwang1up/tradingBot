# Technical Indicators and Confluence Strategy — Design Spec

Date: 2026-09-15. Status: approved. Extends the 2026-09-14 trading bot spec.

## Goal

Add the standard price-based technical indicators (all asset-agnostic and older than crypto) to
the bot, in two phases: (1) a streaming indicator library whose per-symbol snapshot enriches the
context every candidate carries, so the Claude filter reviews with the full picture; (2) a
confluence strategy that generates signals from those layers with weighted votes and a threshold.

## Decisions

| Topic | Decision |
|---|---|
| Direction layers | EMA20 vs EMA50; MACD 12/26/9 histogram and whether it turned this bar; momentum over 1, 5, 60 bars |
| Extremes | ADX(14) with 20 (no trend) and 25 (trending) bands; Bollinger(20, 2) and %B |
| Size of moves | ATR(14) (exists); volume spike as percent of the 20-bar average; buy/sell ratio = (close-low)/(high-low) |
| Structure | 100-bar support/resistance excluding the current bar; breakout/breakdown on a close crossing them; bullish/bearish engulfing; double top/bottom from pivots within a tolerance |
| Order-book walls | Not available historically on Groww; excluded from backtests, live-only later |
| Where they feed | Engine owns one `IndicatorSet` per symbol, updates it every bar, merges its snapshot into `Candidate.indicators` for every strategy |
| Confluence entry | Weighted votes per layer, enter when the sum clears a threshold; ADX below 20 gates everything off; RSI and %B both extreme count against a trade into exhaustion |
| Patterns in phase 2 | Engulfing and double top/bottom only; flags, triple tops and head-and-shoulders later |
| Stops and targets | Unchanged: ATR multiple with the minimum stop distance guard; reward-to-risk target |
| API spend | Phase 1 changes the system prompt (cache key changes on purpose); no paid replay is part of this work |

## Phase 1: `strategy/ta.py` and `IndicatorSet`

Streaming classes, one bar per `update`, `None` until warm, no lookahead, finite-input guard as in
`indicators.py`:

- `MACD(fast=12, slow=26, signal=9)` → `macd, signal, hist, hist_turned` (sign of hist change vs previous bar).
- `ADX(14)` Wilder: +DM/-DM, TR, smoothed DI+, DI-, DX, ADX. Ready after 2×period bars.
- `Bollinger(20, 2.0)` → `mid, upper, lower, pct_b`.
- `Momentum(n)` → `close / close[-n] - 1` in percent, for n in 1, 5, 60.
- `VolumeSpike(20)` → `volume / mean(last 20 volumes excluding current) * 100`.
- `bar_position(candle)` → `(close - low) / (high - low)`, 0.5 when the range is zero.
- `RollingLevels(100)` → `support, resistance` = min low / max high of the previous 100 bars; `breakout` when `close > prior resistance`, `breakdown` when `close < prior support`.
- `Patterns` → `engulfing` in {bullish, bearish, none} from the last two bars; `double_top`, `double_bottom` from the last two pivot highs/lows (5-bar pivots) within `tolerance_pct` and separated by at least `min_gap` bars, confirmed when the close breaks the intervening trough/peak.

`IndicatorSet(params)` holds one of each per symbol plus EMA20/EMA50/RSI14/ATR14, exposes
`update(candle)`, `ready`, and `snapshot()` returning a flat dict of floats and small ints (booleans as
0/1) with stable keys. The engine keeps `dict[symbol, IndicatorSet]`, updates before strategies
run, and the candidate's `indicators` is `{**set.snapshot(), **strategy.snapshot(symbol)}`.

The prompt's "What you receive" paragraph gains a description of the new fields. Prompt tests are
updated. The estimate-ai sample candidates carry the same keys.

## Phase 2: `strategy/confluence.py`

`ConfluenceStrategy(params)` with config section `strategy.confluence`:

```
weights: {trend: 1.0, macd: 1.0, momentum: 1.0, breakout: 1.5, pattern: 1.0, exhaustion: -1.5}
threshold: 3.0        # enter LONG when score >= threshold, SHORT when score <= -threshold
adx_min: 20           # below this no signal
volume_spike_min: 150 # percent of the 20-bar average required for a breakout vote
rsi_overbought: 70, rsi_oversold: 30, pct_b_high: 0.8, pct_b_low: 0.2
atr_stop_mult: 1.5, reward_risk: 2.0, min_stop_pct: 0.1, product: MIS
```

Votes per bar (each in {-1, 0, +1} times its weight): trend (+1 if EMA20 > EMA50), macd (the turn direction on a
bar the histogram turned, else the sign of the histogram when it agrees with trend, else 0), momentum (+1 when the 60-bar
sign agrees with the 5-bar and 1-bar signs, -1 when all negative, else 0), breakout (+1 on a breakout
with volume spike above the minimum, -1 on a breakdown with volume), pattern (+1 bullish engulfing or
double bottom, -1 bearish engulfing or double top), exhaustion (applied against the trade direction
when RSI and %B are both extreme on that side). A signal fires when the score crosses the threshold
and did not on the previous bar (no repeats while the score stays above it; the first ready bar never
fires). An ADX-gated bar counts as score 0, so the first trending bar can fire when the score built up
while ADX was low. Double top/bottom flags are 1 only on the bar the neck first breaks for that pivot pair. Stop and target as the
EMA/RSI strategy. Warm-up: ready when the ADX and the 100-bar levels are ready.

`build_strategy("confluence", params)`; CLI `--strategy confluence`. Backtest on the real window and
compare summaries with `real-1`.

## Testing

Unit tests per indicator against hand-computed sequences; MACD and Bollinger against a straightforward
reference loop; ADX on a trending series (rises) and a flat series (falls); levels and breakout on a
crafted series; engulfing and double top/bottom on crafted bars. Engine test: candidates carry the
merged snapshot keys. Confluence strategy: crafted trending series yields one LONG, its mirror one
SHORT, choppy series yields none, ADX gate, no repeat signals. Golden fixture unchanged for ema_rsi.
