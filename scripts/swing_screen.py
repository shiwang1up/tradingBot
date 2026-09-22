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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["insample", "holdout"])
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    sys.exit("phase %s is not implemented yet" % a.phase)


if __name__ == "__main__":
    main()
