# Cross-sectional momentum on the 50-name universe: results

Date: 2026-09-20. Spec: `docs/superpowers/specs/2026-09-20-cross-sectional-momentum-design.md`.
Plan: `docs/superpowers/plans/2026-09-20-cross-sectional-momentum.md`. Code:
`scripts/momentum_screen.py`, phases `run` and `quintiles`. The screen reads `data/tradebot.db`
read-only and writes nothing.

The answer is no. The pre-registered bar was spread > 0 with t >= 2. The spread is -0.093%/mo at
t -0.21, and the quintiles are not ordered.

## 1. What this tested and why

Eight strategy families have been tested on this bot and all lose money: five intraday (ema_rsi,
confluence, the 15-minute pullback, the opening-range breakout, and the Claude-authored filter over
them) and three daily (RSI-2 mean reversion, 50/200 trend following, the 100-day breakout). The
intraday ones lose mainly to costs, about 0.34R per trade against a signal worth about ±0.1R.

All eight ask the same question in different dialects: does this stock's own recent price path
predict this stock? That is time-series pattern matching — each name is ranked against its own past.

Cross-sectional momentum asks a different question: of these 50 stocks, do the ones that have
outperformed the others keep outperforming? It ranks the names against each other rather than
against their own history. That distinction is the reason it was worth a ninth test after eight
failures of the first kind.

Momentum is also externally pre-registered. It is among the most replicated anomalies in the
published literature, so the hypothesis was not discovered by searching this data. The spec
therefore deliberately used no holdout split, and the run covers 2021-2025 — including the window
(2024-01-01..2025-11-21) that the daily screen holds back as an unspent holdout for its own three
systems. That was an approved choice made in the spec before any result was seen, not an oversight.
It spends nothing that the daily screen's holdout protects, because the daily screen's three systems
are not being judged here.

## 2. The construction

- **Score**: 12-1 momentum, `close[m-1] / close[m-13] - 1` — the return over the twelve months
  ending one month before the rank date. The most recent month is skipped because short-horizon
  reversal contaminates it.
- **Dates**: rank on the month-end close; enter at the close of the next trading day; hold one
  month; exit at the close of the next trading day after the following month end. No price used to
  rank a name is ever a fill price for it.
- **Portfolio**: the top 10 of roughly 45 eligible names, equal-weighted, rebalanced monthly.
- **Baseline**: equal-weight ALL eligible names over exactly the same dates. The claim under test is
  that the top group beats the average stock, not that it beats zero. Over a window in which the
  universe roughly doubled, beating zero would prove nothing.
- **Costs**: charged on TURNOVER only. If k of the 10 names change, the month pays k/10 of a round
  trip. The baseline pays on its own turnover, which is near zero. That asymmetry is real and is
  part of what is being measured — a rebalancing rule has to pay for its rebalancing.
- **Cost rate**: charged at the position value this portfolio actually trades. 1 lakh across ten
  names is 10,000 a position, which costs 0.712% a round trip, not the 0.572% of the daily screen's
  25,000 position. Each secondary grid cell is charged at its own concentration.
- **Closes only, throughout.** Groww's daily `open` field is synthetic for 2025 (section 5), so any
  fill at an open would be fictional over more than half this window. The screen never reads the
  `open` field for a price.

## 3. The result

`.venv/bin/python scripts/momentum_screen.py run`, verbatim:

    primary  12-1 momentum, top 10, monthly   2021-02-26..2025-08-29   55 months
      portfolio   mean +1.079%/mo   cumulative +63.7%   max drawdown 35.8%
      baseline    mean +1.171%/mo   cumulative +81.7%   max drawdown 21.9%
      spread      mean -0.093%/mo   t -0.21        turnover 2.6 of 10 names/mo (round trip 0.712%)
      eligible    45.5 of the universe ranked per month on average

The bar, fixed in the spec before the screen was run, was spread > 0 with t >= 2. It was not
cleared: the spread is negative and t is -0.21.

The ranked portfolio underperformed the equal-weight baseline by 0.093%/mo, returned 63.7% against
the baseline's 81.7%, and did it through a deeper drawdown (35.8% against 21.9%). Concentrating into
the ten strongest names bought more risk and less return.

Sub-periods:

| Window | Months | Spread/mo | t |
|---|---|---|---|
| 2021-2023 | 35 | +0.240% | 0.44 |
| 2024-2025 | 20 | -0.674% | -0.85 |

Neither half is distinguishable from zero. The split is reported because the spec asked for it, not
because a sign change across two underpowered halves means anything.

Secondary grid (descriptive only; the decision is the primary cell):

| Lookback | Top 5 | Top 10 | Top 15 |
|---|---|---|---|
| 12-1 | +0.51% t 0.8 | -0.09% t -0.2 | -0.07% t -0.2 |
| 6-1 | -0.49% t -0.6 | +0.05% t 0.1 | -0.05% t -0.1 |
| 3-1 | +0.28% t 0.6 | -0.40% t -1.2 | -0.51% t -1.8 |

Nine cells were computed and the best of them is +0.51%/mo at t 0.8. That is what noise looks like
across nine cells: with nine draws from a distribution centred near zero, a best cell somewhere
around t 0.8 is the expected outcome, and it is well under the bar the single pre-registered cell
was held to. It is not a finding and it is not a lead. Chasing it would be exactly the search over
this data that the pre-registration was designed to avoid.

## 4. The quintiles

`quintiles` phase, gross of costs, group 1 = highest ranked:

| Group | Mean/mo |
|---|---|
| 1 (highest) | 1.351% |
| 2 | 1.082% |
| 3 | 1.232% |
| 4 | 0.818% |
| 5 (lowest) | 1.439% |

55 months, of which 2 were dropped because some group could not be priced. Top minus bottom:
-0.088%/mo gross. A ranking that carries no information gives about 0.

The means are not monotonic. Group 3 beats group 2, and the bottom group has the highest mean of the
five — the losers outperformed the winners, gross of costs, before any cost asymmetry enters.

Of the two pieces of evidence, the ordering is the more informative here, and it is what I would
rest the conclusion on. A t of -0.21 on its own says only "underpowered, cannot tell": 55 monthly
observations of a noisy spread cannot separate a small edge from nothing, so a t near zero is
consistent with an edge too small to see. The ordering is a different kind of test. A ranking that
carried real information would sort the five groups roughly correctly even on few observations —
the monotonic staircase is a much lower bar than statistical significance, because it asks only for
the sign of four comparisons rather than a magnitude. This ranking does not order the groups at all,
and it puts the worst-ranked group on top. That is the stronger evidence, and it agrees with the t.

### Where the loss comes from: absent signal plus real costs

The primary spread is net of costs, and it is worth separating the two components, because the
answer locates the failure rather than just confirming it.

Turnover is 2.6 of 10 names a month at a 0.712% round trip, so the portfolio pays

    2.6 / 10 x 0.712% = 0.185%/mo

in cost drag. The baseline pays on its own turnover, which is near zero but not exactly zero — the
eligible set changes a little month to month as symbols enter and leave the lookback — so 0.185%/mo
is an upper bound on the cost difference between the two, not an exact figure.

Adding it back to the net spread:

    -0.093%/mo + 0.185%/mo = +0.09%/mo gross

So before costs the ranked portfolio was very slightly ahead of the baseline, and the cost of
rebalancing into that ranking is what turned it negative.

This does not rescue the result. A gross edge of +0.09%/mo on 55 monthly observations is
indistinguishable from zero. The net t is -0.21, which implies a standard error of about
0.44%/mo (0.093 / 0.21); shifting the mean by the cost drag and leaving the standard error alone
gives a gross t of roughly +0.2 (0.092 / 0.443). That is the same verdict as the net figure with the
sign flipped by noise, and it is an order of magnitude short of the t >= 2 bar.

It also has to be read next to the quintiles, which are already gross and point the other way at
-0.088%/mo. The two do not disagree, because they are different cuts. The +0.09%/mo is the top 10 of
roughly 45 names measured against the equal-weight average of all 45. The -0.088%/mo is the top
quintile, about 9 names, measured against the bottom quintile, about 9 names. Both sit within about
0.1%/mo of zero. The honest reading is that gross, every cut of this ranking lands in a band around
zero roughly 0.1%/mo wide, and which side of zero any particular cut falls on is not stable.

Why it matters anyway: it says what kind of failure this is. Momentum over this universe is not
strongly wrong — a signal that were reliably backwards would be as useful as one that were right,
inverted. It is absent. Costs then turn absent into negative. That is the same shape as the five
intraday families, where the signal was worth about ±0.1R against costs of about 0.34R: a real cost
charged against a signal worth approximately nothing. Nine families in, that is the recurring
pattern, and it is a more precise finding than "it lost money".

### The defect in the first version

The first `quintiles` implementation appended each group's return independently, so a month in which
one group could not be priced still counted for the other four. Group 5 ended with 55 observations
against 56 for the rest, and top-minus-bottom was a difference between means computed over different
sets of months. It now keeps a month only if all five groups price. The two dropped months are
2025-09-30 and 2025-10-29, both caused by TATAMOTORS' history ending 2025-10-23. The figure moved
from -0.061%/mo to -0.088%/mo. The conclusion did not change.

## 5. The data defect: Groww's synthetic 2025 `open`

In Groww's daily candles, 98.6% of 2025 bars carry an `open` exactly equal to the previous bar's
close, against 4-7% in 2020-2024 — which is the normal rate for a stock genuinely opening unchanged.
The 2025 `open` field is synthetic.

What it would have broken: any fill at a next-day open over 2025 is really a fill at the prior
close, so a screen entering at opens would have been reporting fictional fills over more than half
this window. This screen sidesteps the problem entirely by using closes only and never reading the
`open` field for a price.

Consequences elsewhere:

- The daily screen's IN-SAMPLE window (2020-2023) is unaffected. Opens are real there and no
  inside-the-bar corporate action falls in it. Its committed results stand.
- The daily screen's unspent HOLDOUT window (2024-01-01..2025-11-21) IS affected, because it enters
  at the next day's open. It must not be run until the fill rule is fixed.
- 15-minute and 5-minute data are clean; intraday work is unaffected.

The second consequence is corporate actions. `gap_mask` detected an unadjusted split by comparing
the open to the previous close, which works only where the open is real. Five corporate actions show
their split INSIDE a bar rather than as an overnight gap, and all five are in 2025, where the open
never moves: SHRIRAMFIN 2025-01-10, BAJFINANCE 2025-06-16, NESTLEIND 2025-08-08, HDFCBANK
2025-08-26, TATAMOTORS 2025-10-14. `gap_mask` now carries a second detector for exactly that shape —
a close more than 35% from the open, conditioned on the open being exactly the previous close. The
condition is deliberate: the inside-the-bar shape only exists because the open is fake, and
conditioning on it widens the separation the threshold has to make.

## 6. Limits

**Survivorship bias, first and largest.** `universe.yaml` is today's index list. Stocks that were
added during the window because they rose are present with the full history of that rise, and stocks
that were dropped after falling are absent entirely. The direction matters: this bias runs IN
MOMENTUM'S FAVOUR. The names that would have been ranked top and then collapsed out of the index are
the ones missing, and the names that rose into the index arrive with their rise already in the
lookback. So the true spread is more likely worse than -0.093%/mo than better, and the negative
result is, if anything, understated. `load_universe` (`src/tradebot/data/universe.py`) takes an
`as_of` parameter that deliberately raises `NotImplementedError` rather than silently returning
today's list; that is the hook a point-in-time fix would use.

Other limits:

- Three symbols have short or truncated histories in the stored data: SHRIRAMFIN 2022-12-20 to
  2025-11-21 (merger), TATACONSUM 2020-02-27 to 2025-11-21 (rename), TATAMOTORS 2020-01-01 to
  2025-10-23 (demerger). They are ineligible where the lookback or the hold cannot be priced, which
  is why the eligible count averages 45.5 rather than 50, and why two quintile months were dropped.
- 55 monthly observations is few. A monthly rebalance gives twelve data points a year, so even five
  years buys little power; this is the arithmetic behind a t of -0.21 meaning less than the ordering
  does.
- 2021-02 to 2025-08 was one long bull market plus two corrections. A single regime, and the
  baseline (holding the whole eligible set) is doing a lot of work in a rising market.
- Charge rates are typed from the spec and have not been verified against Groww's pricing page. No
  net figure in this repository has been.
- No market impact and no bid-ask beyond the slippage assumption in the schedule. A monthly
  rebalance of 2.6 names in liquid large caps is the benign case for this, but it is still an
  assumption.
- Lot sizes are ignored; positions are treated as perfectly divisible.

## 7. Conclusion

Supported: the construction — the 12-1 score, the one-day gap between rank and fill, the
turnover-only cost model at this portfolio's own position size, the eligible-set baseline, and the
all-five-groups-price rule in the quintiles — measures what the spec said it would, and the
inside-the-bar detector closes the 2025 corporate-action hole.

Supported, and negative: cross-sectional 12-1 momentum over these 50 names, 2021-2025, shows no
edge. It lost 0.093%/mo to an equal-weight baseline with t -0.21, took a deeper drawdown to do it
(35.8% against 21.9%), and its quintiles are not ordered — the bottom group has the highest gross
mean. The pre-registered bar was not cleared. The best of nine secondary grid cells is noise.

Not supported: any profitable configuration. Nine families have now been tested — five intraday,
three daily, and this one — and none shows an edge over its baseline.

What remains untried:

1. The delivery-percentage screen, already specced at
   `docs/superpowers/specs/2026-09-20-delivery-signal-screen-design.md`. It is the last queued
   direction that this repository has data for.
2. Non-price sources the project has no data for at all: post-earnings-announcement drift,
   index-inclusion flows, bulk and block deals. Each would need a data acquisition step before any
   screen could be written.
3. Point-in-time constituents, via the `as_of` hook. That is a correctness fix rather than a new
   hypothesis, and on the direction of the bias it would make this result worse, not better.

The honest reading of nine failures is not that no edge exists. It is that this universe, at this
cost level, over this window, does not obviously contain a tradable edge findable by these methods —
ranking on price history, whether against a stock's own past or against its peers. That is a
statement about what has been searched, and the search has been narrow: 50 large caps, one broker's
cost schedule, five to six years, and price data almost exclusively. A different universe, a lower
cost base, or a genuinely non-price signal remains untested rather than refuted. But the delivery
screen is the only such test currently within reach of the data on hand, and if it also fails, the
sensible conclusion is that the constraint is the data, not the strategy search.
