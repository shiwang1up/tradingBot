# Expectancy Reporting, Consistent Risk and a Daily Systems Screen — Design Spec

Date: 2026-09-20. Status: design approved in conversation; rules below are fixed BEFORE any daily
data is pulled. Branch `expectancy-daily-screen`, stacked on `charges-orb-regime`.

## Why

Five intraday strategy/filter combinations have lost after real costs on three months of data. The
framework the user wants applied (a Rayner Teo video: expectancy, the win-rate versus reward-to-risk
trade-off, fixed-fractional position sizing, the law of large numbers, and his own daily mean-reversion
and trend-following systems tested over decades) points at three gaps:

1. Reports do not show expectancy in its own terms (average win, average loss, payoff, breakeven win
   rate), nor how much evidence a result rests on.
2. Sizing is `min(risk-based, margin-based)`: when margin runs short a trade goes ahead risking a few
   rupees where 1,000 was planned (490 of 884 trades in `real-1`). Every trade should risk about the
   same amount or not be taken.
3. Nothing here has been tested on a large sample. Groww serves daily candles from about 2020-01 to
   2025-11-21 (probed 2026-09-20; nothing earlier than mid-2019, nothing later than 2025-11-21), about
   1,450 trading days for each of 49 stocks.

Not in scope: the twelve intraday scoring strategies of a third-party GitHub bot (the same family as
`confluence`, which lost), portfolio or capital simulation for the daily systems, engine support for
multi-day (CNC) holding, any change to the intraday strategies, shorts in the daily screen (cash
shorts cannot be held overnight).

## Part 1: expectancy block in reports

`report/summary.py`, on net-of-charges PnL of closed, non-adopted trades (the same trades as today):

- `avg_win`: mean net PnL of winning trades (net > 0); `avg_loss`: mean net PnL of the rest (a
  scratch counts as a loss, as today), reported as a negative number; both 0.0 when empty.
- `payoff = avg_win / |avg_loss|` (0.0 when there is no loss or no win).
- `expectancy = total net / trades`, printed with its decomposition
  `win_rate x avg_win - loss_rate x |avg_loss|` (the two are equal by identity; a test pins it).
- `breakeven_win_rate = 1 / (1 + payoff)` when payoff > 0.
- Evidence, from the run's `daily_pnl.realised` rows (filtered like the trades when `since_ts` is
  given): number of days, mean per day, standard error, `t = mean / (sd / sqrt(n))`; computed across
  DAYS because same-day trades are correlated. `sd` is the sample standard deviation; with fewer than
  2 days or zero variance t is reported as n/a.
- A run with fewer than 30 trades or fewer than 20 days prints `too few to judge` on the evidence
  line; otherwise `|t| < 2` prints `not distinguishable from zero`.

Output, after the `R on risk` line:

    Avg win / avg loss    +312.40 / -231.10   payoff 1.35
    Expectancy            -146.15 per trade = 15.6% x 312.40 - 84.4% x 231.10
    Breakeven win rate    42.5% at this payoff (actual 15.6%)
    Evidence              62 days, mean -2,084 per day, t -9.1

For old runs whose daily rows are gross (charges estimated after the fact) the evidence line is
computed from per-day sums of the trades' NET PnL by IST close date instead, so it agrees with the
headline; the summary already knows which case it is in (`charges_estimated_trades`). To keep one
code path, the evidence line is ALWAYS computed from the trades' net PnL grouped by IST close date.

`report/compare.py` adds rows `Payoff`, `Expectancy` and `t (days)` to the side-by-side table.

## Part 2: consistent-risk rule

`RiskConfig.min_risk_fraction: float = 0.5` (0 disables; validated `0 <= x <= 1`). In
`risk/engine.evaluate`, after the quantity is computed and the existing `insufficient_size` check:

    planned = state.capital * cfg.per_trade_pct / 100
    actual  = qty * |signal.entry_price - signal.stop_price|
    if cfg.min_risk_fraction > 0 and actual < cfg.min_risk_fraction * planned: Rejection "risk_too_small"

It sits after `insufficient_size` so a zero quantity keeps its existing reason. The tests' base
config sets `min_risk_fraction: 0.0` (existing expectations and the golden-trades fixture stay); the
three shipped configs set `0.5`; a config file without the key gets 0.5, and the README says results
from an existing config change after upgrading, as it does for charges.

Measurement (not tuning): re-run `ema_rsi` and `confluence` on 5-minute bars over the same window as
`real-1` / `real-conf-2` with the rule on, AI stub, run ids `cr-ema-rsi` and `cr-confluence`, and
report trades, net, R on risk, the new expectancy block and the `risk_too_small` count beside the
originals. No parameter is changed in response.

## Part 3: daily data

`scripts/daily_screen.py fetch`: for each universe symbol, `fetch_incremental` at interval 1440 with
NO session filter (daily bars are stamped 00:00 IST and would all be dropped), from 2020-01-01, into
the existing `candles` table. About 28 requests per symbol. A symbol whose fetch fails or returns
nothing is reported and skipped. The engine, `fetch-data` and `SessionClock` are not touched.

Adjustment check (it is not known whether Groww's daily candles are adjusted for splits and bonuses):
every overnight move with `|open_t / close_{t-1} - 1| > 0.20` is listed. For such a symbol, all dates
from the gap to 200 trading days after it are masked: no entry on a masked date, and a trade open
when a masked date arrives is excluded from the statistics. The list and the count of excluded trades
are printed.

## Part 4: three daily systems, fixed in advance

Long only, one open trade per system per symbol, signals while in a trade ignored. A signal is
evaluated on day t's close; the entry is day t+1's OPEN; an exit condition met on day u's close exits
at day u+1's OPEN. Indicators use closes up to and including the evaluation day. `SMA(n)` is the
simple mean of the last n closes; `RSI(2)` and `ATR(20)` use Wilder smoothing; a signal needs every
indicator it uses to be warm.

| System | Entry (on day t's close) | Exit (on day u's close, u > entry day) |
|---|---|---|
| `mr` mean reversion | close > SMA(200) and RSI(2) < 10 | close > SMA(5), or 10 trading days in the trade |
| `tf` trend following | SMA(50) crossed above SMA(200) today (yesterday SMA50 <= SMA200, today >) and close > SMA(200) | close < (highest high since the entry day, inclusive) - 3 x ATR(20) of day u |
| `bo` breakout | close > max(close of the previous 100 trading days) and close > SMA(200) | close < min(close of the previous 50 trading days) |

These are the standard published forms of the three families in the video's book (mean reversion,
trend following, breakout). They are NOT the author's exact rules, which are not public in the
summary the user supplied; the output says so. One parameter set each; no grid; no second look.

Windows: in-sample 2020-01-01..2023-12-31 (the first ~200 trading days are warm-up); holdout
2024-01-01..2025-11-21. A trade belongs to the window of its entry date and is force-exited at the
window's last available open if still open (`WINDOW_END`). Indicators in the holdout are warmed with
earlier data. The `holdout` phase writes `docs/superpowers/notes/daily-screen-holdout.txt` and
refuses to run if that file exists.

Costs per round trip, on an assumed position value of 25,000 rupees at entry, Groww delivery
schedule (to be checked against Groww's pricing page): brokerage `min(max(0.1% x value, 5), 20)` per
order; STT 0.1% on both sides; exchange transaction 0.00297% and SEBI 0.0001% on both sides; stamp
duty 0.015% on the buy; GST 18% on brokerage + exchange + SEBI; a depository charge of 15.34 rupees
on the sell; plus 0.05% slippage against the trade on each open. The total, about 0.6% of position
value, is printed. Rates live in one constants block at the top of the script.

Per system and window the script prints: trades, win rate, average win %, average loss %, payoff,
expectancy % per trade (net), breakeven win rate, median holding days, exit reasons, and:

- Baseline: for a trade on symbol s held h trading days, the mean over every date e in the same
  window (with e+h inside it) of `open[e+h] / open[e] - 1` for s, net of the same costs. Excess =
  trade net return - its baseline. This removes what a rising market and that stock's own drift
  would have paid for simply being long that many days.
- `t` of the excess, across ENTRY DATES: trades entered on one date are averaged first.

Pass bar: in-sample excess expectancy > 0 with t >= 2 and at least 200 trades, AND holdout excess
expectancy > 0. A pass makes engine support for delivery trades the next spec. No pass is recorded
as such. Caveats printed with every table: today's constituent list is survivorship-biased over six
years; trade-level expectancy ignores capital limits and overlapping positions; one parameter set.

## Testing

Test-first, Python 3.9. `tests/test_report.py`: the expectancy identity, payoff and breakeven, the
evidence line (known daily series with a hand-computed t), the too-few and n/a cases, old-run path.
`tests/test_risk.py`: `risk_too_small` at, above and below the fraction, disabled at 0, ordering
after `insufficient_size`. `tests/test_config.py`: default 0.5, validation, shipped configs.
`tests/test_daily_screen.py` (script imported by path): each system on hand-built daily series
(entry next open, exit next open, one trade at a time, time stop, trailing stop, window-end exit),
the cost function against a hand-worked trade, the baseline on a constant-drift series (excess 0),
t across dates with clustered entries, the gap mask, the holdout file guard.

## Deliverables

Code and tests; `scripts/daily_screen.py` (`fetch`, `insample`, `holdout`); the two `cr-*` runs;
`docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`; README sections.
