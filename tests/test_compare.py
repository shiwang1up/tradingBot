# tests/test_compare.py
import json

import pytest

from tradebot.report.compare import Prices, build_compare, format_compare
from tradebot.types import Position, Signal, make_client_id

P = Prices(5.0, 25.0, 0.5, 6.25)
CFG = json.dumps({"strategy": {"ema_rsi": {"fast": 9}}, "session": {"open": "09:15"}, "capital": 100000.0,
                  "risk": {"per_trade_pct": 1.0}, "execution": {"interval_minutes": 5}})


def _seed_pair(repo, cfg_b=CFG):
    """Run A (stub) took three trades; run B (claude) rejected two of them and approved one."""
    repo.create_run("A", "backtest", 0, CFG)
    repo.create_run("B", "backtest", 0, cfg_b)
    trades = [("X", 1000, 50.0), ("Y", 1300, -30.0), ("Z", 1600, 20.0)]  # symbol, bar_ts, pnl in A
    for sym, ts, pnl in trades:
        sig = Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", ts)
        sa = repo.insert_signal("A", sig)
        repo.insert_ai_decision("A", sa, "stub", True, "stub", 1.0, 0, None)
        cid = make_client_id("ema_rsi", sym, ts)
        pid = repo.insert_position("A", Position(sym, "MIS", "LONG", 10, 100.0, 99.0, 102.0, ts + 300, cid, "ema_rsi"))
        repo.close_position(pid, ts + 900, 100.0 + pnl / 10, "TARGET" if pnl > 0 else "STOP", pnl, charges=None)
        sb = repo.insert_signal("B", sig)
        approve = sym == "Z"
        repo.insert_ai_decision("B", sb, "claude", approve, "fine" if approve else "chop", 0.6, 800, None,
                                input_tokens=1000, output_tokens=60, cache_read_tokens=500, cache_write_tokens=100)
        if approve:
            pid = repo.insert_position("B", Position(sym, "MIS", "LONG", 10, 100.0, 99.0, 102.0, ts + 300, cid, "ema_rsi"))
            repo.close_position(pid, ts + 900, 102.0, "TARGET", pnl, charges=None)
    # a signal Claude rejected that A never filled: must not count as avoided PnL
    sig = Signal("ema_rsi", "W", "LONG", 100.0, 99.0, 102.0, "MIS", 1900)
    repo.insert_ai_decision("A", repo.insert_signal("A", sig), "stub", True, "stub", 1.0, 0, None)
    repo.insert_ai_decision("B", repo.insert_signal("B", sig), "claude", False, "late", 0.4, 0, None)
    # a signal Claude rejected whose A position is still open: reported as open, not unfilled
    sig = Signal("ema_rsi", "V", "CNC", 100.0, 95.0, 110.0, "CNC", 2200)
    repo.insert_ai_decision("A", repo.insert_signal("A", sig), "stub", True, "stub", 1.0, 0, None)
    repo.insert_position("A", Position("V", "CNC", "LONG", 1, 100.0, 95.0, 110.0, 2500, make_client_id("ema_rsi", "V", 2200), "ema_rsi"))
    repo.insert_ai_decision("B", repo.insert_signal("B", sig), "claude", False, "slow", 0.3, 0, None)
    repo.upsert_daily_pnl("A", "2026-09-14", 40.0, 0.0, 3, 3)
    repo.upsert_daily_pnl("B", "2026-09-14", 20.0, 0.0, 1, 1)


def test_build_compare_attributes_rejected_pnl(repo):
    _seed_pair(repo)
    c = build_compare(repo, "A", "B", P)
    assert c.a.run_id == "A" and c.b.run_id == "B" and c.warnings == []
    assert (c.rejected, c.rejected_closed_in_a, c.rejected_open_in_a) == (4, 2, 1)
    assert c.rejected_pnl_in_a == pytest.approx(20.0)      # +50 and -30 removed: net +20 the filter cost
    assert c.rejected_losses_avoided == pytest.approx(30.0) and c.rejected_wins_forgone == pytest.approx(50.0)
    assert c.total_pnl_delta == pytest.approx(20.0 - 40.0)
    assert c.calls == 3 and c.input_tokens == 3000 and c.output_tokens == 180
    assert (c.cache_read_tokens, c.cache_write_tokens) == (1500, 300)
    assert c.est_cost_usd == pytest.approx((3000 * 5.0 + 180 * 25.0 + 1500 * 0.5 + 300 * 6.25) / 1e6)
    assert [(r["symbol"], r["status"]) for r in c.rows] == [("X", "closed"), ("Y", "closed"), ("W", "unfilled"), ("V", "open")]
    assert c.rows[2]["pnl_in_a"] is None and c.rows[3]["pnl_in_a"] is None


def test_format_compare_reads_sensibly(repo):
    _seed_pair(repo)
    text = format_compare(build_compare(repo, "A", "B", P))
    assert "Rejected by Claude      4 signals: 2 closed in A, 1 still open in A, 1 unfilled in A" in text
    assert "chop" in text and "unfilled" in text and "open" in text
    assert "Estimated cost" in text and "$0.0" in text
    assert "Verdict: mixed: the rejected signals were net winners" not in text  # delta negative, net positive
    assert "Verdict: filter did not help on this window" in text


def test_verdict_positive_branches(repo):
    _seed_pair(repo)
    repo.upsert_daily_pnl("B", "2026-09-14", 90.0, 0.0, 1, 1)  # B ends far ahead
    repo.conn.execute("UPDATE positions SET pnl = 90.0 WHERE run_id='B'")
    repo.conn.commit()
    text = format_compare(build_compare(repo, "A", "B", P))
    assert "Verdict: mixed: B earned more overall, but the rejected signals were net winners in A" in text
    repo.conn.execute("UPDATE positions SET pnl = -60.0 WHERE run_id='A' AND symbol='X'")
    repo.conn.commit()
    text = format_compare(build_compare(repo, "A", "B", P))
    assert "Verdict: filter helped" in text


def test_compare_with_no_rejections_and_no_usage(repo):
    repo.create_run("A", "backtest", 0, CFG)
    repo.create_run("B", "backtest", 0, CFG)
    c = build_compare(repo, "A", "B", P)
    assert c.rejected == 0 and c.calls == 0 and c.est_cost_usd == 0.0 and c.rows == []
    text = format_compare(c)
    assert "Rejected by Claude      0 signals" in text and "Bar (IST)" not in text


def test_compare_warns_on_config_mismatch_and_refuses_non_backtest(repo):
    other = json.dumps({"strategy": {"ema_rsi": {"fast": 21}}, "session": {"open": "09:15"}, "capital": 100000.0,
                        "risk": {"per_trade_pct": 1.0}, "execution": {"interval_minutes": 5}})
    _seed_pair(repo, cfg_b=other)
    c = build_compare(repo, "A", "B", P)
    assert c.warnings == ["runs differ in 'strategy': the comparison may not be like-for-like"]
    assert format_compare(c).splitlines()[1].startswith("WARNING: runs differ in 'strategy'")
    repo.create_run("L", "live", 0, CFG)
    with pytest.raises(ValueError, match="backtest runs only"):
        build_compare(repo, "A", "L", P)


def test_compare_requires_both_runs(repo):
    repo.create_run("A", "backtest", 0, CFG)
    with pytest.raises(ValueError):
        build_compare(repo, "A", "missing", P)


def test_compare_is_net_when_a_schedule_is_given(repo):
    from tradebot.config import ChargesConfig
    _seed_pair(repo)
    gross = build_compare(repo, "A", "B", P)
    net = build_compare(repo, "A", "B", P, ChargesConfig())
    assert gross.a.charges == 0.0
    assert net.a.charges > 0 and net.a.total_pnl == pytest.approx(gross.a.total_pnl - net.a.charges)
    assert net.rejected_pnl_in_a < gross.rejected_pnl_in_a     # the two rejected trades now carry their costs
    assert "Charges" in format_compare(net)


def test_side_by_side_marks_estimated_charges_and_gross_drawdown(repo):
    """Every stored row in _seed_pair has NULL charges, so a schedule makes both sides estimated:
    the Charges cells and the Max DD (equity) cells must say so; without a schedule neither does."""
    from tradebot.config import ChargesConfig
    _seed_pair(repo)
    net = build_compare(repo, "A", "B", P, ChargesConfig())
    text = format_compare(net)
    assert "(est.)" in text and "(gross)" in text
    gross = build_compare(repo, "A", "B", P)
    text2 = format_compare(gross)
    assert "(est.)" not in text2 and "(gross)" not in text2


def test_compare_warns_on_charges_mismatch(repo):
    """'charges' is one of COMPARABLE_KEYS: two runs whose stored config differ only there must
    warn, the same as any other comparability mismatch."""
    other = json.dumps({**json.loads(CFG), "charges": {"enabled": True}})
    _seed_pair(repo, cfg_b=other)
    c = build_compare(repo, "A", "B", P)
    assert any("'charges'" in w for w in c.warnings)
