"""Does any exit rule beat the one the entry screen actually validated?

    .venv/bin/python scripts/exit_screen.py insample

swing_screen.py measured ENTRIES with a pure horizon exit and no stop, and trend_dip cleared its
bar that way. config-daily.yaml then trades something else: it adds a 2xATR stop that no screen has
ever measured, and in the 2020-2023 backtest that stop produced 70 of 112 exits. The majority of
the traded system's behaviour comes from a component nobody tested.

The entry is FIXED at trend_dip 20/50/200 and is not varied here. Only the exit changes.

Every cell sees identical entries, so cells are compared as a PAIRED per-date difference against
hold60. The pairing is what makes this measurable: it removes the market variance that would
otherwise swamp an exit effect, and an unpaired test at this sample size would have little power.

DISCLOSURE: hold60 versus stop2atr has already been seen informally (+3.97% vs +1.30% mean per
trade over 112 backtest trades). That cell is confirmatory, not exploratory. The untested cells are
stop3atr, stop4atr, trail3atr and target2r_stop2atr.

Entry is at the signal bar's close, matching swing_screen's forward_return so hold60 IS the design
that passed. The ENGINE fills a bar later, at the next bar's close. That divergence is known, it
cancels out of a paired comparison, and it is not fixed here.

This reads the database read-only and writes nothing.
"""
import importlib.util
from collections import namedtuple
from datetime import date
from pathlib import Path

# Bar is defined locally rather than imported from swing_screen.py. swing_screen.py's own Bar is
# structurally identical ("date open high low close volume"), but importing it by path drags in
# everything swing_screen.py does at module scope -- sqlite3, argparse, random, and its own
# importlib load of daily_screen.py -- just to reach a six-field namedtuple. This module's whole
# premise is "rules only, no database, no membership": a one-line local definition has the same
# guarantee of matching (namedtuples are structurally trivial to keep identical) without tying
# this file's import, or its tests' collection, to two unrelated scripts' full module bodies.
Bar = namedtuple("Bar", "date open high low close volume")

# Universe loading, the corporate-action mask, point-in-time membership, the entry itself and the
# cost model come from swing_screen.py rather than being written twice -- this screen fixes the
# same entry (trend_dip) the entry screen validated and must see it exactly as that screen did.
# Loaded by path, the same way swing_screen.py itself loads daily_screen.py: scripts/ is not a
# package.
_SW = Path(__file__).resolve().parent / "swing_screen.py"
_sw_spec = importlib.util.spec_from_file_location("swing_screen", _SW)
swing_screen = importlib.util.module_from_spec(_sw_spec)
_sw_spec.loader.exec_module(swing_screen)
Indicators = swing_screen.Indicators
load_universe = swing_screen.load_universe
is_member = swing_screen.is_member
hold_is_clean = swing_screen.hold_is_clean
trend_dip = swing_screen.trend_dip
date_means = swing_screen.date_means
t_of_means = swing_screen.t_of_means
round_trip_cost = swing_screen.round_trip_cost      # re-exported by swing_screen from daily_screen

HORIZON = 60                      # trading days; the entry screen's primary cell
INCUMBENT = "hold60"
BONFERRONI_T = 2.58               # five challengers at alpha 0.05
INSAMPLE = (date(2020, 1, 1), date(2023, 12, 31))
HOLDOUT_START = date(2024, 1, 1)  # reserved; see guard_holdout

Exit = namedtuple("Exit", "index price reason")


def _horizon_exit(bars, i, horizon):
    """The close of the bar `horizon` trading days after entry, or None if it doesn't exist."""
    j = i + horizon
    return Exit(j, bars[j].close, "HOLD") if j < len(bars) else None


def _stop_exit(bars, i, atr, horizon, mult):
    """A fixed stop `mult` x ATR below the entry close, else the horizon exit.

    The fill is clamped to the bar's open exactly as check_exit does in execution/backtest.py: a
    bar that gaps through the stop fills at the open, worse than the stop. Without the clamp a gap
    would book a smaller loss than the one actually taken.

    The scan only ever looks at bars that exist (bounded by min(i + horizon, len(bars) - 1)): an
    early stop needs only the bars up to where it triggers, not the full horizon. Only the
    fallback -- no trigger seen in the available bars -- defers to _horizon_exit, which is what
    actually requires the full horizon and returns None when it is not there. That is also why a
    signal too near the end of the data is dropped rather than counted as a flat trade: the
    horizon bar the HOLD fallback would need simply is not in the series.
    """
    stop = bars[i].close - mult * atr
    for k in range(i + 1, min(i + horizon, len(bars) - 1) + 1):
        if bars[k].low <= stop:
            return Exit(k, min(bars[k].open, stop), "STOP")
    return _horizon_exit(bars, i, horizon)


def _trail_exit(bars, i, atr, horizon, mult):
    """A trailing stop `mult` x ATR below the highest CLOSE since entry, else the horizon exit.

    Trails on closes, not highs, so a single spiky high cannot ratchet the trail somewhere price
    never closed. The trail is recomputed from the closes seen strictly BEFORE the bar under test
    -- a bar's own low can be touched before its close is known -- and only ever rises. The fill
    clamps to the bar's open exactly as _stop_exit's gap clamp, and the scan is bounded the same
    way, for the same reason: an early trail exit needs only the bars up to where it triggers.
    """
    highest_close = bars[i].close
    for k in range(i + 1, min(i + horizon, len(bars) - 1) + 1):
        trail = highest_close - mult * atr
        if bars[k].low <= trail:
            return Exit(k, min(bars[k].open, trail), "TRAIL")
        highest_close = max(highest_close, bars[k].close)
    return _horizon_exit(bars, i, horizon)


def _target_exit(bars, i, atr, horizon):
    """2xATR stop below entry, target at 2x that initial risk above entry, else the horizon exit.

    A stop and a target hit on the same bar resolve as a stop, matching STOP_FIRST_ON_SAME_BAR in
    execution/backtest.py, so the screen and the engine cannot disagree about an ambiguous bar.
    The stop fill clamps to min(open, stop) and the target fill to max(open, target) -- worse and
    better respectively, exactly as check_exit's two clamps. Scan bounded as in _stop_exit.
    """
    stop = bars[i].close - 2.0 * atr
    risk = 2.0 * atr
    target = bars[i].close + 2.0 * risk
    for k in range(i + 1, min(i + horizon, len(bars) - 1) + 1):
        stop_hit = bars[k].low <= stop
        target_hit = bars[k].high >= target
        if stop_hit:                       # STOP_FIRST_ON_SAME_BAR
            return Exit(k, min(bars[k].open, stop), "STOP")
        if target_hit:
            return Exit(k, max(bars[k].open, target), "TARGET")
    return _horizon_exit(bars, i, horizon)


EXITS = {
    "hold60": lambda bars, i, atr, horizon=HORIZON: _horizon_exit(bars, i, horizon),
    "stop2atr": lambda bars, i, atr, horizon=HORIZON: _stop_exit(bars, i, atr, horizon, 2.0),
    "stop3atr": lambda bars, i, atr, horizon=HORIZON: _stop_exit(bars, i, atr, horizon, 3.0),
    "stop4atr": lambda bars, i, atr, horizon=HORIZON: _stop_exit(bars, i, atr, horizon, 4.0),
    "trail3atr": lambda bars, i, atr, horizon=HORIZON: _trail_exit(bars, i, atr, horizon, 3.0),
    "target2r_stop2atr": lambda bars, i, atr, horizon=HORIZON: _target_exit(bars, i, atr, horizon),
}


Obs = namedtuple("Obs", "symbol entry_date cell ret")


def observations(uni, window):
    """One Obs per (symbol, entry_date, cell), for every member name-day `trend_dip` fires on in
    `window`, net of round-trip cost.

    THE INVARIANT LIVES HERE, not in the rules. The six rules do not agree among themselves about
    when an outcome is measurable -- near the end of a symbol's history a stop can trigger on
    bars a plain 60-bar hold can never reach (see the module docstring's live example: hold60 and
    stop4atr return None there while the other four resolve). So every one of the six cells is
    evaluated for EVERY candidate name-day first, and only afterward is a verdict reached: if even
    one cell returns None, the whole name-day is dropped from every cell -- never entered into the
    five that could resolve it and withheld only from the one that could not. That is what keeps
    the cells paired: every (symbol, entry_date) pair that appears under one cell in the output
    appears under all six, so a per-date difference is always the same trade under two exits,
    never a resolved trade compared against nothing.

    The corporate-action check runs over the full HORIZON, not over whichever bar a particular
    cell happens to exit on: an exit rule that resolves early must still reject a hold spanning a
    split that lands later inside the horizon, because the ENTRY itself was chosen on prices the
    split makes fiction. Checking each cell against only its own (possibly short) realised hold
    would let a split-tainted entry survive in the cells that exit before the split and vanish
    from the ones that don't -- unpairing them exactly like an unresolved None would.
    """
    lo, hi = window
    cost = round_trip_cost()
    out = []
    for symbol, bars in uni.bars.items():
        ind = uni.inds[symbol]
        masked = uni.masked.get(symbol, set())
        for i, bar in enumerate(bars):
            d = bar.date
            if not (lo <= d <= hi) or not is_member(uni.members_by_date, symbol, d):
                continue
            if not trend_dip(ind, i):
                continue
            atr = ind.atr20[i]
            if atr is None:
                continue
            if not hold_is_clean(bars, i, HORIZON, masked):
                continue
            exits = {name: rule(bars, i, atr, HORIZON) for name, rule in EXITS.items()}
            if any(exit_ is None for exit_ in exits.values()):
                continue                                   # the drop: see the docstring above
            entry_close = bar.close
            for name, exit_ in exits.items():
                out.append(Obs(symbol, d, name, exit_.price / entry_close - 1.0 - cost))
    return out


def paired_diffs(obs):
    """{challenger cell: [per-date mean challenger return - per-date mean hold60 return, ...]},
    one list per non-incumbent cell, in entry-date order -- ready to hand straight to
    `t_of_means`, which is why this writes no statistics of its own beyond the subtraction.

    Built from `date_means` rather than a hand-rolled group-by, exactly like `paired_diffs`'s
    caller will use `t_of_means` rather than a hand-rolled t. `date_means` also does the
    within-date averaging: an entry date where the setup fired on several names is one point in
    the paired series, not several, matching how swing_screen treats a clustered date as one
    piece of evidence.

    The zip below only lines up correctly because `observations()` already guarantees every
    challenger cell has observations on exactly the same set of entry dates as hold60 -- that is
    the whole point of the drop. The assert doesn't add that guarantee; it makes it loud if
    something upstream (a change to observations(), or a caller passing in an unpaired list)
    ever breaks it, instead of silently pairing a challenger's July with hold60's August.
    """
    incumbent = [o for o in obs if o.cell == INCUMBENT]
    incumbent_dates = sorted(set(o.entry_date for o in incumbent))
    incumbent_means = date_means(incumbent, "ret")
    out = {}
    for cell in EXITS:
        if cell == INCUMBENT:
            continue
        challenger = [o for o in obs if o.cell == cell]
        challenger_dates = sorted(set(o.entry_date for o in challenger))
        assert challenger_dates == incumbent_dates, (
            "%s and %s disagree on entry dates -- observations() is supposed to make that "
            "impossible" % (cell, INCUMBENT))
        challenger_means = date_means(challenger, "ret")
        out[cell] = [c - h for c, h in zip(challenger_means, incumbent_means)]
    return out
