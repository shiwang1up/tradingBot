from datetime import date

import pytest

from tradebot.data.historical import CHUNK_DAYS, HistoricalSource, fetch_incremental
from tradebot.engine.clock import ist_epoch
from tradebot.execution.groww_adapter import parse_candles, with_retry
from tradebot.types import Candle

DAY = 86400


def test_parse_candles_accepts_epoch_seconds_millis_and_iso():
    resp = {"candles": [
        [1789357500, 100, 101, 99, 100.5, 1000],
        [1789357800000, "100.5", "102", "100", "101", "2000"],
        ["2026-09-14T09:25:00+05:30", 101, 101, 101, 101, 0],
    ]}
    out = parse_candles("RELIANCE", resp)
    assert [c.ts for c in out] == [1789357500, 1789357800, 1789358100]
    assert out[1].volume == 2000
    assert out[1].close == 101.0
    assert all(c.symbol == "RELIANCE" and c.source == "official" for c in out)


def test_parse_candles_empty():
    assert parse_candles("X", {}) == []
    assert parse_candles("X", {"candles": None}) == []


def test_with_retry_retries_then_succeeds():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("rate limit")
        return "ok"

    assert with_retry(fn, attempts=3, sleep=lambda s: None) == "ok"
    assert len(calls) == 3


def test_with_retry_gives_up_after_attempts():
    def fn():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        with_retry(fn, attempts=2, sleep=lambda s: None)


def test_with_retry_never_retries_auth_errors():
    class GrowwAPIAuthenticationException(Exception):
        pass

    calls = []

    def fn():
        calls.append(1)
        raise GrowwAPIAuthenticationException("bad totp")

    with pytest.raises(GrowwAPIAuthenticationException):
        with_retry(fn, attempts=3, sleep=lambda s: None)
    assert len(calls) == 1


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, symbol, exchange, start_ts, end_ts, interval):
        self.calls.append((symbol, start_ts, end_ts))
        # one candle per day boundary inside the window
        first_day = (start_ts // DAY) * DAY
        return [Candle(symbol, t, 1, 2, 0.5, 1.5, 10)
                for t in range(first_day, end_ts + 1, DAY) if start_ts <= t <= end_ts]


def test_fetch_incremental_first_run_chunks_the_lookback(repo):
    now = 100 * DAY
    f = FakeFetcher()
    result = fetch_incremental(repo, f, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now)
    starts = [c[1] for c in f.calls]
    assert starts[0] == now - 30 * DAY
    # 30-day lookback at 15-day chunks => 2 calls
    assert len(f.calls) == 2
    assert f.calls[0][2] == starts[0] + CHUNK_DAYS[5] * DAY
    assert result["RELIANCE"] == 31  # days 70..100 inclusive
    assert repo.latest_candle_ts("RELIANCE", 5) == now


def test_fetch_incremental_second_run_resumes_from_latest_and_is_idempotent(repo):
    now = 100 * DAY
    f = FakeFetcher()
    fetch_incremental(repo, f, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now)
    f2 = FakeFetcher()
    result = fetch_incremental(repo, f2, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now + 2 * DAY)
    assert f2.calls[0][1] == now + 1
    assert result["RELIANCE"] == 2
    # third run with nothing new inserts nothing
    f3 = FakeFetcher()
    assert fetch_incremental(repo, f3, ["RELIANCE"], "NSE", 5, 30, now + 2 * DAY)["RELIANCE"] == 0


def test_fetch_incremental_full_ignores_existing(repo):
    now = 100 * DAY
    fetch_incremental(repo, FakeFetcher(), ["RELIANCE"], "NSE", 5, 30, now)
    f = FakeFetcher()
    fetch_incremental(repo, f, ["RELIANCE"], "NSE", 5, 30, now, full=True)
    assert f.calls[0][1] == now - 30 * DAY


def test_historical_source_groups_by_bar(repo):
    repo.insert_candles([
        Candle("A", 100, 1, 1, 1, 1, 1), Candle("B", 100, 2, 2, 2, 2, 1),
        Candle("A", 400, 1, 1, 1, 1, 1),
    ], interval=5)
    src = HistoricalSource.from_repo(repo, ["A", "B"], interval=5, start_ts=0, end_ts=1000)
    assert src.bar_timestamps() == [100, 400]
    assert set(src.candles_at(100)) == {"A", "B"}
    assert set(src.candles_at(400)) == {"A"}
    assert src.candles_at(999) == {}
