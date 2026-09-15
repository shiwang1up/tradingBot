"""Pullback-to-EMA strategy on a longer bar (spec docs/superpowers/specs/2026-09-15-pullback-15m-design.md).

Trend: EMA fast above EMA slow (below for shorts). A bar whose low touches the fast EMA in an uptrend
opens a pullback; the first pullback bar that closes back above the fast EMA with a higher low fires
LONG at its close, stop one tick under the pullback's lowest low, target at reward_risk times the
stop distance. One trade per pullback: nothing fires again until a bar exceeds the swing high recorded
when the pullback began. A pullback older than max_pullback_bars or a trend flip resets. Shorts mirror."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tradebot.strategy.base import Strategy
from tradebot.strategy.indicators import ATR, EMA
from tradebot.types import TICK, Candle, Signal, round_tick_down, round_tick_up

PRODUCTS = ("MIS", "CNC")
IDLE, PULLBACK, DONE = 0, 1, 2


@dataclass
class _State:
    fast: EMA
    slow: EMA
    atr: ATR
    phase: int = IDLE
    direction: Optional[str] = None
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    pullback_low: Optional[float] = None
    pullback_high: Optional[float] = None
    pullback_bars: int = 0
    prev_low: Optional[float] = None
    prev_high: Optional[float] = None
    ready_bars: int = 0  # bars seen with every indicator warm


def _int(params: dict, key: str) -> int:
    v = params[key]
    if isinstance(v, bool) or int(v) != v:
        raise ValueError(f"pullback.{key} must be an integer, got {v!r}")
    return int(v)


class PullbackStrategy(Strategy):
    name = "pullback"

    def __init__(self, params: dict):
        self.fast_n = _int(params, "ema_fast")
        self.slow_n = _int(params, "ema_slow")
        self.atr_n = _int(params, "atr_period")
        self.max_pullback_bars = _int(params, "max_pullback_bars")
        self.rr = float(params["reward_risk"])
        self.min_stop_pct = float(params.get("min_stop_pct", 0.1))
        self.max_stop_atr = float(params.get("max_stop_atr", 3.0))
        self.product = params.get("product", "MIS")
        if self.fast_n < 1 or self.slow_n < 1 or self.atr_n < 1:
            raise ValueError("pullback.ema_fast, ema_slow and atr_period must be >= 1")
        if self.fast_n >= self.slow_n:
            raise ValueError("pullback.ema_fast must be < pullback.ema_slow")
        if self.max_pullback_bars < 1:
            raise ValueError("pullback.max_pullback_bars must be >= 1")
        if self.rr <= 0 or self.min_stop_pct <= 0 or self.max_stop_atr <= 0:
            raise ValueError("pullback.reward_risk, min_stop_pct and max_stop_atr must be > 0")
        if self.product not in PRODUCTS:
            raise ValueError(f"pullback.product must be one of {PRODUCTS}, got {self.product!r}")
        self._state: dict = {}

    # -- Strategy interface -------------------------------------------------------------------
    def reset(self, symbol: str) -> None:
        self._state[symbol] = _State(EMA(self.fast_n), EMA(self.slow_n), ATR(self.atr_n))

    def _st(self, symbol: str) -> _State:
        if symbol not in self._state:
            self.reset(symbol)
        return self._state[symbol]

    def is_ready(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and st.ready_bars >= 2)

    def snapshot(self, symbol: str) -> dict:
        st = self._state.get(symbol)
        if not st or not (st.fast.ready and st.slow.ready and st.atr.ready):
            return {}
        f = lambda v: 0.0 if v is None else float(v)  # noqa: E731
        trend = {"LONG": 1, "SHORT": -1}.get(st.direction, 0)
        return {"ema_fast": st.fast.value, "ema_slow": st.slow.value, "atr": st.atr.value, "trend": trend, "phase": st.phase,
                "swing_high": f(st.swing_high), "swing_low": f(st.swing_low),
                "pullback_low": f(st.pullback_low), "pullback_high": f(st.pullback_high)}

    def on_candle(self, candle: Candle) -> Optional[Signal]:
        st = self._st(candle.symbol)
        fast = st.fast.update(candle.close)
        slow = st.slow.update(candle.close)
        atr = st.atr.update(candle)
        if fast is None or slow is None or atr is None:
            return None
        st.ready_bars += 1
        trend = "LONG" if fast > slow else ("SHORT" if fast < slow else None)
        signal = None
        if trend != st.direction:
            # A trend change (or the first trend) starts a clean cycle. The flip bar itself only
            # records state: it straddles the EMA by construction, so it must not arm a pullback.
            st.direction, st.phase = trend, IDLE
            st.swing_high, st.swing_low = candle.high, candle.low
            st.pullback_low = st.pullback_high = None
            st.pullback_bars = 0
        elif trend is not None and st.ready_bars >= 2:  # the first ready bar only records state
            signal = self._step(st, candle, trend, fast, atr)
        elif trend is not None:
            self._track_swing(st, candle, trend)
        st.prev_low, st.prev_high = candle.low, candle.high
        return signal

    # -- state machine ------------------------------------------------------------------------
    @staticmethod
    def _track_swing(st: _State, c: Candle, trend: str) -> None:
        if trend == "LONG":
            st.swing_high = c.high if st.swing_high is None else max(st.swing_high, c.high)
        else:
            st.swing_low = c.low if st.swing_low is None else min(st.swing_low, c.low)

    def _step(self, st: _State, c: Candle, trend: str, fast: float, atr: float) -> Optional[Signal]:
        long = trend == "LONG"
        if st.phase == IDLE:
            self._track_swing(st, c, trend)
            touched = c.low <= fast if long else c.high >= fast
            if touched:
                st.phase, st.pullback_bars = PULLBACK, 1
                st.pullback_low, st.pullback_high = c.low, c.high
            return None
        if st.phase == PULLBACK:
            st.pullback_low = min(st.pullback_low, c.low)
            st.pullback_high = max(st.pullback_high, c.high)
            st.pullback_bars += 1
            if st.pullback_bars > self.max_pullback_bars:
                st.phase = IDLE
                return None
            resumed = (c.close > fast and st.prev_low is not None and c.low > st.prev_low) if long else \
                      (c.close < fast and st.prev_high is not None and c.high < st.prev_high)
            if not resumed:
                return None
            st.phase = DONE
            return self._signal(c, trend, st.pullback_low if long else st.pullback_high, atr)
        # DONE: wait for a new extreme beyond the pre-pullback swing before another pullback can arm
        if (long and c.high > st.swing_high) or (not long and c.low < st.swing_low):
            st.phase = IDLE
            self._track_swing(st, c, trend)
        return None

    def _signal(self, c: Candle, direction: str, extreme: float, atr: float) -> Optional[Signal]:
        entry = c.close
        if direction == "LONG":
            stop = round_tick_down(extreme - TICK)
            risk = entry - stop
            target = round_tick_down(entry + risk * self.rr)
            ordered = stop < entry < target
        else:
            stop = round_tick_up(extreme + TICK)
            risk = stop - entry
            target = round_tick_up(entry - risk * self.rr)
            ordered = target < entry < stop
        if not ordered or risk < entry * self.min_stop_pct / 100.0 or risk > atr * self.max_stop_atr:
            return None
        return Signal(self.name, c.symbol, direction, entry, stop, target, self.product, c.ts)
