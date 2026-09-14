"""The per-bar cycle (spec sections 4-8). Mode-independent given a source, broker, and clock.

Order inside a bar:
  1. broker.on_bar        fills pending entries at this bar's open, simulates exits
  2. square-off           once per day, from the square-off bar onward (latched)
  3. kill switch          flatten if requested
  4. strategies           every candle feeds every strategy (indicators stay warm)
  5. risk -> AI -> place  only if entries are allowed and the kill switch is off
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date

from tradebot.ai.filter import AIFilter
from tradebot.config import Config
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, date_of
from tradebot.execution.broker import Broker, BrokerEvent, Closed, Filled, Unfilled
from tradebot.risk.engine import PortfolioState, evaluate
from tradebot.risk.killswitch import KillState, read_kill_switch
from tradebot.store.repo import Repo
from tradebot.strategy.base import Strategy
from tradebot.types import ApprovedOrder, Candidate, Candle, Rejection, Signal

log = logging.getLogger("tradebot.engine")


@dataclass
class _DayCounters:
    realised: float = 0.0
    entries_placed: int = 0
    fills: int = 0
    flattened: bool = False
    squared_off: bool = False


class BacktestEngine:
    def __init__(self, cfg: Config, repo: Repo, source: HistoricalSource, strategies: list[Strategy],
                 broker: Broker, ai_filter: AIFilter, clock: SessionClock, lot_sizes: dict[str, int], run_id: str):
        self.cfg = cfg
        self.repo = repo
        self.source = source
        self.strategies = strategies
        self.broker = broker
        self.ai_filter = ai_filter
        self.clock = clock
        self.lot_sizes = lot_sizes
        self.run_id = run_id
        self.interval_sec = cfg.execution.interval_minutes * 60
        self._history: dict[str, deque[Candle]] = {}
        self._last_close: dict[str, float] = {}
        self._cooldown_until: dict[str, int] = {}
        self.disabled_strategies: set[str] = set()
        self._day = _DayCounters()

    # -- lifecycle -------------------------------------------------------------
    def run(self) -> str:
        self.repo.create_run(self.run_id, "backtest", int(time.time()), json.dumps(self.cfg.raw))
        current: date | None = None
        for ts in self.source.bar_timestamps():
            d = date_of(ts)
            if not self.clock.is_trading_day(d):
                continue
            if d != current:
                if current is not None:
                    self._end_day(current)
                self._start_day()
                current = d
            self.process_bar(ts, self.source.candles_at(ts))
        if current is not None:
            self._end_day(current)
        self.repo.end_run(self.run_id, int(time.time()))
        return self.run_id

    def _start_day(self) -> None:
        self._day = _DayCounters()
        self.disabled_strategies.clear()

    def _end_day(self, d: date) -> None:
        unrealised = self.broker.unrealised_pnl(self._last_close)
        self.repo.upsert_daily_pnl(self.run_id, d.isoformat(), self._day.realised, unrealised,
                                   self._day.fills, self._day.entries_placed)

    # -- per bar ----------------------------------------------------------------
    def process_bar(self, ts: int, candles: dict[str, Candle]) -> None:
        for sym, c in candles.items():
            self._last_close[sym] = c.close
            self._history.setdefault(sym, deque(maxlen=self.cfg.ai.candles_in_context)).append(c)

        self._record(self.broker.on_bar(ts, candles))
        if not self._day.squared_off and self.clock.square_off_due(ts):
            # Latched per day: a missing bar at exactly the square-off time cannot skip it.
            self._record(self.broker.square_off(ts, candles))
            self._day.squared_off = True

        kill = read_kill_switch(self.cfg.paths.kill_switch)
        if kill.flatten and not self._day.flattened:
            self._flatten(ts, candles)

        signals = self._run_strategies(candles)
        if not signals:
            return
        if not self.clock.entries_allowed(ts):
            # Audit trail: every dropped signal gets a row (spec 6), even outside entry hours.
            for _, sig in signals:
                sid = self.repo.insert_signal(self.run_id, sig)
                self.repo.insert_risk_decision(self.run_id, sid, False, "entries_closed", 0)
            return
        self._place(ts, candles, signals, kill)  # a live kill switch is rejected inside evaluate()

    def _run_strategies(self, candles: dict[str, Candle]) -> list[tuple[Strategy, Signal]]:
        out: list[tuple[Strategy, Signal]] = []
        for strat in self.strategies:
            if strat.name in self.disabled_strategies:
                continue
            for sym, c in candles.items():
                try:
                    sig = strat.on_candle(c)
                except Exception:  # noqa: BLE001 - spec 11: disable strategy for the day
                    log.exception("strategy %s failed on %s; disabled for the day", strat.name, sym)
                    self.disabled_strategies.add(strat.name)
                    break
                if sig is not None and strat.is_ready(sym):
                    out.append((strat, sig))
        return out

    def _lev(self, product: str) -> float:
        return self.cfg.risk.mis_leverage if product == "MIS" else 1.0

    def _place(self, ts: int, candles: dict[str, Candle], signals: list[tuple[Strategy, Signal]], kill: KillState) -> None:
        state = PortfolioState(
            capital=self.cfg.capital,
            open_symbols=set(self.broker.open_positions()),
            pending_symbols=self.broker.pending_symbols(),
            realised_today=self._day.realised,
            unrealised=self.broker.unrealised_pnl(self._last_close),
            entries_today=self._day.entries_placed,
            cooldown_until=dict(self._cooldown_until),
        )
        reserved_cash = 0.0  # margin claimed by approvals earlier in this same bar
        batch: list[tuple[int, ApprovedOrder, Candidate]] = []
        for strat, sig in signals:
            sid = self.repo.insert_signal(self.run_id, sig)
            margin = self.broker.available_margin(sig.product) - reserved_cash * self._lev(sig.product)
            res = evaluate(sig, state, self.cfg.risk, self.lot_sizes.get(sig.symbol, 1), margin, kill)
            if isinstance(res, Rejection):
                self.repo.insert_risk_decision(self.run_id, sid, False, res.reason, 0)
                if res.reason == "daily_loss_cap" and self.cfg.risk.flatten_on_daily_cap and not self._day.flattened:
                    self._flatten(ts, candles)
                continue
            self.repo.insert_risk_decision(self.run_id, sid, True, "ok", res.quantity)
            state.pending_symbols.add(sig.symbol)
            state.entries_today += 1
            reserved_cash += sig.entry_price * res.quantity / self._lev(sig.product)
            cand = Candidate(sig, res.quantity, strat.snapshot(sig.symbol), tuple(self._history.get(sig.symbol, ())))
            batch.append((sid, res, cand))
        if not batch:
            return
        decisions = self.ai_filter.review([c for _, _, c in batch])
        if len(decisions) != len(batch):  # zip would silently drop the tail; 3.9 has no strict=
            raise RuntimeError(f"{self.ai_filter.kind} returned {len(decisions)} decisions for {len(batch)} candidates")
        for (sid, order, _), dec in zip(batch, decisions):
            if dec.signal is not order.signal:
                raise RuntimeError(f"{self.ai_filter.kind} returned decisions out of order")
            self.repo.insert_ai_decision(self.run_id, sid, dec.filter_kind, dec.approved, dec.reason,
                                         dec.confidence, dec.latency_ms, dec.failure)
            if not dec.approved:
                continue
            side = "BUY" if order.signal.direction == "LONG" else "SELL"
            self.repo.insert_order(self.run_id, order.client_id, sid, "ENTRY", side, order.quantity,
                                   order.signal.entry_price, "PENDING", ts)
            self.broker.place_entry(order)
            self._day.entries_placed += 1

    def _flatten(self, ts: int, candles: dict[str, Candle]) -> None:
        self._record(self.broker.square_off(ts, candles, products=("MIS", "CNC"), reason="FLATTEN"))
        self._day.flattened = True

    # -- persistence of broker events ------------------------------------------
    def _record(self, events: list[BrokerEvent]) -> None:
        for ev in events:
            if isinstance(ev, Filled):
                ev.position.db_id = self.repo.insert_position(self.run_id, ev.position)
                self.repo.update_order(self.run_id, ev.order.client_id, "FILLED", ev.ts)
                oid = self.repo.order_id(self.run_id, ev.order.client_id)
                if oid is not None:
                    self.repo.insert_fill(oid, ev.position.quantity, ev.position.avg_price, ev.ts)
                self._day.fills += 1
            elif isinstance(ev, Unfilled):
                self.repo.update_order(self.run_id, ev.order.client_id, "UNFILLED", ev.ts)
                log.info("unfilled %s %s: %s", ev.order.signal.symbol, ev.order.client_id, ev.reason)
            elif isinstance(ev, Closed):
                p = ev.position
                if p.db_id is not None:
                    self.repo.close_position(p.db_id, p.closed_ts, p.exit_price, p.exit_reason, p.pnl)
                self._day.realised += p.pnl or 0.0
                if p.exit_reason == "STOP":
                    self._cooldown_until[p.symbol] = p.closed_ts + self.cfg.risk.cooldown_bars * self.interval_sec
