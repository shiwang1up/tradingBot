"""Market-direction gate: the index against its own EMA. The engine feeds it one index close per
bar and asks it, per signal, whether the direction fits the market."""
from __future__ import annotations

from typing import Optional

from tradebot.strategy.indicators import EMA

UP, DOWN, NOT_READY = "UP", "DOWN", "NOT_READY"


class RegimeFilter:
    def __init__(self, ema_period: int):
        self._ema = EMA(ema_period)
        self.state = NOT_READY

    def update(self, close: float) -> str:
        ema = self._ema.update(close)
        if ema is not None:
            self.state = UP if close > ema else DOWN
        return self.state

    def rejection(self, direction: str) -> Optional[str]:
        """The risk-decision reason that blocks `direction` right now, or None when it may trade."""
        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"unknown direction: {direction!r}")
        if self.state == NOT_READY:
            return "regime_not_ready"
        if (direction == "LONG") != (self.state == UP):
            return "regime"
        return None
