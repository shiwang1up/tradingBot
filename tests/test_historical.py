# tests/test_historical.py
import pytest

from tradebot.data.historical import CHUNK_DAYS, HistoricalSource, fetch_incremental
from tradebot.execution.groww_adapter import (GrowwAdapter, GrowwAuthenticationError, candle_interval_name,
                                              groww_symbol, parse_candles, with_retry)
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


def test_parse_candles_respects_non_ist_offsets_and_z_suffix():
    resp = {"candles": [
        ["2026-09-14T03:45:00+00:00", 1, 1, 1, 1, 1],   # 09:15 IST expressed in UTC
        ["2026-09-14T03:45:00Z", 1, 1, 1, 1, 1],
        ["2026-09-14T09:15:00", 1, 1, 1, 1, 1],         # naive => IST
    ]}
    assert [c.ts for c in parse_candles("X", resp)] == [1789357500] * 3


def test_parse_candles_v2_rows_with_open_interest():
    resp = {"candles": [["2026-09-14T09:15:00", 100, 101, 99, 100.5, 1000, None]]}
    out = parse_candles("RELIANCE", resp)
    assert out[0].ts == 1789357500 and out[0].volume == 1000


def test_parse_candles_malformed_row_names_symbol():
    with pytest.raises(ValueError, match="RELIANCE"):
        parse_candles("RELIANCE", {"candles": [[1789357500, 1, 2]]})
    with pytest.raises(ValueError, match="RELIANCE"):
        parse_candles("RELIANCE", {"candles": [{"ts": 1}]})


def test_parse_candles_rejects_non_finite_or_inconsistent_ohlc():
    with pytest.raises(ValueError, match="bad OHLC"):
        parse_candles("X", {"candles": [[1, float("nan"), 2, 0.5, 1.5, 10]]})
    with pytest.raises(ValueError, match="bad OHLC"):
        parse_candles("X", {"candles": [[1, 1, 2, 0.5, 5.0, 10]]})  # close above high


def test_parse_candles_empty():
    assert parse_candles("X", {}) == []
    assert parse_candles("X", {"candles": None}) == []


def test_interval_names_and_groww_symbol():
    assert candle_interval_name(5) == "5minute"
    assert candle_interval_name(1440) == "1day"
    assert groww_symbol("NSE", "RELIANCE") == "NSE-RELIANCE"
    with pytest.raises(ValueError):
        candle_interval_name(7)


def test_with_retry_retries_with_backoff_then_succeeds():
    calls, delays = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("rate limit")
        return "ok"

    assert with_retry(fn, attempts=3, sleep=delays.append) == "ok"
    assert len(calls) == 3
    assert delays == [1.0, 2.0]


def test_with_retry_gives_up_after_attempts():
    def fn():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        with_retry(fn, attempts=2, sleep=lambda s: None)
    with pytest.raises(ValueError):
        with_retry(fn, attempts=0)


@pytest.mark.parametrize("exc_factory", [
    lambda: type("GrowwAPIAuthenticationException", (Exception,), {})("bad totp"),
    lambda: type("GrowwAPINotFoundException", (Exception,), {})("no such symbol"),
    # The SDK raises the generic class with a code for {"status": "FAILURE"} bodies.
    lambda: type("GrowwAPIException", (Exception,), {"code": "401"})("unauthorised"),
    lambda: type("GrowwAPIException", (Exception,), {"code": 403})("forbidden"),
])
def test_with_retry_never_retries_client_errors(exc_factory):
    calls = []

    def fn():
        calls.append(1)
        raise exc_factory()

    with pytest.raises(Exception):
        with_retry(fn, attempts=3, sleep=lambda s: None)
    assert len(calls) == 1


def test_with_retry_does_retry_generic_server_errors():
    calls = []

    def fn():
        calls.append(1)
        raise type("GrowwAPIException", (Exception,), {"code": "500"})("server")

    with pytest.raises(Exception):
        with_retry(fn, attempts=2, sleep=lambda s: None)
    assert len(calls) == 2


def test_fetch_candles_calls_v2_endpoint_with_ist_strings():
    calls = []

    class FakeClient:
        SEGMENT_CASH = "CASH"

        def get_historical_candles(self, **kw):
            calls.append(kw)
            return {"candles": [["2026-09-14T09:15:00", 1, 2, 0.5, 1.5, 10, None]]}

    adapter = GrowwAdapter("key", "secret")
    adapter._client = FakeClient()  # bypass TOTP auth
    out = adapter.fetch_candles("RELIANCE", "NSE", 1789357500, 1789357500 + 3600, 5)
    assert calls[0]["groww_symbol"] == "NSE-RELIANCE"
    assert calls[0]["candle_interval"] == "5minute"
    assert calls[0]["start_time"] == "2026-09-14 09:15:00"
    assert calls[0]["end_time"] == "2026-09-14 10:15:00"
    assert calls[0]["segment"] == "CASH"
    assert out[0].ts == 1789357500


def test_fetch_candles_validates_interval_before_any_call():
    adapter = GrowwAdapter("key", "secret")
    adapter._client = object()  # would explode if called
    with pytest.raises(ValueError):
        adapter.fetch_candles("RELIANCE", "NSE", 0, 1, 7)


def test_adapter_requires_credentials_and_validates_totp_format():
    with pytest.raises(GrowwAuthenticationError):
        GrowwAdapter("", "")
    with pytest.raises(GrowwAuthenticationError, match="GROWW_TOTP_SECRET|GROWW_API_SECRET"):
        GrowwAdapter("key")
    with pytest.raises(GrowwAuthenticationError, match="not a base32"):
        GrowwAdapter("key", totp_secret="TY#Fnotbase32")
    assert GrowwAdapter("key", totp_secret="jbsw y3dp ehpk 3pxp").flow == "totp"
    assert GrowwAdapter("key", api_secret="whatever-shape").flow == "approval"


def test_adapter_login_failure_is_a_single_clear_error(monkeypatch):
    import sys, types
    fake = types.ModuleType("growwapi")

    class GrowwAPI:
        @staticmethod
        def get_access_token(api_key, totp=None, secret=None):
            raise RuntimeError("403 not approved")

    fake.GrowwAPI = GrowwAPI
    monkeypatch.setitem(sys.modules, "growwapi", fake)
    a = GrowwAdapter("jwt", api_secret="s")
    with pytest.raises(GrowwAuthenticationError, match="approval flow.*approved daily"):
        a.client


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, symbol, exchange, start_ts, end_ts, interval):
        self.calls.append((symbol, start_ts, end_ts))
        # one candle per day boundary inside the window (both ends inclusive)
        first_day = (start_ts // DAY) * DAY
        return [Candle(symbol, t, 1, 2, 0.5, 1.5, 10)
                for t in range(first_day, end_ts + 1, DAY) if start_ts <= t <= end_ts]


def test_fetch_incremental_first_run_chunks_the_lookback(repo):
    now = 100 * DAY  # exactly on a bar boundary: the bar opening now is in progress, excluded
    f = FakeFetcher()
    result = fetch_incremental(repo, f, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now)
    starts = [c[1] for c in f.calls]
    assert starts[0] == now - 30 * DAY
    assert len(f.calls) == 2  # 30-day lookback at 15-day chunks
    assert f.calls[0][2] == starts[0] + CHUNK_DAYS[5] * DAY
    assert f.calls[1][1] == f.calls[0][2]  # windows share the boundary instant: no gap either way
    assert result["RELIANCE"] == 30  # days 70..99; day 100 belongs to the in-progress bar
    assert repo.latest_candle_ts("RELIANCE", 5) == now - DAY


def test_fetch_incremental_second_run_resumes_from_latest_and_is_idempotent(repo):
    now = 100 * DAY
    f = FakeFetcher()
    fetch_incremental(repo, f, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now)
    f2 = FakeFetcher()
    result = fetch_incremental(repo, f2, ["RELIANCE"], "NSE", interval=5, lookback_days=30, now_ts=now + 2 * DAY)
    assert f2.calls[0][1] == (now - DAY) + 1
    assert result["RELIANCE"] == 2
    f3 = FakeFetcher()
    assert fetch_incremental(repo, f3, ["RELIANCE"], "NSE", 5, 30, now + 2 * DAY)["RELIANCE"] == 0


def test_fetch_incremental_full_refetches_and_repairs(repo):
    now = 100 * DAY
    fetch_incremental(repo, FakeFetcher(), ["RELIANCE"], "NSE", 5, 30, now)
    repo.conn.execute("UPDATE candles SET c = 999 WHERE symbol='RELIANCE' AND ts=?", (90 * DAY,))
    repo.conn.commit()
    f = FakeFetcher()
    result = fetch_incremental(repo, f, ["RELIANCE"], "NSE", 5, 30, now, full=True)
    assert f.calls[0][1] == now - 30 * DAY
    assert result["RELIANCE"] == 30
    assert repo.load_candles(["RELIANCE"], 5, 90 * DAY, 90 * DAY)[0].close == 1.5


def test_fetch_incremental_ignores_out_of_window_candles_and_empty_symbols(repo):
    def stray(symbol, exchange, start_ts, end_ts, interval):
        return [Candle(symbol, end_ts + 5 * DAY, 1, 1, 1, 1, 1)] if symbol == "A" else []

    result = fetch_incremental(repo, stray, ["A", "B"], "NSE", 5, 30, 100 * DAY)
    assert result == {"A": 0, "B": 0}
    assert repo.latest_candle_ts("A", 5) is None


def test_fetch_incremental_rejects_unknown_interval(repo):
    with pytest.raises(ValueError):
        fetch_incremental(repo, FakeFetcher(), ["A"], "NSE", 7, 30, 100 * DAY)


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
