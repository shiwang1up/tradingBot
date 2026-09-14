from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from tradebot.types import Candle, Product, Signal


class Strategy(ABC):
    """One instance handles every symbol; state is kept per symbol internally.

    Contract: on_candle is deterministic given the candle sequence for a symbol.
    Exceptions propagate; the engine decides what to do with them.
    """

    name: str
    product: Product

    @abstractmethod
    def on_candle(self, candle: Candle) -> Signal | None: ...

    @abstractmethod
    def is_ready(self, symbol: str) -> bool: ...

    @abstractmethod
    def snapshot(self, symbol: str) -> dict[str, float]:
        """Current indicator values for the AI filter. Empty dict if not ready."""

    @abstractmethod
    def reset(self, symbol: str) -> None: ...

    def recompute(self, symbol: str, candles: Iterable[Candle]) -> None:
        """Rebuild state for a symbol from scratch. Signals produced during replay are discarded."""
        self.reset(symbol)
        for c in candles:
            self.on_candle(c)
