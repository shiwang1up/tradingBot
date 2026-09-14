"""Broker interface shared by backtest, paper, and live backends, plus the events they emit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union

from tradebot.types import ApprovedOrder, Candle, Position


@dataclass(frozen=True)
class Filled:
    position: Position
    order: ApprovedOrder
    ts: int


@dataclass(frozen=True)
class Unfilled:
    order: ApprovedOrder
    ts: int


@dataclass(frozen=True)
class Closed:
    position: Position


BrokerEvent = Union[Filled, Unfilled, Closed]  # runtime union; `|` needs 3.10+


class Broker(Protocol):
    def place_entry(self, order: ApprovedOrder) -> None: ...
    def on_bar(self, ts: int, candles: dict[str, Candle]) -> list[BrokerEvent]: ...
    def square_off(self, ts: int, candles: dict[str, Candle], products: tuple[str, ...] = ("MIS",),
                   reason: str = "SQUARE_OFF") -> list[Closed]: ...
    def open_positions(self) -> dict[str, Position]: ...
    def pending_symbols(self) -> set[str]: ...
    def available_margin(self, product: str) -> float: ...
    def unrealised_pnl(self, last_prices: dict[str, float]) -> float: ...
