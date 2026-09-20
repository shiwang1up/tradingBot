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
import math
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


def series_t(values):
    """t of the mean of a monthly series (sample variance, n-1). None for fewer than two months or
    no variance: either says nothing about whether the mean differs from zero."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def run_months(closes, masked, top_n=TOP_N, cost=None, lookback=LOOKBACK_MONTHS, skip=SKIP_MONTHS):
    """One dict per rebalance: the names held, the portfolio's gross and net return, the
    equal-weight baseline's, and the spread.

    A rank date m needs a close at m, at m-skip and at m-(lookback+skip); the portfolio is entered
    at the first trading day AFTER m and exited at the first trading day after the NEXT rank date,
    so no position is bought at a price used to rank it. The baseline holds every eligible name for
    exactly the same dates, and is charged on its own turnover, which is near zero -- momentum's
    cost disadvantage against buy-and-hold is part of what is being measured, not an artefact to
    remove."""
    if cost is None:
        cost = portfolio_cost(top_n)
    all_dates = sorted({d for by in closes.values() for d in by})
    ends = month_ends(all_dates)
    out, held, base_held = [], [], []
    for i, rank_date in enumerate(ends):
        j_skip, j_start = i - skip, i - (lookback + skip)
        if j_start < 0 or i + 1 >= len(ends):
            continue
        entry = next_trading_day(all_dates, rank_date)
        exit_ = next_trading_day(all_dates, ends[i + 1])
        if entry is None or exit_ is None:
            continue
        names = eligible(closes, masked, rank_date, ends[j_skip], ends[j_start])
        if len(names) < top_n:
            continue
        ranked = sorted(names, key=lambda s: momentum_score(
            closes[s], rank_date, ends[j_skip], ends[j_start]), reverse=True)
        target = ranked[:top_n]
        port_gross = basket_return(closes, target, entry, exit_)
        base_gross = basket_return(closes, names, entry, exit_)
        if port_gross is None or base_gross is None:
            continue
        port = port_gross - turnover_cost(held, target, cost)
        base = base_gross - turnover_cost(base_held, names, cost)
        out.append(dict(rank_date=rank_date, entry=entry, exit=exit_, held=target,
                        n_eligible=len(names), port_gross=port_gross, port=port,
                        base_gross=base_gross, base=base, spread=port - base,
                        turnover=len(set(target) - set(held))))
        held, base_held = target, names
    return out


def split_quintiles(ranked, n_groups=QUINTILES):
    """`ranked` highest first, split into `n_groups` groups with any remainder given to the top
    groups. Every name lands in exactly one group, so the groups together are the baseline."""
    n = len(ranked)
    base, extra = divmod(n, n_groups)
    groups, k = [], 0
    for g in range(n_groups):
        size = base + (1 if g < extra else 0)
        groups.append(ranked[k:k + size])
        k += size
    return groups


def _max_drawdown(returns):
    """Largest peak-to-trough fall of the cumulative curve, as a positive fraction."""
    cum, peak, dd = 1.0, 1.0, 0.0
    for r in returns:
        cum *= (1.0 + r)
        peak = max(peak, cum)
        dd = max(dd, (peak - cum) / peak)
    return dd


def summarise(months):
    """Headline figures over the monthly series."""
    n = len(months)
    if n == 0:
        return dict(months=0, mean_port=0.0, mean_base=0.0, mean_spread=0.0, t=None,
                    cum_port=0.0, cum_base=0.0, max_dd_port=0.0, max_dd_base=0.0,
                    mean_turnover=0.0, mean_eligible=0.0)
    ports = [m["port"] for m in months]
    bases = [m["base"] for m in months]
    spreads = [m["spread"] for m in months]

    def cum(rs):
        out = 1.0
        for r in rs:
            out *= (1.0 + r)
        return out - 1.0

    return dict(months=n, mean_port=sum(ports) / n, mean_base=sum(bases) / n,
                mean_spread=sum(spreads) / n, t=series_t(spreads),
                cum_port=cum(ports), cum_base=cum(bases),
                max_dd_port=_max_drawdown(ports), max_dd_base=_max_drawdown(bases),
                mean_turnover=sum(m["turnover"] for m in months) / float(n),
                mean_eligible=sum(m["n_eligible"] for m in months) / float(n))


CAVEATS = (
    "Caveats: universe.yaml is TODAY'S index, so stocks added during the window because they rose\n"
    "are present with the history of that rise (biases FOR momentum) and stocks dropped after\n"
    "falling are absent (biases against the bottom group); the net direction is unknown and could\n"
    "be material, and cannot be fixed without a point-in-time constituent list.\n"
    "Monthly observations are few for a t. The window is one long bull market plus two corrections.\n"
    "Costs assume the delivery schedule in the spec, unverified against Groww's pricing page, and\n"
    "are charged at the portfolio's real per-position value (1 lakh split across the basket), but\n"
    "they ignore market impact and lot sizes."
)


def format_run(label, s, cost):
    t = "n/a" if s["t"] is None else "%.2f" % s["t"]
    return "\n".join([
        "%s   %d months" % (label, s["months"]),
        "  portfolio   mean %+.3f%%/mo   cumulative %+.1f%%   max drawdown %.1f%%"
        % (s["mean_port"] * 100, s["cum_port"] * 100, s["max_dd_port"] * 100),
        "  baseline    mean %+.3f%%/mo   cumulative %+.1f%%   max drawdown %.1f%%"
        % (s["mean_base"] * 100, s["cum_base"] * 100, s["max_dd_base"] * 100),
        "  spread      mean %+.3f%%/mo   t %s        turnover %.1f of %d names/mo (round trip %.3f%%)"
        % (s["mean_spread"] * 100, t, s["mean_turnover"], TOP_N, cost * 100),
        "  eligible    %.1f of the universe ranked per month on average"
        % s["mean_eligible"],
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["run", "quintiles"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
