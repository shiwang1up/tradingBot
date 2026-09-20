"""EMA crossover confirmed by RSI. Stop = ATR multiple, target = reward:risk multiple."""
from __future__ import annotations

from dataclasses import dataclass

from tradebot.strategy.base import Strategy
from tradebot.strategy.indicators import ATR, EMA, RSI
from tradebot.types import Candle, Signal, round_tick_down, round_tick_up

PRODUCTS = ("MIS", "CNC")


@dataclass
class _State:
    fast: EMA
    slow: EMA
    rsi: RSI
    atr: ATR
    prev_diff: float | None = None


def _int(params: dict, key: str) -> int:
    v = params[key]
    if isinstance(v, bool) or int(v) != v:
        raise ValueError(f"ema_rsi.{key} must be an integer, got {v!r}")
    return int(v)


class EmaRsiStrategy(Strategy):
    name = "ema_rsi"

    def __init__(self, params: dict):
        self.fast_n = _int(params, "fast")
        self.slow_n = _int(params, "slow")
        self.rsi_n = _int(params, "rsi_period")
        self.rsi_long_min = float(params["rsi_long_min"])
        self.rsi_short_max = float(params["rsi_short_max"])
        self.atr_n = _int(params, "atr_period")
        self.atr_mult = float(params["atr_stop_mult"])
        self.rr = float(params["reward_risk"])
        self.min_stop_pct = float(params.get("min_stop_pct", 0.1))  # percent of entry
        self.product = params.get("product", "MIS")
        if self.fast_n >= self.slow_n:
            raise ValueError("ema_rsi.fast must be < ema_rsi.slow")
        if self.atr_mult <= 0 or self.rr <= 0 or self.min_stop_pct <= 0:
            raise ValueError("ema_rsi.atr_stop_mult, reward_risk and min_stop_pct must be > 0")
        if self.rsi_long_min <= self.rsi_short_max:
            raise ValueError("ema_rsi.rsi_long_min must be > ema_rsi.rsi_short_max")
        if self.product not in PRODUCTS:
            raise ValueError(f"ema_rsi.product must be one of {PRODUCTS}, got {self.product!r}")
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
        # A stop closer than min_stop_pct of price (or a zero ATR) is noise: slippage alone would
        # exceed it, and sizing would balloon to the margin limit. Emit nothing.
        if atr * self.atr_mult < close * self.min_stop_pct / 100.0:
            return None
        if prev <= 0 < diff and rsi >= self.rsi_long_min:
            stop = round_tick_down(close - atr * self.atr_mult)     # away from entry
            target = round_tick_down(close + (close - stop) * self.rr)  # toward entry
            if not stop < close < target:
                return None
            return Signal(self.name, candle.symbol, "LONG", close, stop, target, self.product, candle.ts)
        if prev >= 0 > diff and rsi <= self.rsi_short_max:
            stop = round_tick_up(close + atr * self.atr_mult)
            target = round_tick_up(close - (stop - close) * self.rr)
            if not target < close < stop:
                return None
            return Signal(self.name, candle.symbol, "SHORT", close, stop, target, self.product, candle.ts)
        return None


def strategy_params(strategy_cfg: dict, name: str) -> dict:
    """The params for one strategy from the config's `strategy` section, with the shared
    `strategy.indicators` block attached as `indicators` unless the strategy sets its own. Keeps the
    confluence strategy's indicator set identical to the engine's shared one."""
    if name not in strategy_cfg:
        raise ValueError(f"no config for strategy '{name}'")
    params = dict(strategy_cfg[name])
    if "indicators" not in params and strategy_cfg.get("indicators"):
        params["indicators"] = dict(strategy_cfg["indicators"])
    return params


def build_strategy(name: str, params: dict) -> Strategy:
    if name == "ema_rsi":
        return EmaRsiStrategy(params)
    if name == "confluence":
        from tradebot.strategy.confluence import ConfluenceStrategy  # local import: no cycle with base/ta
        return ConfluenceStrategy(params)
    if name == "pullback":
        from tradebot.strategy.pullback import PullbackStrategy  # local import, as for confluence
        return PullbackStrategy(params)
    if name == "orb":
        from tradebot.strategy.orb import OrbStrategy  # local import, as for confluence
        return OrbStrategy(params)
    raise ValueError(f"unknown strategy: {name}")
