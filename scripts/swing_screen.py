"""Do technical setups pick better-than-average stocks to hold for weeks?

    .venv/bin/python scripts/swing_screen.py insample

Of roughly 200 index members on any morning, can a setup identify the few worth holding?
The unit of observation is a NAME-DAY, not a trade and not a portfolio: for every (symbol,
date) a setup fires on, record the forward return at each horizon. That gives hundreds of
thousands of observations where a portfolio construction would give a few hundred, and the
momentum screen showed that resolution is what decides whether a small edge is visible.

MEASURED AGAINST TWO BASELINES, because they answer different questions:

  time-series     the mean return of holding this same symbol this many days, entered on
                  every eligible date -- is this a good MOMENT to buy this stock?
  cross-sectional the equal-weight forward return of every eligible index member on this
                  same date -- is this a better STOCK to buy today than the others?

The second is the decision-relevant one when choosing a few names on a morning, and it
removes market-wide moves automatically. A setup that beats the first but not the second
is firing on days the whole market rose.

Costs are charged to the signal and to both baselines alike, so they cancel out of the
excess: excess measures SELECTION. Whether the selection pays is a separate question,
reported as net expectancy beside it, and the two can disagree -- in a rising market a
setup can be profitable and still worse than random.

Horizons are long on purpose. Cost scales with turnover, so the holding period fixes the
hurdle before any signal is considered: a 10-day hold needs 1.43%/mo excess to break even
and a 60-day hold needs 0.238%/mo. See spec section 4.

This reads the database read-only and writes nothing.
"""
import argparse
import importlib.util
import random
import sqlite3
import sys
from collections import defaultdict, namedtuple
from datetime import date
from pathlib import Path

DB = "data/tradebot.db"
HORIZONS = (20, 60, 120)                 # trading days
PRIMARY_HORIZON = 60
INSAMPLE = (date(2020, 1, 1), date(2023, 12, 31))
HOLDOUT_START = date(2024, 1, 1)         # reserved; see guard_holdout
WARMUP_BARS = 200                        # SMA(200)
CAPITAL = 100_000.0
POSITIONS = 8                            # the book the hurdle is computed for
BONFERRONI_T = 2.64                      # six primary cells at alpha 0.05
RANDOM_SEED = 20260922

# Indicators, the corporate-action mask, the cost model and the date-clustered t come from
# the daily screen rather than being written twice: one definition of a split, of a round
# trip, and of how to count clustered observations.
_DS = Path(__file__).resolve().parent / "daily_screen.py"
_spec = importlib.util.spec_from_file_location("daily_screen", _DS)
daily_screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_screen)
gap_mask = daily_screen.gap_mask
load_series = daily_screen.load_series
round_trip_cost = daily_screen.round_trip_cost
sma = daily_screen.sma
rsi_wilder = daily_screen.rsi_wilder
atr_wilder = daily_screen.atr_wilder
rolling_max_prev = daily_screen.rolling_max_prev

Bar = namedtuple("Bar", "date open high low close volume")


class Indicators(object):
    """Everything the setups read, computed once per symbol."""

    def __init__(self, bars):
        self.bars = bars
        self.closes = [b.close for b in bars]
        self.sma20 = sma(self.closes, 20)
        self.sma50 = sma(self.closes, 50)
        self.sma200 = sma(self.closes, 200)
        self.rsi2 = rsi_wilder(self.closes, 2)
        self.atr20 = atr_wilder(bars, 20)
        self.max100_prev = rolling_max_prev(self.closes, 100)
        self.vol20 = sma([float(b.volume) for b in bars], 20)
        # ATR as a fraction of price, and its own 100-day prior low, for the squeeze
        self.atrpct = [None if (self.atr20[i] is None or self.closes[i] <= 0)
                       else self.atr20[i] / self.closes[i] for i in range(len(bars))]
        self.atrpct_min100_prev = _rolling_min_prev_opt(self.atrpct, 100)


def _rolling_min_prev_opt(values, n):
    """Rolling min of the n values BEFORE each index, skipping Nones. None until n are seen."""
    out = [None] * len(values)
    for i in range(len(values)):
        window = [v for v in values[max(0, i - n):i] if v is not None]
        if len(window) == n:
            out[i] = min(window)
    return out


def _ready(ind, i):
    """Every setup needs the slowest indicator warm. Reading a None as a number is the
    failure this guards; it would make setups fire in the first months of the window."""
    return i >= WARMUP_BARS and ind.sma200[i] is not None


def pullback(ind, i):
    """An oversold dip inside an uptrend."""
    if not _ready(ind, i) or ind.rsi2[i] is None:
        return False
    return ind.closes[i] > ind.sma200[i] and ind.rsi2[i] < 10.0


def breakout(ind, i):
    """A new hundred-day closing high, in an uptrend."""
    if not _ready(ind, i) or ind.max100_prev[i] is None:
        return False
    return ind.closes[i] > ind.max100_prev[i] and ind.closes[i] > ind.sma200[i]


def trend_dip(ind, i):
    """A dip to the twenty-day average that holds the fifty. Below both is a breakdown."""
    if not _ready(ind, i) or ind.sma20[i] is None or ind.sma50[i] is None:
        return False
    return (ind.sma50[i] > ind.sma200[i] and ind.closes[i] < ind.sma20[i]
            and ind.closes[i] > ind.sma50[i])


def squeeze(ind, i):
    """Volatility was at a hundred-day low YESTERDAY, and today closes above yesterday's high.

    The contraction is measured on bar i-1 deliberately. Measuring it on bar i is
    self-contradictory: the expansion bar's own range is what lifts ATR, so a genuine
    squeeze-then-break would disqualify itself and the setup would fire only on days that
    broke out without moving."""
    if not _ready(ind, i) or i < 1:
        return False
    if ind.atrpct[i - 1] is None or ind.atrpct_min100_prev[i - 1] is None:
        return False
    return (ind.atrpct[i - 1] <= ind.atrpct_min100_prev[i - 1]
            and ind.closes[i] > ind.bars[i - 1].high)


def gap_vol(ind, i):
    """A gap up on heavy volume that holds its gain into the close."""
    if not _ready(ind, i) or ind.vol20[i] is None:
        return False
    b, prev = ind.bars[i], ind.bars[i - 1]
    return (b.open > prev.close * 1.01 and b.volume > 2.0 * ind.vol20[i]
            and b.close > b.open)


def three_down(ind, i):
    """Three consecutive lower closes inside an uptrend."""
    if not _ready(ind, i) or i < 3:
        return False
    c = ind.closes
    return (c[i] < c[i - 1] < c[i - 2] < c[i - 3] and c[i] > ind.sma200[i])


def make_random(rate, seed=RANDOM_SEED):
    """A control that fires at `rate`, independent of price.

    Not decoration. On the momentum screen a random ranking produced a LARGER apparent edge
    than the real signal, and that number is what made the result interpretable. A setup
    that cannot beat this is noise.
    """
    rng = random.Random(seed)

    def fire(ind, i):
        return _ready(ind, i) and rng.random() < rate

    return fire


SETUPS = (
    ("pullback", pullback),
    ("breakout", breakout),
    ("trend_dip", trend_dip),
    ("squeeze", squeeze),
    ("gap_vol", gap_vol),
    ("three_down", three_down),
)


Obs = namedtuple("Obs", "symbol entry_date horizon ret")


def forward_return(bars, i, h):
    """Close-to-close return from bar i to bar i+h, or None if the exit is past the end.

    Close, not open: Groww's daily `open` is synthetic for 2025 -- 98.6% of bars carry the
    previous close forward -- so a fill at an open would be fictional over much of the window.
    """
    j = i + h
    if j >= len(bars) or bars[i].close <= 0:
        return None
    return bars[j].close / bars[i].close - 1.0


def is_member(members_by_date, symbol, d):
    """Was `symbol` in the index on `d`? Unknown dates are not members: silently treating a
    missing date as membership is how survivorship bias gets back in."""
    return symbol in members_by_date.get(d, ())


def hold_is_clean(bars, i, h, masked):
    """No corporate action anywhere in [entry, exit] inclusive. An unadjusted split makes the
    return fiction wherever in the window it lands, so the check spans the whole hold."""
    j = min(i + h, len(bars) - 1)
    return not any(bars[k].date in masked for k in range(i, j + 1))


def observations(symbol, bars, ind, masked, members_by_date, window, setup, horizons):
    """Every name-day this setup fires on, one Obs per horizon."""
    name, fires = setup
    lo, hi = window
    out = []
    for i in range(len(bars)):
        d = bars[i].date
        if not (lo <= d <= hi) or not is_member(members_by_date, symbol, d):
            continue
        if not fires(ind, i):
            continue
        for h in horizons:
            if not hold_is_clean(bars, i, h, masked):
                continue
            r = forward_return(bars, i, h)
            if r is not None:
                out.append(Obs(symbol, d, h, r))
    return out


def ts_baseline(bars, window, h, masked):
    """Mean h-day close-to-close return of this symbol, entered on every eligible date in the
    window. Answers: is this a good MOMENT to buy this stock?

    Indian large caps rose over this period, so any rule that buys shows a positive return
    from drift alone; only the excess over this is evidence about timing."""
    lo, hi = window
    rs = []
    for i in range(len(bars)):
        if not (lo <= bars[i].date <= hi):
            continue
        j = i + h
        if j < len(bars) and bars[j].date <= hi and hold_is_clean(bars, i, h, masked):
            r = forward_return(bars, i, h)
            if r is not None:
                rs.append(r)
    return (sum(rs) / len(rs)) if rs else None


def xs_baseline(returns_by_key, members_by_date, d, h):
    """Equal-weight forward return of every index member on `d` at horizon `h`. Answers: is
    this a better STOCK to buy today than the others?

    This is the decision-relevant comparison when choosing a few names on a morning, and it
    removes market-wide moves on that date automatically -- a setup that beats the
    time-series baseline but not this one is firing on days everything rose."""
    rs = [returns_by_key[(s, d, h)] for s in members_by_date.get(d, ())
          if (s, d, h) in returns_by_key]
    return (sum(rs) / len(rs)) if rs else None


Ex = namedtuple("Ex", "symbol entry_date horizon ts_excess xs_excess")


def date_means(exs, attr="xs_excess"):
    """Per-entry-date means, sorted for determinism. Observations sharing a date are averaged
    into one before any statistic sees them: setups cluster, and one market-wide dip firing a
    setup across forty names is one piece of evidence, not forty."""
    by_date = defaultdict(list)
    for e in exs:
        by_date[e.entry_date].append(getattr(e, attr))
    return [sum(by_date[d]) / len(by_date[d]) for d in sorted(by_date)]


def t_across_dates(exs, attr="xs_excess"):
    """t of the mean of `date_means`. None for fewer than two dates or zero variance."""
    import math
    means = date_means(exs, attr)
    n = len(means)
    if n < 2:
        return None
    mean = sum(means) / n
    var = sum((m - mean) ** 2 for m in means) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def hurdle_per_month(horizon, capital=CAPITAL, positions=POSITIONS):
    """Monthly excess a system must clear just to pay for its own turnover.

    Cost scales with how often you trade, so the holding period fixes this before any signal
    is considered: 20 days needs 0.715%/mo, 60 needs 0.238%, 120 needs 0.119%. Published
    anomalies run 0.3-0.8%/mo, which is why the primary horizon is 60 and not 10."""
    rt = round_trip_cost(capital / float(positions))
    rotations_per_year = 252.0 / float(horizon)
    return rt * rotations_per_year / 12.0


def summarise(exs, horizon):
    """Headline figures for one (setup, horizon) cell."""
    n = len(exs)
    if n == 0:
        return dict(n=0, dates=0, ts_excess=0.0, xs_excess=0.0, ts_t=None, xs_t=None,
                    win=0.0, horizon=horizon)
    return dict(n=n, dates=len(set(e.entry_date for e in exs)),
                ts_excess=sum(e.ts_excess for e in exs) / n,
                xs_excess=sum(e.xs_excess for e in exs) / n,
                ts_t=t_across_dates(exs, "ts_excess"),
                xs_t=t_across_dates(exs, "xs_excess"),
                win=100.0 * sum(1 for e in exs if e.xs_excess > 0) / n,
                horizon=horizon)


def verdict(xs_excess, xs_t, horizon):
    """The pre-registered bar: cross-sectional excess > 0 with t >= 2.64.

    Clearing the bar and clearing the COST hurdle are different questions, and collapsing
    them would hide which one we have: a real-but-too-small edge is a different finding from
    no edge at all."""
    if xs_t is None or xs_excess <= 0 or xs_t < BONFERRONI_T:
        return "fail"
    monthly = xs_excess / (horizon / 21.0)
    return "PASS" if monthly >= hurdle_per_month(horizon) else "PASS but below the cost hurdle"


def guard_holdout(confirmed):
    """2024-01-01 onward is reserved and unspent. Looking at it before a setup passes
    in-sample turns the only honest out-of-sample test this project has into another
    in-sample one, and it cannot be un-looked-at."""
    if not confirmed:
        sys.exit("the holdout from %s is RESERVED and unspent. Pass --i-am-sure to spend it, "
                 "and only after a setup has passed in-sample." % HOLDOUT_START)


def run_phase(conn, window, label, out=sys.stdout):
    from tradebot.data.membership import MembershipError, constituents_on, load_timeline
    timeline = load_timeline("index_membership.yaml")

    all_symbols = sorted(set(timeline.anchor) |
                         set(s for e in timeline.events for s in (e.include + e.exclude)) |
                         set(c for _, c in timeline.aliases))
    series = load_series(conn, all_symbols)
    # gap_vol needs volume and daily_screen.Bar does not carry it. Join on DATE, not on
    # position: zipping two orderings assumes they agree, and a single missing bar would
    # then shift every volume by one day without any error.
    bars_by_symbol = {}
    for sym, bs in series.items():
        vol_by_date = {}
        for ts, v in conn.execute(
                "SELECT ts, v FROM candles WHERE symbol=? AND interval=1440", (sym,)):
            vol_by_date[date.fromtimestamp(ts + daily_screen.IST_OFFSET)] = v or 0
        bars_by_symbol[sym] = [Bar(b.date, b.open, b.high, b.low, b.close,
                                   vol_by_date.get(b.date, 0)) for b in bs]

    masked = {s: gap_mask(series[s], mask_days=0) for s in series}
    inds = {s: Indicators(bars_by_symbol[s]) for s in bars_by_symbol}

    lo, hi = window
    all_dates = sorted(set(b.date for bs in bars_by_symbol.values() for b in bs
                           if lo <= b.date <= hi))
    members_by_date = {}
    for d in all_dates:
        try:
            members_by_date[d] = constituents_on(timeline, d)
        except MembershipError:
            # Only "outside the timeline's coverage" is an expected miss. Catching every
            # exception here would swallow a broken count invariant, which is the one error
            # this whole dataset exists to surface.
            members_by_date[d] = ()

    # forward returns for every member name-day, so the cross-sectional baseline can be built
    returns_by_key = {}
    for sym, bars in bars_by_symbol.items():
        idx = {b.date: i for i, b in enumerate(bars)}
        for d in all_dates:
            i = idx.get(d)
            if i is None or not is_member(members_by_date, sym, d):
                continue
            for h in HORIZONS:
                if hold_is_clean(bars, i, h, masked.get(sym, set())):
                    r = forward_return(bars, i, h)
                    if r is not None:
                        returns_by_key[(sym, d, h)] = r

    rates = []
    for name, fires in SETUPS:
        fired = total = 0
        for sym, bars in bars_by_symbol.items():
            ind = inds[sym]
            for i, b in enumerate(bars):
                if lo <= b.date <= hi and is_member(members_by_date, sym, b.date):
                    total += 1
                    fired += bool(fires(ind, i))
        rates.append(fired / float(total) if total else 0.0)
    mean_rate = sum(rates) / len(rates) if rates else 0.01
    setups = list(SETUPS) + [("random", make_random(mean_rate))]

    print("swing setup screen -- %s, %s..%s" % (label, lo, hi), file=out)
    print("  %d symbols, %d dates, mean firing rate %.2f%%"
          % (len(bars_by_symbol), len(all_dates), 100.0 * mean_rate), file=out)
    print("  cost hurdle: %s" % ", ".join(
        "%dd %.3f%%/mo" % (h, 100.0 * hurdle_per_month(h)) for h in HORIZONS), file=out)
    print("", file=out)
    print("  %-11s %5s %8s %7s %10s %8s %10s %8s  %s"
          % ("setup", "h", "obs", "dates", "xs excess", "t", "ts excess", "t", "verdict"),
          file=out)

    for name, fires in setups:
        for h in HORIZONS:
            exs = []
            for sym, bars in bars_by_symbol.items():
                ind = inds[sym]
                obs = observations(sym, bars, ind, masked.get(sym, set()), members_by_date,
                                   window, (name, fires), (h,))
                if not obs:
                    continue
                tsb = ts_baseline(bars, window, h, masked.get(sym, set()))
                for o in obs:
                    xsb = xs_baseline(returns_by_key, members_by_date, o.entry_date, h)
                    if tsb is None or xsb is None:
                        continue
                    exs.append(Ex(sym, o.entry_date, h, o.ret - tsb, o.ret - xsb))
            s = summarise(exs, h)
            v = verdict(s["xs_excess"], s["xs_t"], h) if s["n"] else "no observations"
            mark = " <-- primary" if h == PRIMARY_HORIZON else ""
            print("  %-11s %5d %8d %7d %+9.3f%% %8s %+9.3f%% %8s  %s%s"
                  % (name, h, s["n"], s["dates"], 100.0 * s["xs_excess"],
                     "n/a" if s["xs_t"] is None else "%.2f" % s["xs_t"],
                     100.0 * s["ts_excess"],
                     "n/a" if s["ts_t"] is None else "%.2f" % s["ts_t"], v, mark), file=out)
    print("", file=out)
    print("Bar: cross-sectional excess > 0 with t >= %.2f at horizon %d, fixed before the run."
          % (BONFERRONI_T, PRIMARY_HORIZON), file=out)
    print("t is across entry DATES, not observations: setups cluster and forty names firing on"
          " one dip\nis one piece of evidence. Costs cancel out of both excesses, so these"
          " measure selection;\nthe hurdle line above is what selection must beat to pay for"
          " itself.", file=out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["insample", "holdout"])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--i-am-sure", action="store_true",
                    help="required to spend the reserved holdout")
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    try:
        if a.phase == "insample":
            run_phase(conn, INSAMPLE, "in-sample")
        else:
            guard_holdout(a.i_am_sure)
            run_phase(conn, (HOLDOUT_START, date.today()), "HOLDOUT -- now spent")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
