# Daily-cadence engine design

**Status:** drafted 2026-09-22, awaiting review
**Branch:** `dev-daily-engine` (from `main`)
**Question:** can the engine hold a multi-day CNC position on daily bars, so a swing signal can be
traded rather than only measured?

## 1. Why this is the blocking gap

`SessionClock` refuses every interval above 15 minutes:

```
interval    60: ValueError: square-off bar would open before no_new_entries_after
interval  1440: ValueError: interval 1440m does not fit between 09:15 and 15:10
```

It is built on one assumption: a run fits inside a single trading session and flattens before the
close. That assumption is load-bearing across the clock, the loop and the paper engine.

**The consequence is larger than an inconvenience.** Nine of the ten strategy families tested have
been intraday, and that was not a research choice — the engine could not express anything else.
Every daily investigation this project has done (`daily_screen.py`, `momentum_screen.py`,
`swing_screen.py`) is a standalone script for the same reason. Those scripts can measure whether a
signal predicts. None can trade one: no position sizing, no risk limits, no charge accounting, no
fill simulation, no paper path, no reporting.

`trend_dip` currently clears its pre-registered bar in-sample (+0.618%/date at h=60, t 2.96,
permutation p 0.0005). Whether or not it survives the holdout, **there is presently no way to run
it.** That is the gap.

Intraday has separately been abandoned on measured grounds: charges extrapolate to 163% of capital
a year, and only 12.2% of opening-range-breakout trades ever moved 1R in our favour. The engine is
therefore specialised for the one style the evidence rejects.

## 2. Scope

**In:** daily bars (`interval_minutes: 1440`) through the existing backtest path, with CNC
positions held across days, entries and exits at the daily close, real charges, and the existing
report.

**Out, deliberately:** paper and live trading on a daily cadence. That needs a different runner —
a once-a-day job after the close, not a bar-polling loop — and belongs in its own spec once
backtesting works. Also out: intraday behaviour of any kind, which must not change.

**The hard constraint: no existing intraday behaviour may change.** 720 tests pass today and the
golden-trade fixture pins backtest output. A daily mode that alters an intraday number is a
regression, not a feature.

## 3. The clock

`SessionClock` gains a daily mode rather than having its invariants loosened, because those
invariants are correct for intraday and the failure modes they catch are real.

At `interval_minutes >= 1440`:

- one bar per trading day, stamped 00:00 IST, matching how daily candles are already stored
- `no_new_entries_after` and `square_off` do not apply and are not consulted
- `is_square_off_bar` and `square_off_due` are always False
- `in_session` is true for any bar whose date is a trading day
- `entries_allowed` is true for any bar whose date is a trading day

**The last two are not cosmetic and an earlier draft missed them.** Daily bars are stamped 00:00
IST, which is BEFORE `session.open` at 09:15. Every window check in this class is of the form
`open_ts(d) <= ts < something`, so on a daily bar all of them are false: `in_session` would reject
every bar and `entries_allowed` would permit no entry ever. Relaxing only the constructor's
validation would produce an engine that runs, consumes every bar, and silently never trades.

Below 1440 nothing changes at all. The 60 and 240 minute refusals stay as they are: they are
genuine misconfigurations for an intraday session, not cases this spec needs.

## 4. Square-off and the product rule

`BacktestBroker.square_off` already takes `products=("MIS",)` and `loop.py` already passes the
default, so **CNC positions already survive the intraday square-off**. This is existing, tested
behaviour and is not being added.

What changes: in daily mode the loop must not call `square_off` at all on a per-bar basis. A CNC
position exits only when its strategy says so, or on a stop, or at the end of the run.

**End-of-run flattening stays.** An open position at the last bar is closed at that bar's close
and recorded, so no run reports an unrealised position as though it were cash. The existing
`FLATTEN` path at `loop.py:375` already does this.

A MIS strategy is rejected in daily mode with a clear message: MIS on a daily bar has no meaning,
since the position would be squared off on the bar it opened.

## 5. Entry and exit prices

Intraday fills at the next bar's open. **Daily mode fills at the next bar's CLOSE.**

This is not a style preference. Groww's daily `open` is synthetic for 2025 — 98.6% of bars carry
the previous close forward — so a daily backtest filling at the open would be fictional over a
large part of the window. `swing_screen.py` and `momentum_screen.py` already use closes throughout
for this reason, and a portfolio backtest that disagreed with the screen that motivated it would
not be comparable to it.

The signal is computed on bar i's close and filled at bar i+1's close. That is a full day of
slippage in the pessimistic direction and it is the honest construction available from this data.

## 6. Costs

Daily mode uses the CNC delivery schedule, not the MIS one. **`execution/charges.py` does NOT
already carry it** — an earlier draft of this spec said it did, and that was wrong. The module's
own docstring says "charges on one closed INTRADAY trade" and it contains no mention of CNC or
delivery. The delivery schedule exists only in `scripts/daily_screen.py`, as script constants.

The three differences, and why the error would have mattered:

| | intraday (`charges.py`) | delivery (`daily_screen.py`) |
|---|---|---|
| STT | 0.025%, sell side only | **0.1%, BOTH sides** |
| stamp duty | 0.003% buy | 0.015% buy |
| DP charge | none | **Rs 15.34 per sell** |

brokerage (0.1%, floor 5, cap 20), exchange (0.00297%), SEBI (0.0001%) and GST (18%) are the same.

Charging a daily CNC backtest at intraday rates understates a round trip by about 0.31% on a
12,500 position. At 60-day holds that is roughly 0.11%/mo against the 0.216%/mo edge `trend_dip`
currently shows — **half the edge, given away by a wrong default.** A backtest that flattering
would have looked tradable when it is not.

So this task must ADD a delivery schedule: new fields on `ChargesConfig` with the values above as
defaults, and `position_charges` selecting by the position's product. The intraday path must be
untouched and its existing tests must still pass unchanged.

The script constants and the config defaults will then state the same rates in two places. They
are not unified here because doing so would change `daily_screen.py`, whose committed results
depend on those exact numbers. A comment in each points at the other.

Slippage stays configurable and defaults to the existing `slippage_pct`. Per-name liquidity tiers
from `data/liquidity.py` are NOT wired here — that matters for a 200-name universe and belongs
with portfolio work, and wiring it now would change intraday numbers.

## 7. Risk and sizing

Unchanged. `min_risk_fraction`, `per_trade_pct` and the daily loss cap apply as they do today. Two
daily-specific notes:

- **`mis_leverage` does not apply to CNC**, and `backtest.py:175` already returns 1.0 for
  non-MIS. Existing behaviour; stated so nobody "fixes" it.
- The daily loss cap is evaluated per bar. On a daily bar that means per day, which is the
  intended reading, but a multi-day drawdown is NOT capped by it. This spec does not add a
  portfolio drawdown limit; it is noted as a real gap for whoever builds position sizing on top.

## 8. What could go wrong, and what catches it

- **An intraday number moves.** The golden-trade fixture and the 720-test suite are the control.
  Any change to them is a regression and must fail the build.
- **A CNC position is squared off anyway**, silently turning a swing system into an intraday one
  with worse costs. A test asserts a CNC position opened on day 1 is still open on day 3.
- **A daily run is charged MIS rates**, flattering it. A test asserts the charge on a daily CNC
  round trip matches `round_trip_charges` for delivery, not intraday.
- **The clock silently accepts a nonsense interval.** 60 and 240 must still raise.

## 9. Testing

Unit:
- `SessionClock` at 1440 produces one bar a day and never reports a square-off bar
- `SessionClock` at 60 and 240 still raise, with the existing messages
- a CNC position survives a session boundary; a MIS position does not
- daily mode fills at the next close, not the next open
- a MIS strategy in daily mode is rejected with a clear message
- delivery charges, not intraday charges, on a daily CNC round trip

Integration:
- a two-week daily backtest over a handful of symbols opens, holds across days and closes
- the existing golden-trade fixture is byte-identical

## 10. What this does not deliver

It does not make money, and it does not validate any signal. It makes a swing signal *runnable*,
so that `trend_dip` — or whatever survives the delivery screen — can be tested as a portfolio with
real sizing and real charges rather than as a name-day excess.

It also does not deliver paper trading on a daily cadence, which is the next spec and the thing
actually standing between a working backtest and real money.
