import pytest

from tradebot.report.compare import Prices, build_compare, format_compare
from tradebot.types import Position, Signal, make_client_id

P = Prices(5.0, 25.0, 0.5, 6.25)


def _seed_pair(repo):
    """Run A (stub) took three trades; run B (claude) rejected two of them and approved one."""
    repo.create_run("A", "backtest", 0, "{}")
    repo.create_run("B", "backtest", 0, "{}")
    trades = [("X", 1000, 50.0), ("Y", 1300, -30.0), ("Z", 1600, 20.0)]  # symbol, bar_ts, pnl in A
    for sym, ts, pnl in trades:
        sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", ts)
        sa = repo.insert_signal("A", sig)
        repo.insert_ai_decision("A", sa, "stub", True, "stub", 1.0, 0, None)
        cid = make_client_id("ema_rsi", sym, ts)
        pid = repo.insert_position("A", Position(sym, "MIS", "LONG", 10, 100.0, 99.0, 102.0, ts + 300, cid, "ema_rsi"))
        repo.close_position(pid, ts + 900, 100.0 + pnl / 10, "TARGET" if pnl > 0 else "STOP", pnl)
        sb = repo.insert_signal("B", sig)
        approve = sym == "Z"
        repo.insert_ai_decision("B", sb, "claude", approve, "fine" if approve else "chop", 0.6, 800, None,
                                input_tokens=1000, output_tokens=60, cache_read_tokens=500, cache_write_tokens=100)
        if approve:
            pid = repo.insert_position("B", Position(sym, "MIS", "LONG", 10, 100.0, 99.0, 102.0, ts + 300, cid, "ema_rsi"))
            repo.close_position(pid, ts + 900, 102.0, "TARGET", pnl)
    # a signal Claude rejected that A never filled (unfilled in A): must not count as avoided PnL
    sig = Signal("ema_rsi", "W", "LONG", 100.0, 99.0, 102.0, "MIS", 1900)
    repo.insert_ai_decision("A", repo.insert_signal("A", sig), "stub", True, "stub", 1.0, 0, None)
    repo.insert_ai_decision("B", repo.insert_signal("B", sig), "claude", False, "late", 0.4, 0, None)
    repo.upsert_daily_pnl("A", "2026-09-14", 40.0, 0.0, 3, 3)
    repo.upsert_daily_pnl("B", "2026-09-14", 20.0, 0.0, 1, 1)


def test_build_compare_attributes_rejected_pnl(repo):
    _seed_pair(repo)
    c = build_compare(repo, "A", "B", P)
    assert c.a.run_id == "A" and c.b.run_id == "B"
    assert c.rejected == 3 and c.rejected_with_position_in_a == 2
    assert c.rejected_pnl_in_a == pytest.approx(20.0)      # +50 and -30 avoided: net +20 given up
    assert c.rejected_losses_avoided == pytest.approx(30.0) and c.rejected_wins_forgone == pytest.approx(50.0)
    assert c.total_pnl_delta == pytest.approx(20.0 - 40.0)
    assert c.calls == 3 and c.input_tokens == 3000 and c.output_tokens == 180
    assert (c.cache_read_tokens, c.cache_write_tokens) == (1500, 300)
    assert c.est_cost_usd == pytest.approx((3000 * 5.0 + 180 * 25.0 + 1500 * 0.5 + 300 * 6.25) / 1e6)
    assert [r["symbol"] for r in c.rows] == ["X", "Y", "W"]
    assert c.rows[2]["pnl_in_a"] is None


def test_format_compare_reads_sensibly(repo):
    _seed_pair(repo)
    text = format_compare(build_compare(repo, "A", "B", P))
    assert "Rejected by Claude" in text and "X" in text and "chop" in text
    assert "Estimated cost" in text and "$0.0" in text
    assert "Verdict" in text


def test_compare_requires_both_runs(repo):
    repo.create_run("A", "backtest", 0, "{}")
    with pytest.raises(ValueError):
        build_compare(repo, "A", "missing", P)
