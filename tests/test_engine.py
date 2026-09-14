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
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    eng = BacktestEngine(cfg, repo, src, [strat], broker, ai or StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1, "B": 1}, run_id)
    eng.run()
    return eng, broker


def _candles():
    return synth_candles("A", DAYS, phase=0.0) + synth_candles("B", DAYS, phase=2.0, seed=99)


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


def test_engine_records_signals_decisions_and_orders(repo, tmp_path):
    cfg = make_config(tmp_path)
    _run(repo, cfg, _candles())
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
    assert repo.rejection_counts("t1") == {}  # signals dropped before risk, nothing recorded


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
