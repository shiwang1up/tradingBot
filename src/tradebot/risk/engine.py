"""Pure risk checks. Order of checks follows spec section 6."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from tradebot.config import RiskConfig
from tradebot.risk.killswitch import KillState
from tradebot.risk.sizing import compute_quantity
from tradebot.types import ApprovedOrder, Rejection, Signal, make_client_id


@dataclass
class PortfolioState:
    capital: float
    open_symbols: set[str] = field(default_factory=set)
    pending_symbols: set[str] = field(default_factory=set)
    realised_today: float = 0.0
    unrealised: float = 0.0
    entries_today: int = 0
    cooldown_until: dict[str, int] = field(default_factory=dict)  # symbol -> last blocked bar ts

    @property
    def open_count(self) -> int:
        return len(self.open_symbols | self.pending_symbols)

    def daily_loss_breached(self, cfg: RiskConfig) -> bool:
        return (self.realised_today + self.unrealised) <= -(cfg.daily_loss_cap_pct / 100.0) * self.capital


def evaluate(signal: Signal, state: PortfolioState, cfg: RiskConfig, lot_size: int,
             available_margin: float, kill: KillState) -> ApprovedOrder | Rejection:
    if kill.active:
        return Rejection(signal, "kill_switch")
    if state.daily_loss_breached(cfg):
        return Rejection(signal, "daily_loss_cap")
    if state.entries_today >= cfg.max_entries_per_day:
        return Rejection(signal, "max_entries_per_day")
    if state.open_count >= cfg.max_open_positions:
        return Rejection(signal, "max_open_positions")
    if signal.symbol in state.open_symbols or signal.symbol in state.pending_symbols:
        return Rejection(signal, "symbol_already_open")
    if signal.bar_ts <= state.cooldown_until.get(signal.symbol, -1):
        return Rejection(signal, "cooldown")
    if not (math.isfinite(signal.entry_price) and signal.entry_price > 0):
        return Rejection(signal, "invalid_price")
    if signal.direction == "LONG" and not signal.stop_price < signal.entry_price:
        return Rejection(signal, "invalid_stop")
    if signal.direction == "SHORT" and not signal.stop_price > signal.entry_price:
        return Rejection(signal, "invalid_stop")
    if signal.direction == "SHORT" and signal.product != "MIS":
        return Rejection(signal, "short_requires_mis")
    qty = compute_quantity(state.capital, cfg.per_trade_pct, signal.entry_price, signal.stop_price,
                           available_margin, lot_size)
    if qty < max(lot_size, 1):
        return Rejection(signal, "insufficient_size")
    return ApprovedOrder(signal, qty, make_client_id(signal.strategy, signal.symbol, signal.bar_ts))
