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
from collections import namedtuple
from datetime import date

# Bar is defined locally rather than imported from swing_screen.py. swing_screen.py's own Bar is
# structurally identical ("date open high low close volume"), but importing it by path drags in
# everything swing_screen.py does at module scope -- sqlite3, argparse, random, and its own
# importlib load of daily_screen.py -- just to reach a six-field namedtuple. This module's whole
# premise is "rules only, no database, no membership": a one-line local definition has the same
# guarantee of matching (namedtuples are structurally trivial to keep identical) without tying
# this file's import, or its tests' collection, to two unrelated scripts' full module bodies.
Bar = namedtuple("Bar", "date open high low close volume")

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
