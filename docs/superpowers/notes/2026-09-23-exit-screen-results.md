# The exit screen: results

Date: 2026-09-23. Spec: `docs/superpowers/specs/2026-09-23-exit-screen-design.md`, committed before
the screen was written. Plan: `docs/superpowers/plans/2026-09-23-exit-screen.md`. Code:
`scripts/exit_screen.py`. The screen reads `data/tradebot.db` read-only and writes nothing.

**No challenger cleared the bar, and `hold60` — the incumbent — beat every one of them decisively.
That is still an instruction to change the engine, because `hold60` is not what the engine trades.**

## 1. What this tested

`swing_screen.py` measured ENTRIES with a pure 60-bar hold and no stop, and `trend_dip` cleared its
pre-registered bar that way. `config-daily.yaml` then trades something else: it adds a 2×ATR(20)
stop that no screen had ever measured, and in the 2020-2023 backtest that stop produced **70 of 112
exits**. The majority of the traded system's behaviour came from an untested component.

The entry was fixed at `trend_dip` 20/50/200 and not varied. Only the exit changed.

## 2. The result

`.venv/bin/python scripts/exit_screen.py insample`, verbatim:

| cell | obs | dates | ret/trade | diff/date | t | verdict |
|---|---|---|---|---|---|---|
| `hold60` | 13,269 | 778 | +6.621% | n/a | n/a | incumbent |
| `stop2atr` | 13,269 | 778 | +3.377% | −2.749% | −11.99 | fail |
| `stop3atr` | 13,269 | 778 | +4.492% | −1.657% | −10.30 | fail |
| `stop4atr` | 13,269 | 778 | +5.482% | −0.745% | −7.00 | fail |
| `trail3atr` | 13,269 | 778 | +2.092% | −3.620% | −12.54 | fail |
| `target2r_stop2atr` | 13,269 | 778 | +1.193% | −4.852% | −16.15 | fail |

The bar, fixed in advance: a positive mean paired per-date difference against `hold60` AND
t ≥ 2.58. Every challenger is negative, and none is close. All six cells report identical
observation and date counts, which is the pairing invariant holding.

**`ret/trade` is raw return and is mostly market drift** — 2020-2023 was a strong bull market. The
`diff/date` column is the result: because every cell sees identical entries, the paired difference
cancels the drift and isolates what the exit rule did.

**The dose-response is clean and monotone.** Every unit of additional constraint costs return:

    4xATR stop   -0.745%/date
    3xATR stop   -1.657%/date
    2xATR stop   -2.749%/date

A trailing stop costs more than any fixed stop, and adding a profit target costs most of all. That
ordering is what you would expect if the effect is real rather than noise, and it is not something
a single spurious cell could produce.

**The engine trades `stop2atr`.** Against `hold60` that is a gap of **3.244 percentage points per
trade** (+6.621% against +3.377%).

## 3. The confirmatory cell, disclosed

`hold60` versus `stop2atr` was seen informally before this screen existed: +3.97% against +1.30%
mean per trade over the 112 closed trades of the `daily-perf-groww` backtest. That observation is
why the screen was written, so **that cell is confirmatory, not exploratory**, and a reader should
discount it accordingly. It is reported here at proper resolution — 13,269 name-days rather than
112 trades, on the point-in-time universe, with a date-clustered t — and it confirms in the same
direction at a similar ratio.

`stop3atr`, `stop4atr`, `trail3atr` and `target2r_stop2atr` were genuinely untested before this run.
Their agreement with the confirmatory cell, and the monotone ordering across them, is the part of
this result that is new evidence.

## 4. Limits, and the one that matters most

- **This was a bull market, and a stop is insurance.** 2020-2023 rose strongly (an equal-weight
  basket of the same universe returned +129% over it). Insurance looks like a pure cost in a market
  that only goes up. Whether a stop pays for itself in a falling market is **not tested here and
  cannot be tested on this data** — the unspent holdout, 2024-2026, is also a rising market. This
  is the single largest caveat on the result and it does not go away with more of this data.
- **Per-trade, not portfolio.** Unstopped trades hold their slot for the full 60 bars, so a
  five-slot book takes fewer of them. A per-trade improvement and a portfolio improvement can
  disagree, and this screen measures only the first.
- **No drawdown measure.** `hold60` has no stop, so adverse excursion is unbounded. Measured
  separately on the 112 backtest trades: mean worst excursion while held −8.39%, 16 of 112 dipped
  below −15%, worst single name −57.87%. The screen says nothing about whether that is tolerable.
- **Entry convention differs from the engine.** The screen enters at the signal bar's close,
  matching `swing_screen.forward_return` so `hold60` IS the validated design. The engine fills a
  bar later, at the next bar's close. This cancels out of a paired comparison but means the
  absolute `ret/trade` figures are not the engine's.
- Survivorship is handled: the screen runs on point-in-time index membership, not today's list.
- Costs are charged at the `legacy` schedule, as both other screens are, so these figures sit
  alongside theirs. Costs are identical across cells and cancel out of the paired difference.

## 5. Conclusion

**Supported:** the machinery — six exit rules matching `execution/backtest.py`'s gap clamp and its
stop-wins-on-an-ambiguous-bar rule, paired observations that drop a name-day no cell can resolve,
and a date-clustered t against a bar fixed before the screen was written.

**Supported, and negative on the decision:** no challenger beats `hold60`. Every exit rule tested
cost money over this window, and the tighter the constraint the more it cost.

**The instruction this produces:** the engine trades `stop2atr`, which this screen scores 2.749%/date
worse than the design the entry screen actually validated. That is a real finding about a real
divergence. It is **not** a licence to delete the stop, for two reasons stated in the spec before
the run: `risk/engine.py` sizes positions by dividing planned risk by stop distance, so a no-stop
design has nothing to divide by and needs a new sizing rule; and this window cannot tell you what a
stop is worth in a market that falls.

**The holdout, 2024-01-01 onward, was deliberately not run.** No cell passed, so the holdout could
only confirm a rejection already visible in-sample, and spending it would buy nothing. It stays
unspent.

## 6. What would actually settle it

Not another exit rule. The open question is portfolio-level and regime-dependent: what a five-slot
book holding unstopped 60-day positions would have returned, and what its drawdown would have been,
in a window that includes a real decline. The first half is buildable from this data; the second
half is not, and no amount of work on this database will make 2020-2026 contain a bear market.
