"""Paper mode: the wall-clock loop over live REST bars (paper spec, 'Scheduling'). The broker is the
backtest simulator driven by official candles, so nothing here places a real order.

Per iteration: find the latest bar whose close plus grace has passed, fetch every bar from the last
processed one up to it in one window, process them in order, then sleep until the next bar's close
plus grace. A bar handed to process_bar later than the deadline drops its signals as `stale`, so a
catch-up after a late wake or a restart only settles fills and exits. Stop (SIGINT/SIGTERM via
request_stop) finishes the current bar, writes the day's row and leaves the run open so a restart the
same day resumes it; the session end squares off, writes the row and ends the run."""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Callable, Optional

from tradebot.config import resolved_config
from tradebot.data.live import LiveBarSource
from tradebot.engine.clock import date_of, iso_ist
from tradebot.engine.loop import Engine, _DayCounters
from tradebot.types import ApprovedOrder, Candle, Position, Signal

log = logging.getLogger("tradebot.paper")


def position_from_row(r) -> Position:
    return Position(r["symbol"], r["product"], r["direction"], r["qty"], r["avg_price"], r["stop"], r["target"],
                    r["opened_at"], r["client_id"], r["strategy"], fill_status=r["fill_status"],
                    adopted=bool(r["adopted"]), db_id=r["id"])


def order_from_row(r) -> ApprovedOrder:
    sig = Signal(r["strategy"], r["symbol"], r["direction"], r["entry"], r["stop"], r["target"], r["product"], r["bar_ts"])
    return ApprovedOrder(sig, r["qty"], r["client_id"])


class PaperEngine(Engine):
    def __init__(self, cfg, repo, source: LiveBarSource, strategies, broker, ai_filter, clock, lot_sizes: dict,
                 run_id: str, now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        super().__init__(cfg, repo, strategies, broker, ai_filter, clock, lot_sizes, run_id, mode="paper")
        self.source = source
        self.now = now
        self.sleep = sleep
        self.grace = cfg.data.bar_grace_sec
        self.stop_requested = False

    def request_stop(self) -> None:
        """Signal-handler entry point: finish the current bar, write the day's row, leave positions open."""
        self.stop_requested = True

    # -- start-up -----------------------------------------------------------------------------
    def warm(self, candles: list[Candle]) -> Optional[int]:
        """Feed stored candles, oldest first, to the indicators and strategies without touching the
        broker. Returns the latest warmed bar ts, or None when there was nothing to warm."""
        by_ts: dict = {}
        for c in candles:
            by_ts.setdefault(c.ts, {})[c.symbol] = c
        last = None
        for ts in sorted(by_ts):
            self._observe(by_ts[ts])
            self._run_strategies(by_ts[ts])  # signals discarded: warm-up never places
            last = ts
        return last

    def resume(self, today: date) -> bool:
        """Reload today's run from SQLite into the broker and day counters. False when there is no
        such run; raises when it already ended (one run per day unless --run-id says otherwise)."""
        run = self.repo.get_run(self.run_id)
        if run is None:
            return False
        if run["ended_at"] is not None:
            raise ValueError(f"run {self.run_id} already ended today; pass a different --run-id to start another session")
        closed = [r for r in self.repo.list_positions(self.run_id) if r["closed_at"] is not None]
        positions = [position_from_row(r) for r in self.repo.open_positions(self.run_id)]
        pending = [order_from_row(r) for r in self.repo.pending_orders(self.run_id)]
        self.broker.restore(positions, pending, self.cfg.capital + sum(r["pnl"] or 0.0 for r in closed))
        for p in positions:  # unrealised needs a price for every open symbol; a gap falls back to cost
            self._last_close.setdefault(p.symbol, p.avg_price)
        rows = {r["date"]: r for r in self.repo.daily_pnl(self.run_id)}
        row = rows.get(today.isoformat())
        self._day = _DayCounters(realised=sum(r["pnl"] or 0.0 for r in closed if date_of(r["closed_at"]) == today),
                                 entries_placed=row["entries_placed"] if row else 0,
                                 fills=row["fills"] if row else 0)
        log.info("resumed run %s: %d open position(s), %d pending entr%s, last bar %s", self.run_id,
                 len(positions), len(pending), "y" if len(pending) == 1 else "ies",
                 iso_ist(run["last_bar_ts"]) if run["last_bar_ts"] is not None else "none")
        return True

    # -- loop ---------------------------------------------------------------------------------
    def run(self, warm_candles: list[Candle]) -> Optional[str]:
        """Returns the run id, or None when there is no session to trade right now."""
        now = int(self.now())
        today = date_of(now)
        if not self.clock.is_trading_day(today):
            log.info("%s is not a trading day; nothing to do", today)
            return None
        if now >= self.clock.close_ts(today):
            log.info("the %s session has already closed; nothing to do", today)
            return None
        warmed = self.warm(warm_candles)
        resumed = self.resume(today)
        if not resumed:
            self.repo.create_run(self.run_id, self.mode, now, json.dumps(resolved_config(self.cfg), default=str))
            self._start_day()
        open_ts = self.clock.open_ts(today)
        last = open_ts - self.interval_sec
        if warmed is not None and warmed >= open_ts:
            last = warmed  # today's stored bars were replayed by warm(); they are not fetched again
        stored_last = self.repo.get_run(self.run_id)["last_bar_ts"]
        if stored_last is not None:
            last = max(last, stored_last)
        end = self.clock.last_bar_ts(today)
        log.info("paper run %s (%s) %s: next bar %s, strategies=%s, %d symbols", self.run_id,
                 "resumed" if resumed else "new", today, iso_ist(last + self.interval_sec),
                 [s.name for s in self.strategies], len(self.source.symbols))
        try:
            while last < end and not self.stop_requested:
                latest = self.clock.latest_complete_bar(int(self.now()), self.grace)
                if latest is None or latest <= last:
                    self._sleep_until(last + 2 * self.interval_sec + self.grace)  # close of the next bar + grace
                    continue
                first = last + self.interval_sec
                bars = self.source.fetch_range(first, latest)
                for ts in range(first, latest + 1, self.interval_sec):
                    self._bar(ts, bars.get(ts, {}))
                    last = ts
                    if self.stop_requested:
                        break
        finally:
            if self.stop_requested and last < end:
                self._write_daily_row(today)
                log.info("paper run %s suspended after bar %s; start again today to resume", self.run_id,
                         iso_ist(last) if last >= open_ts else "none")
            else:
                self._end_day(today, max(last, open_ts))
                self.repo.end_run(self.run_id, int(self.now()))
                log.info("paper run %s ended: realised %.2f, %d fills / %d entries", self.run_id,
                         self._day.realised, self._day.fills, self._day.entries_placed)
        return self.run_id

    def _bar(self, ts: int, candles: dict) -> None:
        try:
            self.process_bar(ts, candles, now_ts=int(self.now()))
        except Exception:  # noqa: BLE001 - spec: log and keep the loop alive; nothing sits at a real broker
            log.exception("bar %s failed; continuing with the next bar", iso_ist(ts))
        self.repo.set_last_bar_ts(self.run_id, ts)
        self._write_daily_row(date_of(ts))

    def _sleep_until(self, wake_ts: float) -> None:
        """Sleep in short slices so a stop request is honoured within a second."""
        while not self.stop_requested:
            remaining = wake_ts - self.now()
            if remaining <= 0:
                return
            self.sleep(min(1.0, remaining))
