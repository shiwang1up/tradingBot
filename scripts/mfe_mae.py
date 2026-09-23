"""How far did price actually move our way before the exit took it back?

    .venv/bin/python scripts/mfe_mae.py --run perf2-orb60

For every closed position, walk the bars it was open for and record:

    MFE  maximum favourable excursion -- the best unrealised gain, in R
    MAE  maximum adverse excursion    -- the worst drawdown, in R

R is the risk the trade was sized against, |entry - stop| per share, so both are
directly comparable across symbols and position sizes.

WHY THIS BEFORE TESTING EXIT VARIANTS. A strategy can lose for two quite different
reasons: the entries carry no information, or the entries are fine and the exit
gives the move back. These look identical in a PnL table and completely different
here. If most trades never reach 1R in our favour, no exit rule can help and the
entry is the problem. If they do reach it and we still lose, the exit is the
problem and it is worth fixing. Measuring this costs nothing -- the bars are
already stored -- while testing exit variants costs a run each.

The "would have paid" table answers the follow-up directly: for a candidate fixed
target at X R, what would these same trades have returned?

TWO HONESTY NOTES, both of which make the table PESSIMISTIC rather than flattering:

1. Order within a bar is unknown. If a bar's high reaches the target AND its low
   reaches the stop, we cannot tell from OHLC which came first, so this assumes
   the STOP hit first. That understates every candidate target.
2. The replay is bar-resolution, so a target is filled at exactly X R and a stop
   at exactly the stop price -- no slippage beyond what the original run already
   paid, and no gap-through. Real fills are worse.

Costs are carried across from the original trade's recorded charges, which is
right for a like-for-like comparison: a different exit changes WHEN you leave,
not that you paid to get in and out once.

This reads the database read-only and writes nothing.
"""
import argparse
import sqlite3
import sys
from collections import namedtuple

DB = "data/tradebot.db"
IST_OFFSET = 19800
TARGETS = (0.5, 1.0, 1.5, 2.0, 3.0)     # candidate fixed targets, in R

Bar = namedtuple("Bar", "ts open high low close")


def load_positions(conn, run_id):
    """Closed positions with a usable risk figure. A position whose stop equals its entry has
    no R to measure against and is excluded rather than silently counted as zero risk."""
    rows = conn.execute(
        "SELECT symbol, direction, qty, avg_price, stop, opened_at, closed_at, exit_price,"
        " exit_reason, pnl, charges FROM positions"
        " WHERE run_id=? AND closed_at IS NOT NULL ORDER BY opened_at", (run_id,)).fetchall()
    out, skipped = [], 0
    for r in rows:
        if r[4] is None or abs(r[3] - r[4]) < 1e-9:
            skipped += 1
            continue
        out.append(r)
    return out, skipped


def bars_between(conn, symbol, interval, start_ts, end_ts):
    return [Bar(*r) for r in conn.execute(
        "SELECT ts, o, h, l, c FROM candles WHERE symbol=? AND interval=? AND ts>=? AND ts<=?"
        " ORDER BY ts", (symbol, interval, start_ts, end_ts)).fetchall()]


def excursions(bars, direction, entry, risk):
    """(mfe, mae) in R over `bars`. Long: favourable is up. Short: favourable is down."""
    mfe = mae = 0.0
    for b in bars:
        if direction == "LONG":
            mfe = max(mfe, (b.high - entry) / risk)
            mae = min(mae, (b.low - entry) / risk)
        else:
            mfe = max(mfe, (entry - b.low) / risk)
            mae = min(mae, (entry - b.high) / risk)
    return mfe, mae


def replay_target(bars, direction, entry, risk, target_r):
    """R this trade would have returned with a fixed target at `target_r`, walking bars in order.

    Returns (r, reason). Within a single bar that touches both the target and the stop, the STOP
    is taken first -- OHLC cannot order them, and assuming otherwise would invent profit.
    """
    for b in bars:
        if direction == "LONG":
            hit_stop = (b.low - entry) / risk <= -1.0
            hit_tgt = (b.high - entry) / risk >= target_r
        else:
            hit_stop = (entry - b.high) / risk <= -1.0
            hit_tgt = (entry - b.low) / risk >= target_r
        if hit_stop:
            return -1.0, "STOP"
        if hit_tgt:
            return target_r, "TARGET"
    last = bars[-1]
    r = ((last.close - entry) if direction == "LONG" else (entry - last.close)) / risk
    return r, "SQUARE_OFF"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--interval", type=int, default=15)
    a = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    positions, skipped = load_positions(conn, a.run)
    if not positions:
        sys.exit("no closed positions for run %r" % a.run)

    recs, no_bars = [], 0
    for (sym, dirn, qty, entry, stop, opened, closed, xprice, xreason, pnl, charges) in positions:
        bars = bars_between(conn, sym, a.interval, opened, closed)
        if not bars:
            no_bars += 1
            continue
        risk = abs(entry - stop)
        mfe, mae = excursions(bars, dirn, entry, risk)
        actual_r = (((xprice - entry) if dirn == "LONG" else (entry - xprice)) / risk)
        recs.append(dict(sym=sym, dirn=dirn, qty=qty, entry=entry, risk=risk, bars=bars,
                         mfe=mfe, mae=mae, actual_r=actual_r, reason=xreason,
                         pnl=(pnl or 0.0), charges=(charges or 0.0),
                         cost_r=((charges or 0.0) / (qty * risk)) if qty and risk else 0.0))
    conn.close()

    n = len(recs)
    print("MFE / MAE for %s -- %d closed trades" % (a.run, n))
    if skipped:
        print("  %d excluded: stop equals entry, so there is no R to measure against" % skipped)
    if no_bars:
        print("  %d excluded: no %d-minute bars stored for the holding period" % (no_bars, a.interval))
    print()

    print("how far price went OUR WAY before we exited")
    print("  %-10s %8s %8s" % ("reached", "trades", "share"))
    for t in TARGETS:
        k = sum(1 for r in recs if r["mfe"] >= t)
        print("  %-10s %8d %7.1f%%" % ("%.1fR" % t, k, 100.0 * k / n))
    print()

    ms = sorted(r["mfe"] for r in recs)
    aes = sorted(r["mae"] for r in recs)

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(p / 100.0 * len(xs)))]

    print("  MFE  median %+.2fR   75th %+.2fR   90th %+.2fR   max %+.2fR"
          % (pct(ms, 50), pct(ms, 75), pct(ms, 90), ms[-1]))
    print("  MAE  median %+.2fR   25th %+.2fR   10th %+.2fR   min %+.2fR"
          % (pct(aes, 50), pct(aes, 25), pct(aes, 10), aes[0]))
    print()

    mean_cost = sum(r["cost_r"] for r in recs) / n
    print("what each fixed target WOULD have paid, same entries, same stops")
    print("  %-8s %8s %8s %10s %10s %10s" % ("target", "hit", "stopped", "mean R", "net of cost", "vs actual"))
    actual_mean = sum(r["actual_r"] for r in recs) / n
    actual_net = actual_mean - mean_cost
    for t in TARGETS:
        rs, hits, stops = [], 0, 0
        for r in recs:
            v, why = replay_target(r["bars"], r["dirn"], r["entry"], r["risk"], t)
            rs.append(v)
            hits += why == "TARGET"
            stops += why == "STOP"
        m = sum(rs) / n
        print("  %-8s %8d %8d %+10.3f %+11.3f %+10.3f"
              % ("%.1fR" % t, hits, stops, m, m - mean_cost, (m - mean_cost) - actual_net))
    print("  %-8s %8s %8s %+10.3f %+11.3f %10s"
          % ("actual", "-", "-", actual_mean, actual_net, "-"))
    print()
    print("  mean cost %.3fR per trade, carried from the run's recorded charges" % mean_cost)
    print()
    print("Caveats: bar-resolution replay, so a target fills at exactly its price and a stop at"
          " exactly the stop.\nWhen one bar touches both, the STOP is taken first -- OHLC cannot"
          " order them, so every target\nfigure above is pessimistic rather than flattering."
          " Real fills are worse again.")


if __name__ == "__main__":
    main()
