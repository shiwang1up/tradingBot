# Charges, opening-range breakout and regime filter: results

Date: 2026-09-20. Spec: `docs/superpowers/specs/2026-09-19-charges-orb-regime-design.md` (see its
Amendments). Every figure below was read back from `data/tradebot.db` with `tradebot report --run <id>`
or read-only SQL. No candle, signal or price dated 2026-08-16 or later was used for the ORB or regime
work.

## What changed

- **Charges.** Backtest, paper and reports are net of brokerage and statutory charges (`charges:` in
  the config). `positions.pnl` stays gross; `positions.charges` holds the cost (schema v4). Runs stored
  before the model are estimated after the fact and marked so in the report.
- **`R on risk`** is the decision metric: net PnL over rupees at risk, all trades pooled. Its sign is the
  sign of net PnL, and scrap-sized trades cannot dominate it. The per-trade `Avg R` is still printed.
- **ORB.** A new strategy `orb` on 15-minute bars with its own config, `config-orb.yaml`.
- **Signal priority.** A signal carries a `priority` (schema v5, `signals.priority`). Signals on one bar
  are taken highest priority first, then alphabetically, in backtest and paper alike.
- **Regime filter.** Longs only while the index is above its EMA, shorts only while at or below. The
  index comes from stored NIFTY candles, or from a composite built from the universe's own returns.
- **Index candles** are fetched and stored with the universe and never traded.

## Charges

Schedule used (from `config.yaml`; identical in `config-15m.yaml` and `config-orb.yaml`): brokerage 0.1%
of order value per order, floor 5 rupees, cap 20; STT 0.025% on the sell side; exchange transaction
0.00297% both sides; SEBI 0.0001% both sides; stamp duty 0.003% on the buy side; GST 18% on brokerage,
exchange and SEBI charges. **These rates were typed into the config and have not been checked against
Groww's pricing page. Check them before trusting any net figure.**

The old runs, restated. Charges are estimated from each position's entry and exit, since none were
recorded at the time. These runs were made over the data stored at the time, not the tuning window
used below, and they do not all cover the same dates: `real-1-sonnet` stops 17 trading days before
`real-1` (spans under the table).

| Run | Strategy | Trades | Gross | Charges (est.) | Net | Avg R (per trade) | R on risk |
|---|---|---|---|---|---|---|---|
| `real-1` | ema_rsi, 5 min, stub filter | 884 | -84,699 | 44,501 | -129,200 | -3.96 | -0.61 |
| `real-1-sonnet` | ema_rsi, 5 min, Claude filter | 346 | -63,474 | 26,086 | -89,560 | -2.14 | -0.56 |
| `real-conf-2` | confluence, 5 min | 1,018 | -65,112 | 54,747 | -119,859 | -4.26 | -0.41 |
| `pb-15m-1` | pullback, 15 min | 512 | -50,200 | 37,139 | -87,338 | -2.07 | -0.35 |

Spans, from each run's daily rows: `real-1` 2026-06-17 to 2026-09-11, 62 trading days;
`real-1-sonnet` 2026-06-17 to 2026-08-19, 45 trading days; `real-conf-2` 2026-06-17 to 2026-09-11, 62;
`pb-15m-1` 2026-06-18 to 2026-09-15, 62. Rows of different spans are not comparable on rupee totals.

Charges add another 41 to 84% of the gross loss. Every old strategy was already negative before them.

Why the unweighted `Avg R` is not used. On `real-1` the median order value is 2,888 rupees and the
median trade risks 9.87 rupees, against a budget of 1,000. 490 of the 884 trades risk under 25 rupees,
and on those the median charge is 4.05 R, because the brokerage floor is 5 rupees per order whatever the
size. Averaging per-trade R lets those scraps set the figure: -3.96, against -0.61 when every rupee at
risk counts once. The cause is sizing. 1% of capital against a median stop of 0.34% of price asks for
about three times capital in notional per position. With 5 slots and 5x leverage the first position or
two take the margin and the later ones are sized from what is left (risk per trade: 10th percentile
1.1, median 9.9, 75th percentile 427, 90th percentile 787 rupees; only 64 trades risk 900 or more). A
minimum order value or minimum risk rule in the risk engine would remove the scraps. It is NOT built.
It is an open decision.

Realised risk runs above the budget. Size is set from the signal's entry price, but the fill is the
next bar's open plus slippage, further from the stop. On the `real-1` trades opened before 2026-08-16
(597 of 884, joined to their signals through the entry order), fill-based risk sums to 176,373 rupees
against 152,152 planned: 16% more. On `orb-t-60-1.5` it is 83,522 against 79,708: 5% more, because the
stop is three times wider.

The Claude filter, read again, on the same window. `real-1-sonnet` covers 45 trading days (to
2026-08-19) and `real-1` covers 62, so the table rows above are not like for like. `real-1` restricted
to positions opened on or before 2026-08-19 (the same 45 days): 638 trades, gross -71,987, charges
(est.) 35,249, net -107,235, `R on risk` -0.589. Against `real-1-sonnet` (346 trades, net -89,560,
-0.557) the filter loses 17,675 rupees less and `R on risk` moves from -0.59 to -0.56. The
whole-window difference (39,640 rupees, -0.61 to -0.56) overstates the saving, because the filtered
run is shorter: 21,965 of it is 17 further days of `real-1` losing money. The filter approved 347 of
1,326 signals. It mostly traded less of a losing strategy; it did not find the good trades.

## ORB

Design. The range is the high and low of the first 30 or 60 minutes. The first 15-minute bar that
closes beyond it fires: long above with the stop at the range low, short below with the stop at the
range high, target at `reward_risk` times the stop distance, or no target. One signal per symbol per
day. Ranges narrower than 0.4% or wider than 1.5% of price are skipped. At most two entries a day, none
after 13:00, flat at 15:00. Priority is the breakout bar's volume over the mean range-bar volume. The
point of the design is a stop near 1% of price, so that costs are small in R and a full-size position
fits in the margin.

Tuning, 2026-06-17 to 2026-08-15 (41 trading days, 49 symbols, stub filter, 5 bps slippage):

| Run | Trades | Win | Gross | Charges | Net | R on risk |
|---|---|---|---|---|---|---|
| `orb-t-30-1.5` | 82 | 34.1% | -21,565 | 6,978 | -28,543 | -0.335 |
| `orb-t-30-2.0` | 82 | 32.9% | -23,033 | 6,977 | -30,010 | -0.353 |
| `orb-t-30-none` | 82 | 32.9% | -21,822 | 6,978 | -28,800 | -0.339 |
| `orb-t-60-1.5` | 81 | 33.3% | -17,394 | 6,593 | -23,987 | -0.287 |
| `orb-t-60-2.0` | 81 | 33.3% | -18,122 | 6,593 | -24,715 | -0.296 |
| `orb-t-60-none` | 81 | 33.3% | -18,122 | 6,593 | -24,715 | -0.296 |

Structure:

- **Entries sit on one bar.** In `orb-t-30-2.0`, 81 of 82 positions come from the 09:45 signal bar, the
  first bar after the range (filled at the 10:00 open). In `orb-t-60-1.5`, 80 of 81 come from the 10:15
  bar (filled at 10:30). With two entries a day and about 1,300 to 1,500 signals in the window, the two
  slots are gone on the first bar: 999 of 1,328 signals in `orb-t-60-1.5` were rejected
  `max_entries_per_day`. Every day used both slots by 10:45, so the 13:00 cut-off never binds.
- **Exits** for `orb-t-60-1.5`: SQUARE_OFF 60 (average -0.12 R net), STOP 18 (-1.13 R), TARGET 3
  (+1.24 R). For `orb-t-30-2.0`: SQUARE_OFF 55, STOP 25, TARGET 2. `orb-t-60-2.0` never reached a
  target, so it equals `orb-t-60-none` trade for trade.
- **Cost per trade** for `orb-t-60-1.5`: median charges 0.078 R, median stop distance 1.09% of price,
  median order value about 96,000 rupees. For `real-1`: 1.32 R and 0.34%. Pooled (total charges over
  total rupees at risk) the figures are 0.079 R against 0.21 R; the pooled gap is smaller because the
  few full-size `real-1` trades carry most of its risk.
- **Targets are out of reach by geometry.** The stop is the far side of the range and the entry is
  already beyond the near side. The median stop distance is 1.24 range widths (`orb-t-60-1.5`; 1.25 for
  `orb-t-30-2.0`; range widths from the stored tuning-window candles). A 1.5R target needs a further
  1.9 widths and a 2R target 2.5 widths, after a breakout, before 15:00. Three in four trades end at
  the square-off, close to flat.

Evidence, `orb-t-60-1.5` only. 41 days, 7 positive. Daily realised PnL (net): mean -585, standard
deviation 963, t = -3.89. Gross of charges, from the positions: mean -424, 9 positive days, t = -2.82.
40 of the 41 days hold two positions and on 39 of those both entered on the same bar, so this is about
41 bets, not 81.

Reading. Cost per unit of risk is no longer the dominant problem: the median cost per trade fell from
1.32 R to 0.08 R (pooled, 0.21 R to 0.079 R) and every position is full size. It is not gone. Charges
are still 27% of the net loss of `orb-t-60-1.5` (6,593 of 23,987), and 0.08 R per trade is comparable
to a realistic intraday edge, so a strategy would have to earn that much before it breaks even. The
strategy has no edge on the tuning window. It is negative before charges
and more negative after, for all six parameter sets, and the parameters barely matter (-0.29 to
-0.35). 60/1.5 is the least bad and is the one carried into the regime experiment; that is not a
selection worth defending.

Verification during development: every position of all six runs was re-derived from raw candles by an
independent script and matched. No look-ahead was found.

The shipped `config-orb.yaml` carries `range_minutes: 30, reward_risk: 2.0` (the plan's starting
values), not the least-bad tuning row (60 / 1.5). No configuration is recommended.

**The holdout (2026-08-16 to 2026-09-15) was NOT run for ORB.** This is deliberate. Every tuning
configuration is clearly negative, so the holdout could only confirm a rejection. Leaving it unspent
keeps that window clean for future variants. `scripts/orb_experiment.py holdout` remains available and
guarded (fixed run id, runs once).

## Regime filter

Definition. UP when the index's last close is above its 20-bar EMA on the execution interval,
otherwise DOWN. Longs need UP, shorts need DOWN. A signal before the EMA is warm is rejected
`regime_not_ready`. The gate runs before the risk check.

Index data stored: NIFTY, 4,884 five-minute rows and 1,628 fifteen-minute rows, first bar 2026-06-17
14:45, last bar 2026-09-18. `--source index` was used; the composite source exists and is tested but
was not part of the experiment.

Experiment: tuning window only, ORB 60/1.5 and 5-minute confluence, filter off and on. Each pair is
also scored from a common start, the day after the filter-on run's last `regime_not_ready` day, so
warm-up days do not count against either row.

| Run | Scored from | Trades | Win | Gross | Charges | Net | R on risk |
|---|---|---|---|---|---|---|---|
| `rg-orb-off-tune` | whole window | 81 | 33.3% | -17,394 | 6,593 | -23,987 | -0.287 |
| `rg-orb-on-tune` | whole window | 79 | 36.7% | -16,178 | 6,416 | -22,594 | -0.278 |
| `rg-orb-off-tune` | 2026-06-19 | 79 | 32.9% | -16,977 | 6,443 | -23,420 | -0.287 |
| `rg-orb-on-tune` | 2026-06-19 | 79 | 36.7% | -16,178 | 6,416 | -22,594 | -0.278 |
| `rg-confluence-off-tune` | whole window | 620 | 17.4% | -41,499 | 34,517 | -76,016 | -0.415 |
| `rg-confluence-on-tune` | whole window | 622 | 20.1% | -35,622 | 36,404 | -72,026 | -0.370 |
| `rg-confluence-off-tune` | 2026-06-17 | 620 | 17.4% | -41,499 | 34,517 | -76,016 | -0.415 |
| `rg-confluence-on-tune` | 2026-06-17 | 622 | 20.1% | -35,622 | 36,404 | -72,026 | -0.370 |

Filter-on rejections: ORB `regime` 417, `regime_not_ready` 31 (all on 2026-06-18, the first day);
confluence `regime` 4,064, `regime_not_ready` 0, so its scored rows equal its whole-window rows.

Determinism check: `rg-orb-off-tune`, run with the index loaded and stripped, reproduces `orb-t-60-1.5`
exactly, position for position (81 of 81, same fills, PnL and charges).

Reading, by the rule written down before the runs: the filter is worth validating out of sample only
if `R on risk` is higher with it AND above zero. It is higher for both strategies (-0.287 to -0.278,
-0.415 to -0.370) and clearly negative for both. It is not worth validating. Trade counts barely
changed because a blocked signal frees its slot for the next-ranked one: filter-on is a different
portfolio, not a subset. ORB went from 40 long and 41 short to 42 long and 37 short. The `regime`
count is signals gated, not trades removed.

Caveat. ORB enters off the 09:45 or 10:15 bar. On 15-minute bars a 20-bar EMA at that time is mostly
the previous session's prices, so the filter is closer to an overnight-gap filter than a same-day trend
filter. ONE alternative definition is pre-registered in the spec (index above today's session open).
It has NOT been tested.

## Modelling choices and known limits

- Survivorship bias: `universe.yaml` is today's constituent list, so every result is overstated.
- Three months of data, one market regime. The tuning window is 41 trading days.
- 49 of 50 universe symbols have candles, at 15 minutes and at 5. TATAMOTORS has none.
- A 0.05 tick is assumed for every symbol. A few trade on a finer tick; the effect is negligible.
- Slippage is 5 bps on entries, stops and square-offs, none on targets.
- When one bar touches both stop and target, the stop is assumed first.
- An entry that does not fill (next open beyond the 0.1% buffer) still uses a daily entry slot. It
  happened once in `orb-t-60-1.5`.
- Unrealised PnL is gross of the exit charges still to come, so the daily loss cap can trigger slightly
  late.
- Sizing uses the fixed starting capital; the margin cap uses running cash.
- Ranking is per bar. An ordinary early breakout beats a stronger later one.
- Groww's fee for a broker-initiated auto square-off is not modelled; the bot squares off itself.
- `R` uses the filled entry price, not the signal's.
- Paper mode for ORB must be running before the first bar after the range closes. A breakout seen
  during warm-up is spent, not traded.
- The charge rates are unverified (see Charges).

## Conclusion

Supported: the machinery. Charges in backtest, paper and reports, with old runs restated; `R on risk`;
the `orb` strategy; priority ranking of same-bar signals; the regime filter with index and composite
sources; index fetch and storage; the guarded experiment script. ORB runs match an independent
re-derivation and the filter-off regime run reproduces its tuning twin exactly.

Supported, and negative: every old strategy is worse net of charges than it looked. With ORB, cost per
unit of risk is no longer the dominant problem (0.08 R per trade, still 27% of its net loss), and it
has no edge on the tuning window. The regime filter improves `R on risk` slightly and
rescues nothing.

Not supported: any profitable configuration.

Open decisions:

1. Whether to spend the ORB holdout. It can only confirm the rejection.
2. A minimum order value or minimum risk rule in the risk engine.
3. Whether to test the one pre-registered alternative regime definition, on the tuning window only.
4. More data, a longer history, before more strategy work.
