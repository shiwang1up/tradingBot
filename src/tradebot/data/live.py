"""Live bars for paper mode: the just-closed official candle for every symbol in `symbols` - the
universe, plus the regime index when the CLI decides the filter needs it - fetched over REST at
each bar boundary (paper spec, 'Bar source'). Nothing here places orders."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Optional

from tradebot.data.historical import Fetcher
from tradebot.engine.clock import SessionClock, date_of, iso_ist
from tradebot.store.repo import Repo
from tradebot.types import Candle

log = logging.getLogger("tradebot.live")


class LiveBarSource:
    """`fetcher` has the historical.Fetcher shape (symbol, exchange, start_ts, end_ts, interval) so the
    CLI passes GrowwAdapter.fetch_candles and tests pass a fake."""

    def __init__(self, fetcher: Fetcher, repo: Repo, symbols: list[str], exchange: str, interval: int,
                 concurrency: int, clock: SessionClock, budget_sec: Optional[float] = None,
                 index_symbol: str = ""):
        self.fetcher = fetcher
        self.repo = repo
        self.symbols = list(symbols)
        self.exchange = exchange
        self.interval = interval
        self.interval_sec = interval * 60
        self.concurrency = max(1, concurrency)
        self.clock = clock
        self.budget_sec = budget_sec  # None waits for every symbol; paper passes the bar deadline
        self.index_symbol = index_symbol  # in `symbols` when the regime filter needs it; "" otherwise.
        # Never traded, so it is excluded from the tradable counts below; storage and fetching treat
        # it like any other symbol.

    def fetch_range(self, start_ts: int, end_ts: int) -> dict[int, dict[str, Candle]]:
        """Bars with start_ts <= ts <= end_ts (bar open times), keyed by ts then symbol. Requests one
        window per symbol from the session open of start_ts's day to the close of the end bar; every
        completed in-session bar that comes back is stored, so the day grows the candle cache. A
        symbol whose fetch raises is absent from the result and logged; the bar in progress
        (ts > end_ts) is neither stored nor returned."""
        t0 = time.monotonic()
        win_start = self.clock.open_ts(date_of(start_ts))
        win_end = end_ts + self.interval_sec

        def one(sym: str) -> tuple:
            try:
                return sym, self.fetcher(sym, self.exchange, win_start, win_end, self.interval)
            except Exception as e:  # noqa: BLE001 - one symbol's failure must not sink the bar
                log.warning("candle fetch failed for %s: %s: %s", sym, type(e).__name__, e, extra={"symbol": sym})
                return sym, None

        # A symbol still fetching when the budget runs out counts as failed for this bar: one slow name
        # must not push every other symbol's signals past the deadline. Its worker finishes in the
        # background and is discarded; the next bar fetches it afresh.
        pool = ThreadPoolExecutor(max_workers=self.concurrency)
        futures = {pool.submit(one, sym): sym for sym in self.symbols}
        done, pending = wait(futures, timeout=self.budget_sec)
        pool.shutdown(wait=False, cancel_futures=True)
        for f in pending:
            log.warning("candle fetch for %s did not finish within %.0fs; skipped for this bar", futures[f],
                        self.budget_sec or 0.0, extra={"symbol": futures[f]})

        out: dict[int, dict[str, Candle]] = {}
        stored: list[Candle] = []
        results = dict(f.result() for f in done)
        ok: dict[str, bool] = {}
        for sym in self.symbols:  # symbol order, so every bar reaches the strategies the same way; SQLite stays here
            candles = results.get(sym)  # None: over budget (absent from `results`) or the fetch raised
            if candles is None:
                ok[sym] = False
                continue
            ok[sym] = True
            completed = [c for c in candles if c.ts <= end_ts and self.clock.in_session(c.ts)]
            stored.extend(completed)
            for c in completed:
                if c.ts >= start_ts:
                    out.setdefault(c.ts, {})[sym] = c
        new_bars = self.repo.insert_candles(stored, self.interval)  # the whole window is offered: earlier gaps heal
        # The index is never traded, so it counts toward neither the "every fetch failed" alarm nor
        # the tradable tally: an index-only outage must not drown out (or hide behind) the stocks.
        tradables = [s for s in self.symbols if s != self.index_symbol]
        tradable_failed = sum(1 for s in tradables if not ok.get(s, False))
        if tradables and tradable_failed == len(tradables):
            log.error("every candle fetch failed for bars %s..%s", iso_ist(start_ts), iso_ist(end_ts))
        index_note = (f"; index {'ok' if ok.get(self.index_symbol, False) else 'missing'}"
                     if self.index_symbol else "")
        log.info("bars %s..%s: %d of %d symbols in %.1fs, %d failed, %d new bars stored%s", iso_ist(start_ts),
                 iso_ist(end_ts), len(tradables) - tradable_failed, len(tradables), time.monotonic() - t0,
                 tradable_failed, new_bars, index_note)
        return out

    def fetch_bar(self, bar_ts: int) -> dict[str, Candle]:
        return self.fetch_range(bar_ts, bar_ts).get(bar_ts, {})
