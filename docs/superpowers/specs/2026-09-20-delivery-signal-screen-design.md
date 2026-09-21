# Delivery-Data Signal Screen — Design Spec

Date: 2026-09-20. Status: approved in conversation. Branch `dev-nonprice-signals`, off `main` at
`513fed5`. EXPERIMENTAL: nothing here is promoted to `main` unless a signal passes the bar below.

## Why

Eight strategy families have now been tested on this bot — five intraday (ema_rsi, confluence,
15-minute pullback, opening-range breakout, and the Claude filter over them) and three daily (RSI-2
mean reversion, 50/200 trend following, 100-day breakout). Every one is a function of the recent
price path, and none has an edge after real costs; the daily three also fail to beat holding the
same stock for the same number of days. The measured decomposition of the intraday runs is that the
signal is worth about +/-0.1R per trade while costs take about 0.34R, so the loss is structural.

The untested question is whether a NON-price observable predicts anything here. NSE publishes, per
symbol per trading day, the share of traded volume actually taken to delivery, the delivery
quantity, and the number of trades. None is derived from the price path. A probe on 2026-09-20
confirmed: the daily file is reachable for 2020-01-02 through 2025-12-04, all 50 universe symbols
join exactly on the EQ series, and DELIV_PER spans 18.8% to 78.3% across the universe on a single
day, so there is variation to test.

Out of scope, and each needing a different source that does not yet exist here: earnings surprises
and post-earnings drift, index inclusion and exclusion flows, bulk and block deals, and any
fundamental data. Groww's API serves candles, quotes and option chains only.

## Part 1: the data

New table, separate from `candles` (which is price data keyed by interval; this is not):

    CREATE TABLE IF NOT EXISTS delivery (
      symbol        TEXT NOT NULL,
      date          TEXT NOT NULL,          -- ISO date, IST trading day
      ttl_trd_qnty  INTEGER NOT NULL,
      turnover_lacs REAL NOT NULL,
      no_of_trades  INTEGER NOT NULL,
      deliv_qty     INTEGER NOT NULL,
      deliv_per     REAL NOT NULL,
      PRIMARY KEY (symbol, date)
    );

`scripts/delivery_fetch.py` downloads
`https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv` for every
weekday from 2020-01-01 to 2025-12-05, keeps rows whose SERIES is `EQ` and whose SYMBOL is in
`universe.yaml`, and inserts with `INSERT OR IGNORE` so a re-run resumes rather than duplicating.
The request needs a browser User-Agent and a `https://www.nseindia.com/` Referer. A 404 (market
holiday) or a malformed file is counted and skipped, never fatal. Rate: one request per 0.7s.
The header has leading spaces in its field names; strip both names and values.

Coverage report at the end: days attempted, days fetched, days missing (with the first ten dates),
and rows per symbol with the minimum and maximum flagged.

**Mandatory validation, a separate `verify` phase.** `delivery.ttl_trd_qnty` and the `v` column of
`candles` at interval 1440 come from independent sources for the same symbol-day and must agree.
Report the count of matched symbol-days, the fraction where the two are within 1%, and the ten
largest disagreements. If under 95% of matched days agree within 1%, the join is wrong: STOP and
report rather than screening on it. (Corporate-action days may legitimately disagree; they are
reported separately using the existing gap dates.)

## Part 2: a shared screening core

`scripts/daily_screen.py` already holds the machinery that scores a signal honestly: `simulate`,
`baseline_return`, `attach_excess`, `t_across_dates`, `date_means`, `describe`, `format_system`,
`round_trip_cost`, `gap_mask`, `load_series`, the indicator helpers and `Bar`/`Trade`. Move that
core verbatim into `scripts/screenlib.py`; `daily_screen.py` and the new `delivery_screen.py` both
import from it.

The refactor must be provably behaviour-preserving: the full suite stays green, and
`python scripts/daily_screen.py insample` must produce output IDENTICAL to the table committed in
`docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`. Any difference means
the refactor is wrong, not the note.

## Part 3: five signals, fixed before any delivery row is stored

Long only (a cash short cannot be held overnight). A signal on day t's close enters at day t+1's
OPEN and exits at the OPEN after a fixed hold of H trading days. One open trade per signal per
symbol; a signal while a trade is open is ignored. A day with no delivery row for that symbol
produces no signal. Indicator windows use delivery rows only, and a signal needs its full window.

Let `dp` be `deliv_per`, `dq` be `deliv_qty`, `vol` be `ttl_trd_qnty`, `ats` be `vol / no_of_trades`
(average trade size), and `ret` the close-to-close return from `candles`.

| Signal | Entry on day t | Thesis |
|---|---|---|
| `dspike` | `dp[t] > mean(dp[t-60..t-1]) + 2 * sd(dp[t-60..t-1])` and `ret[t] > 0` | institutional accumulation |
| `quiet`  | `dp[t] >= the 80th percentile of dp[t-60..t-1]` and `-1% <= ret[t] <= +1%` | buying without moving the price |
| `dqty`   | `dq[t] > 3 * mean(dq[t-20..t-1])` | real demand, not churn |
| `blocks` | `ats[t] >= the 90th percentile of ats[t-60..t-1]` | block or institutional activity |
| `churn`  | `vol[t] > 3 * mean(vol[t-20..t-1])` and `dp[t] <= the 20th percentile of dp[t-60..t-1]` | **CONTROL**: volume without delivery |

`churn` is the falsification check and is not a candidate strategy. If it scores as well as the
delivery signals, delivery is not what is doing the work and the idea is dead regardless of the
other four. Percentiles are the linear-interpolation kind (`statistics.quantiles` method="inclusive"
equivalent); windows exclude day t itself so no signal reads its own bar.

## Part 4: how it is judged

Horizons H = 5, 20, 60 and 120 trading days. The long end is deliberate: at a 0.572% delivery round
trip, cost is about 11 bps a month over 120 days, so if cost was the binding constraint on every
earlier attempt, the long columns are where that stops being true.

- **Primary grid**: 5 signals x 4 horizons = 20 cells, in-sample 2020-01-01..2023-12-31.
- **Multiplicity**: twenty tests at a plain t of 2 would throw up about one false positive every
  time the grid is run. The bar is therefore **t >= 3.1** (Bonferroni, twenty tests, 5% two-sided;
  the exact figure is 3.02, rounded up), AND excess over baseline > 0, AND at least 200 trades.
- **Thin cells are inconclusive, not failures.** One trade at a time per symbol means a 120-day
  hold locks a symbol for 120 days after each entry, so the ceiling is about 416 trades in-sample
  even if a signal fired every day. A cell with fewer than 200 trades is reported `inconclusive
  (n too small)`; a null from 60 trades says nothing and must not be read as a rejection.
- **Holdout** 2024-01-01..2025-11-21: run only for a cell that clears the primary bar, and only
  that cell. Written to `docs/superpowers/notes/delivery-screen-holdout.txt`, which the script
  refuses to overwrite.

**A second, descriptive reading, kept separate from the pass bar.** The long-hold hypothesis has a
softer test that does not depend on any cell being significant: does excess trend upward with the
horizon? If cost was the binding constraint, the 120-day column should look systematically better
than the 5-day column across all five signals. Report the per-horizon mean excess across signals
and say plainly whether such a trend exists. This is a description of the grid, never a pass, and
it must be labelled as such: picking the best of four horizons after seeing them is selection, and
the Bonferroni bar is what guards the actual decision.

**Note on the control at long horizons.** `churn` was designed to falsify at the short end, where
churn plausibly precedes a reversal. At 60 and 120 days it degenerates into a generic high-volume
signal and tests less. Keep it in every cell, and state in the results that its value as a control
is strongest at H = 5 and H = 20.

Scoring is the existing machinery: net of the ~0.572% delivery round trip, excess over holding the
same stock the same number of days, t across ENTRY DATES (delivery spikes cluster on market-wide
days), and the corporate-action gap mask applied to BOTH trades and the baseline.

## Part 5: what this can and cannot answer

It answers: does publicly available delivery data predict returns on these 50 symbols over this
period, beyond what holding them would have paid anyway? A null result rules out these five
formulations on these symbols in this period — not "delivery data" in general, and not the
untested sources listed as out of scope.

Limits carried into the results: today's constituent list is survivorship-biased over six years;
trade-level expectancy ignores capital, overlap and sizing; one parameter set per signal;
2020-2023 was one long bull market plus two corrections; the charge rates are still unverified
against Groww's pricing page; delivery percentage itself is a noisy proxy for conviction, since it
counts any position carried overnight.

## Testing

Test-first, Python 3.9. `tests/test_screenlib.py`: the moved core keeps its existing tests (move
them from `tests/test_daily_screen.py`, unchanged). `tests/test_delivery_fetch.py`: the NSE row
parser on a captured sample (field names with leading spaces, a `-` in a numeric column, a non-EQ
row, a symbol outside the universe); the idempotent insert; the coverage counter. 
`tests/test_delivery_screen.py`: each of the five signals on hand-built series, including that each
fires when it should and does not when one clause fails; the fixed-hold exit; that a day missing a
delivery row produces no signal; the percentile helper against hand-worked values.

## Deliverables

`scripts/screenlib.py`, `scripts/delivery_fetch.py` (`fetch`, `verify`), `scripts/delivery_screen.py`
(`insample`, `holdout`), their tests, the `delivery` table, and
`docs/superpowers/notes/2026-09-20-delivery-signal-screen-results.md`. No change to `main` unless a
signal passes.
