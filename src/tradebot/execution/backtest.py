"""Simulated broker for backtests. Fills entries at the next bar's open; simulates
broker-side stop/target exits against each bar's high/low."""
from __future__ import annotations

import logging
import math

from tradebot.config import ChargesConfig
from tradebot.execution.broker import BrokerEvent, Closed, Filled, Unfilled
from tradebot.execution.charges import position_charges
from tradebot.types import ApprovedOrder, Candle, Position, round_tick

log = logging.getLogger("tradebot.backtest")

# Spec 8.1: if one bar touches both the stop and the target, assume the stop was hit first.
STOP_FIRST_ON_SAME_BAR = True


def check_exit(pos: Position, c: Candle) -> tuple[str, float] | None:
    """Return (reason, level) if the bar triggers an exit, else None.

    Levels are clamped to the bar's open: a bar that gaps through the stop fills at the
    open (worse than the stop), and one that gaps through the target fills at the open
    (better than the target). Without the clamp an entry bar gapping through the stop
    would book a profit on a losing trade.
    """
    long = pos.direction == "LONG"
    if long:
        stop_hit = c.low <= pos.stop_price
        target_hit = pos.target_price is not None and c.high >= pos.target_price
    else:
        stop_hit = c.high >= pos.stop_price
        target_hit = pos.target_price is not None and c.low <= pos.target_price
    if stop_hit and (STOP_FIRST_ON_SAME_BAR or not target_hit):  # flip the constant to model target-first
        return "STOP", (min(c.open, pos.stop_price) if long else max(c.open, pos.stop_price))
    if target_hit:
        return "TARGET", (max(c.open, pos.target_price) if long else min(c.open, pos.target_price))
    return None


class BacktestBroker:
    def __init__(self, capital: float, slippage_pct: float, mis_leverage: float,
                 entry_buffer_pct: float | None = None, charges: ChargesConfig | None = None):
        """entry_buffer_pct mirrors the live marketable limit: an entry whose next open is beyond
        signal_price * (1 +/- buffer) is left unfilled, exactly as the live order would be.
        None disables the check. charges=None means a free broker (tests); the CLI always passes
        cfg.charges."""
        self.cash = float(capital)
        self.slip = slippage_pct / 100.0
        self.lev = mis_leverage
        self.buffer = None if entry_buffer_pct is None else entry_buffer_pct / 100.0
        self._charges_cfg = charges
        self._pending: dict[str, ApprovedOrder] = {}
        self._positions: dict[str, Position] = {}
        self.closed: list[Position] = []

    # -- interface -----------------------------------------------------------
    def place_entry(self, order: ApprovedOrder) -> None:
        sym = order.signal.symbol
        if sym in self._pending or sym in self._positions:
            raise ValueError(f"{sym} already has a pending entry or an open position")
        self._pending[sym] = order

    def pending_symbols(self) -> set[str]:
        return set(self._pending)

    def cancel_pending(self, ts: int, reason: str = "cancelled") -> list[Unfilled]:
        out = [Unfilled(order, ts, reason) for order in self._pending.values()]
        self._pending.clear()
        return out

    def open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def restore(self, positions: list[Position], pending: list[ApprovedOrder], cash: float) -> None:
        """Load the state an earlier process left in SQLite (paper resume). Replaces whatever is
        held; `closed` starts empty because the earlier process already recorded its closes."""
        self.cash = float(cash)
        self._positions = {p.symbol: p for p in positions}
        self._pending = {o.signal.symbol: o for o in pending}
        self.closed = []

    def _beyond_buffer(self, sig, open_price: float) -> bool:
        if self.buffer is None:
            return False
        if sig.direction == "LONG":
            return open_price > sig.entry_price * (1 + self.buffer)
        return open_price < sig.entry_price * (1 - self.buffer)

    def on_bar(self, ts: int, candles: dict[str, Candle]) -> list[BrokerEvent]:
        events: list[BrokerEvent] = []
        for sym, order in list(self._pending.items()):
            del self._pending[sym]
            c = candles.get(sym)
            if c is None:
                events.append(Unfilled(order, ts, "no_candle"))
                continue
            sig = order.signal
            if self._beyond_buffer(sig, c.open):
                events.append(Unfilled(order, ts, "beyond_buffer"))
                continue
            price = self._entry_price(sig.direction, c.open)
            pos = Position(sym, sig.product, sig.direction, order.quantity, price, sig.stop_price,
                           sig.target_price, ts, order.client_id, sig.strategy)
            self._positions[sym] = pos
            events.append(Filled(pos, order, ts))
        for sym, pos in list(self._positions.items()):
            c = candles.get(sym)
            if c is None:
                continue
            hit = check_exit(pos, c)
            if hit is None:
                continue
            reason, level = hit
            price = self._exit_price(pos.direction, level) if reason == "STOP" else level
            self._close(pos, ts, price, reason)
            events.append(Closed(pos))
        return events

    def square_off(self, ts: int, candles: dict[str, Candle], products: tuple[str, ...] = ("MIS",),
                   reason: str = "SQUARE_OFF", last_prices: dict[str, float] | None = None) -> list[Closed]:
        """Close every position in `products` at this bar's close. A symbol with no candle this
        bar closes at its last known price (`last_prices`) so a data hole cannot carry an
        intraday position overnight; with no price at all it stays open and the caller retries."""
        out: list[Closed] = []
        for sym, pos in list(self._positions.items()):
            if pos.product not in products:
                continue
            c = candles.get(sym)
            ref = c.close if c is not None else (last_prices or {}).get(sym)
            if ref is None:
                continue
            self._close(pos, ts, self._exit_price(pos.direction, ref), reason)
            out.append(Closed(pos))
        return out

    def available_margin(self, product: str) -> float:
        """Free cash times leverage. Margin is charged on entry cost, not marked to market."""
        used = 0.0
        for pos in self._positions.values():
            used += pos.avg_price * pos.quantity / self._lev_for(pos.product)
        for order in self._pending.values():
            used += order.signal.entry_price * order.quantity / self._lev_for(order.signal.product)
        free = max(self.cash - used, 0.0)
        return free * self._lev_for(product)

    def unrealised_pnl(self, last_prices: dict[str, float]) -> float:
        """Sum over open positions. A position with no price in last_prices raises: the engine
        always has a last close for any symbol that has a position."""
        total = 0.0
        for s, p in self._positions.items():
            if s not in last_prices:
                raise KeyError(f"no last price for open position {s}")
            total += p.unrealised(last_prices[s])
        return total

    # -- internals ----------------------------------------------------------
    def _lev_for(self, product: str) -> float:
        return self.lev if product == "MIS" else 1.0

    def _entry_price(self, direction: str, ref: float) -> float:
        return round_tick(ref * (1 + self.slip)) if direction == "LONG" else round_tick(ref * (1 - self.slip))

    def _exit_price(self, direction: str, ref: float) -> float:
        # Exiting a long sells (worse = lower); exiting a short buys (worse = higher).
        return round_tick(ref * (1 - self.slip)) if direction == "LONG" else round_tick(ref * (1 + self.slip))

    def _close(self, pos: Position, ts: int, price: float, reason: str) -> None:
        """All-or-nothing: nothing about `pos` or `self` changes unless every computation below
        succeeds. Compute pnl and charges first, then commit; a raise from position_charges must
        not leave the position half-closed."""
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"{pos.symbol}: expected a finite exit price greater than 0, got {price!r}")
        pnl = (price - pos.avg_price) * pos.quantity if pos.direction == "LONG" else (pos.avg_price - price) * pos.quantity
        pnl = round(pnl, 2)
        charges = position_charges(pos.direction, pos.avg_price, price, pos.quantity, self._charges_cfg)
        pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl, pos.charges = ts, price, reason, pnl, charges
        self.cash += pnl - charges
        if self.cash <= 0:
            log.warning("simulated cash is %.2f after closing %s: account is blown", self.cash, pos.symbol)
        del self._positions[pos.symbol]
        self.closed.append(pos)
