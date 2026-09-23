# A verified, broker-selectable cost model: design

Date: 2026-09-22. Status: proposed.

## 1. Why this is the binding constraint

Eleven strategy families have been tested on this bot. Ten were rejected. The eleventh,
`trend_dip`, is the only one to clear a pre-registered statistical bar — and it is rejected on
economics alone:

    in-sample 2020-01-01..2023-12-31, 314 point-in-time constituents, 994 dates
    trend_dip  h=60   +0.618%/date   t 2.96   (bar: t >= 2.64)   = 0.216%/mo
    cost hurdle at 1 lakh across 8 positions, 60-day hold        = 0.238%/mo

(0.618% earned over 60 trading days is 0.618 / (60/21) = 0.216% a month. The hurdle of 0.238%/mo is
the figure the swing screen set — computed, as section 2 shows, against an unverified DP fee.)

The signal passes. The costs eat it. Every lever this project has left is therefore a cost lever,
and the cost model has never been checked against a real broker's pricing page. `daily_screen.py`
line 45 says so in as many words: *"CHECK THESE against groww.in/pricing before trusting any figure
computed from them."* Nobody did.

This spec is that check, made permanent.

For completeness, the full in-sample screen, run 2026-09-22 and not previously recorded in any
note. `random` is a control setup; that it beats three of the six real ones is the context in which
every figure here should be read:

| setup | h | obs | dates | xs/date | t | verdict |
|---|---|---|---|---|---|---|
| pullback | 60 | 9,041 | 722 | −0.344% | −1.29 | fail |
| breakout | 60 | 11,271 | 750 | +0.073% | 0.27 | fail |
| **trend_dip** | **60** | **13,269** | **778** | **+0.618%** | **2.96** | **pass, below hurdle** |
| squeeze | 60 | 3,611 | 522 | −0.686% | −1.64 | fail |
| gap_vol | 60 | 1,341 | 545 | +0.117% | 0.19 | fail |
| three_down | 60 | 9,109 | 747 | +0.226% | 0.93 | fail |
| random (control) | 60 | 6,219 | 794 | +0.230% | 1.07 | fail |
| trend_dip | 120 | 13,044 | 777 | +0.605% | 1.89 | fail |

`trend_dip` at h=120 is the reason a longer hold does not rescue this: the excess is flat from day
60 to day 120 (+0.618% to +0.605%), so per month it *halves* to 0.106% while the hurdle only falls
to 0.119%. The edge is entirely front-loaded. Holding longer is not a cost lever here.

## 2. Three defects in the shipped cost model, all verified today

**a. The schedule matches no real broker.** `ChargesConfig.dp_charge = 15.34` is Zerodha's DP fee
(₹3.5 CDSL + ₹9.5 Zerodha + ₹2.34 GST, zerodha.com/charges). `brokerage_pct/max/min = 0.1/20/5`
is Groww's. The shipped default is a chimera: Groww's brokerage with Zerodha's depository fee.

**b. Groww's real DP fee is ₹23.60, not ₹15.34.** groww.in/pricing: depository ₹3.5 plus Groww
₹16.5 = ₹20, plus 18% GST = ₹23.60. The model understates every Groww delivery round trip by
₹8.26. At 1 lakh across 8 positions that moves the 60-day hurdle from 0.238%/mo to 0.261%/mo.

**c. The rates are duplicated.** `scripts/daily_screen.py` lines 47-55 restate them as module
constants, and `swing_screen.py` imports `round_trip_cost` from there. A screen and the engine can
disagree about what a trade costs with nothing to catch it. They currently agree only by
coincidence of typing.

## 3. What the verified rates are

Checked 2026-09-22 against each broker's own pricing page.

**Statutory, identical for every broker:**

| Charge | Rate | Basis |
|---|---|---|
| STT, delivery | 0.1% | both sides |
| Stamp duty | 0.015% | buy side only |
| SEBI turnover fee | 0.0001% (₹10/crore) | both sides |
| NSE transaction charge | 0.00297% | both sides |
| GST | 18% | on brokerage + transaction + SEBI, and on the DP fee — but see below: the DP figures in this spec are already GST-inclusive |

**Broker-specific:**

| Broker | Delivery brokerage | DP fee per sell (GST-inclusive) |
|---|---|---|
| `groww` | ₹20 or 0.1%, whichever lower, min ₹5 | ₹23.60 (₹3.5 depository + ₹16.5 Groww + GST) |
| `zerodha` | zero | ₹15.34 (₹3.5 CDSL + ₹9.5 Zerodha + GST) |

One discrepancy, recorded rather than resolved: groww.in/pricing states the NSE transaction charge
as 0.00297% and zerodha.com/charges states 0.00307%. It is an exchange-set rate and cannot differ
by broker, so one page is stale. The difference is 0.0001% of turnover — about 2.5 paise on a
25,000 round trip — and is immaterial to every decision in this project. Both schedules use
0.00297% and carry a note pointing at this paragraph.

**The DP fee is stated GST-inclusive.** This is currently implicit: `charges.py` computes GST on
brokerage + transaction + SEBI and then adds `dp_charge` untaxed, which is only correct because
Zerodha's 15.34 already includes its ₹2.34 of GST. The convention must be written down, because
the obvious next edit — adding DP to the GST base — would double-tax it.

## 4. `brokers.yaml`

A new file at repo root. Each entry is a complete schedule plus its provenance:

```yaml
groww:
  verified_on: 2026-09-22
  source: https://groww.in/pricing
  brokerage_pct: 0.1
  brokerage_max: 20.0
  brokerage_min: 5.0
  dp_charge: 23.60          # GST-inclusive; see spec section 3
  # ... statutory fields ...
```

Three entries ship: `groww`, `zerodha`, and `legacy`.

`legacy` is the schedule exactly as it stands today, chimera and all. It exists so every figure
already committed to `docs/superpowers/notes/` stays reproducible. It carries a comment saying it
corresponds to no real broker and must not be used for a new decision.

`verified_on` and `source` are mandatory on every entry. A schedule without them is a load error.
The point of this work is that a rate is traceable to a page someone read on a date.

## 5. Selection, and defaults that change nothing

`charges.broker: groww` in config selects a schedule. `ChargesConfig` keeps every field it has and
every current default value, so the 731 passing tests and `tests/fixtures/golden_trades.json` are
untouched by the wiring commit.

The Groww DP correction (15.34 → 23.60) is a real behaviour change to every CNC figure. It lands in
its own commit, separate from the wiring, and that commit's message restates the numbers it moves.
No correction is folded silently into a refactor.

The three shipped configs that are intraday (`config.yaml`, `config-15m.yaml`, `config-orb.yaml`)
are unaffected: intraday charges do not include a DP fee and their brokerage is already Groww's.

## 6. `tradebot hurdle`

    tradebot hurdle --capital 100000 --slots 8 --hold 60 [--broker groww]

prints round-trip cost and the excess-per-month a strategy must beat, per slot count. This number
decides every go/no-go in the project and currently exists only as hand-arithmetic inside spec
documents, which is how the hurdle table in the swing screen spec came to be computed against an
unverified DP fee.

Output, at the verified rates, is the table this spec was argued from:

    1 lakh, 60-day hold, 0.05% slippage per side.  trend_dip's measured edge: 0.216%/mo

    slots   position    groww   zerodha
        8     12,500   0.261%    0.156% *
        5     20,000   0.237%    0.140% *
        4     25,000   0.212% *  0.134% *
        3     33,333   0.187% *  0.129% *
        2     50,000   0.162% *  0.124% *

    * below the measured edge

## 7. Unifying the screens

`scripts/daily_screen.py` loses its rate constants and reads a named schedule instead.
`round_trip_cost(position_value, broker="legacy")` keeps `legacy` as its default, so
`daily_screen.py insample` and `swing_screen.py insample` reproduce their committed numbers to the
digit. A test pins that: the unified `round_trip_cost` at `legacy` must equal the constant-based
figure the notes were computed from.

`swing_screen.py` lives on `dev-swing-screen` and imports this function. That branch must merge
before or with this work; the spec does not duplicate the screen to avoid the merge.

## 8. Scope: validation only, no trading

**This system does not place orders, and nothing in this spec moves it closer to placing them.**

- `tradebot paper` already refuses anything but MIS (`cli.py:362`), so the daily CNC path cannot
  reach it. That refusal stays, unchanged and untested-against.
- Live trading does not exist. `cli.py:1` says it "arrives in the live plan". No live plan is in
  scope here and none is written.
- The only thing that runs is `tradebot backtest` against stored candles.

Also out of scope: any change to `trend_dip`'s parameters (20/50/200, 60-day hold — fixed in
advance and not to be tuned), any intraday work, and the point-in-time universe wiring, which is
Part A and gets its own spec once this lands.

## 9. Testing

- Each shipped schedule gets a test computing one worked round trip and asserting the total against
  a figure derived by hand from the pricing page in section 3, not from the code.
- A test that `legacy` reproduces the committed screen constants exactly.
- A test that a schedule missing `verified_on` or `source` fails to load.
- A test that `dp_charge` is not added to the GST base, with a comment naming the double-tax bug it
  prevents.
- `tests/fixtures/golden_trades.json` must be byte-identical throughout. It is the only guard
  against intraday behaviour moving, and a change to it is a regression to report, not to accept.

## 10. What this buys, stated honestly

It does not make the bot profitable. It makes the cost of trading a verified input rather than a
typed assumption, and in doing so it makes one thing visible that was not visible before: on Groww,
at 1 lakh, `trend_dip` clears its hurdle only at four concurrent positions or fewer, and then by 2%
— which is breakeven, not an edge. The same strategy at the same account size on a zero-brokerage
delivery broker clears at eight positions by 38%, diversified.

That comparison is the deliverable. The decision it informs — whether to change broker, concentrate,
or neither — is the user's, and this spec deliberately does not presume it.

It also does not validate `trend_dip`. That edge is one in-sample measurement on a universe of 314
names over four years. The 2024-onward holdout is unspent and stays that way until Part A.
