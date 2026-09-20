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
        repo.close_position(pid, ct, x, why, pnl, charges=None)
    adopted = Position("ADO", "MIS", "LONG", 1, 100.0, 98.5, None, 1, "cidA", "adopted", adopted=True)
    pid = repo.insert_position("r1", adopted)
    repo.close_position(pid, 50, 90.0, "STOP", -10.0, charges=None)
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
    repo.close_position(pid, 2, 105.0, "TARGET", 50.0, charges=None)
    pid2 = repo.insert_position("r2", Position("N", "MIS", "LONG", 1, 100.0, 99.0, None, 3, "c2", "s"))
    repo.close_position(pid2, 4, None, "SQUARE_OFF", None, charges=None)
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


def test_summary_is_net_of_stored_charges(repo):
    repo.create_run("n1", "backtest", 0, "{}")
    for i, (exit_, pnl, ch) in enumerate([(102.0, 20.0, 5.0), (100.2, 2.0, 5.0)]):
        p = Position("S%d" % i, "MIS", "LONG", 10, 100.0, 99.0, None, i, "c%d" % i, "ema_rsi")
        repo.close_position(repo.insert_position("n1", p), 10 + i, exit_, "TARGET", pnl, charges=ch)
    s = build_summary(repo, "n1")
    assert s.gross_pnl == pytest.approx(22.0) and s.charges == pytest.approx(10.0)
    assert s.total_pnl == pytest.approx(12.0) and s.charges_estimated is False
    assert s.wins == 1 and s.losses == 1          # +2 gross is -3 net: a loss
    assert s.avg_r == pytest.approx((15.0 / 10.0 + -3.0 / 10.0) / 2)
    text = format_summary(s)
    assert "Gross PnL" in text and "Charges" in text and "estimated" not in text


def test_old_rows_are_estimated_with_the_given_schedule(repo):
    from tradebot.config import ChargesConfig
    repo.create_run("o1", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 100, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("o1", p), 9, 102.0, "TARGET", 200.0, charges=None)  # charges NULL
    bare = build_summary(repo, "o1")
    assert bare.charges == 0.0 and bare.total_pnl == pytest.approx(200.0)           # no schedule given: as before
    s = build_summary(repo, "o1", ChargesConfig())
    assert s.charges == pytest.approx(27.42) and s.total_pnl == pytest.approx(172.58)
    assert s.charges_estimated is True and "est." in format_summary(s)


def test_estimated_note_counts_only_the_estimated_trades(repo):
    """A run mixing stored and NULL charges must say how many of its trades were actually
    estimated, not imply the whole run predates charges."""
    from tradebot.config import ChargesConfig
    repo.create_run("mix1", "backtest", 0, "{}")
    p1 = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("mix1", p1), 9, 102.0, "TARGET", 20.0, charges=5.0)
    p2 = Position("B", "MIS", "LONG", 10, 100.0, 99.0, None, 2, "c2", "ema_rsi")
    repo.close_position(repo.insert_position("mix1", p2), 10, 102.0, "TARGET", 20.0, charges=None)
    s = build_summary(repo, "mix1", ChargesConfig())
    assert s.charges_estimated is True and s.charges_estimated_trades == 1 and s.trades == 2
    text = format_summary(s)
    assert "est. for 1 of 2 trades (not recorded at the time)" in text


def test_gross_equity_drawdown_is_marked_when_charges_are_estimated(repo):
    from tradebot.config import ChargesConfig
    repo.create_run("gdd1", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("gdd1", p), 9, 102.0, "TARGET", 20.0, charges=None)
    repo.upsert_daily_pnl("gdd1", "2026-09-14", realised=20.0, unrealised=0.0, fills=1, entries_placed=1)
    s = build_summary(repo, "gdd1", ChargesConfig())
    text = format_summary(s)
    assert "Max drawdown (equity)" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("Max drawdown (equity)"))
    assert "gross here: the daily rows have no recorded charges" in line


def test_equity_drawdown_says_partly_gross_for_a_mixed_run(repo):
    """Only one of two trades predates charges: calling the whole run's daily rows gross would be
    false, so the caveat must say "partly gross" instead."""
    from tradebot.config import ChargesConfig
    repo.create_run("mixdd1", "backtest", 0, "{}")
    p1 = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("mixdd1", p1), 9, 102.0, "TARGET", 20.0, charges=5.0)
    p2 = Position("B", "MIS", "LONG", 10, 100.0, 99.0, None, 2, "c2", "ema_rsi")
    repo.close_position(repo.insert_position("mixdd1", p2), 10, 102.0, "TARGET", 20.0, charges=None)
    repo.upsert_daily_pnl("mixdd1", "2026-09-14", realised=40.0, unrealised=0.0, fills=2, entries_placed=2)
    s = build_summary(repo, "mixdd1", ChargesConfig())
    text = format_summary(s)
    line = next(ln for ln in text.splitlines() if ln.startswith("Max drawdown (equity)"))
    assert "partly gross: some daily rows have no recorded charges" in line


def test_adopted_line_says_gross_pnl(repo):
    _seed(repo)
    text = format_summary(build_summary(repo, "r1"))
    line = next(ln for ln in text.splitlines() if ln.startswith("Adopted"))
    assert "gross PnL" in line


def test_row_charges_tolerates_a_bad_stored_row(repo, caplog):
    """A closed row with a nonsensical exit price (e.g. a zero from a bad candle, spec elsewhere)
    must not crash the report when a schedule is given to estimate its charges: position_charges
    raises ValueError on a non-positive price, and row_charges must contain that per row, log a
    warning naming the position, and mark it unknown so the report can count it."""
    import logging

    from tradebot.config import ChargesConfig
    from tradebot.report.summary import row_charges
    repo.create_run("bad1", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    pid = repo.insert_position("bad1", p)
    repo.close_position(pid, 9, 0.0, "TARGET", -1000.0, charges=None)
    row = repo.list_positions("bad1")[0]
    with caplog.at_level(logging.WARNING, logger="tradebot.report"):
        assert row_charges(row, ChargesConfig()) == (0.0, False, True)
    assert str(pid) in caplog.text and "ValueError" in caplog.text
    s = build_summary(repo, "bad1", ChargesConfig())
    assert s.charges == 0.0 and s.total_pnl == pytest.approx(-1000.0)
    assert s.charges_unknown == 1
    assert "1 trade(s) could not be charged (bad stored prices)" in format_summary(s)


def test_row_charges_exit_price_none_is_not_unknown(repo):
    """A position closed without a price (e.g. SQUARE_OFF with nothing to mark against) predates a
    price rather than having a bad one, so it must not count as unknown."""
    from tradebot.config import ChargesConfig
    from tradebot.report.summary import row_charges
    repo.create_run("noexit", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("noexit", p), 9, None, "SQUARE_OFF", None, charges=None)
    row = repo.list_positions("noexit")[0]
    assert row_charges(row, ChargesConfig()) == (0.0, False, False)


def test_since_ts_filters_positions_by_opened_at(repo):
    """D3: `since_ts` keeps only positions opened at or after it, so a filter-on run's warm-up days
    (all rejected regime_not_ready) can be scored against a filter-off run over the same days."""
    _seed(repo)
    s = build_summary(repo, "r1", since_ts=15)
    assert s.trades == 3 and s.wins == 1 and s.losses == 2      # the first trade (opened_at=5) drops out
    assert s.total_pnl == pytest.approx(-10 - 10 + 5)
    assert s.open_positions == 1                                # OPEN's opened_at=60 >= 15: still counted
    assert s.adopted_trades == 0                                # ADO's opened_at=1 < 15: excluded now
    unfiltered = build_summary(repo, "r1")
    assert unfiltered.trades == 4                                # sanity: since_ts=None is unchanged


def test_since_ts_filters_daily_pnl_rows_by_ist_date(repo):
    """D3: daily rows dated before the IST date of since_ts must not enter the summary."""
    from datetime import date

    from tradebot.engine.clock import ist_epoch
    repo.create_run("sd1", "backtest", 0, "{}")
    repo.upsert_daily_pnl("sd1", "2026-09-14", realised=10.0, unrealised=0.0, fills=1, entries_placed=1)
    repo.upsert_daily_pnl("sd1", "2026-09-15", realised=20.0, unrealised=0.0, fills=1, entries_placed=1)
    since = ist_epoch(date(2026, 9, 15), "00:00")
    s = build_summary(repo, "sd1", since_ts=since)
    assert [d["date"] for d in s.days] == ["2026-09-15"]
    assert build_summary(repo, "sd1") is not None and len(build_summary(repo, "sd1").days) == 2


def test_r_on_risk_pools_all_trades_instead_of_averaging_per_trade(repo):
    """Avg R is an unweighted mean over trades, so a scrap-sized position with a razor-thin stop can
    dominate it once the per-order brokerage floor eats most of its tiny risk. R on risk pools net
    PnL and rupees at risk across every trade instead, so its sign follows net PnL, not outliers.

    Trade 1: qty 10, entry 100, stop 99 -> risk 1*10 = 10.0; gross pnl 20, charges 5 -> net 15.0;
             R = 15.0 / 10.0 = 1.5
    Trade 2: qty 1, entry 100, stop 99.9 -> risk 0.1*1 = 0.1; gross pnl -0.1, charges 5 -> net -5.1;
             R = -5.1 / 0.1 = -51.0
    avg_r (unweighted mean of per-trade R) = (1.5 + -51.0) / 2 = -24.75
    r_on_risk = total net / total risk = (15.0 + -5.1) / (10.0 + 0.1) = 9.9 / 10.1 ~= 0.980
    """
    repo.create_run("ronrisk", "backtest", 0, "{}")
    p1 = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("ronrisk", p1), 9, 102.0, "TARGET", 20.0, charges=5.0)
    p2 = Position("B", "MIS", "LONG", 1, 100.0, 99.9, None, 2, "c2", "ema_rsi")
    repo.close_position(repo.insert_position("ronrisk", p2), 10, 99.9, "STOP", -0.1, charges=5.0)
    s = build_summary(repo, "ronrisk")
    assert s.avg_r == pytest.approx((1.5 + -51.0) / 2)
    assert s.r_on_risk == pytest.approx(9.9 / 10.1)
    text = format_summary(s)
    assert "R on risk" in text
