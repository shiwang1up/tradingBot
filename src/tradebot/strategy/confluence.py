"""Confluence strategy: weighted votes from the technical layers, entered when the score crosses a
threshold (spec docs/superpowers/specs/2026-09-15-indicators-and-confluence-design.md, phase 2).

Layers (each votes -1, 0 or +1, times its weight):
  trend      EMA20 above EMA50 is +1, below is -1
  macd       the turn direction on a bar the histogram turned, else its sign when it agrees with the trend
  momentum   +1 when the 60-, 5- and 1-bar moves are all positive, -1 when all negative
  breakout   +1 on a breakout with a volume spike, -1 on a breakdown with one
  pattern    +1 bullish engulfing or double bottom, -1 bearish engulfing or double top
  exhaustion RSI and %B both high pushes the score down (against a long), both low pushes it up
ADX below `adx_min` gates everything off: a gated bar counts as score 0, so the first bar with a trend
can fire even if the score built up while ADX was low. A signal fires when the score crosses the
threshold from inside, so a score that stays beyond it does not repeat; the first ready bar never fires
(there is no previous bar to have crossed from)."""
from __future__ import annotations

from typing import Optional

from tradebot.strategy.base import Strategy
from tradebot.strategy.ta import IndicatorSet
from tradebot.types import Candle, Signal, round_tick_down, round_tick_up

PRODUCTS = ("MIS", "CNC")
DEFAULT_WEIGHTS = {"trend": 1.0, "macd": 1.0, "momentum": 1.0, "breakout": 1.5, "pattern": 1.0, "exhaustion": -1.5}


def _number(name: str, v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number, got {v!r}")
    return float(v)


def _sign(x: Optional[float]) -> int:
    if x is None or x == 0:
        return 0
    return 1 if x > 0 else -1


class ConfluenceStrategy(Strategy):
    name = "confluence"

    def __init__(self, params: dict):
        weights = {**DEFAULT_WEIGHTS, **dict(params.get("weights") or {})}
        unknown = set(weights) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"confluence.weights has unknown layers {sorted(unknown)}")
        self.weights = {k: _number(f"confluence.weights.{k}", v) for k, v in weights.items()}
        negative = [k for k, v in self.weights.items() if k != "exhaustion" and v < 0]
        if negative:
            raise ValueError(f"confluence.weights must be >= 0 except exhaustion, got negative {negative}")
        self.threshold = float(params.get("threshold", 3.0))
        self.adx_min = float(params.get("adx_min", 20))
        self.volume_spike_min = float(params.get("volume_spike_min", 150))
        self.rsi_overbought = float(params.get("rsi_overbought", 70))
        self.rsi_oversold = float(params.get("rsi_oversold", 30))
        self.pct_b_high = float(params.get("pct_b_high", 0.8))
        self.pct_b_low = float(params.get("pct_b_low", 0.2))
        self.atr_mult = float(params.get("atr_stop_mult", 1.5))
        self.rr = float(params.get("reward_risk", 2.0))
        self.min_stop_pct = float(params.get("min_stop_pct", 0.1))
        self.product = params.get("product", "MIS")
        self.indicator_params = dict(params.get("indicators") or {})
        if self.threshold <= 0 or self.atr_mult <= 0 or self.rr <= 0 or self.min_stop_pct <= 0:
            raise ValueError("confluence.threshold, atr_stop_mult, reward_risk and min_stop_pct must be > 0")
        if self.adx_min < 0 or self.volume_spike_min < 0:
            raise ValueError("confluence.adx_min and volume_spike_min must be >= 0")
        if self.weights["exhaustion"] > 0:
            raise ValueError("confluence.weights.exhaustion must be <= 0 (it counts against a trade into exhaustion)")
        if self.rsi_oversold >= self.rsi_overbought or self.pct_b_low >= self.pct_b_high:
            raise ValueError("confluence oversold/low thresholds must be below overbought/high ones")
        if self.product not in PRODUCTS:
            raise ValueError(f"confluence.product must be one of {PRODUCTS}, got {self.product!r}")
        self._sets: dict = {}
        self._prev_score: dict = {}
        self._last_score: dict = {}

    # -- Strategy interface ---------------------------------------------------------------------
    def reset(self, symbol: str) -> None:
        self._sets[symbol] = IndicatorSet(self.indicator_params)
        self._prev_score[symbol] = None   # no crossing can be judged until a second ready bar
        self._last_score[symbol] = None

    def is_ready(self, symbol: str) -> bool:
        s = self._sets.get(symbol)
        return bool(s and s.ready and self._last_score.get(symbol) is not None)

    def snapshot(self, symbol: str) -> dict:
        s = self._sets.get(symbol)
        if not s or not s.ready:
            return {}
        return {**s.snapshot(), "score": round(self._last_score.get(symbol) or 0.0, 3)}

    def on_candle(self, candle: Candle) -> Optional[Signal]:
        sym = candle.symbol
        if sym not in self._sets:
            self.reset(sym)
        ind = self._sets[sym]
        ind.update(candle)
        if not ind.ready:
            return None
        snap = ind.snapshot()
        score = self.score(snap)
        self._last_score[sym] = score
        direction = self._direction(sym, score, snap["adx"])
        if direction is None:
            return None
        atr = snap["atr"] or 0.0
        close = candle.close
        if atr * self.atr_mult < close * self.min_stop_pct / 100.0:
            return None
        if direction == "LONG":
            stop = round_tick_down(close - atr * self.atr_mult)
            target = round_tick_down(close + (close - stop) * self.rr)
            if not stop < close < target:
                return None
        else:
            stop = round_tick_up(close + atr * self.atr_mult)
            target = round_tick_up(close - (stop - close) * self.rr)
            if not target < close < stop:
                return None
        return Signal(self.name, sym, direction, close, stop, target, self.product, candle.ts)

    def _direction(self, sym: str, score: float, adx: Optional[float]) -> Optional[str]:
        """Threshold-crossing state machine. The remembered score is the *eligible* score: a bar
        gated by ADX is recorded as 0 (inside the band), so the crossing is judged over trending bars
        only and a trend that arrives with the score already beyond the threshold can fire."""
        eligible = adx is not None and adx >= self.adx_min
        effective = score if eligible else 0.0
        prev = self._prev_score.get(sym)
        self._prev_score[sym] = effective
        if not eligible or prev is None:
            return None
        if effective >= self.threshold and prev < self.threshold:
            return "LONG"
        if effective <= -self.threshold and prev > -self.threshold:
            return "SHORT"
        return None

    # -- scoring ---------------------------------------------------------------------------------
    def votes(self, snap: dict) -> dict:
        w = self.weights
        trend = snap.get("trend") or 0
        turned = _sign(snap.get("macd_turned"))
        hist_sign = _sign(snap.get("macd_hist"))
        macd = turned if turned else (hist_sign if hist_sign == trend else 0)
        m60, m5, m1 = (_sign(snap.get(k)) for k in ("mom_60", "mom_5", "mom_1"))
        momentum = 1 if m60 == m5 == m1 == 1 else (-1 if m60 == m5 == m1 == -1 else 0)
        vol_ok = (snap.get("volume_spike_pct") or 0.0) >= self.volume_spike_min
        breakout = 1 if (snap.get("breakout") and vol_ok) else (-1 if (snap.get("breakdown") and vol_ok) else 0)
        bull = snap.get("engulfing") == 1 or snap.get("double_bottom") == 1
        bear = snap.get("engulfing") == -1 or snap.get("double_top") == 1
        pattern = 1 if bull and not bear else (-1 if bear and not bull else 0)
        rsi, pct_b = snap.get("rsi"), snap.get("pct_b")
        overbought = rsi is not None and pct_b is not None and rsi >= self.rsi_overbought and pct_b >= self.pct_b_high
        oversold = rsi is not None and pct_b is not None and rsi <= self.rsi_oversold and pct_b <= self.pct_b_low
        exhaustion = 1 if overbought else (-1 if oversold else 0)  # +1 means "exhausted on the upside"
        return {"trend": trend * w["trend"], "macd": macd * w["macd"], "momentum": momentum * w["momentum"],
                "breakout": breakout * w["breakout"], "pattern": pattern * w["pattern"],
                "exhaustion": exhaustion * w["exhaustion"]}

    def score(self, snap: dict) -> float:
        return sum(self.votes(snap).values())
