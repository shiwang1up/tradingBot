"""Simulated broker for backtests. Fills entries at the next bar's open (or its close, for daily
bars); simulates broker-side stop/target exits against each bar's high/low, and closes a position
whose signal carried a `max_hold_bars` once it has been held that many bars.

The time exit is simulation-only: no live broker holds a timed order, so a daily cadence in paper
or live would need a runner that closes the position itself. Nothing here does that today."""
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
                 entry_buffer_pct: float | None = None, charges: ChargesConfig | None = None,
                 fill_on_close: bool = False):
        """entry_buffer_pct mirrors the live marketable limit: an entry whose next open is beyond
        signal_price * (1 +/- buffer) is left unfilled, exactly as the live order would be.
        None disables the check. charges=None means a free broker (tests); the CLI always passes
        cfg.charges.

        fill_on_close fills a pending entry at the bar's close instead of its open, for daily
        bars: Groww's daily open is synthetic for much of the history (it carries the previous
        close forward), and the daily screens all work on closes. Defaults to False, so every
        intraday path is unchanged."""
        self.cash = float(capital)
        self.slip = slippage_pct / 100.0
        self.lev = mis_leverage
        self.buffer = None if entry_buffer_pct is None else entry_buffer_pct / 100.0
        self._charges_cfg = charges
        self._fill_on_close = fill_on_close
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

    def _beyond_buffer(self, sig, ref: float) -> bool:
        """`ref` is the price the entry would fill at, which is the bar's open intraday and its
        close in daily mode. It must be the same price the fill uses: judging the buffer against
        the open while filling at the close would admit exactly the runaway entries it rejects."""
        if self.buffer is None:
            return False
        if sig.direction == "LONG":
            return ref > sig.entry_price * (1 + self.buffer)
        return ref < sig.entry_price * (1 - self.buffer)

    def on_bar(self, ts: int, candles: dict[str, Candle]) -> list[BrokerEvent]:
        """Fill every pending entry at this bar's open (its close when `fill_on_close`), then close
        every open position whose stop or target this bar hits, and after that any whose hold has
        run out. When `fill_on_close`, the bar that fills an entry cannot also exit it: its range
        printed before the fill. A close is contained per position: one whose exit price is bad (see
        `_close`) is logged at error level and left open for the caller to retry, while every other
        fill and close on this bar still happens and is still returned."""
        events: list[BrokerEvent] = []
        for sym, order in list(self._pending.items()):
            del self._pending[sym]
            c = candles.get(sym)
            if c is None:
                events.append(Unfilled(order, ts, "no_candle"))
                continue
            sig = order.signal
            ref = c.close if self._fill_on_close else c.open  # one reference price for both checks below
            if self._beyond_buffer(sig, ref):
                events.append(Unfilled(order, ts, "beyond_buffer"))
                continue
            price = self._entry_price(sig.direction, ref)
            pos = Position(sym, sig.product, sig.direction, order.quantity, price, sig.stop_price,
                           sig.target_price, ts, order.client_id, sig.strategy,
                           max_hold_bars=sig.max_hold_bars)
            self._positions[sym] = pos
            events.append(Filled(pos, order, ts))
        for sym, pos in list(self._positions.items()):
            c = candles.get(sym)
            if c is None:
                continue
            if pos.opened_ts == ts:
                # The entry bar does not count: max_hold_bars is bars held AFTER the fill. Bars with
                # no candle for this symbol are skipped here, so a data hole never ages a position.
                if self._fill_on_close:
                    # Filled at this bar's close, so its high and low printed BEFORE the position
                    # existed. Checking them would close a trade at a price from before its entry:
                    # a long can be "stopped" above its own fill for a profit on a losing setup.
                    # An open fill is different -- the rest of that bar really does follow it.
                    continue
            else:
                pos.bars_held += 1
            hit = check_exit(pos, c)
            if hit is None:
                # After the stop/target check, so a bar that does both is a stop, not a time exit.
                if pos.max_hold_bars is None or pos.bars_held < pos.max_hold_bars:
                    continue
                reason, level = "TIME_EXIT", c.close
            else:
                reason, level = hit
            price = self._exit_price(pos.direction, level) if reason in ("STOP", "TIME_EXIT") else level
            try:
                self._close(pos, ts, price, reason)
            except ValueError as e:
                log.error("%s: close failed on bar %d, leaving position open: %s", sym, ts, e,
                         extra={"symbol": sym, "client_id": pos.client_id})
                continue
            events.append(Closed(pos))
        return events

    def square_off(self, ts: int, candles: dict[str, Candle], products: tuple[str, ...] = ("MIS",),
                   reason: str = "SQUARE_OFF", last_prices: dict[str, float] | None = None) -> list[Closed]:
        """Close every position in `products` at this bar's close. A symbol with no candle this
        bar closes at its last known price (`last_prices`) so a data hole cannot carry an
        intraday position overnight; with no price at all it stays open and the caller retries.
        A close that fails (bad exit price) is contained the same way: logged at error level and
        left open, while every other position in `products` is still closed."""
        out: list[Closed] = []
        for sym, pos in list(self._positions.items()):
            if pos.product not in products:
                continue
            c = candles.get(sym)
            ref = c.close if c is not None else (last_prices or {}).get(sym)
            if ref is None:
                continue
            try:
                self._close(pos, ts, self._exit_price(pos.direction, ref), reason)
            except ValueError as e:
                log.error("%s: close failed on bar %d, leaving position open: %s", sym, ts, e,
                         extra={"symbol": sym, "client_id": pos.client_id})
                continue
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
        # product selects the schedule: CNC pays STT on both sides, the higher stamp duty and the
        # DP fee. Omitting it charged every delivery position at intraday rates.
        charges = position_charges(pos.direction, pos.avg_price, price, pos.quantity, self._charges_cfg,
                                   product=pos.product)
        pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl, pos.charges = ts, price, reason, pnl, charges
        self.cash += pnl - charges
        if self.cash <= 0:
            log.warning("simulated cash is %.2f after closing %s: account is blown", self.cash, pos.symbol)
        del self._positions[pos.symbol]
        self.closed.append(pos)
