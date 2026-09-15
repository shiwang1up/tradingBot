import logging
import time
from datetime import date

from tests.helpers import make_config, synth_candles
from tradebot.data.live import LiveBarSource
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.types import Candle

D = date(2026, 9, 14)


def _source(repo, tmp_path, fetcher, symbols=("A", "B")):
    cfg = make_config(tmp_path)
    clock = SessionClock(cfg.session, 5)
    return LiveBarSource(fetcher, repo, list(symbols), "NSE", 5, 2, clock), clock


def test_fetch_bar_returns_the_closed_bar_and_stores_only_completed_bars(repo, tmp_path):
    a, b = synth_candles("A", [D]), synth_candles("B", [D], phase=4.0, seed=99)
    calls = []

    def fetcher(sym, exch, start, end, interval):
        calls.append((sym, exch, start, end, interval))
        return [c for c in (a if sym == "A" else b) if start <= c.ts <= end]  # `end` is the bar in progress

    src, clock = _source(repo, tmp_path, fetcher)
    bar = ist_epoch(D, "09:30")
    got = src.fetch_bar(bar)
    assert set(got) == {"A", "B"}
    assert got["A"] == a[3] and got["B"].ts == bar
    assert sorted(c[0] for c in calls) == ["A", "B"]
    assert all(c[1] == "NSE" and c[2] == clock.open_ts(D) and c[3] == bar + 300 and c[4] == 5 for c in calls)
    stored = repo.load_candles(["A"], 5, 0, 2_000_000_000)
    assert [c.ts for c in stored] == [c.ts for c in a[:4]]   # 09:15..09:30; the 09:35 bar in progress is not stored


def test_symbol_failure_is_skipped_and_total_failure_is_empty(repo, tmp_path, caplog):
    a = synth_candles("A", [D])

    def flaky(sym, exch, start, end, interval):
        if sym == "B":
            raise RuntimeError("boom")
        return [c for c in a if start <= c.ts <= end]

    src, _ = _source(repo, tmp_path, flaky)
    with caplog.at_level(logging.WARNING, logger="tradebot.live"):
        got = src.fetch_bar(ist_epoch(D, "09:20"))
    assert set(got) == {"A"}
    assert "candle fetch failed for B" in caplog.text

    def dead(sym, exch, start, end, interval):
        raise RuntimeError("down")

    src2, _ = _source(repo, tmp_path, dead)
    with caplog.at_level(logging.ERROR, logger="tradebot.live"):
        assert src2.fetch_bar(ist_epoch(D, "09:20")) == {}
    assert "every candle fetch failed" in caplog.text


def test_fetch_range_groups_bars_by_open_time(repo, tmp_path):
    a = synth_candles("A", [D])
    src, _ = _source(repo, tmp_path, lambda s, e, st, en, i: [c for c in a if st <= c.ts <= en], symbols=("A",))
    first, last = ist_epoch(D, "10:00"), ist_epoch(D, "10:10")
    got = src.fetch_range(first, last)
    assert sorted(got) == [first, first + 300, last]
    assert all(got[t]["A"].ts == t for t in got)


def test_pre_open_rows_are_neither_stored_nor_returned(repo, tmp_path):
    a = synth_candles("A", [D])
    early = Candle("A", ist_epoch(D, "09:05"), 1.0, 1.0, 1.0, 1.0, 1)

    def fetcher(sym, exch, start, end, interval):
        return [early] + [c for c in a if start <= c.ts <= end]

    src, _ = _source(repo, tmp_path, fetcher, symbols=("A",))
    got = src.fetch_range(ist_epoch(D, "09:05"), ist_epoch(D, "09:20"))
    assert sorted(got) == [ist_epoch(D, "09:15"), ist_epoch(D, "09:20")]
    assert repo.load_candles(["A"], 5, 0, 2_000_000_000)[0].ts == ist_epoch(D, "09:15")


def test_a_symbol_slower_than_the_budget_is_skipped_for_the_bar(repo, tmp_path, caplog):
    a, b = synth_candles("A", [D]), synth_candles("B", [D], phase=4.0, seed=99)

    def slow_b(sym, exch, start, end, interval):
        if sym == "B":
            time.sleep(0.5)
        return [c for c in (a if sym == "A" else b) if start <= c.ts <= end]

    cfg = make_config(tmp_path)
    src = LiveBarSource(slow_b, repo, ["A", "B"], "NSE", 5, 2, SessionClock(cfg.session, 5), budget_sec=0.1)
    with caplog.at_level(logging.INFO, logger="tradebot.live"):
        got = src.fetch_bar(ist_epoch(D, "09:20"))
    assert set(got) == {"A"}
    assert "candle fetch for B did not finish" in caplog.text
    assert "1 of 2 symbols" in caplog.text and "1 failed" in caplog.text
