"""What holding the basket would have returned, so a run can be judged against not selecting.

Every statistic a report prints -- win rate, payoff, expectancy, drawdown, t -- describes the
strategy against itself. None of them answers whether picking beat not picking. On the best
in-sample run this project has produced, the answer was no by a factor of seven, and nothing in the
output said so.

This is an equal-weight basket, not an index: there are no market caps in this database, so it
cannot be capitalisation-weighted and must not be labelled as an index. It is bought once and sold
once, with no rebalancing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from tradebot.config import ChargesConfig
from tradebot.execution.charges import round_trip_charges
from tradebot.types import Candle


@dataclass(frozen=True)
class Benchmark:
    gross: float
    charges: float
    net: float
    names_held: int      # bought at least one share
    names_skipped: int   # priced, but one share cost more than the slice
    slice_value: float   # capital / priced names, before whole-share truncation


def build_benchmark(candles: Iterable[Candle], capital: float,
                    charges: Optional[ChargesConfig]) -> Optional[Benchmark]:
    """Equal-weight buy-and-hold over `candles`, or None when there is no basket to build.

    Each symbol is bought at its earliest close in `candles` and sold at its latest. A symbol with
    fewer than two candles is not a round trip and takes no slice -- counting it would shrink every
    other name's allocation. A symbol whose single share costs more than the slice cannot be bought
    at all; its slice stays in cash and is NOT redistributed, because a real buyer would not have
    bought more of the others instead.
    """
    first: dict = {}
    last: dict = {}
    counts: dict = {}
    for c in candles:
        counts[c.symbol] = counts.get(c.symbol, 0) + 1
        if c.symbol not in first or c.ts < first[c.symbol].ts:
            first[c.symbol] = c
        if c.symbol not in last or c.ts > last[c.symbol].ts:
            last[c.symbol] = c
    priced = [s for s in first if counts[s] >= 2 and first[s].close > 0]
    if not priced:
        return None
    slice_value = capital / len(priced)
    gross = charged = 0.0
    held = skipped = 0
    for sym in priced:
        buy, sell = first[sym].close, last[sym].close
        qty = math.floor(slice_value / buy)
        if qty < 1:
            skipped += 1
            continue
        held += 1
        gross += (sell - buy) * qty
        charged += round_trip_charges(buy * qty, sell * qty, charges, product="CNC")
    if held == 0:
        return None
    return Benchmark(gross=round(gross, 2), charges=round(charged, 2),
                     net=round(gross - charged, 2), names_held=held,
                     names_skipped=skipped, slice_value=slice_value)
