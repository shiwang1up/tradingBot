# Cross-sectional momentum on the 50-name universe: results

Date: 2026-09-20. Spec: `docs/superpowers/specs/2026-09-20-cross-sectional-momentum-design.md`.
Plan: `docs/superpowers/plans/2026-09-20-cross-sectional-momentum.md`. Code:
`scripts/momentum_screen.py`, phases `run` and `quintiles`, as of `3e1958c`. The screen reads
`data/tradebot.db` read-only and writes nothing.

The pre-registered bar was spread > 0 AND t >= 2. Spread > 0 is met, at +0.187%/mo. The t is 0.49.
**The bar is not cleared and momentum does not pass.**

It is not a clean null either, and this note does not present it as one. Six independent measures
all lean the same way and not one of them is significant. The accurate summary is that the result is
consistently weakly positive, uniformly underpowered, and plausibly explained in full by
survivorship bias.

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
It spends nothing the daily screen's holdout protects, because the daily screen's three systems are
not being judged here.

## 2. The construction

- **Score**: 12-1 momentum, `close[m-1] / close[m-13] - 1` — the return over the twelve months
  ending one month before the rank date. The most recent month is skipped because short-horizon
  reversal contaminates it.
- **Dates**: rank on the month-end close; enter at the close of the next trading day; hold one
  month; exit at the close of the next trading day after the following month end. No price used to
  rank a name is ever a fill price for it.
- **Portfolio**: the top 10 of roughly 46 eligible names, equal-weighted, rebalanced monthly.
- **Baseline**: equal-weight ALL eligible names over exactly the same dates. The claim under test is
  that the top group beats the average stock, not that it beats zero. Over a window in which the
  universe roughly doubled, beating zero would prove nothing.
- **Eligibility**: the symbol prices at m-13, m-1 and m, and no corporate action falls anywhere in
  `[m-13, exit]` — the lookback AND the month actually held. An ineligible symbol leaves both the
  portfolio and the baseline that month, so the two always hold the same candidate set. The
  hold-period half of that guard was missing until `3e1958c`; see section 6.
- **Costs**: charged on TURNOVER only. If k of the 10 names change, the month pays k/10 of a round
  trip. The baseline pays on its own turnover, which is near zero. That asymmetry is real and is
  part of what is measured — a rebalancing rule has to pay for its rebalancing.
- **Cost rate**: charged at the position value this portfolio actually trades. 1 lakh across ten
  names is 10,000 a position, which costs 0.712% a round trip, not the 0.572% of the daily screen's
  25,000 position. Each secondary grid cell is charged at its own concentration.
- **Closes only, throughout.** Groww's daily `open` field is synthetic for 2025 (section 7), so any
  fill at an open would be fictional over more than half this window. The screen never reads the
  `open` field for a price.

## 3. The result

`.venv/bin/python scripts/momentum_screen.py run`, verbatim:

    primary  12-1 momentum, top 10, monthly   2021-02-26..2025-09-30   56 months
      portfolio   mean +1.750%/mo   cumulative +142.9%   max drawdown 25.6%
      baseline    mean +1.563%/mo   cumulative +129.0%   max drawdown 17.5%
      spread      mean +0.187%/mo   t 0.49        turnover 2.6 of 10 names/mo (round trip 0.712%)
      eligible    46.4 of the universe ranked per month on average

The bar, fixed in the spec before the screen was run, was spread > 0 AND t >= 2. The first half is
met and the second is not: t is 0.49, against a required 2. The bar is not cleared.

That bar was written in advance precisely so that a near-miss could not be relabelled a hit once the
numbers were visible. It is not being relabelled. A spread of +0.187%/mo at t 0.49 is what a true
edge of zero produces roughly half the time, and the sections below quantify how little this data
could have distinguished.

The ranked portfolio did beat the baseline, returning +142.9% against +129.0%, but it carried a
deeper drawdown to do it (25.6% against 17.5%). Concentrating into ten names bought a little more
return and a visibly worse ride.

Sub-periods:

| Window | Months | Spread/mo | t |
|---|---|---|---|
| 2021-2023 | 35 | +0.246% | 0.49 |
| 2024-2025 | 21 | +0.089% | 0.15 |

Both halves are positive, both are far from significant, and the split is reported because the spec
asked for it. Two underpowered halves agreeing in sign is worth slightly more than two disagreeing,
and much less than one adequately powered test.

Secondary grid (descriptive only; the decision is the primary cell). `n` is the month count, which
rises as the lookback shortens and more early months become scoreable:

| Lookback | Top 5 | Top 10 | Top 15 |
|---|---|---|---|
| 12-1 | +0.82% t 1.8 (n 56) | +0.19% t 0.5 (n 56) | +0.11% t 0.4 (n 56) |
| 6-1 | +0.20% t 0.3 (n 62) | +0.19% t 0.6 (n 62) | +0.12% t 0.4 (n 62) |
| 3-1 | -0.08% t -0.2 (n 65) | -0.34% t -1.1 (n 65) | -0.28% t -1.1 (n 65) |

The strongest cell is 12-1 top-5 at +0.82%/mo, t 1.8. Nine cells were computed. The permutation
test in section 5 puts a number on what that is worth: 5.3% of single random rankings exceed
|t| = 2 outright, so one cell reaching t 1.8 among nine correlated cells is unremarkable. It is not
a lead and it is not a finding, and following it up would be exactly the search over this data that
the pre-registration exists to prevent. The concentration pattern across the top row — 0.82, 0.19,
0.11 as the basket widens — is what a small real edge would look like and equally what noise in a
five-name basket would look like; five names of 46 is not a portfolio whose month-to-month variance
is small enough to separate those.

## 4. The quintiles

`quintiles` phase, gross of costs, group 1 = highest ranked. 56 months, 1 dropped because a group
could not be priced:

| Group | Mean/mo |
|---|---|
| 1 (highest) | 1.962% |
| 2 | 1.482% |
| 3 | 1.379% |
| 4 | 1.595% |
| 5 (lowest) | 1.440% |

Top minus bottom: **+0.522%/mo** gross. A ranking that carries no information gives about 0.

Group 1 is clearly the best of the five, by 0.37 points over the next best. That is the single most
encouraging number in this note. But the remaining four are jumbled — group 4 sits above both group
2 and group 3, and group 5 sits above group 3 — so the set is NOT monotonic.

The honest reading of that shape: the top group separating cleanly while groups 2 through 5 shuffle
is consistent with a real signal concentrated in the extreme of the ranking, which is a common and
well-documented shape for momentum. It is equally consistent with one group of nine names getting
lucky over 56 months. A monotonic staircase across all five would have been strong evidence on its
own, because ordering four comparisons correctly is a demanding test that does not require
significance. One group out of order might be noise around a real effect; three out of order is not
evidence of structure. The quintiles therefore support the same verdict as everything else:
directionally encouraging, individually inconclusive.

## 5. Is the signal real? Controls, permutation, IC and power

Four checks were run against the shipped code, all after the fix in section 6.

### Controls

Identical pipeline, same eligibility, same dates, same costing; only the ranking function changes:

| Ranking | Net | t | Gross | t |
|---|---|---|---|---|
| Momentum 12-1 (the result) | +0.187% | 0.49 | +0.354% | 0.93 |
| Perfect foresight (cheats, ranks by next month's realised return) | +8.332% | 29.41 | +8.867% | 31.54 |
| Inverse momentum | -0.330% | -1.00 | -0.168% | -0.51 |

The positive control finds a very large edge, so the harness detects edges when they exist. Inverse
momentum is correctly negative, and more negative than momentum is positive, which is the sign
consistency a real-but-weak signal would produce and a sign error would not.

The gross figure of +0.354%/mo also reconciles with the cost model: turnover of 2.6 of 10 names at a
0.712% round trip is 2.6/10 x 0.712% = 0.185%/mo of drag, so +0.187% net implies about +0.372%
gross if the baseline traded free. The measured +0.354% is 0.018 below that, which is the
baseline's own small turnover cost. The two agree.

### Permutation test

2,000 random rankings through the same pipeline, GROSS of costs:

    random spread  mean +0.010%  sd 0.248%   5th -0.416%  95th +0.414%
    random t       5th -1.68  50th +0.01  95th +1.66   |t|>2 in 5.3% of draws
    momentum gross +0.354% beats 91.5% of random   one-sided p = 0.085

Momentum sits at the 91.5th percentile of chance, p = 0.085 one-sided. That is the same verdict as
the t: leaning positive, short of conventional significance, and nowhere near the pre-registered
bar.

A methodological note, because the first version of this test was wrong in an instructive way. It
charged costs, and returned a spurious p = 0.002. A random ranking replaces roughly 8 of 10 names a
month against momentum's 2.6, so charging costs to both makes the comparison mostly a measure of
turnover rather than of skill: momentum "wins" because it trades less, which is not the question.
The test must be run gross, and is.

### Information coefficient

The Spearman rank correlation between the 12-1 score and the next month's return, computed per
month across all eligible names, 56 months and 2,600 name-months:

    mean IC +0.0306   se 0.0324   t +0.94   95% CI [-0.0330, +0.0942]
    months with a positive IC: 31 of 56

An IC of 0.031 is small but not unusual for a genuine equity signal; published ones commonly run
0.02 to 0.05. The interval comfortably contains zero and also contains a signal a real fund would be
pleased with. This is the most direct measurement available of whether the ranking carries
information, and it says: probably a little, cannot prove it.

### Power

The spread's standard error is 0.382%/mo (0.187 / 0.49). Everything follows from that:

| True edge | Months to reach t = 2 | Years |
|---|---|---|
| +0.30%/mo | 363 | 30 |
| +0.50%/mo | 131 | 11 |
| +1.00%/mo | 33 | 3 |

To clear t >= 2 on 56 months, the spread would have to be +0.764%/mo, which compounds to about
9.6%/yr of outperformance over an equal-weight basket of the same names. That is not a realistic
momentum premium; it is an extraordinary one. The pre-registered bar, on this dataset, could only
ever have been cleared by an effect several times larger than the literature would lead anyone to
expect. The bar was still correct to set — it is the honest bar — but it is worth being explicit
that failing it here carries much less information than failing it on a well-powered test would.

Taken together: net spread +0.187%, gross +0.354%, top quintile clearly best, top-minus-bottom
+0.522%, IC +0.0306, permutation p = 0.085, inverse control correctly negative. Six measures, all
leaning the same way, none significant. Consistently weakly positive, uniformly underpowered, and
plausibly explained in full by the survivorship bias described in section 8.

## 6. What the hold-window defect teaches

The first version of these results was wrong, and the way it was wrong is worth more than the
numbers it produced.

**Defect A, the hold window.** `eligible` excluded a symbol when a corporate action fell in
`[m-13, m]` — the lookback and the rank date. But the month actually HELD runs strictly after m, so
an unadjusted split during the hold was never excluded and priced straight into the basket as a real
return of -50% to -91%. It happened 14 times in 56 months, 4 of them inside the top-10 portfolio.
The guard now covers `[m-13, exit]`.

**Defect B, the inherited mask.** Both phases carried the daily screen's 200-trading-day forward
mask, which the spec for this screen does not ask for: this screen uses the window rule only, since
a monthly hold either contains a corporate action or it does not, and there is nothing for a
200-day carry to protect. Removing it took masked symbol-dates from 2,614 to 16, and is why the
eligible count rose from 45.5 to 46.4 names a month.

The effect was not marginal. Both headline figures changed SIGN: the spread went from -0.093%/mo to
+0.187%/mo, and top-minus-bottom from -0.088%/mo to +0.522%/mo. The quintiles inverted their
headline claim, from "the bottom group has the highest mean" to "the top group is clearly highest".
An earlier draft of this note reasoned at length about why a non-monotonic ordering with losers on
top was strong evidence against momentum. That reasoning was sound and the input was corrupt.

Three lessons, in descending order of how much they should change future practice.

**A plausibility check only works if a failing answer is allowed to stop the run.** The plan's own
Task 6 checklist asked whether the cumulative returns were consistent with an index that roughly
doubled over the window. The contaminated baseline returned +81.7%, and that was accepted as "in
the right region". It was not in the right region: the corrected baseline is +129.0%, and +81.7%
against a doubling index was the defect announcing itself in the one place the plan had thought to
look. The check was correctly specified and then waved through. A check with no defined failing
range, and no obligation to halt, is documentation rather than verification.

**A positive control validates the harness, never the data.** The controls in section 5 were also
run on the contaminated version, and they passed: perfect foresight found a huge edge, inverse
momentum was correctly signed, and both were taken as evidence the screen was sound. They could not
have detected this bug, because they ran through the same corrupted forward returns. Every ranking
function, honest or cheating, saw the same poisoned -91% months. Controls establish that the
plumbing carries signal; they say nothing about what was poured in. A data-quality check is a
different instrument and has to be run separately — which, for this repository, means checking
extreme returns against a corporate-action list rather than checking that a cheat wins.

**Reused machinery brings its assumptions with it.** The 200-day mask carry was inherited from the
daily screen because the code was there and worked, without anyone asking whether a rule designed
for an indicator with a 200-day warm-up suited a screen whose holding period is one month. It did
not, and it silently discarded most of the eligible universe. Borrowing an implementation means
borrowing its assumptions, and those need re-derived against the new context rather than assumed to
travel.

Both defects and the corrected results are in `3e1958c`, which also closed a lookahead gap in the
tests. Two earlier commits (`9eefe82`, `ef3dfa3`) wrote up the contaminated figures and remain in
history; this note supersedes them.

## 7. Groww's synthetic 2025 `open`, and the corporate-action mask

A separate, pre-existing data defect, which shaped the design rather than corrupting it.

In Groww's daily candles, 98.6% of 2025 bars carry an `open` exactly equal to the previous bar's
close, against 4-7% in 2020-2024, which is the normal rate for a stock genuinely opening unchanged.
The 2025 `open` field is synthetic. Any fill at a next-day open over 2025 is really a fill at the
prior close, so a screen entering at opens would report fictional fills over more than half this
window. This screen sidesteps that entirely by using closes only.

Consequences elsewhere:

- The daily screen's IN-SAMPLE window (2020-2023) is unaffected. Opens are real there and no
  inside-the-bar corporate action falls in it. Its committed results stand.
- The daily screen's unspent HOLDOUT window (2024-01-01..2025-11-21) IS affected, because it enters
  at the next day's open. It must not be run until the fill rule is fixed.
- 15-minute and 5-minute data are clean; intraday work is unaffected.

The same defect hides corporate actions. `gap_mask` detected an unadjusted split by comparing the
open to the previous close, which only works where the open is real. Five corporate actions show
their split INSIDE a bar rather than as an overnight gap, and all five are in 2025, where the open
never moves: SHRIRAMFIN 2025-01-10, BAJFINANCE 2025-06-16, NESTLEIND 2025-08-08, HDFCBANK
2025-08-26, TATAMOTORS 2025-10-14. `gap_mask` now carries a second detector for that shape — a close
more than 35% from the open, conditioned on the open being exactly the previous close. The
condition is deliberate: the inside-the-bar shape exists only because the open is fake, and
conditioning on it widens the margin the threshold has to separate.

## 8. Limits

**Survivorship bias, first and largest, and large enough to account for the entire result.**
`universe.yaml` is today's index list. Stocks added during the window because they rose are present
with the full history of that rise, and stocks dropped after falling are absent entirely. The
direction is not neutral: this bias runs IN MOMENTUM'S FAVOUR. The names that would have ranked top
and then collapsed out of the index are the ones missing, and the names that rose into the index
arrive with their rise already sitting in the lookback. At a measured spread of +0.187%/mo, the bias
could plausibly account for all of it, which is why this note does not treat a positive spread as
weak evidence for momentum. `load_universe` (`src/tradebot/data/universe.py`) takes an `as_of`
parameter that deliberately raises `NotImplementedError` rather than silently returning today's
list; that is the hook a point-in-time fix would use, and it is the single highest-value correction
available to this screen.

Other limits:

- 56 monthly observations is few, and the power table in section 5 is the consequence: a standard
  error of 0.382%/mo means nothing below +0.764%/mo could have been resolved.
- The power ceiling is a property of this DATASET, not of momentum. 50 names over 56 monthly
  observations gives a spread standard error near 0.4%/mo for ANY monthly cross-sectional screen on
  this universe.
- Three symbols have short or truncated histories in the stored data: SHRIRAMFIN 2022-12-20 to
  2025-11-21 (merger), TATACONSUM 2020-02-27 to 2025-11-21 (rename), TATAMOTORS 2020-01-01 to
  2025-10-23 (demerger). They are ineligible where the lookback or the hold cannot be priced, which
  is part of why the eligible count averages 46.4 rather than 50.
- 2021-02 to 2025-09 was one long bull market plus two corrections. A single regime, and momentum is
  known in the literature to crash hard at sharp reversals, which this window does not really test.
- Charge rates are typed from the spec and have not been verified against Groww's pricing page. No
  net figure in this repository has been.
- No market impact and no bid-ask beyond the slippage assumption in the schedule. A monthly
  rebalance of 2.6 names in liquid large caps is the benign case, but it is still an assumption.
- Lot sizes are ignored; positions are treated as perfectly divisible.

## 9. Conclusion

Supported: the construction — the 12-1 score, the one-day gap between rank and fill, the
`[m-13, exit]` eligibility guard, the turnover-only cost model at this portfolio's own position
size, and the eligible-set baseline — measures what the spec said it would, now that the hold-window
defect is closed. The controls, permutation test, IC and power analysis in section 5 were all re-run
against the corrected code.

Supported, and negative on the decision: cross-sectional 12-1 momentum over these 50 names,
2021-2025, does not clear its pre-registered bar. Spread > 0 is met at +0.187%/mo; t is 0.49 against
a required 2. Momentum does not pass, and the bar is not being relaxed after the fact.

Not a clean null, and the note does not claim one. Six measures lean positive and none is
significant: net spread +0.187%, gross +0.354%, top quintile clearly best, top-minus-bottom
+0.522%, IC +0.0306, permutation p = 0.085, and a correctly negative inverse control. The result is
consistently weakly positive, uniformly underpowered, and plausibly explained in full by
survivorship bias. The correct statement is that this dataset cannot resolve whether the effect is
real, and that anything small enough to hide in it is too small to trade here: at 0.185%/mo of
turnover cost, even a +0.50%/mo gross edge nets +0.315%/mo and would take 11 years of monthly
observations to confirm.

Running tally: nine families tested, none clearing its bar. That is not the same as nine families
showing nothing — this one showed a weak, unconfirmed signal, and it is the first to do so.

What to do next:

1. **Point-in-time constituents, via the `as_of` hook.** This is now the highest-value work on this
   screen. The one plausible explanation for the entire +0.187%/mo is a bias whose direction is
   known and whose fix is already scaffolded. Until it is done, no positive result from this
   universe can be taken at face value.
2. **The delivery-percentage screen**, specced at
   `docs/superpowers/specs/2026-09-20-delivery-signal-screen-design.md`, faces the same wall unless
   its effect is large, because the ceiling belongs to the dataset. It should be tested at the
   name-month level — an IC or a pooled excess return, roughly 2,600 observations — rather than as
   a 10-name monthly portfolio, which gives 56. Same data, same signal, roughly an order of
   magnitude more power. That change should be made to the spec before the screen is built.
3. **Non-price sources the project has no data for at all**: post-earnings-announcement drift,
   index-inclusion flows, bulk and block deals. Each needs a data acquisition step first.

The broader reading after nine families: the binding constraint has shifted. For the first eight it
was the signal — rules built on a stock's own price path, tested against costs of about 0.34R, with
nothing there to find. Here the rule may well have something in it, and the constraint is that 50
names over 56 months cannot tell, while survivorship bias sits on the scale in the signal's favour.
The next meaningful gain is therefore more likely to come from widening the universe, lengthening
the history, fixing the constituent list, or testing at the name-month level than from proposing a
tenth strategy family.
