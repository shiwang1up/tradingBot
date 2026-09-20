# Cross-Sectional Momentum Screen — Design Spec

Date: 2026-09-20. Status: approved in conversation. Branch `dev-nonprice-signals`, off `main` at
`513fed5`. EXPERIMENTAL: nothing reaches `main` unless it performs. Runs BEFORE the delivery screen
specced in `2026-09-20-delivery-signal-screen-design.md`, which stays queued.

## Why this is a different question

Eight strategy families have failed on this bot: five intraday (ema_rsi, confluence, 15-minute
pullback, opening-range breakout, the Claude filter over them) and three daily (RSI-2 mean
reversion, 50/200 trend following, 100-day breakout). Every one asks the SAME question in a
different dialect: does this stock's own recent price path predict this stock? The measured answer
is no, eight times, and for the intraday ones costs of about 0.34R per trade then guarantee a loss.

Cross-sectional momentum asks a different question: of these 50 stocks, do the ones that have
outperformed the others keep outperforming? It ranks across names rather than matching a pattern
within one. It is the most heavily documented anomaly in finance, replicated across decades,
countries and asset classes, and it carries an economic story rather than a chart shape. It needs
only price data, which is already stored.

It is also the hypothesis the daily screen's own baseline pointed at: all three daily systems lost
to simply holding the same stocks, i.e. exposure paid and timing did not.

## DATA DEFECT that shapes this design

Established read-only on 2026-09-20: in Groww's DAILY candles, 98.6% of 2025 bars have an `open`
exactly equal to the previous bar's `close`, against 4-7% in 2020-2024 (the normal rate for a stock
genuinely opening unchanged). The 2025 `open` field is synthetic. Consequences:

- Any fill at a next-day OPEN over 2025 is really a fill at the prior close.
- The five corporate actions whose split appears INSIDE a bar rather than as an overnight gap are
  all in 2025, and are invisible to the existing `gap_mask`, which compares open to previous close.
  Before 2025 the open is real, so splits appear as gaps and the mask catches them.
- The daily screen's IN-SAMPLE window (2020-2023) is unaffected: opens are real and no
  inside-the-bar action falls in it. Its committed results stand.
- The daily screen's HOLDOUT window (2024-01..2025-11) is corrupted for open-based fills. It is
  deliberately unspent, so nothing has been read from it, but it must not be run until fixed.
- 15-minute (0.2%) and 5-minute (1.0%) data are clean; intraday work is unaffected.

This design therefore uses CLOSES ONLY and never reads the `open` field.

## Part 1: construction, fixed before any result is seen

Universe: the 50 symbols in `universe.yaml`, daily candles at interval 1440.

- **Rank date**: the last trading day of each calendar month, m.
- **Score**: the 12-1 momentum, `close[m-1] / close[m-13] - 1`, i.e. the return over twelve months
  ending one month before the rank date. The skipped month is standard: short-horizon reversal
  contaminates the most recent month and would work against the signal.
- **Entry**: the close of the FIRST trading day after the rank date, so no position is bought at a
  price that was used to rank it.
- **Exit**: the close of the first trading day after the next rank date. Hold is one month.
- **Portfolio**: equal-weight the top 10 of the eligible names (a quintile of 50). Not the top 5:
  at 1 lakh of capital five positions is 20,000 each and one name dominates the result.
- **Eligibility** for a rank date: the symbol has closes at m-13, m-1 and m; and no corporate-action
  date (Part 2) falls in [m-13, m]. An ineligible symbol is excluded from BOTH the ranked portfolio
  and the baseline for that month, so the two always hold the same candidate set.
- **Costs**: the delivery schedule already in the codebase (`round_trip_cost`), charged on
  TURNOVER only, and sized to the position this portfolio actually trades: 1 lakh across ten
  names is 10,000 each, which costs 0.712% a round trip, not the 0.572% of the daily screen's
  25,000 position. Each secondary grid cell is charged at its own concentration, so a more
  concentrated basket is correctly dearer per rupee rather than sharing the primary's number. If k of the 10 names change at a rebalance, the
  month is charged `k/10 * round_trip_cost`. The baseline is charged the same way on its own
  turnover, which is near zero, and that asymmetry is real and must not be papered over.
- **Window**: rank dates from 2021-01 (the first month with 13 months of history) to the last full
  month in the data, about 59 monthly observations.

## Part 2: a corporate-action detector that catches both patterns

`gap_mask` today flags `|open[t] / close[t-1] - 1| > 0.20`. That misses the 2025 pattern. Add a
second detector and take the union:

- **Overnight**: `|open[t] / close[t-1] - 1| > 0.20` (existing).
- **Inside the bar**: `|close[t] / open[t] - 1| > 0.35`, AND `open[t]` is exactly `close[t-1]`.

The second detector is conditioned on the synthetic open rather than applied everywhere, because
the inside-the-bar shape only exists BECAUSE the open is fake; where the open is real a split is an
overnight gap and the first detector has it. Measured over this universe, among bars moving more
than 20% from open to close: those with a real open top out at +37.8% (INDUSINDBK 2020-03-26, a
COVID-crash rebound) and are all genuine; those with a synthetic open are the five splits, -40.2% to
-89.9%, plus one genuine crash at -27.2% (INDUSINDBK 2025-03-11). The threshold therefore has to
separate 27.2% from 40.2%, and 0.35 sits in the middle. Applied unconditionally it would have to
separate 37.8% from 40.2% instead, a window too narrow to trust. Over-masking costs data;
under-masking manufactures a -90% return, which is far worse.

Both detectors mask the event date. For the daily screen's trade simulation the existing 200-day
forward mask stays. For THIS screen, eligibility uses the [m-13, m] window rule in Part 1, which is
the relevant exclusion for a 13-month lookback.

This change also belongs in the delivery screen spec, which shares `gap_mask`; amend it when that
work starts.

## Part 3: what is measured

Per month: the equal-weight return of the top-10 portfolio net of its turnover cost, and the
equal-weight return of ALL eligible names net of its own turnover cost (the baseline). The spread
is `top10 - baseline`.

Reported:
- mean monthly return of the portfolio and of the baseline, and their difference;
- the **t of the monthly spread** over the ~59 months (sample standard deviation, n-1);
- cumulative return of both over the window, and the largest peak-to-trough drawdown of each,
  because a spread that only appears with a 40% drawdown is not tradable at this capital;
- monthly turnover (mean names changed of 10) and the total cost paid, so the cost drag is visible
  rather than buried;
- the count of months and of ineligible symbol-months.

**Quintile monotonicity, the more convincing evidence.** Split all eligible names into five ranked
groups each month and report each group's mean monthly return. If momentum carries information the
ordering should be roughly monotonic from top to bottom. That pattern is much harder to produce by
chance than one significant cell, and it is the first thing to read. The BOTTOM quintile is a
built-in control: if top and bottom perform the same, the ranking says nothing regardless of what
the top group's absolute return looks like.

## Part 4: the bar

- **Primary, fixed in advance**: 12-1 lookback, top 10, monthly hold, 2021-01 to the last full
  month. Pass requires the monthly spread over the equal-weight baseline to be positive with
  **t >= 2**. One primary test, so no multiplicity correction is needed.
- **Secondary, descriptive only**: 6-1 and 3-1 lookbacks; top-5 and top-15 portfolios. Reported,
  never decision-relevant. Fixing one primary stops the lookback and the portfolio size becoming
  parameters to shop in.
- **No holdout split.** Momentum is pre-registered by decades of published literature rather than
  discovered in this data, so the usual data-mining concern does not apply the same way, and 59
  months does not survive being halved. Instead report 2021-2023 and 2024-2025 separately as a
  stability check. If the effect appears in one sub-period only, say so plainly; that is a weaker
  result than the headline t would suggest.

## Part 5: the honest threat, and what a pass would and would not mean

**Survivorship is worse here than for any earlier screen.** `universe.yaml` is today's Nifty 50.

- Stocks ADDED to the index during 2021-2025 because they rose are present with their full history,
  including the rise that earned them a place. That manufactures momentum and biases FOR the result.
- Stocks DROPPED after falling are absent, which removes losers from the bottom quintile and biases
  AGAINST the spread.

The net direction is unclear and the magnitude could be material. It cannot be fixed without a
point-in-time constituent list, which is not freely available. Mitigation: report which symbols have
short or truncated histories (known: SHRIRAMFIN from 2022-12-20, a merger; TATACONSUM from
2020-02-27, a rename; TATAMOTORS to 2025-10-23, a demerger) and state the bias prominently beside
any positive result.

A pass therefore means: worth investigating with point-in-time data, not proven. A null means: this
formulation, on these 50 survivors, over this period, did not beat equal-weighting them.

Other limits carried into the results: 59 monthly observations is a small sample for a t; 2021-2025
is one long bull market plus two corrections; the charge rates are still unverified against Groww's
pricing page; trade-level costs assume the delivery schedule and ignore impact; and the portfolio
ignores capital constraints, lot sizes and the fact that 10 equal positions at 1 lakh is 10,000 each.

## Testing

Test-first, Python 3.9. `tests/test_momentum_screen.py`, importing the script by path as the other
script tests do: the 12-1 score on a hand-built series (including that it skips the right month);
month-end rank-date selection across a month boundary and a holiday; entry at the first close AFTER
the rank date, never on it; eligibility rejecting a symbol with a corporate action anywhere in
[m-13, m] and a symbol with missing history; turnover costing (0 changed names costs nothing, 10
changed costs a full round trip); the baseline covering exactly the eligible set; the monthly spread
and its t on a hand-worked three-month example; quintile assignment with a count not divisible by 5.
Extend `tests/test_daily_screen.py` for the new inside-the-bar detector, including that INDUSINDBK's
+37.8% real move is NOT masked at a 30% threshold while a -50% split IS.

## Deliverables

`scripts/momentum_screen.py` (phases `run` and `quintiles`), the `gap_mask` change, their tests, and
`docs/superpowers/notes/2026-09-20-cross-sectional-momentum-results.md`. No change to `main`.
