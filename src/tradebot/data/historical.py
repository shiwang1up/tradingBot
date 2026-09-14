"""Incremental candle download into SQLite, and the backtest candle source."""
from __future__ import annotations

from collections.abc import Callable

from tradebot.store.repo import Repo
from tradebot.types import Candle

# Request window per interval in minutes: half of Groww's documented maximum for
# get_historical_candles (1-5m: 30 days, 10-30m: 90 days, 1h+: 180 days), so a
# window that is inclusive on both ends can never trip the limit (spec 4.2).
CHUNK_DAYS = {1: 15, 2: 15, 3: 15, 5: 15, 10: 45, 15: 45, 30: 45, 60: 90, 240: 90, 1440: 90}
DAY = 86400

Fetcher = Callable[[str, str, int, int, int], list[Candle]]  # (symbol, exchange, start, end, interval)


def fetch_incremental(repo: Repo, fetcher: Fetcher, symbols: list[str], exchange: str, interval: int,
                      lookback_days: int, now_ts: int, full: bool = False,
                      log: Callable[[str], None] = lambda s: None) -> dict[str, int]:
    """For each symbol, fetch from (latest stored ts + 1) or (now - lookback) up to now_ts.

    Idempotent: inserts use INSERT OR IGNORE on (symbol, ts, interval). Returns inserted counts.
    """
    chunk = CHUNK_DAYS[interval] * DAY
    inserted: dict[str, int] = {}
    for sym in symbols:
        latest = None if full else repo.latest_candle_ts(sym, interval)
        start = now_ts - lookback_days * DAY if latest is None else latest + 1
        n = 0
        while start < now_ts:
            end = min(start + chunk, now_ts)
            n += repo.insert_candles(fetcher(sym, exchange, start, end, interval), interval)
            start = end + 1
        inserted[sym] = n
        log(f"{sym}: +{n} candles")
    return inserted


class HistoricalSource:
    """Replays stored candles one bar at a time. bar_timestamps() is sorted ascending."""

    def __init__(self, candles: list[Candle]):
        self._by_ts: dict[int, dict[str, Candle]] = {}
        for c in candles:
            self._by_ts.setdefault(c.ts, {})[c.symbol] = c
        self._ts = sorted(self._by_ts)

    @classmethod
    def from_repo(cls, repo: Repo, symbols: list[str], interval: int, start_ts: int, end_ts: int) -> "HistoricalSource":
        return cls(repo.load_candles(symbols, interval, start_ts, end_ts))

    def bar_timestamps(self) -> list[int]:
        return list(self._ts)

    def candles_at(self, ts: int) -> dict[str, Candle]:
        return dict(self._by_ts.get(ts, {}))
