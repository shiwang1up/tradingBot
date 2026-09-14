"""Incremental (streaming) indicators. Each update() consumes one bar and returns the
current value, or None until warm-up completes. No lookahead: only past inputs are used."""
from __future__ import annotations

from tradebot.types import Candle


class EMA:
    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = x * self.k + self.value * (1 - self.k)
        return self.value


class RSI:
    """Wilder's RSI. Seeded with a simple average of the first `period` changes."""

    def __init__(self, period: int):
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self.avg_gain = sum(self._gains) / self.period
            self.avg_loss = sum(self._losses) / self.period
        else:
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        if self.avg_loss == 0:
            self.value = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder's ATR. The first bar's true range is high-low (no previous close)."""

    def __init__(self, period: int):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, c: Candle) -> float | None:
        if self._prev_close is None:
            tr = c.high - c.low
        else:
            tr = max(c.high - c.low, abs(c.high - self._prev_close), abs(c.low - self._prev_close))
        self._prev_close = c.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value
