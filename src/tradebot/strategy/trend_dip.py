"""Dip inside a long uptrend, on daily bars, held as delivery (CNC).

Setup: SMA(50) > SMA(200) (the long trend is up), close < SMA(20) (this bar is a dip) and
close > SMA(50) (a dip, not a breakdown). Long only.

WHAT THE SCREEN MEASURED, AND WHAT THIS IS NOT
----------------------------------------------
This entry rule is the only one of six that cleared the swing screen's pre-registered bar:
+0.618% per date at h=60, t 2.96, permutation p 0.0005 over 2,000 draws. Two things must be
said about that number before anyone reads a backtest of this module next to it.

1. The measured edge, 0.216%/mo, is BELOW the 0.238%/mo cost hurdle at eight positions. On its
   own numbers the setup does not clear its costs. The holdout (2024-01-01 onward) is unspent,
   so this is a candidate, not a validated strategy.

2. **This module is not the screen's construction.** The screen measured an unsized, unstopped,
   equal-weight name-day return over a fixed 60-day horizon. A strategy the engine can run must
   size by risk and must carry a stop, so this one adds a 2 x ATR(20) stop and is sized off that
   stop distance by the risk engine. That makes it a DIFFERENT STRATEGY THAT SHARES AN ENTRY
   RULE. Its backtested PnL is not comparable to +0.618%/date and does not validate, refute or
   replicate the screen. Anyone reading the two together must not conclude otherwise.

Exit: no target; the position is closed after `max_hold_bars` bars (60, the screen's horizon)
by the backtest broker's time exit, or earlier on the stop.

The 20/50/200 periods and the 60-bar hold were fixed before the screen ran. The screen's grid
showed neighbouring parameters scoring higher; adopting one of those after the fact would be
curve-fitting and would invalidate the measurement, so they stay as they are.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from tradebot.strategy.base import Strategy
from tradebot.strategy.indicators import ATR
from tradebot.types import Candle, Signal, round_tick_down

PRODUCTS = ("CNC",)


class SMA:
    """Simple moving average over a fixed window. Local to this module: `strategy/indicators.py`
    carries EMA, RSI and ATR only, and adding to it is not in this change's scope."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError(f"SMA period must be >= 1, got {period}")
        self.period = period
        self._window: Deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self.value: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> Optional[float]:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(x)
        self._sum += x
        # Recomputed from the window, not carried: a running sum drifts over thousands of bars.
        self.value = sum(self._window) / self.period if len(self._window) == self.period else None
        return self.value


@dataclass
class _State:
    fast: SMA
    mid: SMA
    slow: SMA
    atr: ATR
    ready_bars: int = 0
    last: Optional[Candle] = field(default=None, repr=False)


def _int(params: dict, key: str, default: int) -> int:
    v = params.get(key, default)
    if isinstance(v, bool) or int(v) != v:
        raise ValueError(f"trend_dip.{key} must be an integer, got {v!r}")
    return int(v)


class TrendDipStrategy(Strategy):
    name = "trend_dip"

    def __init__(self, params: dict):
        self.fast_n = _int(params, "sma_fast", 20)
        self.mid_n = _int(params, "sma_mid", 50)
        self.slow_n = _int(params, "sma_slow", 200)
        self.atr_n = _int(params, "atr_period", 20)
        self.max_hold_bars = _int(params, "max_hold_bars", 60)
        self.atr_stop_mult = float(params.get("atr_stop_mult", 2.0))
        self.product = params.get("product", "CNC")
        if min(self.fast_n, self.mid_n, self.slow_n, self.atr_n) < 1:
            raise ValueError("trend_dip sma and atr periods must be >= 1")
        if not self.fast_n < self.mid_n < self.slow_n:
            raise ValueError("trend_dip.sma_fast must be < sma_mid must be < sma_slow")
        if self.max_hold_bars < 1:
            raise ValueError("trend_dip.max_hold_bars must be >= 1")
        if self.atr_stop_mult <= 0:
            raise ValueError("trend_dip.atr_stop_mult must be > 0")
        if self.product not in PRODUCTS:
            raise ValueError(f"trend_dip.product must be one of {PRODUCTS}, got {self.product!r}")
        self._state: dict = {}

    # -- Strategy interface -------------------------------------------------------------------
    def reset(self, symbol: str) -> None:
        self._state[symbol] = _State(SMA(self.fast_n), SMA(self.mid_n), SMA(self.slow_n), ATR(self.atr_n))

    def _st(self, symbol: str) -> _State:
        if symbol not in self._state:
            self.reset(symbol)
        return self._state[symbol]

    def is_ready(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and st.ready_bars >= 1)

    def snapshot(self, symbol: str) -> dict:
        st = self._state.get(symbol)
        if not st or st.ready_bars < 1:  # base contract: empty until ready
            return {}
        return {"sma_fast": st.fast.value, "sma_mid": st.mid.value, "sma_slow": st.slow.value,
                "atr": st.atr.value}

    def on_candle(self, candle: Candle) -> Optional[Signal]:
        st = self._st(candle.symbol)
        fast = st.fast.update(candle.close)
        mid = st.mid.update(candle.close)
        slow = st.slow.update(candle.close)
        atr = st.atr.update(candle)
        st.last = candle
        if fast is None or mid is None or slow is None or atr is None:
            return None  # warming up: the slow SMA needs its full window before anything fires
        st.ready_bars += 1
        uptrend = mid > slow
        dip = candle.close < fast
        not_a_breakdown = candle.close > mid
        if not (uptrend and dip and not_a_breakdown):
            return None
        entry = candle.close
        stop = round_tick_down(entry - self.atr_stop_mult * atr)
        if stop <= 0 or stop >= entry:
            return None  # a stop at or through the entry cannot be sized
        return Signal(self.name, candle.symbol, "LONG", entry, stop, None, self.product, candle.ts,
                      max_hold_bars=self.max_hold_bars)
