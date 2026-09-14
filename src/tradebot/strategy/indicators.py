"""Incremental (streaming) indicators. Each update() consumes one bar and returns the
current value, or None until warm-up completes. No lookahead: only past inputs are used.

Inputs must be finite: a NaN would otherwise poison the state forever while `ready`
still reports True, silently disabling the symbol. We fail loud instead."""
from __future__ import annotations

import math

from tradebot.types import Candle


def _check_period(period: int) -> int:
    if not isinstance(period, int) or period < 1:
        raise ValueError(f"period must be an integer >= 1, got {period!r}")
    return period


def _check_finite(name: str, x: float) -> float:
    if not math.isfinite(x):
        raise ValueError(f"{name}: non-finite input {x!r}")
    return x


class EMA:
    def __init__(self, period: int):
        self.period = _check_period(period)
        self.k = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        _check_finite("EMA", x)
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = x * self.k + self.value * (1 - self.k)
        return self.value


class RSI:
    """Wilder's RSI. Seeded with a simple average of the first `period` changes.
    A perfectly flat series (no gains, no losses) reads 50, not 100."""

    def __init__(self, period: int):
        self.period = _check_period(period)
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
        _check_finite("RSI", close)
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
        if self.avg_gain == 0 and self.avg_loss == 0:
            self.value = 50.0
        elif self.avg_loss == 0:
            self.value = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder's ATR. The first bar's true range is high-low (no previous close)."""

    def __init__(self, period: int):
        self.period = _check_period(period)
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, candle: Candle) -> float | None:
        for name, x in (("high", candle.high), ("low", candle.low), ("close", candle.close)):
            _check_finite(f"ATR.{name}", x)
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(candle.high - candle.low, abs(candle.high - self._prev_close), abs(candle.low - self._prev_close))
        self._prev_close = candle.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value
