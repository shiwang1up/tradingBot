"""Per-name slippage, from the name's own traded value.

The flat 5 bps used until now was calibrated on NIFTY 50 large caps. NIFTY 200 includes
midcaps whose spreads are several times wider, and momentum premia are largest in exactly
those names, so a flat rate would flatter the thin end of the universe most.

At the position sizes this project trades -- 1 lakh across ten names is 10,000 each, about
0.00065% of daily turnover in even the thinnest current name -- this is the BID-ASK SPREAD,
not market impact. Traded value is a proxy for spread, not a measurement of it. See section
5.3 of the spec: the tiers are unvalidated against quote data, and may not be changed after
seeing a result.
"""
import statistics
from datetime import date
from typing import Optional

INTERVAL = 1440
IST_OFFSET = 19800
WINDOW = 60                                   # trading days of history behind the estimate
CRORE = 1e7

# (minimum median daily traded value in rupees, percent slippage per side), richest first.
TIERS = (
    (500 * CRORE, 0.05),
    (100 * CRORE, 0.08),
    (25 * CRORE, 0.15),
    (0.0, 0.30),
)
UNKNOWN_SLIPPAGE_PCT = 0.30                   # never the cheapest tier


def slippage_pct_for_value(value):
    """Percent per side for a median daily traded value in rupees. None means no history,
    which is charged at the most conservative tier rather than assumed liquid."""
    if value is None:
        return UNKNOWN_SLIPPAGE_PCT
    for floor, pct in TIERS:
        if value >= floor:
            return pct
    return UNKNOWN_SLIPPAGE_PCT


def median_traded_value(conn, symbol, as_of, window=WINDOW, interval=INTERVAL):
    """Median of close x volume over the `window` daily bars ending the day BEFORE `as_of`.
    None when the symbol has no bars in that span. Strictly before `as_of` so an estimate
    never uses the day it is applied to."""
    # Daily bars are stamped 00:00 IST, so the bar FOR `as_of` sits at exactly this ts.
    # Using a strict < is the whole guard: an estimate must never see the day it prices.
    as_of_ts = (as_of - date(1970, 1, 1)).days * 86400 - IST_OFFSET
    rows = conn.execute(
        "SELECT c * v FROM candles WHERE symbol=? AND interval=? AND ts<? AND v>0"
        " ORDER BY ts DESC LIMIT ?", (symbol, interval, as_of_ts, window)).fetchall()
    vals = [r[0] for r in rows if r[0] is not None and r[0] > 0]
    if not vals:
        return None
    return statistics.median(vals)


def slippage_for(conn, symbol, as_of, window=WINDOW, interval=INTERVAL):
    """Percent per side for `symbol` on `as_of`."""
    return slippage_pct_for_value(median_traded_value(conn, symbol, as_of, window, interval))


def describe_tiers():
    """One line per tier, for printing beside any result that used them."""
    out = ["slippage tiers (percent per side, by median daily traded value):"]
    prev = None
    for floor, pct in TIERS:
        if prev is None:
            out.append("  >= %5.0f cr   %.2f%%" % (floor / CRORE, pct))
        else:
            out.append("  %5.0f-%5.0f cr  %.2f%%" % (floor / CRORE, prev / CRORE, pct))
        prev = floor
    out.append("  unvalidated against quote data; see spec 5.3")
    return "\n".join(out)
