"""Opening-range breakout (spec docs/superpowers/specs/2026-09-19-charges-orb-regime-design.md).

The range is the high and low of the bars that start before session_open + range_minutes. After it,
the first bar that closes beyond the range fires: LONG above with the stop at the range low, SHORT
below with the stop at the range high, target at reward_risk times the stop distance (none when
reward_risk is null: the trade then ends at its stop or the session square-off). One signal per symbol
per day, whether or not it is traded. A range narrower than min_range_pct of its midpoint (costs
dominate), wider than max_range_pct (the size becomes tiny) or built from fewer bars than expected
(missing data) trades nothing that day. priority is the breakout bar's volume relative to the mean
range-bar volume, so the engine gives scarce slots to the breakouts with the most participation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from tradebot.engine.clock import date_of, ist_epoch
from tradebot.strategy.base import Strategy
from tradebot.types import Candle, Signal, round_tick_down, round_tick_up


@dataclass
class _Day:
    day: Optional[date] = None
    open_ts: int = 0
    range_end: int = 0
    high: Optional[float] = None
    low: Optional[float] = None
    bars: int = 0
    volume: float = 0.0
    complete: bool = False   # the first bar after the range has been seen
    done: bool = False       # nothing more fires today
    rel_volume: float = 0.0  # of the latest post-range bar


def _int(params: dict, key: str) -> int:
    v = params[key]
    if isinstance(v, bool) or int(v) != v:
        raise ValueError(f"orb.{key} must be an integer, got {v!r}")
    return int(v)


class OrbStrategy(Strategy):
    name = "orb"

    def __init__(self, params: dict):
        self.range_minutes = _int(params, "range_minutes")
        self.interval = _int(params, "interval_minutes")
        self.session_open = str(params["session_open"])
        rr = params.get("reward_risk")
        self.rr = None if rr is None else float(rr)
        self.min_range_pct = float(params["min_range_pct"])
        self.max_range_pct = float(params["max_range_pct"])
        self.product = params.get("product", "MIS")
        if self.interval < 1 or self.range_minutes < 1:
            raise ValueError("orb.range_minutes and orb.interval_minutes must be >= 1")
        if self.range_minutes % self.interval:
            raise ValueError("orb.range_minutes must be a multiple of orb.interval_minutes")
        if not 0 < self.min_range_pct < self.max_range_pct:
            raise ValueError("orb.min_range_pct must be > 0 and below orb.max_range_pct")
        if self.rr is not None and self.rr <= 0:
            raise ValueError("orb.reward_risk must be > 0 or null")
        if self.product != "MIS":
            raise ValueError("orb is an intraday strategy: orb.product must be MIS")
        ist_epoch(date(2000, 1, 3), self.session_open)  # raises ValueError unless it is HH:MM
        self.need = self.range_minutes // self.interval
        self._state: dict[str, _Day] = {}

    # -- Strategy interface ----------------------------------------------------
    def reset(self, symbol: str) -> None:
        self._state[symbol] = _Day()

    def is_ready(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and st.complete)

    def snapshot(self, symbol: str) -> dict[str, float]:
        st = self._state.get(symbol)
        if not st or not st.complete or st.high is None or st.low is None:
            return {}
        return {"range_high": st.high, "range_low": st.low, "range_width_pct": self._width_pct(st),
                "rel_volume": st.rel_volume}

    def on_candle(self, candle: Candle) -> Signal | None:
        st = self._state.setdefault(candle.symbol, _Day())
        day = date_of(candle.ts)
        if day != st.day:
            open_ts = ist_epoch(day, self.session_open)
            st = self._state[candle.symbol] = _Day(day=day, open_ts=open_ts,
                                                   range_end=open_ts + self.range_minutes * 60)
        if candle.ts < st.open_ts:
            return None
        if candle.ts < st.range_end:
            st.high = candle.high if st.high is None else max(st.high, candle.high)
            st.low = candle.low if st.low is None else min(st.low, candle.low)
            st.bars += 1
            st.volume += candle.volume
            return None
        if not st.complete:
            st.complete = True
            if st.bars < self.need or st.high is None or st.low is None:
                st.done = True
            elif not self.min_range_pct <= self._width_pct(st) <= self.max_range_pct:
                st.done = True
        mean_volume = st.volume / st.bars if st.bars else 0.0
        st.rel_volume = candle.volume / mean_volume if mean_volume > 0 else 0.0
        if st.done:
            return None
        close = candle.close
        if close > st.high:
            stop = round_tick_down(st.low)
            target = None if self.rr is None else round_tick_down(close + (close - stop) * self.rr)
            ok = stop < close and (target is None or close < target)
            direction = "LONG"
        elif close < st.low:
            stop = round_tick_up(st.high)
            target = None if self.rr is None else round_tick_up(close - (stop - close) * self.rr)
            ok = close < stop and (target is None or target < close)
            direction = "SHORT"
        else:
            return None
        st.done = True  # the first breakout is the only one, traded or not
        if not ok:
            return None
        return Signal(self.name, candle.symbol, direction, close, stop, target, self.product, candle.ts,
                      priority=st.rel_volume)

    @staticmethod
    def _width_pct(st: _Day) -> float:
        mid = (st.high + st.low) / 2.0
        return (st.high - st.low) / mid * 100.0 if mid > 0 else 0.0
