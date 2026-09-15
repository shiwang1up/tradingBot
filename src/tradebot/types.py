"""Core value types shared by every package. No I/O here.

Annotations use PEP 604/585 syntax and are strings under Python 3.9 thanks to the
`from __future__ import annotations` import; never resolve them with typing.get_type_hints()
until the project drops 3.9.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Literal

Direction = Literal["LONG", "SHORT"]
Product = Literal["MIS", "CNC"]

TICK = 0.05


def round_tick(price: float, tick: float = TICK) -> float:
    return round(round(price / tick) * tick, 2)


def round_tick_down(price: float, tick: float = TICK) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 2)


def round_tick_up(price: float, tick: float = TICK) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 2)


def make_client_id(strategy: str, symbol: str, bar_ts: int) -> str:
    """Idempotent order id: same (strategy, symbol, bar) always yields the same id.

    16 hex chars satisfies Groww's 8-20 alphanumeric order_reference_id rule.
    """
    raw = f"{strategy}|{symbol}|{bar_ts}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Candle:
    symbol: str
    ts: int  # UTC epoch seconds, bar open time
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str = "official"  # "official" | "tick_built"


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float
    target_price: float | None
    product: Product
    bar_ts: int


@dataclass(frozen=True)
class ApprovedOrder:
    signal: Signal
    quantity: int
    client_id: str


@dataclass(frozen=True)
class Rejection:
    signal: Signal
    reason: str


@dataclass
class Position:
    symbol: str
    product: Product
    direction: Direction
    quantity: int
    avg_price: float
    stop_price: float
    target_price: float | None
    opened_ts: int
    client_id: str
    strategy: str
    closed_ts: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # "STOP" | "TARGET" | "SQUARE_OFF" | "FLATTEN"
    pnl: float | None = None
    fill_status: str = "full"  # "full" | "partial"
    adopted: bool = False
    db_id: int | None = None

    def unrealised(self, last_price: float) -> float:
        if self.direction == "LONG":
            return (last_price - self.avg_price) * self.quantity
        return (self.avg_price - last_price) * self.quantity


@dataclass(frozen=True)
class Candidate:
    """What the AI filter sees for one signal."""
    signal: Signal
    quantity: int
    indicators: dict[str, float]
    candles: tuple[Candle, ...]


@dataclass(frozen=True)
class Decision:
    signal: Signal
    approved: bool
    reason: str
    confidence: float
    filter_kind: str
    latency_ms: int = 0
    failure: str | None = None
    input_tokens: int = 0        # usage is attributed to the first decision of a batch
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
