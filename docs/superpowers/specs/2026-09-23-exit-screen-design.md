# An exit screen for trend_dip: design

Date: 2026-09-23. Status: proposed. **Pre-registration: this document fixes the grid, the metric
and the pass bar before the screen is written or run.**

## 1. The gap this closes

`scripts/swing_screen.py` measured **entry quality with a pure horizon exit and no stop**: enter on
the signal, hold h days, exit at the close. `trend_dip` cleared its bar that way (+0.618%/date at
h=60, t 2.96 against a Bonferroni 2.64).

`config-daily.yaml` then trades something else. It adds a 2×ATR(20) stop that no screen has ever
measured, and in the 2020-2023 backtest that stop produced **70 of 112 exits — 62%**. The majority
of the traded system's behaviour comes from a component that was never tested.

That is the gap: **the thing being traded is not the thing that was validated.**

## 2. What is fixed, and what varies

**Fixed, and not to be tuned by this screen:** the entry. `trend_dip` with SMA(20)/SMA(50)/SMA(200)
— fires when SMA50 > SMA200, close < SMA20 and close > SMA50. These parameters were pre-registered
before the entry screen ran and re-opening them here would be a second look at the same data.

**Varies:** the exit, and only the exit. Six cells, fixed now:

| Cell | Exit rule |
|---|---|
| `hold60` | Close of the 60th bar after entry. **The incumbent** — the design the entry screen validated. |
| `stop2atr` | 2×ATR(20) below entry, else `hold60`. **What is traded today.** |
| `stop3atr` | 3×ATR(20) below entry, else `hold60`. |
| `stop4atr` | 4×ATR(20) below entry, else `hold60`. |
| `trail3atr` | 3×ATR(20) below the highest close since entry, else `hold60`. |
| `target2r_stop2atr` | Target at 2× initial risk, stop 2×ATR, else `hold60`. |

ATR(20) is measured on the entry bar and held constant for stop placement, matching how
`trend_dip` sets `stop_price` today. A stop and a target hit on the same bar resolve as a stop,
matching `STOP_FIRST_ON_SAME_BAR` in `execution/backtest.py`, so the screen and the engine cannot
disagree about an ambiguous bar.

## 3. Disclosure: one cell has already been seen

**`hold60` versus `stop2atr` is confirmatory, not exploratory.** Before this spec was written, a
diagnostic over the 112 closed trades of the `daily-perf-groww` backtest compared them: mean
+3.97% per trade held to 60 bars against +1.30% as traded, with 66% of stopped trades ending better
unstopped. That is why this screen exists at all.

So the screen is not discovering that comparison; it is re-measuring it properly, at name-day
resolution, on the point-in-time universe, with a date-clustered t. Reporting it as a fresh
discovery would be dishonest, and a reader should discount it accordingly.

**The genuinely untested cells are `stop3atr`, `stop4atr`, `trail3atr` and `target2r_stop2atr`.**
Nothing is known about any of them.

## 4. Unit, metric and baseline

**Unit: the name-day**, as in the entry screen. Every (symbol, date) the entry fires on yields one
observation per cell. That gives on the order of 13,000 observations rather than the few hundred a
portfolio construction would give, and the momentum screen established that this resolution is what
decides whether a small effect is visible.

**Metric: net return per trade**, costs charged, at the `legacy` schedule — the same schedule both
existing screens pin to, so these figures sit alongside theirs.

**Baseline: `hold60`, paired by entry date.** Every cell sees identical entries, so the comparison
is a paired difference on the same dates, not two independent means. This matters more than it
sounds: the pairing removes essentially all of the market variance that would otherwise swamp an
exit effect, and an unpaired test on this sample size would have very little power.

t is computed **across entry dates, not observations**. Setups cluster — one market-wide dip fires
`trend_dip` on forty names the same morning — so observations are not independent and dates are the
unit. Both existing screens were bitten by this and both now do it this way.

## 5. The pass bar, fixed before running

A challenger passes if, against `hold60`, on in-sample data:

1. its mean **paired per-date difference is positive**, and
2. the date-clustered **t on that difference is ≥ 2.58**.

2.58 is Bonferroni for five challengers at α=0.05. The uncorrected t is printed beside it.

**If nothing clears the bar, `hold60` stands.** That is not a null result: `hold60` is the design
the entry screen validated and it is *not* what is currently traded, so "nothing beat the
incumbent" is already an instruction to change what the engine does.

**The bar will not be relaxed after the numbers are seen.** Every screen in this repository has
been judged against a bar fixed in advance, including the three that failed and the two that
nearly passed.

## 6. Windows

- **In-sample: 2020-01-01 to 2023-12-31.** Everything in this spec happens here.
- **Holdout: 2024-01-01 onward. RESERVED, UNSPENT, AND NOT TOUCHED BY THIS WORK.**

The screen ships a `guard_holdout` matching `swing_screen.py`'s: the holdout phase refuses to run
without an explicit flag, and the flag is not to be passed as part of this work. This was a
deliberate decision, taken before the grid was written: the holdout is the only data in this
project that has never influenced a decision, and spending it on an exit design that has not yet
been narrowed would waste it.

## 7. The problem a winner creates, which this screen does not solve

**If `hold60` or any wide-stop cell wins, position sizing breaks.** `risk/engine.py` sizes a
position as planned risk divided by stop distance. With no stop there is no distance to divide by,
and with a 4×ATR stop the distance roughly doubles, halving every position at the same risk budget
— which changes the portfolio's return profile in ways a per-trade screen does not measure at all.

This screen measures **whether an exit rule is better per trade**. It does not measure what a
portfolio holding those trades concurrently would have made, and the two can disagree. Acting on a
winner therefore needs a sizing rule first, and that is separate work. Saying so here is the point:
the last time this project traded something a screen had not measured, it was the 2×ATR stop, and
that is the defect this spec exists to fix.

## 8. Scope

**This system places no orders and nothing here changes that.** The screen reads
`data/tradebot.db` read-only and writes nothing.

Out of scope: entry parameters, any change to `config-daily.yaml` or the engine, position sizing,
portfolio simulation, the holdout, and any exit rule outside the six cells in §2.

## 9. Testing

- Each exit rule as a pure function over a hand-built bar series: a stop that triggers, one that
  does not, a gap through the stop clamping to the open, a trail that ratchets and does not
  un-ratchet, a target and a stop on the same bar resolving as a stop.
- `hold60` on a series with no adverse move equals the entry screen's `forward_return` at h=60.
  If those two ever disagree, the screen is not measuring the design that was validated.
- The pairing is real: every cell must report the same observation count on the same dates.
- Corporate-action masking spans the whole hold, as `hold_is_clean` already does — an exit rule
  that resolves early must still reject a hold that spans an unadjusted split, because the entry
  was chosen on prices the split makes fiction.
- `guard_holdout` refuses without the flag.
