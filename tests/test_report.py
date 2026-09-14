import pytest

from tradebot.report.summary import build_summary, format_summary
from tradebot.types import Position, Signal


def _seed(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    trades = [  # (direction, entry, stop, qty, exit, reason, closed_at)
        ("LONG", 100.0, 99.0, 10, 102.0, "TARGET", 10),   # +20, R=2
        ("LONG", 100.0, 99.0, 10, 99.0, "STOP", 20),      # -10, R=-1
        ("SHORT", 100.0, 101.0, 10, 101.0, "STOP", 30),   # -10, R=-1
        ("LONG", 100.0, 98.0, 5, 101.0, "SQUARE_OFF", 40),  # +5, R=0.5
    ]
    for i, (d, e, s, q, x, why, ct) in enumerate(trades):
        p = Position("S%d" % i, "MIS", d, q, e, s, None, ct - 5, "cid%d" % i, "ema_rsi")
        pid = repo.insert_position("r1", p)
        pnl = (x - e) * q if d == "LONG" else (e - x) * q
        repo.close_position(pid, ct, x, why, pnl)
    adopted = Position("ADO", "MIS", "LONG", 1, 100.0, 98.5, None, 1, "cidA", "adopted", adopted=True)
    pid = repo.insert_position("r1", adopted)
    repo.close_position(pid, 50, 90.0, "STOP", -10.0)
    open_pos = Position("OPEN", "CNC", "LONG", 1, 100.0, 95.0, None, 60, "cidO", "ema_rsi")
    repo.insert_position("r1", open_pos)
    sid = repo.insert_signal("r1", Signal("ema_rsi", "X", "LONG", 1, 0.5, 2, "MIS", 1))
    repo.insert_risk_decision("r1", sid, False, "cooldown", 0)
    repo.insert_ai_decision("r1", sid, "stub", False, "n", 0.5, 1, None)
    repo.upsert_daily_pnl("r1", "2026-09-14", 5.0, 0.0, 4, 5)


def test_build_summary_metrics(repo):
    _seed(repo)
    s = build_summary(repo, "r1")
    assert s.trades == 4 and s.wins == 2 and s.losses == 2
    assert s.win_rate == pytest.approx(0.5)
    assert s.total_pnl == pytest.approx(5.0)
    assert s.avg_r == pytest.approx((2 - 1 - 1 + 0.5) / 4)
    assert s.max_drawdown == pytest.approx(20.0)  # peak 20 after t1, trough 0 after t3
    assert s.exit_reasons == {"TARGET": 1, "STOP": 2, "SQUARE_OFF": 1}
    assert s.risk_rejections == {"cooldown": 1}
    assert s.ai_rejections == 1
    assert s.open_positions == 1
    assert s.adopted_pnl == pytest.approx(-10.0)
    assert [d["date"] for d in s.days] == ["2026-09-14"]


def test_format_summary_mentions_key_numbers(repo):
    _seed(repo)
    text = format_summary(build_summary(repo, "r1"))
    assert "Trades" in text and "4" in text
    assert "Win rate" in text and "50.0%" in text
    assert "Survivorship" in text


def test_unknown_run_raises(repo):
    with pytest.raises(ValueError):
        build_summary(repo, "nope")
