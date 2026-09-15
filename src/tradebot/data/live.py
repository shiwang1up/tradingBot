"""Live bars for paper mode: the just-closed official candle for every universe symbol, fetched over
REST at each bar boundary (paper spec, 'Bar source'). Nothing here places orders."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from tradebot.data.historical import Fetcher
from tradebot.engine.clock import SessionClock, date_of, iso_ist
from tradebot.store.repo import Repo
from tradebot.types import Candle

log = logging.getLogger("tradebot.live")


class LiveBarSource:
    """`fetcher` has the historical.Fetcher shape (symbol, exchange, start_ts, end_ts, interval) so the
    CLI passes GrowwAdapter.fetch_candles and tests pass a fake."""

    def __init__(self, fetcher: Fetcher, repo: Repo, symbols: list[str], exchange: str, interval: int,
                 concurrency: int, clock: SessionClock):
        self.fetcher = fetcher
        self.repo = repo
        self.symbols = list(symbols)
        self.exchange = exchange
        self.interval = interval
        self.interval_sec = interval * 60
        self.concurrency = max(1, concurrency)
        self.clock = clock

    def fetch_range(self, start_ts: int, end_ts: int) -> dict[int, dict[str, Candle]]:
        """Bars with start_ts <= ts <= end_ts (bar open times), keyed by ts then symbol. Requests one
        window per symbol from the session open of start_ts's day to the close of the end bar; every
        completed in-session bar that comes back is stored, so the day grows the candle cache. A
        symbol whose fetch raises is absent from the result and logged; the bar in progress
        (ts > end_ts) is neither stored nor returned."""
        win_start = self.clock.open_ts(date_of(start_ts))
        win_end = end_ts + self.interval_sec

        def one(sym: str) -> tuple:
            try:
                return sym, self.fetcher(sym, self.exchange, win_start, win_end, self.interval)
            except Exception as e:  # noqa: BLE001 - one symbol's failure must not sink the bar
                log.warning("candle fetch failed for %s: %s: %s", sym, type(e).__name__, e, extra={"symbol": sym})
                return sym, None

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            results = list(pool.map(one, self.symbols))

        out: dict[int, dict[str, Candle]] = {}
        failed = 0
        for sym, candles in results:  # SQLite writes stay on this thread
            if candles is None:
                failed += 1
                continue
            completed = [c for c in candles if c.ts <= end_ts and self.clock.in_session(c.ts)]
            self.repo.insert_candles(completed, self.interval)
            for c in completed:
                if c.ts >= start_ts:
                    out.setdefault(c.ts, {})[sym] = c
        if self.symbols and failed == len(self.symbols):
            log.error("every candle fetch failed for bars %s..%s", iso_ist(start_ts), iso_ist(end_ts))
        return out

    def fetch_bar(self, bar_ts: int) -> dict[str, Candle]:
        return self.fetch_range(bar_ts, bar_ts).get(bar_ts, {})
