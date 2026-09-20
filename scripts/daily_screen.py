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

This reads the database read-only and writes nothing to it.
"""
import argparse
import math
import sqlite3
import sys
from collections import defaultdict, namedtuple
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


def gap_mask(bars, threshold=GAP_THRESHOLD, mask_days=GAP_MASK_DAYS):
    """Dates unusable because of an unadjusted corporate action: any bar whose open is more than
    `threshold` away from the previous close, and the `mask_days` trading days after it. An
    unadjusted split reads as a crash, which would manufacture mean-reversion entries, and it
    poisons every long average for as long as it stays in the window."""
    masked = set()
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        if prev_close > 0 and abs(bars[i].open / prev_close - 1.0) > threshold:
            for k in range(i, min(i + mask_days + 1, len(bars))):
                masked.add(bars[k].date)
    return masked


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["fetch", "insample", "holdout"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
