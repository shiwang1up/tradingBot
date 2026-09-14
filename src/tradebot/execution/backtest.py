"""Simulated broker for backtests. Fills entries at the next bar's open; simulates
broker-side stop/target exits against each bar's high/low."""
from __future__ import annotations

from tradebot.execution.broker import BrokerEvent, Closed, Filled, Unfilled
from tradebot.types import ApprovedOrder, Candle, Position, round_tick

# Spec 8.1: if one bar touches both the stop and the target, assume the stop was hit first.
STOP_FIRST_ON_SAME_BAR = True


def check_exit(pos: Position, c: Candle) -> tuple[str, float] | None:
    """Return (reason, level) if the bar triggers an exit, else None."""
    if pos.direction == "LONG":
        stop_hit = c.low <= pos.stop_price
        target_hit = pos.target_price is not None and c.high >= pos.target_price
    else:
        stop_hit = c.high >= pos.stop_price
        target_hit = pos.target_price is not None and c.low <= pos.target_price
    if stop_hit and (STOP_FIRST_ON_SAME_BAR or not target_hit):
        return "STOP", pos.stop_price
    if target_hit:
        return "TARGET", pos.target_price
    return None


class BacktestBroker:
    def __init__(self, capital: float, slippage_pct: float, mis_leverage: float):
        self.cash = float(capital)
        self.slip = slippage_pct / 100.0
        self.lev = mis_leverage
        self._pending: dict[str, ApprovedOrder] = {}
        self._positions: dict[str, Position] = {}
        self.closed: list[Position] = []

    # -- interface -----------------------------------------------------------
    def place_entry(self, order: ApprovedOrder) -> None:
        self._pending[order.signal.symbol] = order

    def pending_symbols(self) -> set[str]:
        return set(self._pending)

    def open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def on_bar(self, ts: int, candles: dict[str, Candle]) -> list[BrokerEvent]:
        events: list[BrokerEvent] = []
        for sym, order in list(self._pending.items()):
            del self._pending[sym]
            c = candles.get(sym)
            if c is None:
                events.append(Unfilled(order, ts))
                continue
            sig = order.signal
            price = self._entry_price(sig.direction, c.open)
            pos = Position(sym, sig.product, sig.direction, order.quantity, price, sig.stop_price,
                           sig.target_price, c.ts, order.client_id, sig.strategy)
            self._positions[sym] = pos
            events.append(Filled(pos, order, c.ts))
        for sym, pos in list(self._positions.items()):
            c = candles.get(sym)
            if c is None:
                continue
            hit = check_exit(pos, c)
            if hit is None:
                continue
            reason, level = hit
            price = self._exit_price(pos.direction, level) if reason == "STOP" else level
            self._close(pos, c.ts, price, reason)
            events.append(Closed(pos))
        return events

    def square_off(self, ts: int, candles: dict[str, Candle], products: tuple[str, ...] = ("MIS",),
                   reason: str = "SQUARE_OFF") -> list[Closed]:
        out: list[Closed] = []
        for sym, pos in list(self._positions.items()):
            c = candles.get(sym)
            if pos.product not in products or c is None:
                continue
            self._close(pos, ts, self._exit_price(pos.direction, c.close), reason)
            out.append(Closed(pos))
        return out

    def available_margin(self, product: str) -> float:
        used = 0.0
        for pos in self._positions.values():
            used += pos.avg_price * pos.quantity / self._lev_for(pos.product)
        for order in self._pending.values():
            used += order.signal.entry_price * order.quantity / self._lev_for(order.signal.product)
        free = max(self.cash - used, 0.0)
        return free * self._lev_for(product)

    def unrealised_pnl(self, last_prices: dict[str, float]) -> float:
        return sum(p.unrealised(last_prices[s]) for s, p in self._positions.items() if s in last_prices)

    # -- internals ----------------------------------------------------------
    def _lev_for(self, product: str) -> float:
        return self.lev if product == "MIS" else 1.0

    def _entry_price(self, direction: str, ref: float) -> float:
        return round_tick(ref * (1 + self.slip)) if direction == "LONG" else round_tick(ref * (1 - self.slip))

    def _exit_price(self, direction: str, ref: float) -> float:
        # Exiting a long sells (worse = lower); exiting a short buys (worse = higher).
        return round_tick(ref * (1 - self.slip)) if direction == "LONG" else round_tick(ref * (1 + self.slip))

    def _close(self, pos: Position, ts: int, price: float, reason: str) -> None:
        pnl = (price - pos.avg_price) * pos.quantity if pos.direction == "LONG" else (pos.avg_price - price) * pos.quantity
        pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = ts, price, reason, round(pnl, 2)
        self.cash += pos.pnl
        del self._positions[pos.symbol]
        self.closed.append(pos)
