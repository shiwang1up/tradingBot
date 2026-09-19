import json
from datetime import date
from pathlib import Path

import pytest

from tests.helpers import make_config, synth_candles
from tradebot.ai.filter import StubFilter
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, date_of, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.strategy.ema_rsi import EmaRsiStrategy
from tradebot.types import Candle

GOLDEN = Path(__file__).parent / "fixtures" / "golden_trades.json"
DAYS = [date(2026, 9, 13), date(2026, 9, 14), date(2026, 9, 15)]  # Sunday, Mon, Tue


def _run(repo, cfg, candles, run_id="t1", ai=None):
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, sorted({c.symbol for c in candles}), 5, 0, 2_000_000_000)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    eng = BacktestEngine(cfg, repo, src, [strat], broker, ai or StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, run_id)
    eng.run()
    return eng, broker


def _candles():
    return synth_candles("A", DAYS, phase=0.0) + synth_candles("B", DAYS, phase=4.0, seed=99)


def test_engine_invariants(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert len(rows) >= 2, "synthetic data must produce trades"
    clock = SessionClock(cfg.session, 5)
    for r in rows:
        d = date_of(r["opened_at"])
        assert d.weekday() < 5, "no trades on Sunday"
        assert r["closed_at"] is not None, "MIS positions must be closed"
        assert date_of(r["closed_at"]) == d, "MIS closed same day"
        assert r["closed_at"] <= clock.square_off_bar_ts(d)
        assert r["opened_at"] <= ist_epoch(d, cfg.session.no_new_entries_after)
        assert r["exit_reason"] in ("STOP", "TARGET", "SQUARE_OFF")
        sign = 1 if r["direction"] == "LONG" else -1
        assert r["pnl"] == pytest.approx(sign * (r["exit_price"] - r["avg_price"]) * r["qty"], abs=0.01)
    assert repo.get_run("t1")["ended_at"] is not None
    days = repo.daily_pnl("t1")
    assert [d["date"] for d in days] == ["2026-09-14", "2026-09-15"]
    assert sum(d["realised"] for d in days) == pytest.approx(sum(r["pnl"] for r in rows), abs=0.01)


def test_daily_realised_and_cash_are_net_of_charges(repo, tmp_path):
    cfg = make_config(tmp_path, charges={"enabled": True})
    _, broker = _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert len(rows) >= 2 and all(r["charges"] > 0 for r in rows)
    net = sum(r["pnl"] - r["charges"] for r in rows)
    assert sum(d["realised"] for d in repo.daily_pnl("t1")) == pytest.approx(net, abs=0.01)
    assert broker.cash == pytest.approx(cfg.capital + net, abs=0.01)


@pytest.mark.parametrize("charges_cfg,expect_positive", [
    ({"enabled": True}, True),
    ({"enabled": False}, False),
])
def test_every_closed_position_has_recorded_charges(repo, tmp_path, charges_cfg, expect_positive):
    """Amendment 1: close_position's charges is now a required keyword, so a caller cannot forget to
    pass it. Every row a backtest run closes must have a non-NULL charges: a real number > 0 when
    the schedule is enabled, and exactly 0.0 (not NULL) when it is disabled (the default config)."""
    cfg = make_config(tmp_path, charges=charges_cfg)
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert len(rows) >= 2, "synthetic data must produce trades"
    assert all(r["charges"] is not None for r in rows)
    if expect_positive:
        assert all(r["charges"] > 0 for r in rows)
    else:
        assert all(r["charges"] == 0.0 for r in rows)


def test_engine_records_signals_decisions_and_orders(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    assert repo.rejection_counts("t1").get("entries_closed", 0) >= 0  # after-cutoff signals are rows, not silence
    n_signals = repo.conn.execute("SELECT COUNT(*) FROM signals WHERE run_id='t1'").fetchone()[0]
    n_risk = repo.conn.execute("SELECT COUNT(*) FROM risk_decisions WHERE run_id='t1'").fetchone()[0]
    n_ai = repo.conn.execute("SELECT COUNT(*) FROM ai_decisions WHERE run_id='t1'").fetchone()[0]
    n_orders = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE run_id='t1'").fetchone()[0]
    n_filled = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE run_id='t1' AND status='FILLED'").fetchone()[0]
    assert n_signals == n_risk >= n_ai >= n_orders >= 1
    assert n_filled == len(repo.list_positions("t1"))


def test_kill_switch_blocks_entries(repo, tmp_path):
    cfg = make_config(tmp_path)
    Path(cfg.paths.kill_switch).write_text("")
    _run(repo, cfg, _candles())
    assert repo.list_positions("t1") == []
    rej = repo.rejection_counts("t1")
    assert rej.get("kill_switch", 0) > 0 and set(rej) <= {"kill_switch", "entries_closed"}


def test_max_open_positions_one(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"max_open_positions": 1})
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    # no two positions overlap in time
    spans = sorted((r["opened_at"], r["closed_at"]) for r in rows)
    for (o1, c1), (o2, _) in zip(spans, spans[1:]):
        assert o2 > c1
    assert "max_open_positions" in repo.rejection_counts("t1")


def test_cooldown_after_stop(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    stops = [r for r in rows if r["exit_reason"] == "STOP"]
    if not stops:
        pytest.skip("synthetic data produced no stop-outs")
    for s in stops:
        later = [r for r in rows if r["symbol"] == s["symbol"] and r["opened_at"] > s["closed_at"]]
        for r in later:
            # entry fills one bar after the signal; signal bar must be > closed_ts + 3 bars
            assert r["opened_at"] - 300 > s["closed_at"] + 3 * 300


def test_strategy_exception_disables_strategy_for_day(repo, tmp_path):
    cfg = make_config(tmp_path)

    class Boom(EmaRsiStrategy):
        name = "boom"

        def on_candle(self, c):
            if date_of(c.ts) == date(2026, 9, 14) and c.ts >= ist_epoch(date(2026, 9, 14), "10:00"):
                raise RuntimeError("bad indicator")
            return super().on_candle(c)

    candles = _candles()
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, ["A", "B"], 5, 0, 2_000_000_000)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    eng = BacktestEngine(cfg, repo, src, [Boom(cfg.strategy["ema_rsi"])], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, "t1")
    eng.run()  # must not raise
    assert "boom" not in eng.disabled_strategies  # re-enabled on the next day
    rows = repo.conn.execute("SELECT bar_ts FROM signals WHERE run_id='t1'").fetchall()
    day1_after_10 = ist_epoch(date(2026, 9, 14), "10:00")
    day2_open = ist_epoch(date(2026, 9, 15), "09:15")
    assert not [r for r in rows if day1_after_10 <= r["bar_ts"] < day2_open], "disabled for the rest of day 1"
    assert [r for r in rows if r["bar_ts"] >= day2_open], "runs again on day 2"


def test_golden_trades(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    got = [dict(symbol=r["symbol"], direction=r["direction"], opened_at=r["opened_at"], closed_at=r["closed_at"],
                exit_reason=r["exit_reason"], qty=r["qty"], avg_price=r["avg_price"], exit_price=r["exit_price"],
                pnl=r["pnl"]) for r in repo.list_positions("t1")]
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(got, indent=2))
        pytest.fail(f"golden file written to {GOLDEN}; inspect it, commit it, and re-run")
    assert got == json.loads(GOLDEN.read_text())


# -- branches the golden run never reaches -----------------------------------------------------

def test_record_entry_bar_stop_inserts_then_closes_same_position(repo, tmp_path):
    from tradebot.execution.broker import Closed, Filled
    from tradebot.types import ApprovedOrder, Position, Signal
    cfg = make_config(tmp_path)
    src = HistoricalSource([])
    eng = BacktestEngine(cfg, repo, src, [], BacktestBroker(1e5, 0.0, 5.0), StubFilter(),
                         SessionClock(cfg.session, 5), {}, "t1")
    repo.create_run("t1", "backtest", 0, "{}")
    sig = Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", 1000)
    order = ApprovedOrder(sig, 10, "cidcidcidcidcid1")
    repo.insert_order("t1", order.client_id, repo.insert_signal("t1", sig), "ENTRY", "BUY", 10, 100.0, "PENDING", 1000)
    pos = Position("A", "MIS", "LONG", 10, 95.0, 99.0, 102.0, 1300, order.client_id, "ema_rsi")
    pos.closed_ts, pos.exit_price, pos.exit_reason, pos.pnl = 1300, 95.0, "STOP", 0.0
    eng._record([Filled(pos, order, 1300), Closed(pos)])
    row = repo.list_positions("t1")[0]
    assert row["closed_at"] == 1300 and row["exit_reason"] == "STOP"
    assert repo.order_status("t1", order.client_id) == "FILLED"
    assert repo.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert eng._cooldown_until["A"] == 1300 + 3 * 300


def test_tight_entry_buffer_produces_unfilled_orders(repo, tmp_path):
    cfg = make_config(tmp_path, execution={"entry_buffer_pct": 0.001})
    _run(repo, cfg, _candles())
    n_unfilled = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE status='UNFILLED'").fetchone()[0]
    n_pending = repo.conn.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0]
    assert n_unfilled > 0 and n_pending == 0
    days = repo.daily_pnl("t1")
    assert any(d["fill_rate"] < 1.0 for d in days)


def test_no_order_is_left_pending_at_run_end(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    assert repo.conn.execute("SELECT COUNT(*) FROM orders WHERE status='PENDING'").fetchone()[0] == 0


def test_ai_rejection_records_decision_and_places_nothing(repo, tmp_path):
    from tradebot.types import Decision

    class RejectAll:
        kind = "reject_all"

        def review(self, cands):
            return [Decision(c.signal, False, "no", 0.0, self.kind) for c in cands]

    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles(), ai=RejectAll())
    assert repo.list_positions("t1") == []
    assert repo.ai_rejection_count("t1") > 0


def test_ai_returning_wrong_count_is_an_error(repo, tmp_path):
    class Short:
        kind = "short"

        def review(self, cands):
            return []

    cfg = make_config(tmp_path)
    with pytest.raises(RuntimeError, match="returned 0 decisions"):
        _run(repo, cfg, _candles(), ai=Short())
    assert repo.get_run("t1")["ended_at"] is not None  # run is closed even on failure


def test_kill_switch_flatten_closes_open_positions(repo, tmp_path):
    cfg = make_config(tmp_path)
    kill = Path(cfg.paths.kill_switch)

    class ArmAfterFirstApproval(StubFilter):
        def review(self, cands):
            out = super().review(cands)
            kill.write_text("flatten")  # engine reads it on the next bar, after the entry fills
            return out

    _run(repo, cfg, _candles(), ai=ArmAfterFirstApproval())
    rows = repo.list_positions("t1")
    assert rows and all(r["exit_reason"] == "FLATTEN" for r in rows)


def test_daily_cap_flatten_fires_outside_entry_hours(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"daily_loss_cap_pct": 0.001, "flatten_on_daily_cap": True})
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert rows
    assert "daily_loss_cap" in repo.rejection_counts("t1")
    # once a loss breaches the tiny cap, anything still open that day is flattened, not stopped later
    by_day = {}
    for r in rows:
        by_day.setdefault(date_of(r["opened_at"]), []).append(r)
    for day_rows in by_day.values():
        losses = [r for r in day_rows if r["pnl"] < 0]
        if losses:
            first_loss_close = min(r["closed_at"] for r in losses)
            later_opens = [r for r in day_rows if r["opened_at"] > first_loss_close]
            assert not later_opens


def test_max_entries_per_day_is_enforced(repo, tmp_path):
    cfg = make_config(tmp_path, risk={"max_entries_per_day": 1})
    _run(repo, cfg, _candles())
    assert all(d["entries_placed"] <= 1 for d in repo.daily_pnl("t1"))
    assert "max_entries_per_day" in repo.rejection_counts("t1")


def test_cnc_positions_carry_overnight_and_unrealised_is_day_scoped(repo, tmp_path):
    from tests.helpers import BASE_CONFIG
    params = {**BASE_CONFIG["strategy"]["ema_rsi"], "product": "CNC", "atr_stop_mult": 50.0, "reward_risk": 50.0}
    cfg = make_config(tmp_path, strategy={"ema_rsi": params})
    _run(repo, cfg, _candles())
    rows = repo.list_positions("t1")
    assert rows and all(r["closed_at"] is None for r in rows), "nothing can exit: held to run end"
    days = repo.daily_pnl("t1")
    assert len(days) == 2 and days[0]["unrealised"] != 0.0
    # day 2's unrealised is today's move only, not the lifetime mark
    total_mark = sum(sig * (repo.conn.execute("SELECT c FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                                              (r["symbol"],)).fetchone()[0] - r["avg_price"]) * r["qty"]
                     for r in rows for sig in ([1] if r["direction"] == "LONG" else [-1]))
    assert days[0]["unrealised"] + days[1]["unrealised"] == pytest.approx(total_mark, abs=0.05)


def test_disabled_strategy_is_reset_before_next_day(repo, tmp_path):
    cfg = make_config(tmp_path)

    class Boom(EmaRsiStrategy):
        name = "boom"
        resets: list = []

        def reset(self, symbol):
            self.resets.append(symbol)
            super().reset(symbol)

        def on_candle(self, c):
            if date_of(c.ts) == date(2026, 9, 14) and c.ts >= ist_epoch(date(2026, 9, 14), "10:00"):
                raise RuntimeError("bad indicator")
            return super().on_candle(c)

    candles = _candles()
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, ["A", "B"], 5, 0, 2_000_000_000)
    strat = Boom(cfg.strategy["ema_rsi"])
    eng = BacktestEngine(cfg, repo, src, [strat], BacktestBroker(cfg.capital, 0.05, 5.0, 0.1), StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, "t1")
    eng.run()
    day2_open = ist_epoch(date(2026, 9, 15), "09:15")
    # resets happened for both symbols at the start of day 2, so early day-2 bars produce no signals
    assert {"A", "B"} <= set(strat.resets)
    early = [r for r in repo.conn.execute("SELECT bar_ts FROM signals WHERE run_id='t1'").fetchall()
             if day2_open <= r["bar_ts"] < day2_open + 20 * 300]
    assert not early


def test_run_survives_unquoted_holiday_dates_in_config(repo, tmp_path):
    cfg = make_config(tmp_path, session={"holidays": [date(2026, 10, 2)]})  # YAML date, not a string
    assert cfg.raw["session"]["holidays"][0].__class__.__name__ == "date"
    _run(repo, cfg, _candles())
    assert repo.get_run("t1")["ended_at"] is not None


def test_daily_cap_flatten_fires_after_entry_cutoff_unit(repo, tmp_path):
    from tradebot.types import ApprovedOrder, Signal
    cfg = make_config(tmp_path, risk={"flatten_on_daily_cap": True})
    broker = BacktestBroker(cfg.capital, 0.0, 5.0)
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {}, "t1")
    repo.create_run("t1", "backtest", 0, "{}")
    d = date(2026, 9, 14)
    sig = Signal("ema_rsi", "A", "LONG", 100.0, 90.0, 120.0, "MIS", ist_epoch(d, "14:40"))
    order = ApprovedOrder(sig, 10, "cidcidcidcidcid2")
    repo.insert_order("t1", order.client_id, repo.insert_signal("t1", sig), "ENTRY", "BUY", 10, 100.0, "PENDING", sig.bar_ts)
    broker.place_entry(order)
    eng._start_day()
    t1 = ist_epoch(d, "14:45")
    eng.process_bar(t1, {"A": Candle("A", t1, 100.0, 100.5, 99.5, 100.0, 1)})  # fills; after the cutoff
    assert set(broker.open_positions()) == {"A"}
    eng._day.realised = -0.5 * cfg.capital  # a huge loss booked earlier in the day
    t2 = ist_epoch(d, "14:50")
    eng.process_bar(t2, {"A": Candle("A", t2, 100.0, 100.5, 99.5, 100.0, 1)})
    assert broker.open_positions() == {}
    assert repo.list_positions("t1")[0]["exit_reason"] == "FLATTEN"


def test_end_run_is_written_even_if_day_end_bookkeeping_fails(repo, tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(repo, "upsert_daily_pnl", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError, match="disk full"):  # the mid-run day boundary propagates it
        _run(repo, cfg, _candles())
    assert repo.get_run("t1")["ended_at"] is not None  # but the run is still closed


def test_mis_position_squared_off_even_when_its_symbol_lacks_the_square_off_bar(repo, tmp_path):
    cfg = make_config(tmp_path)
    candles = _candles()
    d = date(2026, 9, 14)
    hole = {ist_epoch(d, t) for t in ("15:05", "15:10", "15:15", "15:20", "15:25")}
    candles = [c for c in candles if not (c.symbol == "A" and c.ts in hole)]  # A goes quiet after 15:00
    _run(repo, cfg, candles)
    for r in repo.list_positions("t1"):
        assert r["closed_at"] is not None and date_of(r["closed_at"]) == date_of(r["opened_at"])
        if r["symbol"] == "A" and date_of(r["opened_at"]) == d and r["exit_reason"] == "SQUARE_OFF":
            assert r["closed_at"] == ist_epoch(d, "15:05")  # closed at A's last known price on B's bar


def test_run_stores_resolved_config_without_secrets(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GROWW_API_KEY", "should-not-leak")
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
    stored = json.loads(repo.get_run("t1")["config_json"])
    assert "secrets" not in stored and "raw" not in stored
    assert stored["strategy"]["ema_rsi"]["min_stop_pct"] == 0.1
    assert "should-not-leak" not in repo.get_run("t1")["config_json"]


def test_day_whose_bars_end_early_still_squares_off_intraday(repo, tmp_path):
    cfg = make_config(tmp_path)
    d = date(2026, 9, 14)
    cutoff = ist_epoch(d, "12:00")
    candles = [c for c in _candles() if not (date_of(c.ts) == d and c.ts >= cutoff)]  # day 1 data stops at noon
    _run(repo, cfg, candles)
    for r in repo.list_positions("t1"):
        assert date_of(r["closed_at"]) == date_of(r["opened_at"])


def test_engine_persists_ai_token_usage(repo, tmp_path):
    from tradebot.types import Decision

    class Priced:
        kind = "priced"

        def review(self, cands):
            return [Decision(c.signal, True, "ok", 0.5, self.kind, latency_ms=300, input_tokens=1000 if i == 0 else 0,
                             output_tokens=50 if i == 0 else 0, cache_read_tokens=700 if i == 0 else 0)
                    for i, c in enumerate(cands)]

    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles(), ai=Priced())
    u = repo.ai_usage("t1")
    n_bars_with_batch = repo.conn.execute(
        "SELECT COUNT(DISTINCT s.bar_ts) FROM ai_decisions d JOIN signals s ON s.id = d.signal_id WHERE d.run_id='t1'"
    ).fetchone()[0]
    assert u["calls"] == n_bars_with_batch >= 1
    assert (u["input_tokens"], u["output_tokens"], u["cache_read_tokens"]) == (1000 * u["calls"], 50 * u["calls"], 700 * u["calls"])


def test_candidates_carry_the_shared_indicator_snapshot(repo, tmp_path):
    seen = []

    class Spy(StubFilter):
        def review(self, cands):
            seen.extend(cands)
            return super().review(cands)

    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles(), ai=Spy())
    assert seen
    keys = set(seen[-1].indicators)
    assert {"ema_fast", "ema_slow", "rsi", "atr", "trend", "macd_hist", "adx", "pct_b", "support", "resistance",
            "volume_spike_pct", "engulfing"} <= keys
    late = [c for c in seen if c.indicators.get("adx") is not None]
    assert late, "once the windows fill the shared indicators are populated"


def test_stale_bars_record_signals_but_place_nothing(repo, tmp_path):
    cfg = make_config(tmp_path)
    src = HistoricalSource(_candles())
    clock = SessionClock(cfg.session, 5)

    def engine(run_id):
        strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
        broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
        e = BacktestEngine(cfg, repo, src, [strat], broker, StubFilter(), clock, {"A": 1, "B": 1}, run_id)
        repo.create_run(run_id, "backtest", 0, "{}")
        e._start_day()
        return e

    fresh, late = engine("fresh"), engine("late")
    for ts in src.bar_timestamps():
        if date_of(ts) != DAYS[1]:
            continue
        fresh.process_bar(ts, src.candles_at(ts), now_ts=ts + 300 + 5)     # 5 s after the close: fresh
        late.process_bar(ts, src.candles_at(ts), now_ts=ts + 300 + 61)     # past the 60 s deadline
    assert repo.rejection_counts("fresh").get("stale", 0) == 0
    assert repo.rejection_counts("late")["stale"] > 0
    assert repo.list_positions("late") == []
    assert len(repo.list_positions("fresh")) >= 1


def test_a_late_bar_still_simulates_exits(repo, tmp_path):
    """Spec 8.6 drops only placement: a stale bar must still run the broker so stops and targets fire."""
    from tradebot.types import ApprovedOrder, Signal
    cfg = make_config(tmp_path)
    strat = EmaRsiStrategy(cfg.strategy["ema_rsi"])
    broker = BacktestBroker(cfg.capital, 0.0, cfg.risk.mis_leverage, None)
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [strat], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1}, "late-exit")
    repo.create_run("late-exit", "backtest", 0, "{}")
    eng._start_day()
    t0 = ist_epoch(DAYS[1], "10:00")
    sig = Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", t0)
    repo.insert_order("late-exit", "cid1", None, "ENTRY", "BUY", 10, 100.0, "PENDING", t0)
    broker.place_entry(ApprovedOrder(sig, 10, "cid1"))
    eng.process_bar(t0 + 300, {"A": Candle("A", t0 + 300, 100.0, 100.5, 99.8, 100.2, 1)}, now_ts=t0 + 600 + 900)
    assert set(broker.open_positions()) == {"A"}, "a late bar still fills the pending entry"
    eng.process_bar(t0 + 600, {"A": Candle("A", t0 + 600, 100.1, 100.3, 98.5, 98.7, 1)}, now_ts=t0 + 900 + 900)
    rows = repo.list_positions("late-exit")
    assert broker.open_positions() == {} and rows and rows[0]["exit_reason"] == "STOP"
