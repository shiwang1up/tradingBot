"""Screen three classic DAILY systems for expectancy in excess of a same-holding-period baseline.

    .venv/bin/python scripts/daily_screen.py fetch      # pull daily candles (needs the Groww key approved)
    .venv/bin/python scripts/daily_screen.py insample   # 2020-01-01..2023-12-31
    .venv/bin/python scripts/daily_screen.py holdout    # 2024-01-01..2025-11-21, ONCE

Rules for the three systems, the cost schedule and the pass bar are fixed in
docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md and were written down
before any daily candle was stored. They are the standard published forms of mean reversion, trend
following and breakout; they are NOT any particular author's exact rules.

Long only: a cash (CNC) short cannot be held overnight. One open trade per system per symbol; a
signal while a trade is open is ignored. A signal on day t's close enters at day t+1's OPEN; an exit
condition met on day u's close exits at day u+1's OPEN. Trade-level expectancy only: no capital,
no portfolio, no overlap limit, so these numbers say whether an edge exists, not what an account
would have made.

The round-trip cost cancels out of the reported "excess": it is subtracted from both the trade's
return and the baseline's, so excess = trade gross - baseline gross. Excess therefore measures
entry timing against a random entry of the same holding length, not profitability; the profitability
figure, net of costs, is the expectancy line.

This reads the database read-only and writes nothing to it.
"""
import argparse
import io
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict, namedtuple
from datetime import date, timedelta

DB = "data/tradebot.db"
INTERVAL = 1440
IST_OFFSET = 19800                      # daily bars are stamped 00:00 IST
INSAMPLE = (date(2020, 1, 1), date(2023, 12, 31))
HOLDOUT = (date(2024, 1, 1), date(2025, 11, 21))
HOLDOUT_FILE = "docs/superpowers/notes/daily-screen-holdout.txt"
WARMUP_BARS = 200                       # SMA(200) plus a bar to cross on
GAP_THRESHOLD = 0.20                    # an overnight move this large is an unadjusted corporate action
GAP_MASK_DAYS = 200                     # ... and the 200-day average is unusable for this long after it
INTRABAR_THRESHOLD = 0.35               # see gap_mask: only applied when the open is synthetic

# Groww DELIVERY (CNC) schedule on an assumed position value, plus slippage. CHECK THESE against
# groww.in/pricing before trusting any figure computed from them.
POSITION_VALUE = 25_000.0
BROKERAGE_PCT, BROKERAGE_MIN, BROKERAGE_MAX = 0.1, 5.0, 20.0
STT_PCT = 0.1                           # both sides on delivery
EXCHANGE_PCT = 0.00297
SEBI_PCT = 0.0001
STAMP_BUY_PCT = 0.015
GST_PCT = 18.0
DP_CHARGE = 15.34                       # depository, per sell
SLIPPAGE_PCT = 0.05                     # each side, against the trade

Bar = namedtuple("Bar", "date open high low close")


def round_trip_cost(position_value=POSITION_VALUE):
    """Round-trip cost as a FRACTION of position value: charges plus slippage on both opens."""
    def brokerage(v):
        return min(max(v * BROKERAGE_PCT / 100.0, BROKERAGE_MIN), BROKERAGE_MAX)

    v = position_value
    brok = brokerage(v) * 2
    stt = v * STT_PCT / 100.0 * 2
    exch = v * EXCHANGE_PCT / 100.0 * 2
    sebi = v * SEBI_PCT / 100.0 * 2
    stamp = v * STAMP_BUY_PCT / 100.0
    gst = (brok + exch + sebi) * GST_PCT / 100.0
    charges = brok + stt + exch + sebi + stamp + gst + DP_CHARGE
    return charges / v + 2 * SLIPPAGE_PCT / 100.0


def load_series(conn, symbols):
    """{symbol: [Bar, ...]} ascending by date, daily candles only. A symbol with no rows is absent."""
    out = {}
    for sym in symbols:
        rows = conn.execute(
            "SELECT ts, o, h, l, c FROM candles WHERE symbol=? AND interval=? ORDER BY ts",
            (sym, INTERVAL)).fetchall()
        if rows:
            out[sym] = [Bar(date.fromtimestamp(r[0] + IST_OFFSET), r[1], r[2], r[3], r[4]) for r in rows]
    return out


def gap_mask(bars, threshold=GAP_THRESHOLD, mask_days=GAP_MASK_DAYS,
             intrabar=INTRABAR_THRESHOLD):
    """Dates unusable because of an unadjusted corporate action, plus the `mask_days` trading days
    after each. An unadjusted split reads as a crash, which would manufacture mean-reversion
    entries and poison every long average while it sits in the window.

    Two detectors, because the data has two shapes.

    1. Overnight: the open jumps more than `threshold` from the previous close. This is how a split
       looks whenever the open is real, which is all of 2020-2024.
    2. Inside the bar: the close is more than `intrabar` from the open, AND the open is exactly the
       previous close. Groww's daily open is synthetic for 2025 (98.6% of bars carry the previous
       close forward), so a split there moves the close away from an open that never moved, and
       detector 1 sees nothing.

    Detector 2 is deliberately conditioned on the synthetic open rather than applied everywhere.
    The inside-the-bar shape only exists BECAUSE the open is fake; where the open is real, a split
    is an overnight gap and detector 1 has it. Conditioning also buys a much wider safety margin.
    Measured over this universe, among bars moving more than 20% from open to close: those with a
    real open top out at +37.8% (INDUSINDBK, 2020-03-26, a COVID-crash rebound) and are all genuine;
    those with a synthetic open are the five splits, from -40.2% to -89.9%, plus one genuine crash
    at -27.2% (INDUSINDBK, 2025-03-11). So the threshold has to separate 27.2% from 40.2%, and 0.35
    sits in the middle of that gap. Applied unconditionally it would instead have to separate 37.8%
    from 40.2%, a window too narrow to trust.

    Over-masking costs data; under-masking manufactures a -90% return, which is far worse."""
    masked = set()
    for i, bar in enumerate(bars):
        hit = False
        if i > 0 and bars[i - 1].close > 0:
            prev_close = bars[i - 1].close
            hit = abs(bar.open / prev_close - 1.0) > threshold
            if not hit and bar.open > 0 and abs(bar.open - prev_close) < 1e-9:
                hit = abs(bar.close / bar.open - 1.0) > intrabar
        if hit:
            for k in range(i, min(i + mask_days + 1, len(bars))):
                masked.add(bars[k].date)
    return masked


# -- indicators. Each returns a list aligned with the bars, None until warm; no lookahead: index i
# uses only bars 0..i.
def sma(values, n):
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi_wilder(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr_wilder(bars, n):
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = [0.0]
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)))
    atr = sum(trs[1:n + 1]) / n
    out[n] = atr
    for i in range(n + 1, len(bars)):
        atr = (atr * (n - 1) + trs[i]) / n
        out[i] = atr
    return out


def rolling_max_prev(values, n):
    """max of the n values BEFORE i (i excluded), None until there are n of them."""
    return [max(values[i - n:i]) if i >= n else None for i in range(len(values))]


def rolling_min_prev(values, n):
    return [min(values[i - n:i]) if i >= n else None for i in range(len(values))]


class Indicators(object):
    """Everything the three systems read, computed once per symbol."""

    def __init__(self, bars):
        self.bars = bars
        self.closes = [b.close for b in bars]
        self.sma5 = sma(self.closes, 5)
        self.sma50 = sma(self.closes, 50)
        self.sma200 = sma(self.closes, 200)
        self.rsi2 = rsi_wilder(self.closes, 2)
        self.atr20 = atr_wilder(bars, 20)
        self.max100_prev = rolling_max_prev(self.closes, 100)
        self.min50_prev = rolling_min_prev(self.closes, 50)


# -- the three systems. Long only. Entry is judged on day i's close and filled at day i+1's open;
# an exit condition met on day u's close (u strictly after the entry day) is filled at u+1's open.
def mr_entry(ind, i):
    """Mean reversion: an oversold dip inside an uptrend."""
    return (ind.sma200[i] is not None and ind.rsi2[i] is not None
            and ind.closes[i] > ind.sma200[i] and ind.rsi2[i] < 10.0)


def mr_exit(ind, i, entry_i, run_high):
    """Out on the bounce, or out anyway after 10 trading days: the edge is short-lived, and a dip
    that has not bounced by then is a downtrend, not a pullback."""
    if ind.sma5[i] is not None and ind.closes[i] > ind.sma5[i]:
        return "sma5"
    if i - entry_i >= 10:
        return "time"
    return None


def tf_entry(ind, i):
    """Trend following: the bar the 50-day average crosses above the 200-day, price above the 200."""
    if i == 0:
        return False
    prev_fast, prev_slow = ind.sma50[i - 1], ind.sma200[i - 1]
    fast, slow = ind.sma50[i], ind.sma200[i]
    if prev_fast is None or prev_slow is None or fast is None or slow is None:
        return False
    return prev_fast <= prev_slow and fast > slow and ind.closes[i] > slow


def tf_exit(ind, i, entry_i, run_high):
    """A trailing stop, not a target: the whole point is to let one winner run."""
    if ind.atr20[i] is None:
        return None
    return "trail" if ind.closes[i] < run_high - 3.0 * ind.atr20[i] else None


def bo_entry(ind, i):
    """Breakout: a new 100-day closing high, in an uptrend."""
    return (ind.max100_prev[i] is not None and ind.sma200[i] is not None
            and ind.closes[i] > ind.max100_prev[i] and ind.closes[i] > ind.sma200[i])


def bo_exit(ind, i, entry_i, run_high):
    return "min50" if ind.min50_prev[i] is not None and ind.closes[i] < ind.min50_prev[i] else None


SYSTEMS = {"mr": (mr_entry, mr_exit), "tf": (tf_entry, tf_exit), "bo": (bo_entry, bo_exit)}
SYSTEM_TITLES = {
    "mr": "mean reversion (close > SMA200, RSI(2) < 10; exit close > SMA5 or 10 days)",
    "tf": "trend following (SMA50 crosses above SMA200, close > SMA200; exit 3 x ATR(20) trail)",
    "bo": "breakout (100-day closing high, close > SMA200; exit below the 50-day closing low)",
}

Trade = namedtuple("Trade", "symbol system entry_date exit_date entry_price exit_price days "
                            "gross net reason excess")
Trade.__new__.__defaults__ = (0.0,)


def simulate(symbol, name, bars, ind, masked, window, cost, system=None, warmup=WARMUP_BARS):
    """(trades, excluded) for one system on one symbol inside one window.

    One trade at a time: a signal while a trade is open is ignored, so the count is not inflated by
    a rule that fires every day of a dip. A trade still open at the window's last bar is closed at
    that bar's open, so an in-sample trade can never reach into the holdout. A trade any of whose
    days is masked by a corporate action is dropped and counted in `excluded` rather than silently
    scoring a split as a 50% loss."""
    entry_fn, exit_fn = system if system is not None else SYSTEMS[name]
    lo, hi = window
    in_window = [i for i in range(len(bars)) if lo <= bars[i].date <= hi]
    if not in_window:
        return [], 0
    first_i, last_i = in_window[0], in_window[-1]
    trades, excluded = [], 0
    i = max(first_i, warmup)
    while i < last_i:
        if bars[i].date in masked or not entry_fn(ind, i):
            i += 1
            continue
        entry_i = i + 1
        if entry_i >= last_i:
            break
        run_high = bars[entry_i].high
        u, reason = entry_i + 1, None
        while u < last_i:
            run_high = max(run_high, bars[u].high)
            reason = exit_fn(ind, u, entry_i, run_high)
            if reason:
                break
            u += 1
        exit_i = min(u + 1, last_i)
        if reason is None:
            reason = "window_end"
        if any(bars[k].date in masked for k in range(entry_i, exit_i + 1)):
            excluded += 1
        else:
            entry_px, exit_px = bars[entry_i].open, bars[exit_i].open
            gross = (exit_px / entry_px - 1.0) if entry_px > 0 else 0.0
            trades.append(Trade(symbol, name, bars[entry_i].date, bars[exit_i].date, entry_px,
                                exit_px, exit_i - entry_i, gross, gross - cost, reason))
        i = exit_i
    return trades, excluded


def baseline_return(bars, window, h, cost, masked):
    """Mean net return of simply holding this symbol for h trading days, entered at the open of
    every date in the window whose exit also lands in it. None when no such hold fits.

    A candidate hold is skipped if ANY date from its entry bar to its exit bar inclusive is in
    `masked` -- the same corporate-action mask, and the same inclusive check, that `simulate`
    applies to a trade -- so the baseline is not polluted by an unadjusted split any more than a
    trade is allowed to be.

    Subtracts the same `cost` a trade's net return does, so that difference cancels out of the
    excess computed against this baseline and leaves excess measuring entry timing, not cost.

    Indian large caps rose over this period, so any rule that buys shows a positive return from the
    drift alone; only the excess over this baseline is evidence of an edge."""
    lo, hi = window
    rs = []
    for i in range(len(bars)):
        if not (lo <= bars[i].date <= hi) or bars[i].open <= 0:
            continue
        j = i + h
        if j < len(bars) and bars[j].date <= hi:
            if any(bars[k].date in masked for k in range(i, j + 1)):
                continue
            rs.append(bars[j].open / bars[i].open - 1.0 - cost)
    return (sum(rs) / len(rs)) if rs else None


def attach_excess(trades, bars, window, cost, masked):
    """Each trade's net return less what holding the same symbol the same number of days paid on
    average. The baseline is cached per holding length: a system's trades repeat a few lengths."""
    cache = {}
    out = []
    for t in trades:
        if t.days not in cache:
            cache[t.days] = baseline_return(bars, window, t.days, cost, masked)
        base = cache[t.days]
        out.append(t._replace(excess=t.net - (base if base is not None else 0.0)))
    return out


def date_means(trades, attr="excess"):
    """Per-entry-date means of `attr`, sorted by date for determinism. Trades entered on the same
    date are averaged into one observation before any further statistic sees them, because signals
    cluster: one market-wide dip fires mean reversion across forty names at once, and counting
    those as forty independent observations would inflate t by roughly the square root of the
    cluster size."""
    by_date = defaultdict(list)
    for tr in trades:
        by_date[tr.entry_date].append(getattr(tr, attr))
    return [sum(by_date[d]) / len(by_date[d]) for d in sorted(by_date)]


def t_across_dates(trades, attr="excess"):
    """t of the mean of `date_means`. None for fewer than 2 dates or zero variance."""
    means = date_means(trades, attr)
    n = len(means)
    if n < 2:
        return None
    mean = sum(means) / n
    var = sum((m - mean) ** 2 for m in means) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def describe(trades):
    """Rayner's block: win rate, the two averages, payoff, expectancy, the win rate this payoff
    would need to break even, plus the excess over the baseline and its t."""
    n = len(trades)
    if n == 0:
        return dict(trades=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0, payoff=None, expectancy=0.0,
                    breakeven=None, median_days=0, excess_per_trade=0.0, excess_per_date=0.0,
                    t=None, dates=0, exits={})
    wins = [t.net for t in trades if t.net > 0]
    losses = [t.net for t in trades if t.net <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = avg_win / abs(avg_loss) if avg_loss < 0 and avg_win > 0 else None
    days = sorted(t.days for t in trades)
    # Trades cluster on entry dates (one market-wide dip fires mean reversion across many
    # symbols), so the trade-weighted mean and the date-weighted mean can disagree, even in
    # sign. `t` tests the date means, and the pass bar ("excess > 0 with t >= 2") is judged on
    # excess_per_date; a large gap between the two means the result is driven by a few busy dates.
    dm = date_means(trades)
    return dict(
        trades=n,
        win_rate=len(wins) / n,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=payoff,
        expectancy=sum(t.net for t in trades) / n,
        breakeven=(1.0 / (1.0 + payoff)) if payoff is not None else None,
        median_days=days[n // 2],
        excess_per_trade=sum(t.excess for t in trades) / n,
        excess_per_date=(sum(dm) / len(dm)) if dm else 0.0,
        t=t_across_dates(trades),
        dates=len(set(t.entry_date for t in trades)),
        exits=dict(Counter(t.reason for t in trades)),
    )


def format_system(name, label, window, s, excluded, cost):
    t = "n/a" if s["t"] is None else "%.2f" % s["t"]
    payoff = "n/a" if s["payoff"] is None else "%.2f" % s["payoff"]
    breakeven = "n/a" if s["breakeven"] is None else "%.1f%%" % (s["breakeven"] * 100)
    return "\n".join([
        "%s   %s %s..%s" % (SYSTEM_TITLES[name], label, window[0], window[1]),
        "  trades %-6d win %5.1f%%   avg win %+.2f%%   avg loss %+.2f%%   payoff %s   median hold %dd"
        % (s["trades"], s["win_rate"] * 100, s["avg_win"] * 100, s["avg_loss"] * 100,
           payoff, s["median_days"]),
        "  expectancy %+.3f%% per trade (net of %.3f%% round trip)   breakeven win %s"
        % (s["expectancy"] * 100, cost * 100, breakeven),
        "  excess over baseline %+.3f%% per entry date   t %s across %d entry dates   (%+.3f%% per trade)"
        % (s["excess_per_date"] * 100, t, s["dates"], s["excess_per_trade"] * 100),
        "  exits %s   excluded by the gap mask %d"
        % (", ".join("%s %d" % kv for kv in sorted(s["exits"].items())) or "none", excluded),
    ])


CAVEATS = (
    "Caveats: universe.yaml is today's constituent list, so six years of it is survivorship-biased;\n"
    "trade-level expectancy ignores capital, overlapping positions and position sizing;\n"
    "one parameter set per system, fixed in the spec before any daily candle was stored;\n"
    "the charge rates are from the spec and have not been checked against Groww's pricing page."
)


def run_phase(conn, symbols, window, label, out=sys.stdout):
    cost = round_trip_cost()
    lo, hi = window
    series = load_series(conn, symbols)
    missing = [s for s in symbols if s not in series]
    print("%d symbols with daily candles%s"
          % (len(series), ("; no candles for " + ", ".join(missing)) if missing else ""), file=out)
    prepared = {}
    gaps = {}
    for sym, bars in series.items():
        # Full-history mask: the simulation itself must still catch a trade near the window edge
        # that reaches just outside it, so this is not restricted to the window.
        masked = gap_mask(bars)
        prepared[sym] = (bars, Indicators(bars), masked)
        in_window = sum(1 for d in masked if lo <= d <= hi)
        if in_window:
            gaps[sym] = in_window
    print("corporate-action gaps masked: %s"
          % (", ".join("%s %d dates" % kv for kv in sorted(gaps.items())) if gaps else "none"), file=out)
    print("", file=out)
    for name in ("mr", "tf", "bo"):
        trades, excluded = [], 0
        for sym in sorted(prepared):
            bars, ind, masked = prepared[sym]
            got, ex = simulate(sym, name, bars, ind, masked, window, cost)
            trades.extend(attach_excess(got, bars, window, cost, masked))
            excluded += ex
        print(format_system(name, label, window, describe(trades), excluded, cost), file=out)
        print("", file=out)
    print(CAVEATS, file=out)


def guard_holdout():
    """The holdout is worth one look. A second look at the same window, after seeing the first, is
    tuning with extra steps."""
    if os.path.exists(HOLDOUT_FILE):
        sys.exit("%s exists: the holdout has already been run and is meant to be looked at once. "
                 "Read that file instead." % HOLDOUT_FILE)


def fetch_daily(db_path):
    """Pull daily candles from 2020-01-01 for the universe. Needs the Groww key approved today.
    No session filter: daily bars are stamped 00:00 IST and SessionClock would drop every one."""
    import time
    from tradebot.config import load_config
    from tradebot.data.historical import fetch_incremental
    from tradebot.data.universe import load_universe
    from tradebot.execution.groww_adapter import GrowwAdapter
    from tradebot.store.db import connect
    from tradebot.store.repo import Repo

    cfg = load_config("config.yaml")
    uni = load_universe(cfg.paths.universe)
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret,
                           cfg.secrets.groww_api_secret)
    adapter.client
    repo = Repo(connect(db_path))
    now = int(time.time())
    lookback = (date.today() - date(2020, 1, 1)).days + 1

    def throttled(sym, exch, start, end, interval):
        try:
            return adapter.fetch_candles(sym, exch, start, end, interval)
        finally:
            time.sleep(0.4)

    total, failed = 0, []
    for i, sym in enumerate(uni.symbols, 1):
        try:
            n = fetch_incremental(repo, throttled, [sym], uni.exchange, INTERVAL, lookback, now,
                                  keep=None)[sym]
            total += n
            print("[%d/%d] %s: +%d" % (i, len(uni.symbols), sym, n))
        except Exception as e:                      # noqa: BLE001 - isolate per symbol
            failed.append(sym)
            print("[%d/%d] %s: FAILED %s: %s" % (i, len(uni.symbols), sym, type(e).__name__, str(e)[:160]))
    print("done: %d daily candles inserted; failed: %s" % (total, failed or "none"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["fetch", "insample", "holdout"])
    ap.add_argument("--db", default=DB, help="read-only for insample and holdout")
    a = ap.parse_args()
    if a.phase == "fetch":
        fetch_daily(a.db)
        return
    if a.phase == "holdout":
        guard_holdout()
    from tradebot.config import load_config
    from tradebot.data.universe import load_universe
    symbols = list(load_universe(load_config("config.yaml").paths.universe).symbols)
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    try:
        if a.phase == "insample":
            run_phase(conn, symbols, INSAMPLE, "in-sample")
        else:
            buf = io.StringIO()
            run_phase(conn, symbols, HOLDOUT, "holdout", out=buf)
            with open(HOLDOUT_FILE, "w") as fh:
                fh.write(buf.getvalue())
            print(buf.getvalue())
            print("written to %s; this window is now spent" % HOLDOUT_FILE)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
