import logging
from datetime import date

import pytest

from tests.helpers import FakeTime, make_config, synth_candles
from tradebot.ai.filter import StubFilter
from tradebot.data.historical import HistoricalSource
from tradebot.data.live import LiveBarSource
from tradebot.engine.clock import SessionClock, date_of, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.engine.paper import PaperEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.strategy.ema_rsi import EmaRsiStrategy

PRIOR = [date(2026, 9, 10), date(2026, 9, 11)]   # Thu, Fri: warm-up days
TODAY = date(2026, 9, 14)                         # Monday
OPEN = ist_epoch(TODAY, "09:15")


def _market():
    return {"A": synth_candles("A", PRIOR + [TODAY]), "B": synth_candles("B", PRIOR + [TODAY], phase=4.0, seed=99)}


def _fetcher(market, calls=None):
    def fetch(sym, exch, start, end, interval):
        if calls is not None:
            calls.append((sym, start, end))
        return [c for c in market[sym] if start <= c.ts <= end]
    return fetch


def _engine(repo, cfg, market, clock_time, run_id="paper-2026-09-14", calls=None):
    clock = SessionClock(cfg.session, 5)
    src = LiveBarSource(_fetcher(market, calls), repo, ["A", "B"], "NSE", 5, 2, clock)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    return PaperEngine(cfg, repo, src, [strat], broker, StubFilter(), clock, {"A": 1, "B": 1}, run_id,
                       now=clock_time.now, sleep=clock_time.sleep)


def _warm(market):
    return [c for cs in market.values() for c in cs if c.ts < OPEN]


def _trades(repo, rid, day=None):
    rows = repo.list_positions(rid)
    return [(r["symbol"], r["direction"], r["qty"], r["opened_at"], r["closed_at"], r["exit_reason"], r["pnl"])
            for r in rows if day is None or date_of(r["opened_at"]) == day]


def test_full_day_matches_the_backtester_bar_for_bar(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"cooldown_bars": 0})
    market = _market()
    calls = []
    t = FakeTime(ist_epoch(TODAY, "09:00"))
    rid = _engine(repo, cfg, market, t, calls=calls).run(_warm(market))
    assert rid == "paper-2026-09-14"
    run = repo.get_run(rid)
    assert run["mode"] == "paper" and run["ended_at"] is not None
    assert run["last_bar_ts"] == ist_epoch(TODAY, "15:25")
    assert len([c for c in calls if c[0] == "A"]) == 75                     # one fetch per bar per symbol
    assert calls[0][1] == OPEN and calls[0][2] == OPEN + 300
    assert [d["date"] for d in repo.daily_pnl(rid)] == ["2026-09-14"]
    assert repo.rejection_counts(rid).get("stale", 0) == 0
    assert t.t >= ist_epoch(TODAY, "15:30") + cfg.data.bar_grace_sec

    # The same day through the backtester, warmed by the same prior days, must produce the same trades.
    all_candles = market["A"] + market["B"]
    repo.insert_candles(all_candles, interval=5)
    src = HistoricalSource(all_candles)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    BacktestEngine(cfg, repo, src, [strat], broker, StubFilter(), SessionClock(cfg.session, 5), {"A": 1, "B": 1}, "bt").run()
    assert _trades(repo, rid) == _trades(repo, "bt", day=TODAY)
    assert _trades(repo, rid), "synthetic data must produce trades"


def test_late_start_catches_up_in_one_fetch_and_drops_stale_signals(repo, tmp_path):
    cfg = make_config(tmp_path)
    market = _market()
    calls = []
    t = FakeTime(ist_epoch(TODAY, "12:00") + 10)
    eng = _engine(repo, cfg, market, t, calls=calls)
    eng.run(_warm(market))
    assert calls[0][1] == OPEN and calls[0][2] == ist_epoch(TODAY, "12:00")   # 09:15..11:55 in one window
    cutoff = ist_epoch(TODAY, "11:55")
    n = repo.conn.execute("SELECT COUNT(*) FROM signals WHERE run_id=? AND bar_ts < ?", (eng.run_id, cutoff)).fetchone()[0]
    assert n > 0
    assert repo.rejection_counts(eng.run_id)["stale"] == n
    assert repo.get_run(eng.run_id)["ended_at"] is not None


def test_stop_and_resume_matches_a_continuous_run(repo, tmp_path):
    cfg = make_config(tmp_path)   # default cooldown of 3 bars: resume must restore it from the closed rows
    market = _market()
    warm = _warm(market)
    _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="cont").run(warm)

    t = FakeTime(ist_epoch(TODAY, "09:00"))
    first = _engine(repo, cfg, market, t, run_id="split")
    stop_at = ist_epoch(TODAY, "11:30")

    def sleep_then_stop(seconds):
        t.sleep(seconds)
        if t.t >= stop_at:
            first.request_stop()

    first.sleep = sleep_then_stop
    first.run(warm)
    run = repo.get_run("split")
    assert run["ended_at"] is None and run["last_bar_ts"] is not None
    assert repo.daily_pnl("split"), "the day's row is written on suspend"

    stored_today = [c for c in repo.load_candles(["A", "B"], 5, OPEN, 2_000_000_000) if c.ts <= run["last_bar_ts"]]
    second = _engine(repo, cfg, market, t, run_id="split")
    second.run(warm + stored_today)
    assert repo.get_run("split")["ended_at"] is not None
    assert _trades(repo, "split") == _trades(repo, "cont")
    assert _trades(repo, "cont")
    a, b = repo.daily_pnl("cont")[0], repo.daily_pnl("split")[0]
    assert (a["realised"], a["fills"], a["entries_placed"]) == (b["realised"], b["fills"], b["entries_placed"])


def test_outside_session_creates_no_run(repo, tmp_path):
    cfg = make_config(tmp_path)
    market = _market()
    saturday = FakeTime(ist_epoch(date(2026, 9, 12), "10:00"))
    assert _engine(repo, cfg, market, saturday, run_id="sat").run([]) is None
    late = FakeTime(ist_epoch(TODAY, "15:31"))
    assert _engine(repo, cfg, market, late, run_id="late").run([]) is None
    assert repo.get_run("sat") is None and repo.get_run("late") is None


def test_resuming_an_ended_run_is_refused(repo, tmp_path):
    cfg = make_config(tmp_path)
    market = _market()
    _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="done").run(_warm(market))
    again = _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "15:00")), run_id="done")
    with pytest.raises(ValueError, match="already ended"):
        again.run([])


def test_a_failing_bar_is_logged_and_the_loop_continues(repo, tmp_path, caplog, monkeypatch):
    cfg = make_config(tmp_path)
    market = _market()
    eng = _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="boom")
    real = eng.process_bar
    hits = []

    def flaky(ts, candles, now_ts=None):
        if not hits:
            hits.append(ts)
            raise RuntimeError("kaboom")
        return real(ts, candles, now_ts=now_ts)

    monkeypatch.setattr(eng, "process_bar", flaky)
    with caplog.at_level(logging.ERROR, logger="tradebot.paper"):
        eng.run(_warm(market))
    assert "failed; continuing" in caplog.text
    assert hits == [OPEN]
    assert repo.get_run("boom")["last_bar_ts"] == ist_epoch(TODAY, "15:25")


def test_resume_replays_stored_bars_past_the_last_processed_one(repo, tmp_path):
    """The CLI's start-up fetch stores today's bars up to now before the engine resumes. Those past
    runs.last_bar_ts were never seen by the broker, so they must be replayed, not warmed away."""
    cfg = make_config(tmp_path)
    market = _market()
    warm = _warm(market)
    _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="cont").run(warm)

    t = FakeTime(ist_epoch(TODAY, "09:00"))
    first = _engine(repo, cfg, market, t, run_id="split")
    stop_at = ist_epoch(TODAY, "11:30")

    def sleep_then_stop(seconds):
        t.sleep(seconds)
        if t.t >= stop_at:
            first.request_stop()

    first.sleep = sleep_then_stop
    first.run(warm)
    last = repo.get_run("split")["last_bar_ts"]
    assert last < ist_epoch(TODAY, "12:00")
    # Simulate the CLI: fetch-incremental stores today's completed bars up to now, beyond last_bar_ts.
    repo.insert_candles([c for cs in market.values() for c in cs if OPEN <= c.ts < ist_epoch(TODAY, "12:00")], 5)
    stored_today = repo.load_candles(["A", "B"], 5, OPEN, 2_000_000_000)
    assert max(c.ts for c in stored_today) > last
    second = _engine(repo, cfg, market, t, run_id="split")
    second.run(warm + stored_today)
    assert _trades(repo, "split") == _trades(repo, "cont")
    assert any(r["opened_at"] <= last < r["closed_at"] for r in repo.list_positions("cont")), \
        "the scenario must have a position open across the stop for the test to mean anything"


def test_an_open_run_started_after_the_close_settles_and_ends(repo, tmp_path):
    """A process that died mid-session leaves the run open; starting it after 15:30 must replay the
    missed bars (stale, so no entries), square off and end it rather than leave positions open forever."""
    cfg = make_config(tmp_path)
    market = _market()
    warm = _warm(market)
    t = FakeTime(ist_epoch(TODAY, "09:00"))
    first = _engine(repo, cfg, market, t, run_id="crashed")
    stop_at = ist_epoch(TODAY, "11:30")

    def sleep_then_stop(seconds):
        t.sleep(seconds)
        if t.t >= stop_at:
            first.request_stop()

    first.sleep = sleep_then_stop
    first.run(warm)
    assert repo.get_run("crashed")["ended_at"] is None
    assert repo.open_positions("crashed"), "the scenario needs a position open at the stop"

    evening = FakeTime(ist_epoch(TODAY, "15:35"))
    stored_today = repo.load_candles(["A", "B"], 5, OPEN, 2_000_000_000)
    assert _engine(repo, cfg, market, evening, run_id="crashed").run(warm + stored_today) == "crashed"
    run = repo.get_run("crashed")
    assert run["ended_at"] is not None and run["last_bar_ts"] == ist_epoch(TODAY, "15:25")
    assert repo.open_positions("crashed") == []
    assert repo.rejection_counts("crashed").get("stale", 0) > 0
    assert all(r["opened_at"] <= stop_at + 300 for r in repo.list_positions("crashed")), "no entries from stale bars"


def test_bookkeeping_failure_does_not_stop_the_loop(repo, tmp_path, caplog, monkeypatch):
    cfg = make_config(tmp_path)
    market = _market()
    eng = _engine(repo, cfg, market, FakeTime(ist_epoch(TODAY, "09:00")), run_id="db-hiccup")
    real = repo.set_last_bar_ts
    calls = []

    def flaky(run_id, ts):
        calls.append(ts)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(run_id, ts)

    monkeypatch.setattr(repo, "set_last_bar_ts", flaky)
    with caplog.at_level(logging.ERROR, logger="tradebot.paper"):
        eng.run(_warm(market))
    assert "bookkeeping for bar" in caplog.text
    assert repo.get_run("db-hiccup")["ended_at"] is not None
    assert repo.get_run("db-hiccup")["last_bar_ts"] == ist_epoch(TODAY, "15:25")
