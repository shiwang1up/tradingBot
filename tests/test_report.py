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
    assert s.max_drawdown_equity == pytest.approx(0.0)  # single positive daily row
    assert s.r_trades == 4
    assert s.exit_reasons == {"TARGET": 1, "STOP": 2, "SQUARE_OFF": 1}
    assert s.risk_rejections == {"cooldown": 1}
    assert s.ai_rejections == 1
    assert s.open_positions == 1
    assert s.adopted_trades == 1 and s.adopted_pnl == pytest.approx(-10.0)
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


def test_empty_run_reports_without_dividing_by_zero(repo):
    repo.create_run("empty", "backtest", 0, "{}")
    s = build_summary(repo, "empty")
    assert (s.trades, s.win_rate, s.avg_r, s.r_trades, s.max_drawdown, s.max_drawdown_equity) == (0, 0.0, 0.0, 0, 0.0, 0.0)
    text = format_summary(s)
    assert "n/a" in text and "none" in text


def test_zero_risk_trade_is_excluded_from_avg_r_and_null_pnl_tolerated(repo):
    repo.create_run("r2", "backtest", 0, "{}")
    pid = repo.insert_position("r2", Position("Z", "MIS", "LONG", 10, 100.0, 100.0, None, 1, "c", "s"))
    repo.close_position(pid, 2, 105.0, "TARGET", 50.0)
    pid2 = repo.insert_position("r2", Position("N", "MIS", "LONG", 1, 100.0, 99.0, None, 3, "c2", "s"))
    repo.close_position(pid2, 4, None, "SQUARE_OFF", None)
    s = build_summary(repo, "r2")
    assert s.trades == 2 and s.r_trades == 1 and s.avg_r == 0.0 and s.total_pnl == 50.0


def test_equity_drawdown_counts_open_excursions(repo):
    repo.create_run("r3", "backtest", 0, "{}")
    repo.upsert_daily_pnl("r3", "2026-09-14", realised=0.0, unrealised=-800.0, fills=1, entries_placed=1)
    repo.upsert_daily_pnl("r3", "2026-09-15", realised=0.0, unrealised=800.0, fills=0, entries_placed=0)
    s = build_summary(repo, "r3")
    assert s.max_drawdown == 0.0 and s.max_drawdown_equity == pytest.approx(800.0)


def test_daily_table_alignment_survives_large_numbers(repo):
    repo.create_run("r4", "backtest", 0, "{}")
    repo.upsert_daily_pnl("r4", "2026-09-14", realised=12_345_678.9, unrealised=-2_345.5, fills=4, entries_placed=5)
    repo.upsert_daily_pnl("r4", "2026-09-15", realised=5.0, unrealised=0.0, fills=12345, entries_placed=99999)
    lines = format_summary(build_summary(repo, "r4")).splitlines()
    table = [ln for ln in lines if ln[:4] in ("Date", "2026")]
    assert len({len(ln) for ln in table}) == 1, "header and every row have identical width"
