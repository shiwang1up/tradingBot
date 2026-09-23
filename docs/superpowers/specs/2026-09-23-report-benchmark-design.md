# A buy-and-hold benchmark on every report: design

Date: 2026-09-23. Status: proposed.

## 1. Why

`daily-perf-groww`, the best in-sample run this project has produced, returned +14,043 net on
100,000 of capital. Equal-weight buy-and-hold of the same fifty names, over the same run window and
charged the same delivery costs, returned **+99,030 net** — seven times as much. The bot was 85%
deployed and held a position on 100% of bars, so this is not cash drag: it was fully invested and
returned roughly a seventh of simply owning the basket.

(A cruder version of this comparison, quoted earlier in the work that motivated this spec, put the
basket at +129% against the strategy's +14.0%. That figure measured the strategy's *traded* span
rather than its full run window, ignored costs and ignored whole-share truncation. §3 and §4 below
define the figure this report will actually print, which is the more conservative +99,030 — and it
is still seven times the strategy.)

Nothing in the repository says so. `tradebot report` prints win rate, payoff, expectancy, drawdown
and a t — every one of which said this run was unremarkable-but-positive — and not one of them
answers the only question that decides whether to run the thing at all: **did selecting beat not
selecting?**

That comparison has to be computed by hand today, which means in practice it is not computed. This
spec makes it a line in every report.

## 2. What it is, stated so it cannot mislead

**An equal-weight basket of the run's own universe, bought at the start of the run's window and
sold at the end, charged real costs.** Three things it is deliberately NOT, each of which will be
visible in the output rather than buried here:

- **Not an index.** A real index is capitalisation-weighted and this database holds no market caps.
  Calling the line "NIFTY" would be a lie; it is labelled "equal-weight basket".
- **Not survivorship-free.** `universe.yaml` is today's constituent list, so the basket is built
  from names that survived to today. This inflates the benchmark. It also inflates the strategy,
  which picks from the same list, so the *difference* is fairer than either absolute figure — but
  neither is clean, and the existing survivorship note stays.
- **Not rebalanced.** One buy, one sell. A rebalanced basket would be a different and better
  benchmark; it is out of scope because it needs a rebalancing rule nobody has specified.

## 3. The computation

Given a run, the symbols its universe held, its capital, its bar interval and its charge schedule:

1. **Window.** The first and last `date` in `repo.daily_pnl(run_id)`, which is exactly the span the
   report's own daily table covers.

   **This is a deliberate choice with a consequence worth stating.** A strategy needing 200 bars of
   SMA warm-up cannot trade the first year of its own window, while the basket is bought on day one
   — so the benchmark gets a head start the strategy does not. That is intended: the question the
   line answers is "should I have run this bot, or just bought the basket?", and the warm-up is a
   real cost of running the bot. Deriving the window from the first and last trade instead would
   hand the strategy the more flattering comparison, and `daily-perf-groww` shows the gap is
   material — its traded span starts ten months after its run window.
2. **Per name:** `capital / N`, where N is the number of symbols priced at both ends. Buy at the
   first stored close at or after the window start; sell at the last stored close at or before the
   window end. **Whole shares only**; the remainder stays in cash and earns nothing.
3. **Costs:** one CNC round trip per name through `round_trip_charges` at the run's own schedule,
   so both sides of the comparison pay the same rates. A basket is not free, and pretending it is
   would understate the bar the strategy has to clear.
4. **Names without a price at both ends are excluded and counted.** Silently dropping them would
   quietly change the capital split.

The result is net rupees, alongside the count of names actually held.

## 4. What the report prints

Three lines, after the existing `Total PnL`. These are the real figures for `daily-perf-groww`,
computed while writing this spec:

    Benchmark             +99,029.71   equal-weight buy & hold, 50 names, net of one round trip
    Strategy vs benchmark -84,986.46   selecting lost to not selecting

Worth noting what that costs line does: 100,000 across 50 names is 2,000 a name, where the flat
23.60 DP fee alone is 1.18% of the position. The basket pays 1,663.49 in charges, 1.66% of capital.
A benchmark that ignored costs would overstate the bar by that much, in the strategy's favour.

The verdict phrase is mechanical — "selecting beat not selecting" or "selecting lost to not
selecting" — because a signed number alone is too easy to read past, and this is the line the
report exists to make unavoidable.

## 5. Where the code goes

`src/tradebot/report/benchmark.py`, new, holding `Benchmark` (a frozen value object: net, names
held, names dropped, window start and end) and `build_benchmark(...)`.

`build_summary` gains an optional `benchmark: Optional[Benchmark] = None` and carries it onto
`Summary`; `format_summary` renders it when present and omits the block entirely when absent, so
every existing caller and every existing test is unaffected. The CLI builds the `Benchmark` and
passes it in — keeping the I/O in `cli.py` and out of `build_summary`, the same split
`tradebot hurdle` already uses.

**The universe comes from the config the `report` command was given**, not from the run's stored
config. This is a real limitation and the output names the universe file and the symbol count so a
mismatch is visible: reporting a year-old run against a since-edited `universe.yaml` benchmarks it
against a basket it never faced. Recording the resolved symbol list on the run would fix it
properly and is a schema change, deliberately out of scope here.

## 6. Scope

**This system places no orders and nothing here changes that.** The benchmark is arithmetic over
stored candles.

Out of scope: rebalancing, cap weighting, an actual index tracker (no index candles are stored —
`data.index_symbol` is `NIFTY` and the database has none at the daily interval), benchmarking the
`--compare` view, and any change to how the strategy trades.

## 7. Testing

- A basket of one name over a two-bar window is the whole calculation by hand: shares, charges, net.
- A name priced at only one end is excluded, and the capital split reflects the reduced count.
- Whole-share truncation leaves the remainder in cash — a name at a price that does not divide the
  slice evenly must not silently round up.
- The charge schedule reaches it: the same basket under `groww` and under `zerodha` costs different
  amounts, in the right direction.
- `build_summary` without a benchmark produces exactly today's `Summary` and today's rendered text.
  Every existing report test is the guard, and none of them may change.
- One end-to-end assertion against `daily-perf-groww`: benchmark net +99,029.71 on 50 names against
  that run's +14,043.25, and the verdict phrase is the losing one. Pinning the actual number, not
  just its sign, catches a silent inversion and a silent change in the cost or truncation rules.

## 8. What this does not do

It does not make the strategy better, and it is not evidence about the future. It reports what
holding the basket did over one window in the past, on a survivorship-biased list, with no
rebalancing. Its value is that it is printed every time, next to the number it should be compared
against, instead of being a calculation someone has to remember to do.
