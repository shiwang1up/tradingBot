"""The per-bar cycle (spec sections 4-8). Mode-independent given a broker and clock; subclasses supply the loop.

Order inside a bar:
  0. usable -> strip the index -> feed the regime filter
                          drop unusable candles, then strip the index; the regime filter sees it, nothing else does
  1. broker.on_bar        fills pending entries at this bar's open, simulates exits
  2. square-off           once per day, from the square-off bar onward (latched)
  3. daily loss cap       flatten once per day if breached and configured to
  4. kill switch          flatten if requested
  5. strategies           every candle feeds every strategy (indicators stay warm)
  6. rank -> regime -> risk -> AI -> place  only if entries are allowed; a live kill switch rejects inside evaluate()

A bar handed in with now_ts past the deadline (paper) records its signals as `stale` and places
nothing (spec 8.6).

"PnL for the day" (spec 6.2) is realised today, net of charges, plus the change in unrealised
since the day opened, so a position carried overnight only charges today's move against today's
cap. Unrealised PnL is gross of the exit charges an open position will still pay when it closes,
so the daily-loss cap can trigger late by at most roughly the brokerage cap plus taxes per open
position; this is accepted.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Optional

from tradebot.ai.filter import AIFilter
from tradebot.config import Config, resolved_config
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, date_of, iso_ist
from tradebot.execution.broker import Broker, BrokerEvent, Closed, Filled, Unfilled
from tradebot.risk.engine import PortfolioState, evaluate
from tradebot.risk.killswitch import KillState, read_kill_switch
from tradebot.risk.regime import NOT_READY, RegimeFilter
from tradebot.store.repo import Repo
from tradebot.strategy.base import Strategy
from tradebot.strategy.ta import IndicatorSet, validate_indicator_params
from tradebot.types import ApprovedOrder, Candidate, Candle, Rejection, Signal

log = logging.getLogger("tradebot.engine")


@dataclass
class DayCounters:
    realised: float = 0.0
    entries_placed: int = 0
    fills: int = 0
    flattened: bool = False
    squared_off: bool = False
    unrealised_at_open: float = 0.0  # mark of positions carried in from earlier days


class Engine:
    """Mode-independent per-bar cycle. BacktestEngine replays stored bars; PaperEngine (engine/paper.py)
    feeds live bars on the wall clock. Everything here is shared; only the loop that calls process_bar differs."""

    def __init__(self, cfg: Config, repo: Repo, strategies: list[Strategy], broker: Broker, ai_filter: AIFilter,
                 clock: SessionClock, lot_sizes: dict[str, int], run_id: str, mode: str):
        self.cfg = cfg
        self.repo = repo
        self.strategies = strategies
        self.broker = broker
        self.ai_filter = ai_filter
        self.clock = clock
        self.lot_sizes = lot_sizes
        self.run_id = run_id
        self.mode = mode
        self.interval_sec = cfg.execution.interval_minutes * 60
        self._history: dict[str, deque[Candle]] = {}
        self._indicators: dict[str, IndicatorSet] = {}  # shared technical context, one set per symbol
        self._indicator_params = validate_indicator_params(cfg.strategy.get("indicators"))  # fail at start, not mid-run
        self._last_close: dict[str, float] = {}
        self._cooldown_until: dict[str, int] = {}
        self.disabled_strategies: set[str] = set()
        self._day = DayCounters()
        self._index_symbol = cfg.data.index_symbol
        self._regime = RegimeFilter(cfg.regime.ema_period) if cfg.regime.enabled else None
        self._composite_level = 100.0  # regime.source == "composite": chained from 100, unused otherwise
        self._regime_stale_warned_on: Optional[date] = None  # rate-limits the missing-index-candle warning
        self._regime_miss_streak = 0  # consecutive index-less bars (source 'index'); reported on recovery

    def _reenable_strategies(self) -> None:
        """A strategy disabled earlier missed bars; its incremental indicators would silently carry
        state across the hole. Reset so is_ready() gates signals until they are warm again."""
        for strat in self.strategies:
            if strat.name in self.disabled_strategies:
                for sym in self._history:
                    strat.reset(sym)
        self.disabled_strategies.clear()

    def _start_day(self) -> None:
        self._reenable_strategies()
        self._day = DayCounters(unrealised_at_open=self._unrealised_now())

    def _end_day(self, d: date, ts: int) -> None:
        # A day whose bar stream ends before the square-off bar must still not carry MIS overnight.
        leftovers = self.broker.square_off(ts, {}, last_prices=self._last_close)
        if leftovers:
            log.warning("day %s ended before square-off; closed %d intraday position(s) at last price",
                        d, len(leftovers))
            self._record(leftovers)
        # Entries queued on the last bar must not fill against tomorrow's open on a stale signal.
        self._record(self.broker.cancel_pending(ts, "day_end"))
        self._write_daily_row(d)

    def _write_daily_row(self, d: date) -> None:
        self.repo.upsert_daily_pnl(self.run_id, d.isoformat(), self._day.realised, self._unrealised_today(),
                                   self._day.fills, self._day.entries_placed)

    def _unrealised_now(self) -> float:
        return self.broker.unrealised_pnl(self._last_close)

    def _unrealised_today(self) -> float:
        return self._unrealised_now() - self._day.unrealised_at_open

    # -- per bar ----------------------------------------------------------------
    def _usable(self, candles: dict[str, Candle]) -> dict[str, Candle]:
        """Drop a candle whose open, high, low or close is not finite and > 0 before anything (last
        close, history, indicators, the broker) sees it. A stored row can be bad even though the
        Groww parser now skips such rows on the way in: it may predate that check, or come from
        HistoricalSource/load_candles/Repo.insert_candles, none of which validate. The dropped
        symbol is then simply absent from the bar, which already has tested semantics: exits are
        deferred, square-off falls back to the last good price, a pending entry is recorded
        unfilled (no_candle)."""
        out: dict[str, Candle] = {}
        for sym, c in candles.items():
            if all(math.isfinite(v) and v > 0 for v in (c.open, c.high, c.low, c.close)):
                out[sym] = c
            else:
                log.warning("dropping unusable candle for %s at %s: non-finite or non-positive OHLC",
                           sym, iso_ist(c.ts), extra={"symbol": sym})
        return out

    def _strip_index(self, candles: dict[str, Candle]) -> tuple[dict[str, Candle], Optional[Candle]]:
        """The tradable candles of a bar, and the index candle if this bar carries one - so no
        strategy, indicator, broker call or AI prompt ever sees the index. Pure: it neither reads
        nor writes the regime filter; _feed_regime does that separately. Must run after _usable (a
        bad index candle is dropped, not fed to the filter) and before _observe."""
        idx = self._index_symbol
        index_candle = candles.get(idx) if idx else None
        tradable = {s: c for s, c in candles.items() if s != idx} if index_candle is not None else candles
        return tradable, index_candle

    def _feed_regime(self, index_candle: Optional[Candle], tradable: dict[str, Candle], quiet: bool = False) -> None:
        """Update the regime filter for this bar, if enabled. Must run before _observe - already
        the order in process_bar and PaperEngine.warm - because with source 'composite' this reads
        the previous closes that _observe is about to overwrite. With no index bar at this
        timestamp (source 'index') the filter simply keeps its last state; if the bar was otherwise
        tradable, that also logs a warning (see `_warn_stale_regime`) - in paper mode a hole in the
        index feed could otherwise last all day with nothing in the log to say so. Consecutive
        index-less bars are counted (`_regime_miss_streak`) so that when an index candle arrives
        again, one line reports how many bars it was missing for.

        `quiet=True` (passed by `PaperEngine.warm`) replays stored bars without touching the log or
        the once-a-day warning slot: a cold start commonly warms through days of history that
        predate the index feed, and none of that is paper's live operation the warning exists for.
        The miss streak itself still updates while quiet, so a recovery once live bars resume
        reports the whole gap, warm-up bars included.

        The `composite` source is the equal-weight mean of the universe's close-to-close bar
        returns, chained from 100, for when index history is unavailable; the index candle, if
        present, is stripped but not fed to it. It only ever sees this bar's own tradable candles
        (`tradable`), so the first bar of a day carries the overnight gap exactly as the real index
        does, a symbol returning after a missing bar contributes a multi-bar return, and an
        unadjusted split moves the level by about 1/N in one bar - all accepted, same as the index
        source's own EMA would react to an unadjusted split.

        Average only over symbols with both a previous close and a usable candle this bar; the
        update is skipped (state and level left unchanged) when that set is empty, when it is too
        small to trust (see `_composite_return`), or when the resulting level would be non-finite
        or <= 0."""
        if self._regime is None:
            return
        if self.cfg.regime.source == "index":
            if index_candle is not None:
                if self._regime_miss_streak and not quiet:
                    log.warning("regime filter (source: index) saw its index candle again at %s: "
                               "index candle back after %d bar(s)", iso_ist(index_candle.ts),
                               self._regime_miss_streak)
                self._regime_miss_streak = 0
                self._regime.update(index_candle.close)
            elif tradable:
                self._regime_miss_streak += 1
                if not quiet:
                    self._warn_stale_regime(next(iter(tradable.values())).ts)
            return
        ret = self._composite_return(tradable)
        if ret is None:
            return
        level = self._composite_level * (1.0 + ret)
        if math.isfinite(level) and level > 0:
            self._composite_level = level
            self._regime.update(level)

    def _warn_stale_regime(self, ts: int) -> None:
        """A bar with tradable candles but no index candle (source 'index') leaves the filter on
        its last state - fine for one missed bar, but silent for the rest of a paper session if the
        index feed stays down. Rate-limited to once per trading day: `ts` comes from a tradable
        candle, since `_feed_regime` is never handed the method's own timestamp."""
        d = date_of(ts)
        if d != self._regime_stale_warned_on:
            self._regime_stale_warned_on = d
            log.warning("regime filter (source: index) saw no index candle at %s; keeping its last state (%s); "
                       "further misses today are not logged", iso_ist(ts), self._regime.state)

    def _composite_return(self, tradable: dict[str, Candle]) -> Optional[float]:
        """Equal-weight mean close-to-close return of this bar, over symbols that have both a
        previous close in `_last_close` and a usable candle this bar. None (no update) when that
        set is empty, or when it is too small to trust: a bar carrying returns for only a few of
        the symbols `_last_close` knows about (fetch failures in paper mode) would let two or
        three names drive the "index", so at least half of `_last_close` (never fewer than one)
        must be present. This guards against a bar where most known symbols are missing, not
        against a thin start: the partial first bar of a data set is not covered by it (the
        denominator is the symbols seen *so far*, so a thin first bar just sets a thin denominator
        for the second) - a thin start only ever costs the one update it lands on. `PaperEngine.resume`
        may also seed `_last_close` with an open position's entry price rather than a real close;
        that contributes one 1/N-weighted odd return on the first bar after a restart, accepted."""
        returns = [c.close / self._last_close[s] - 1.0 for s, c in tradable.items() if self._last_close.get(s)]
        if not returns or len(returns) < max(1, len(self._last_close) // 2):
            return None
        return sum(returns) / len(returns)

    def _observe(self, candles: dict[str, Candle]) -> None:
        """Update last closes, the AI context history and the shared indicators. Also used by the
        paper warm-up, which must not touch the broker."""
        for sym, c in candles.items():
            self._last_close[sym] = c.close
            self._history.setdefault(sym, deque(maxlen=self.cfg.ai.candles_in_context)).append(c)
            if sym not in self._indicators:  # not setdefault: that would build a throwaway set every bar
                self._indicators[sym] = IndicatorSet(self._indicator_params)
            self._indicators[sym].update(c)

    def process_bar(self, ts: int, candles: dict[str, Candle], now_ts: Optional[int] = None) -> None:
        """`now_ts` is the wall clock when the bar is handed in (paper); None means the bar is on time."""
        candles, index_candle = self._strip_index(self._usable(candles))
        self._feed_regime(index_candle, candles)  # before _observe: composite reads last closes it is about to overwrite
        self._observe(candles)

        self._record(self.broker.on_bar(ts, candles))
        if not self._day.squared_off and self.clock.square_off_due(ts):
            # Latched per day only once every intraday position is gone: a symbol with no candle at
            # the square-off bar closes at its last known price, and anything still open is retried.
            self._record(self.broker.square_off(ts, candles, last_prices=self._last_close))
            if not any(p.product == "MIS" for p in self.broker.open_positions().values()):
                self._day.squared_off = True

        # Both flatten triggers share one per-day latch, set only once nothing in the flattened
        # products remains open (mirrors the square-off latch): a close that failed this bar is
        # retried on the next one. Sharing is safe: once the cap is breached every entry is rejected
        # for the day, so nothing can be opened after a cap flatten for a kill flatten to close.
        if (self.cfg.risk.flatten_on_daily_cap and not self._day.flattened
                and self._state().daily_loss_breached(self.cfg.risk)):
            log.warning("daily loss cap breached at %s (%s); flattening", ts, iso_ist(ts))
            self._flatten(ts, candles)

        kill = read_kill_switch(self.cfg.paths.kill_switch)
        if kill.flatten and not self._day.flattened:
            self._flatten(ts, candles)

        signals = self._run_strategies(candles)
        if not signals:
            return
        if now_ts is not None and now_ts - (ts + self.interval_sec) > self.cfg.execution.bar_deadline_sec:
            for _, sig in signals:
                sid = self.repo.insert_signal(self.run_id, sig)
                self.repo.insert_risk_decision(self.run_id, sid, False, "stale", 0)
            log.warning("bar %s handed in %ds after its close; %d signal(s) dropped as stale: %s", iso_ist(ts),
                        now_ts - (ts + self.interval_sec), len(signals), sorted({sig.symbol for _, sig in signals}))
            return
        if not self.clock.entries_allowed(ts):
            # Audit trail: every dropped signal gets a row (spec 6), even outside entry hours.
            for _, sig in signals:
                sid = self.repo.insert_signal(self.run_id, sig)
                self.repo.insert_risk_decision(self.run_id, sid, False, "entries_closed", 0)
            return
        self._place(ts, signals, kill)

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
        # Deterministic in every mode: backtest bars arrive in SQL order and paper bars in universe
        # order, so without this the two modes could hand the last free slot to different symbols.
        out.sort(key=lambda pair: (-pair[1].priority, pair[1].symbol))
        return out

    def _lev(self, product: str) -> float:
        return self.cfg.risk.mis_leverage if product == "MIS" else 1.0

    def _state(self) -> PortfolioState:
        return PortfolioState(
            capital=self.cfg.capital,
            open_symbols=set(self.broker.open_positions()),
            pending_symbols=self.broker.pending_symbols(),
            realised_today=self._day.realised,
            unrealised=self._unrealised_today(),
            entries_today=self._day.entries_placed,
            cooldown_until=dict(self._cooldown_until),
        )

    def _place(self, ts: int, signals: list[tuple[Strategy, Signal]], kill: KillState) -> None:
        state = self._state()
        reserved_cash = 0.0  # margin claimed by approvals earlier in this same bar
        batch: list[tuple[int, ApprovedOrder, Candidate]] = []
        for strat, sig in signals:
            sid = self.repo.insert_signal(self.run_id, sig)
            # Runs before the risk check so a blocked signal takes no slot: it never joins
            # state.pending_symbols or bumps entries_today, leaving the slot free for the next
            # signal this same bar. Consequence: a `regime` rejection masks whatever reason the
            # risk check would have given (kill switch, daily loss cap, max entries); the `regime`
            # count in a report is the number of signals gated, not the number of trades removed.
            blocked = self._regime.rejection(sig.direction) if self._regime is not None else None
            if blocked is not None:
                self.repo.insert_risk_decision(self.run_id, sid, False, blocked, 0)
                log.info("signal %s %s rejected: %s", sig.direction, sig.symbol, blocked, extra={"symbol": sig.symbol})
                continue
            margin = self.broker.available_margin(sig.product) - reserved_cash * self._lev(sig.product)
            res = evaluate(sig, state, self.cfg.risk, self.lot_sizes.get(sig.symbol, 1), margin, kill)
            if isinstance(res, Rejection):
                self.repo.insert_risk_decision(self.run_id, sid, False, res.reason, 0)
                log.info("signal %s %s rejected: %s", sig.direction, sig.symbol, res.reason, extra={"symbol": sig.symbol})
                continue
            self.repo.insert_risk_decision(self.run_id, sid, True, "ok", res.quantity)
            # Deliberately conservative within the bar: a risk-approved candidate holds its slot,
            # symbol and margin even if the AI then rejects it. The day counter counts placements.
            state.pending_symbols.add(sig.symbol)
            state.entries_today += 1
            reserved_cash += sig.entry_price * res.quantity / self._lev(sig.product)
            shared = self._indicators[sig.symbol].snapshot() if sig.symbol in self._indicators else {}
            cand = Candidate(sig, res.quantity, {**shared, **strat.snapshot(sig.symbol)},
                             tuple(self._history.get(sig.symbol, ())))
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
                                         dec.confidence, dec.latency_ms, dec.failure,
                                         input_tokens=dec.input_tokens, output_tokens=dec.output_tokens,
                                         cache_read_tokens=dec.cache_read_tokens,
                                         cache_write_tokens=dec.cache_write_tokens)
            if not dec.approved:
                continue
            side = "BUY" if order.signal.direction == "LONG" else "SELL"
            self.repo.insert_order(self.run_id, order.client_id, sid, "ENTRY", side, order.quantity,
                                   order.signal.entry_price, "PENDING", ts)
            self.broker.place_entry(order)
            self._day.entries_placed += 1
            log.info("entry %s %s x%d @ %.2f stop %.2f target %s", side, order.signal.symbol, order.quantity,
                     order.signal.entry_price, order.signal.stop_price, order.signal.target_price,
                     extra={"symbol": order.signal.symbol, "client_id": order.client_id})

    def _flatten(self, ts: int, candles: dict[str, Candle]) -> None:
        products = ("MIS", "CNC")
        self._record(self.broker.square_off(ts, candles, products=products, reason="FLATTEN",
                                            last_prices=self._last_close))
        if not any(p.product in products for p in self.broker.open_positions().values()):
            self._day.flattened = True

    # -- persistence of broker events ------------------------------------------
    def _record(self, events: list[BrokerEvent]) -> None:
        for ev in events:
            if isinstance(ev, Filled):
                ev.position.db_id = self.repo.insert_position(self.run_id, ev.position)
                self.repo.update_order(self.run_id, ev.order.client_id, "FILLED", ev.ts)
                oid = self.repo.order_id(self.run_id, ev.order.client_id)
                if oid is None:
                    log.error("fill for unknown order %s %s", ev.position.symbol, ev.order.client_id)
                else:
                    self.repo.insert_fill(oid, ev.position.quantity, ev.position.avg_price, ev.ts)
                self._day.fills += 1
                log.info("filled %s x%d @ %.2f", ev.position.symbol, ev.position.quantity, ev.position.avg_price,
                         extra={"symbol": ev.position.symbol, "client_id": ev.order.client_id})
            elif isinstance(ev, Unfilled):
                self.repo.update_order(self.run_id, ev.order.client_id, "UNFILLED", ev.ts)
                log.info("unfilled %s %s: %s", ev.order.signal.symbol, ev.order.client_id, ev.reason)
            elif isinstance(ev, Closed):
                p = ev.position
                if p.db_id is not None:
                    self.repo.close_position(p.db_id, p.closed_ts, p.exit_price, p.exit_reason, p.pnl, charges=p.charges)
                self._day.realised += (p.pnl or 0.0) - (p.charges or 0.0)  # the daily cap runs on net
                log.info("closed %s %s @ %.2f pnl %.2f charges %.2f", p.symbol, p.exit_reason, p.exit_price or 0.0,
                         p.pnl or 0.0, p.charges or 0.0, extra={"symbol": p.symbol, "client_id": p.client_id})
                if p.exit_reason == "STOP":
                    # Wall-clock cooldown: an overnight gap absorbs it, which is intended (intraday rule).
                    self._cooldown_until[p.symbol] = p.closed_ts + self.cfg.risk.cooldown_bars * self.interval_sec


class BacktestEngine(Engine):
    def __init__(self, cfg: Config, repo: Repo, source: HistoricalSource, strategies: list[Strategy],
                 broker: Broker, ai_filter: AIFilter, clock: SessionClock, lot_sizes: dict[str, int],
                 run_id: str, mode: str = "backtest"):
        super().__init__(cfg, repo, strategies, broker, ai_filter, clock, lot_sizes, run_id, mode)
        self.source = source

    # -- lifecycle -------------------------------------------------------------
    def run(self) -> str:
        self.repo.create_run(self.run_id, self.mode, int(time.time()), json.dumps(resolved_config(self.cfg), default=str))
        current: date | None = None
        last_ts = 0
        bars = self.source.bar_timestamps()
        log.info("run %s (%s) started: %d bars, strategies=%s", self.run_id, self.mode, len(bars),
                 [s.name for s in self.strategies])
        try:
            for ts in bars:
                candles = self.source.candles_at(ts)
                d = date_of(ts)
                if not self.clock.is_trading_day(d):
                    continue
                if self._index_symbol and candles and all(sym == self._index_symbol for sym in candles):
                    # Nothing tradable this instant: feed the filter and skip everything else (day
                    # bookkeeping and last_ts included) so this timestamp never reaches broker.on_bar,
                    # which would otherwise mark every pending entry unfilled (no_candle) for a bar
                    # with no real trading activity. Checked after is_trading_day so a holiday's
                    # index bars are not fed either, the same as a holiday's mixed bars are skipped.
                    # No tradable candles here, so composite source has no returns and is a no-op.
                    _, index_candle = self._strip_index(self._usable(candles))
                    self._feed_regime(index_candle, {})
                    continue
                if d != current:
                    if current is not None:
                        self._end_day(current, last_ts)
                    self._start_day()
                    current = d
                last_ts = ts  # set before processing so a failure mid-bar still stamps this bar
                self.process_bar(ts, candles)
        finally:
            # A mid-run exception still leaves a closed run and, where possible, the last day's row.
            # Cleanup must never mask the original exception or skip end_run.
            try:
                if current is not None:
                    self._end_day(current, last_ts)
            except Exception:  # noqa: BLE001
                log.exception("end-of-day bookkeeping failed for run %s", self.run_id)
            finally:
                if current is not None and self._regime is not None and self._regime.state == NOT_READY:
                    # This warning protects callers that bypass the CLI (scripts/orb_experiment.py does
                    # its own index check, but not the coverage check): the filter never warmed up,
                    # so every entry of this run was silently rejected as regime_not_ready. Skipped
                    # when the run processed no trading day at all (current is None): there were no
                    # entries to have silently rejected.
                    why = ("fewer than ema_period usable index candles reached the filter" if self.cfg.regime.source == "index"
                          else "the universe never gave the composite enough breadth to warm up")
                    log.warning("run %s ended with the regime filter still NOT_READY (%s); every entry "
                               "was rejected as regime_not_ready", self.run_id, why)
                self.repo.end_run(self.run_id, int(time.time()))
                log.info("run %s ended: realised %.2f on last day, %d closed positions",
                         self.run_id, self._day.realised, len(getattr(self.broker, "closed", [])))
        return self.run_id
