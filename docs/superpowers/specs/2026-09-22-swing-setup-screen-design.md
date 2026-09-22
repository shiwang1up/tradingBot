# Swing setup screen design

**Status:** drafted 2026-09-22, awaiting review
**Branch:** `dev-swing-screen` (to be created from `main`)
**Question:** of ~200 stocks on any given morning, can a technical setup identify 5-10 worth
holding for days to weeks — better than picking at random?

## 1. Why, and what would make this different from the last time

Intraday is being abandoned for a measured reason. On ₹1 lakh, `perf2-conf` paid ₹40,747 in
charges over three months — about **₹1,62,987 a year, 163% of capital**, before any question of
whether the signal works. A swing book of eight positions on 15-day holds rotating monthly costs
about **₹6,864 a year**, roughly 24x less. Cost stops being the dominant term.

The MFE/MAE diagnostic on ORB showed the rest: only 12.2% of trades ever moved 1R in our favour,
median MFE +0.34R against median MAE -0.59R, and every candidate fixed target still lost. The
intraday entries carry no information; no exit rule rescues that.

**Three daily swing systems were already tested and all failed**, in-sample 2020-2023:

| System | Trades | Raw expectancy | Excess vs baseline | t |
|---|---|---|---|---|
| RSI-2 mean reversion | 1,220 | -0.001% | -0.203% | -1.32 |
| 50/200 trend following | 100 | +0.868% | -1.348% | -1.80 |
| 100-day breakout | 229 | **+10.742%** | **-2.532%** | -2.15 |

That breakout row is the whole lesson. It booked +10.7% a trade and still failed, because holding
the same stock the same number of days did better. Between 2020 and 2025 the index doubled;
almost any rule that buys shows a profit. **Raw profit is not evidence.**

So why retest at all? Because that test was underpowered and biased:

| Then | Now |
|---|---|
| 50 names, today's index | 315 symbols, true point-in-time membership |
| Survivorship inflating every result | Measured and removed: 61 of 2020's 200 names are gone today |
| Trade-level, a few hundred observations | **Name-day level, ~400,000 observations** |
| Flat 5 bps slippage | Per-name, liquidity-tiered |

A bad test is not a settled answer. The resolution gain is the point: the momentum screen could
only have detected an edge above 0.76%/mo, which is larger than most real anomalies.

## 2. The unit of observation

**Name-day, not trade, and not portfolio.** For every (symbol, date) where a setup fires, record
the forward return over each horizon. This is the single most important design choice: the
momentum screen's portfolio construction gave 56 observations where its own information
coefficient had 2,600, and that is why it could not resolve anything.

No positions, no capital allocation, no engine. If a setup clears this, THEN build a portfolio
around it. Building the portfolio first is how the last three attempts wasted their power.

## 3. The setups, fixed before any result is seen

Six, chosen to span the standard families rather than to be exhaustive, plus a control.

| key | fires when |
|---|---|
| `pullback` | close > SMA(200) and RSI(2) < 10 |
| `breakout` | close is a new 100-day closing high and close > SMA(200) |
| `trend_dip` | SMA(50) > SMA(200), close < SMA(20), close > SMA(50) |
| `squeeze` | ATR(20)/close at a 100-day low, and close > the prior day's high |
| `gap_vol` | open > prior close x 1.01, volume > 2x its 20-day average, close > open |
| `three_down` | three consecutive lower closes, close > SMA(200) |
| `random` | **control.** Fires with the mean probability of the six above, seeded. |

The control is not decoration. On the momentum screen a random ranking produced +0.329%/mo
gross with t 1.31 — a larger apparent edge than the real signal — and that single number is what
made the result interpretable. Any setup that cannot beat `random` here is noise.

Adding a setup after seeing results invalidates the correction in section 6. If one is added, the
whole grid is rerun and the bar moves.

## 4. Horizons

2, 5, 10 and 20 trading days, exit at the close of the horizon's last day. No stops, no targets,
no discretion. This measures whether the SETUP predicts, uncorrupted by exit design — the ORB
diagnostic showed how badly exit questions can be confused with entry questions.

**The primary cell is horizon 10** (about two calendar weeks), fixed here in advance. The other
three are descriptive and carry no pass bar.

## 5. Two baselines, because they answer different questions

**Time-series baseline** — the mean return of holding that same symbol for the same number of
days, entered on every eligible date in the window. Answers: *is this a good moment to buy this
stock?* This is `baseline_return` in `scripts/daily_screen.py`, reused unchanged.

**Cross-sectional baseline** — the equal-weight mean forward return over the same horizon of every
eligible index member on that same date. Answers: *is this a better stock to buy today than the
others?* This is the decision-relevant one for the stated goal, since the choice being made is
among stocks on a morning, and it removes market-wide moves on that date automatically.

Both are reported for every cell. **Disagreement between them is a finding, not a problem**: a
setup that beats the time-series baseline but not the cross-sectional one is firing on days the
whole market rose, and would not have helped you choose.

Costs are charged to the signal and to both baselines alike, so they **cancel out of the excess**.
Excess therefore measures selection, not cost. The money figure is reported separately as net
expectancy per trade, and the two can disagree: in a rising market a setup can be profitable and
still worse than random. Both are printed; the pass bar is on excess.

## 6. The pass bar, fixed here

**Excess over the CROSS-SECTIONAL baseline > 0, with t >= 2.64, at horizon 10.**

t is computed **across entry dates, not across observations**. Setups cluster — one market-wide
dip fires `pullback` on forty symbols the same morning — so observations are not independent and
dates are the unit. The last screen was bitten by exactly this: mean reversion showed a positive
trade-weighted excess beside a negative date-weighted t.

2.64 is Bonferroni for six primary cells at α=0.05. The uncorrected t is printed beside it.

Reported but NOT part of the bar: the time-series excess, the other three horizons, net
expectancy, win rate, and the distribution of excess by year.

## 7. Windows

- **In-sample: 2020-01-01 to 2023-12-31.** All development and all decisions happen here.
- **Holdout: 2024-01-01 onward, RESERVED AND UNSPENT.** This extends the daily screen's existing
  reservation (2024-01-01..2025-11-21) to cover the newly fetched data through 2026-09-21.

No statistic over the holdout is computed until a setup passes in-sample. The script must refuse
to run the holdout phase unless explicitly invoked, and must record that it ran, so the window
cannot be spent twice or by accident.

## 8. Universe and data hygiene

- Membership comes from `load_universe(path, as_of=date)` at each date's own value — the whole
  point of the point-in-time work. A symbol is eligible on a date only if it was in the index then.
- `gap_mask` (both detectors, including the intrabar one for Groww's synthetic 2025 opens) excludes
  corporate-action windows. A masked date disqualifies both the signal and the baselines, the same
  inclusive check `daily_screen.simulate` already applies.
- A name-day needs a full indicator warm-up (200 bars for SMA200) or it is not eligible.
- Slippage per name from `liquidity.slippage_for`; a short history takes the conservative tier.
- Entry and exit at the CLOSE. Groww's daily `open` is synthetic for 2025 (98.6% of bars carry the
  previous close), so any fill at an open would be fictional over a large part of the window.

## 9. Testing

Unit, on synthetic bars:
- each setup fires exactly where it should and not adjacent to it, tested at the boundary value
- the control fires at the intended rate
- the cross-sectional baseline covers exactly the eligible set on that date
- a masked date disqualifies signal and both baselines alike
- t is computed across dates, not observations — a fixture where the two differ in sign
- insufficient warm-up makes a name-day ineligible

Integration:
- the holdout guard refuses without the explicit flag, and records having run
- no name-day is counted where the symbol was not an index member on that date

## 10. What this cannot answer

It measures selection, not a tradable system. Position sizing, how many of the 5-10 to hold,
correlation between simultaneous picks, capacity, and the fact that eight positions on 1 lakh is
12,500 each are all out of scope. A setup passing here is a reason to build a portfolio and test
it on the holdout, not a strategy.

It also cannot rule out that a setup works in a regime this window does not contain: 2020-2023 is
one crash and one long bull market.
