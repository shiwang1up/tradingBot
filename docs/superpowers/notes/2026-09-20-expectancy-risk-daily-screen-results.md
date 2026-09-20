# Expectancy in reports, a consistent-risk rule, and a daily systems screen: results

Date: 2026-09-20. Spec: `docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md`,
committed 2026-09-20, before any daily candle was fetched. Every figure below was read back with
`tradebot report --run <id>` or `scripts/daily_screen.py insample`, or with read-only SQL against
`data/tradebot.db` (`sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`). The holdout window
(2024-01-01..2025-11-21) was not touched by any of it.

## 1. What changed

- **Expectancy block in reports.** `report/summary.py` (`build_summary`, `format_summary`) prints
  average win/loss, payoff, expectancy with its decomposition, breakeven win rate, and an evidence
  line (days, mean, t). `report/compare.py` adds `Payoff`, `Expectancy` and `t (days)` rows to the
  side-by-side table.
- **`risk.min_risk_fraction`** (`src/tradebot/config.py`, default 0.5) rejects a trade whose actual
  risk (filled quantity times stop distance) has fallen below that fraction of the planned risk,
  instead of taking it undersized. Enforced in `risk/engine.evaluate` (`src/tradebot/risk/engine.py`),
  after the existing `insufficient_size` check, as rejection reason `risk_too_small`. The three shipped
  configs (`config.yaml`, `config-15m.yaml`, `config-orb.yaml`) set it to 0.5; the test base config
  keeps it at 0.0 so existing fixtures are unaffected.
- **The daily screen**, `scripts/daily_screen.py`, three phases (`fetch`, `insample`, `holdout`). It
  reads `data/tradebot.db` read-only and writes nothing except the one-shot holdout file. It screens
  three classic daily systems for expectancy in excess of a same-holding-period baseline.

## 2. Expectancy in reports

The four new lines, worked from `real-1`'s actual block:

    Avg win / avg loss    +505.15 / -266.64   payoff 1.89
    Expectancy            -146.15 per trade   15.6% x 505.15 won against 84.4% x 266.64 lost
    Breakeven win rate    34.5% at the payoff this run actually achieved (1.89); actual win rate 15.6%
    Evidence              62 days traded, mean -2,083.87 per day, t -8.6   consistently losing

- **Avg win / avg loss, payoff.** Mean net PnL of winning trades against the mean net PnL of the rest
  (a scratch counts as a loss); payoff is the ratio. `real-1`: a win averages 505, a loss averages
  -267, so a win pays 1.89 times what a loss costs.
- **Expectancy.** Net PnL per trade, shown as its own decomposition (win rate times avg win, minus
  loss rate times avg loss) for the shape of the argument, not as an identity that reconciles to the
  last rupee — both averages are rounded to 2dp for display first.
- **Breakeven win rate.** `1 / (1 + payoff)`. It answers: at the payoff this run actually achieved, what
  win rate would have broken even. For `real-1`, at a payoff of 1.89 the breakeven win rate is 34.5%;
  the run's actual win rate was 15.6%, well under it, which is why it lost money even though each win
  paid nearly twice each loss.
- **Evidence.** Net PnL per trade summed by IST close date, then a t-test of that per-day series
  against zero. It is computed across days, not trades, because trades opened the same day are
  correlated (one signal condition often fires across several symbols at once); folding them into one
  per-trade series would overstate the sample size and understate the variance. Below 30 trades or 20
  days it prints "too few to judge"; otherwise it grades `|t|` into wording tiers ("consistently
  losing/profitable" at `|t| >= 4`, "some evidence of a loss/edge" at `|t| >= 2`, "not distinguishable
  from zero" below that).

## 3. The consistent-risk rule

| Run | Trades | Net | R on risk | Payoff | Win rate | t (days) | `risk_too_small` |
|---|---|---|---|---|---|---|---|
| `real-1` | 884 | -129,200 | -0.61 | 1.89 | 15.6% | -8.6 | — |
| `cr-ema-rsi` | 197 | -79,121 | -0.49 | 1.09 | 28.4% | -5.1 | 3,169 |
| `real-conf-2` | 1,018 | -119,859 | -0.41 | 2.78 | 15.7% | -6.7 | — |
| `cr-confluence` | 332 | -74,673 | -0.27 | 1.12 | 36.4% | -4.0 | 6,404 |

(All eight figures re-read this run and match the spec's measurement figures exactly.)

The rule removed 3,169 `ema_rsi` signals and 6,404 `confluence` signals whose margin-shrunk size would
have risked under half the planned amount. `R on risk` improved in both pairs (-0.61 to -0.49, -0.41
to -0.27), not because the surviving strategy is better, but because the removed trades were worse per
rupee risked: a fixed brokerage floor charged against a few rupees of risk can exceed a full 1R of cost
on its own, so a scrap-sized loser looks far worse in R terms than a full-size one.

The payoff figures of the original runs (1.89, 2.78) were inflated by hundreds of tiny losses dragging
the average loss toward zero — a loss of a few rupees barely moves `avg_loss`, but the fixed cost floor
still applies to it, so those trades' losses are disproportionately made of cost rather than of the
trade going against the position. Once the rule removes them, the real payoff on properly sized trades
is about 1.1 (`cr-ema-rsi` 1.09, `cr-confluence` 1.12), and a payoff of 1.1 needs a roughly 48% win rate
to break even, against the actual 28.4% and 36.4%.

Rupee expectancy moved from -146 (`real-1`) to -402 (`cr-ema-rsi`), and it looks worse, but that move is
an artefact of sizing, not of the rule making things worse: the surviving trades are all full-size,
where before the average blended full-size and scrap-size trades. Rupee figures are not comparable
across runs with different sizing; `R on risk` is, and it improved.

The rule removes a measurement artefact. It is not an edge: every one of the four rows above is still
clearly losing.

## 4. The daily screen

Three systems, long only, one open trade per system per symbol, entry at the next day's open after a
signal on today's close:

- `mr` mean reversion: enter when close > SMA(200) and RSI(2) < 10; exit when close > SMA(5), or after
  10 trading days in the trade.
- `tf` trend following: enter the day SMA(50) crosses above SMA(200) with close > SMA(200); exit on a
  trailing stop, 3 x ATR(20) below the run's highest high since entry.
- `bo` breakout: enter on a new 100-trading-day closing high with close > SMA(200); exit when close
  falls below the 50-trading-day closing low.

In-sample window 2020-01-01..2023-12-31, 50 universe symbols, all with stored daily candles. Round-trip
cost, on an assumed 25,000-rupee position (Groww delivery schedule plus 0.05% slippage per side, rates
unverified against Groww's pricing page): 0.572% of position value.

`.venv/bin/python scripts/daily_screen.py insample`, verbatim:

| System | Trades | Win rate | Payoff | Median hold | Expectancy (net) | Excess/date | t (dates) | Excess/trade |
|---|---|---|---|---|---|---|---|---|
| `mr` mean reversion | 1,220 | 56.9% | 0.76 | 3d | -0.001% | -0.203% | -1.32 (442 dates) | +0.137% |
| `tf` trend following | 100 | 46.0% | 1.55 | 24d | +0.868% | -1.348% | -1.80 (84 dates) | -1.493% |
| `bo` breakout | 229 | 50.2% | 3.70 | 68d | +10.742% | -2.532% | -2.15 (180 dates) | -1.689% |

No system passed. The pass bar, fixed in the spec (`docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md`,
committed 2026-09-20) before the daily data existed (the fetch ran after that commit): in-sample excess
expectancy > 0 with t >= 2 and at least 200 trades, AND holdout excess expectancy > 0. All three systems
clear the trade-count floor, but every excess/date figure is negative and every t is well short of +2 —
in fact all three are negative. None clears the first gate, so none reaches the second.

The central finding: the raw per-trade expectancies are positive and, for breakout, large (+10.742% per
trade). Almost all of that is the market, not the rule. Against the baseline of simply holding the same
stock for the same number of trading days from the same entry date, all three systems come out negative
(excess/date -0.203%, -1.348%, -2.532%). A rule that buys into a six-year rise in Indian large caps
shows a positive return from the drift alone; only the excess over that drift is evidence the rule's
timing does anything, and here it does not.

Three corrections were made during this work. First, the screen originally printed a trade-weighted
excess beside a date-weighted t, which for mean reversion showed a positive mean (+0.137%) next to a
negative t (-1.32) — an apparent contradiction. Trades cluster on entry dates: one market-wide dip fires
mean reversion across many symbols on the same day, so a few busy dates can carry a trade-weighted
average that a date-weighted one does not. Dates, not trades, are the independent unit here, so the
date-weighted figure (-0.203%) is the one the t tests and the one the pass bar is judged on; the script
now prints both, labelled, so this cannot recur silently. Second: the trade and the baseline are charged
the same round-trip cost, so the cost cancels out of the excess figure — excess measures entry timing
against a random entry of the same holding length, nothing more. The `expectancy` line, separately, is
the money figure, net of costs.

Third, found by review, not by the screen: the first in-sample run applied the corporate-action gap
mask to trades (via `simulate`) but not to the baseline (`baseline_return` took no mask at all), so the
baseline's average included holds that spanned an unadjusted split — single-day moves of roughly -90%
and +95% — which pulled it too close to zero and made every system's excess look less negative than it
is. With the baseline masked the same way trades already were, every t moves more negative: mean
reversion -1.16 to -1.32, trend following -1.41 to -1.80, breakout -1.46 to -2.15. The conclusion does
not change — no system passed before the fix and none passes after it — and now rests on stronger
evidence, not weaker. Because this was a measurement bug the screen itself gave no sign of (the numbers
it printed looked plausible on their own), the excess figures above should be read alongside the caveat
list in section 5 rather than taken alone.

The holdout (2024-01-01..2025-11-21) was deliberately not run. No system cleared the in-sample gate, so
the holdout could only have confirmed a rejection that is already visible in-sample; running it would
spend the window for no additional information. It stays unspent for future work on this data.

## 5. Limits

- Survivorship bias: `universe.yaml` is today's constituent list, so six years of results are
  overstated by however much the index's membership has turned over.
- Trade-level expectancy only: no capital simulation, no overlap limit, no position sizing. These
  numbers say whether entry timing has an edge, not what an account holding these positions
  concurrently would have made.
- One parameter set per system, fixed in the spec before any data was pulled; no grid, no second look.
- Groww's daily history, as stored, spans 2020-01-01 to 2025-12-05: 50 symbols, 71,654 daily rows.
  Nearly every symbol's series runs through 2025-11-21; one row (HINDUNILVR) reaches 2025-12-05. It is
  NOT adjusted for corporate actions. 11 overnight moves exceed 20% across the full history, in 10
  symbols (POWERGRID has two): BAJAJFINSV (2022-09-13), BEL (2022-09-15), BPCL (2024-06-21), DRREDDY
  (2024-10-28), EICHERMOT (2020-08-24), NESTLEIND (2024-01-05), POWERGRID (2021-07-29 and 2023-09-12),
  RELIANCE (2024-10-28), TATASTEEL (2022-07-28), WIPRO (2024-12-03). This is why the gap mask exists:
  each masks its symbol for 200 trading days after the event, so it does not manufacture mean-reversion
  entries out of an unadjusted split or read as an outsized loss on a trade that spans it. That is 200
  trading days of that symbol excluded from that system's signals per event.
- Three symbols have short histories, verified against the stored data: SHRIRAMFIN starts 2022-12-20
  (merger), TATACONSUM starts 2020-02-27 (rename), TATAMOTORS stops 2025-10-23 (demerger).
- Costs assume a fixed 25,000-rupee position value at entry; the rates themselves are typed from the
  spec and have not been checked against Groww's pricing page.
- 2020-01 to 2023-12 (the in-sample window) was one long bull market plus two corrections (the
  2020 COVID crash recovery and the 2022 drawdown). A baseline built from holding the same stock for
  the same number of days is doing a lot of work in a rising market; a flatter or falling market could
  change which side of zero the excess falls on.
- The t values above are not corrected for having tried three systems at once; a look at three rules
  and reporting the least-bad would need a stricter bar than `t >= 2`.

## 6. Conclusion

Supported: the machinery — the expectancy block in reports, `min_risk_fraction` and its
`risk_too_small` rejection, and the daily screen's three systems, cost model, gap mask and baseline,
all read back and matching the spec's own worked figures exactly. Supported, and negative: the
consistent-risk rule is a measurement fix, not an edge — every one of the four risk-rule rows is still
clearly losing, with the real payoff on full-size trades around 1.1 against win rates of 28-36% and a
roughly 48% breakeven. None of the three daily systems clears the in-sample pass bar; all three are
negative against a same-holding-period baseline despite large positive raw expectancies that are mostly
market drift.

Not supported: any profitable configuration, intraday or daily.

Open threads, no recommendation beyond what the evidence says:

1. The daily holdout (2024-01-01..2025-11-21) is unspent, and so is the ORB holdout
   (2026-08-16..2026-09-15, from the prior note). Both stay clean because nothing cleared the gate that
   would justify spending them.
2. Whether to verify the Groww charge rates (intraday and delivery) against the pricing page; no net
   figure in this repository has been checked against it yet.
3. Every family tried so far — five intraday strategy/filter combinations and three daily systems — is
   a function of the recent price path (moving averages, RSI, ATR, breakout levels, all derived from
   price and volume history). A signal built on something other than price has not been tried.
