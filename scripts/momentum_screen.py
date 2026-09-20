"""Does ranking these 50 stocks against EACH OTHER predict, where ranking a stock against its own
past did not?

    .venv/bin/python scripts/momentum_screen.py run         # the primary test and the secondary grid
    .venv/bin/python scripts/momentum_screen.py quintiles   # the five ranked groups, monthly means

Cross-sectional momentum: at each month end rank every eligible symbol by its return over the
twelve months ending ONE month earlier (the skipped month is standard; short-horizon reversal
contaminates it), equal-weight the top 10, hold one month, rebalance. The comparator is
equal-weighting ALL eligible symbols over the same month, because the claim being tested is that
the top group beats the average stock -- not that it beats zero, which in a market that doubled
would prove nothing.

CLOSES ONLY. Groww's daily `open` field is synthetic for 2025 (98.6% of bars carry the previous
close forward), so any fill at an open would be fictional over half the window. Ranking on a
month-end close and entering at the NEXT day's close is also the standard construction.

Costs are charged on TURNOVER: if k of the 10 names change at a rebalance, the month pays
k/10 of a round trip. The baseline pays on its own turnover, which is near zero. That asymmetry is
real -- momentum's cost disadvantage against buy-and-hold is part of what is being measured.

Survivorship warning, stated here because it is the largest threat to any positive result:
universe.yaml is TODAY'S index. Stocks added during the window because they rose are present with
the full history of the rise that earned them a place, which manufactures momentum. Stocks dropped
after falling are absent, which flatters the bottom of the ranking. A pass means "worth
investigating with point-in-time data", never "proven".

This reads the database read-only and writes nothing to it.
"""
import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path

DB = "data/tradebot.db"
FIRST_RANK_MONTH = (2021, 1)            # the first month with 13 months of history behind it
TOP_N = 10                              # a quintile of 50; five names is too concentrated at 1 lakh
LOOKBACK_MONTHS = 12                    # the "12" of 12-1
SKIP_MONTHS = 1                         # the "-1"
QUINTILES = 5

# The loader, the cost model and the corporate-action mask are shared with the daily screen rather
# than duplicated: one definition of a daily bar, of what a round trip costs, and of a split.
_DS = Path(__file__).resolve().parent / "daily_screen.py"
_spec = importlib.util.spec_from_file_location("daily_screen", _DS)
daily_screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_screen)
round_trip_cost = daily_screen.round_trip_cost
gap_mask = daily_screen.gap_mask
load_series = daily_screen.load_series


def load_closes(conn, symbols):
    """({symbol: {date: close}}, {symbol: [Bar]}) from daily candles. The bars are kept only so the
    corporate-action mask can look at opens; every return in this screen uses closes."""
    bars = load_series(conn, symbols)
    closes = {sym: {b.date: b.close for b in bs} for sym, bs in bars.items()}
    return closes, bars


def month_ends(dates):
    """The last available trading day of each calendar month, in order. Derived from the dates that
    exist, so a holiday or weekend at a month boundary just moves the month end earlier."""
    last = {}
    for d in dates:
        last[(d.year, d.month)] = max(d, last.get((d.year, d.month), d))
    return [last[k] for k in sorted(last)]


def momentum_score(closes_for_symbol, rank_date, skip_date, start_date):
    """12-1 momentum: the return from `start_date` to `skip_date`, i.e. the twelve months ending one
    month before the rank date. `rank_date` is accepted so the caller's intent is readable and so a
    symbol missing its rank-date close is rejected here rather than later."""
    a = closes_for_symbol.get(start_date)
    b = closes_for_symbol.get(skip_date)
    if a is None or b is None or closes_for_symbol.get(rank_date) is None or a <= 0:
        return None
    return b / a - 1.0


def eligible(closes, masked, rank_date, skip_date, start_date):
    """Symbols that may be ranked at `rank_date`, sorted for determinism. A symbol qualifies only
    if it has closes at all three legs and no corporate action anywhere in [start_date, rank_date]:
    an unadjusted split inside the lookback makes the score meaningless, and one at the rank date
    makes the entry price meaningless. The same list feeds BOTH the ranked portfolio and the
    baseline, so the two always compare the same candidate set."""
    out = []
    for sym, by_date in closes.items():
        if momentum_score(by_date, rank_date, skip_date, start_date) is None:
            continue
        if any(start_date <= d <= rank_date for d in masked.get(sym, ())):
            continue
        out.append(sym)
    return sorted(out)


def next_trading_day(all_dates, after):
    """The first trading day strictly after `after`, or None. Entering on the rank date itself
    would buy at a price that was used to rank the name. `all_dates` must be sorted ascending; the
    first date past `after` is returned, so an unsorted list would give the wrong day."""
    for d in all_dates:
        if d > after:
            return d
    return None


def turnover_cost(held, target, cost):
    """The month's cost as a fraction of the portfolio: the share of names replaced, times a round
    trip. Selling one name and buying another is one round trip between them, so the share that
    changed is the right multiplier. An unchanged basket costs nothing; the first month costs a
    full round trip because everything is bought."""
    if not target:
        return 0.0
    changed = len(set(target) - set(held))
    return cost * changed / float(len(target))


def basket_return(closes, names, entry_date, exit_date):
    """Equal-weight return of `names` from `entry_date`'s close to `exit_date`'s close, before
    costs. None if any name lacks either close, because a silently smaller basket would not be the
    portfolio the ranking chose."""
    if not names:
        return None
    rs = []
    for sym in names:
        a = closes[sym].get(entry_date)
        b = closes[sym].get(exit_date)
        if a is None or b is None or a <= 0:
            return None
        rs.append(b / a - 1.0)
    return sum(rs) / len(rs)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["run", "quintiles"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
