"""EMA crossover confirmed by RSI. Stop = ATR multiple, target = reward:risk multiple."""
from __future__ import annotations

from dataclasses import dataclass

from tradebot.strategy.base import Strategy
from tradebot.strategy.indicators import ATR, EMA, RSI
from tradebot.types import Candle, Signal, round_tick


@dataclass
class _State:
    fast: EMA
    slow: EMA
    rsi: RSI
    atr: ATR
    prev_diff: float | None = None


class EmaRsiStrategy(Strategy):
    name = "ema_rsi"

    def __init__(self, params: dict):
        self.fast_n = int(params["fast"])
        self.slow_n = int(params["slow"])
        self.rsi_n = int(params["rsi_period"])
        self.rsi_long_min = float(params["rsi_long_min"])
        self.rsi_short_max = float(params["rsi_short_max"])
        self.atr_n = int(params["atr_period"])
        self.atr_mult = float(params["atr_stop_mult"])
        self.rr = float(params["reward_risk"])
        self.product = params.get("product", "MIS")
        self._state: dict[str, _State] = {}

    def _st(self, symbol: str) -> _State:
        if symbol not in self._state:
            self.reset(symbol)
        return self._state[symbol]

    def reset(self, symbol: str) -> None:
        self._state[symbol] = _State(EMA(self.fast_n), EMA(self.slow_n), RSI(self.rsi_n), ATR(self.atr_n))

    def is_ready(self, symbol: str) -> bool:
        st = self._state.get(symbol)
        return bool(st and st.fast.ready and st.slow.ready and st.rsi.ready and st.atr.ready and st.prev_diff is not None)

    def snapshot(self, symbol: str) -> dict[str, float]:
        st = self._state.get(symbol)
        if not st or not (st.fast.ready and st.slow.ready and st.rsi.ready and st.atr.ready):
            return {}
        return {"ema_fast": st.fast.value, "ema_slow": st.slow.value, "rsi": st.rsi.value, "atr": st.atr.value}

    def on_candle(self, candle: Candle) -> Signal | None:
        st = self._st(candle.symbol)
        fast = st.fast.update(candle.close)
        slow = st.slow.update(candle.close)
        rsi = st.rsi.update(candle.close)
        atr = st.atr.update(candle)
        if fast is None or slow is None or rsi is None or atr is None:
            st.prev_diff = None
            return None
        diff = fast - slow
        prev = st.prev_diff
        st.prev_diff = diff
        if prev is None:
            return None
        close = candle.close
        if prev <= 0 < diff and rsi >= self.rsi_long_min:
            stop = round_tick(close - atr * self.atr_mult)
            target = round_tick(close + (close - stop) * self.rr)
            return Signal(self.name, candle.symbol, "LONG", close, stop, target, self.product, candle.ts)
        if prev >= 0 > diff and rsi <= self.rsi_short_max:
            stop = round_tick(close + atr * self.atr_mult)
            target = round_tick(close - (stop - close) * self.rr)
            return Signal(self.name, candle.symbol, "SHORT", close, stop, target, self.product, candle.ts)
        return None


def build_strategy(name: str, params: dict) -> Strategy:
    if name == "ema_rsi":
        return EmaRsiStrategy(params)
    raise ValueError(f"unknown strategy: {name}")
