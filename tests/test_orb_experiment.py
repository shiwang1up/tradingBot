"""Unit tests for scripts/orb_experiment.py. The script lives outside the package and has no
`from __future__ import annotations`, so it is loaded straight from its path rather than imported
as `tradebot.*` (amendment D7)."""
import importlib.util
from datetime import date
from pathlib import Path

from dataclasses import replace

from tradebot.config import SessionConfig
from tradebot.engine.clock import ist_epoch
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.types import Candle


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "orb_experiment.py"
    spec = importlib.util.spec_from_file_location("orb_experiment", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SESSION = SessionConfig(open="09:15", close="15:30", square_off="15:10", no_new_entries_after="13:00", holidays=())
INTERVAL = 15
EXPECTED_BARS = (15 * 60 + 30 - (9 * 60 + 15)) // INTERVAL  # 25 bars/day at this interval


def _day_candles(symbol: str, day: date, n: int) -> list:
    t0 = ist_epoch(day, "09:15")
    return [Candle(symbol, t0 + i * INTERVAL * 60, 1.0, 1.0, 1.0, 1.0, 1) for i in range(n)]


# The expected days are the TRADING days of the window's calendar (SessionClock.is_trading_day:
# weekdays that are not configured holidays), not the days that happen to have rows. 2026-08-16, the
# holdout's first date, is a Sunday, so these cases use Monday 17th to Wednesday 19th.
MON, TUE, WED = date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)


def test_check_holdout_data_counts_a_zero_bar_symbol_day_as_incomplete(capsys):
    """D7: a symbol-day with ZERO bars (B on day 2, C on both days) must count as incomplete - the
    first version only tallied symbol-days that had at least one row, so it missed exactly this
    case. Arithmetic on the expected-days basis: the window Mon..Tue holds 2 expected trading days,
    so the grid is 2 x 3 symbols = 6 symbol-days. A is complete on both, B on Monday only, C on
    neither: 3 complete, 3 incomplete of 6 (the same figures as before, because every expected day
    here has rows for at least one symbol)."""
    mod = _load_script()
    conn = connect(":memory:")
    repo = Repo(conn)
    repo.insert_candles(_day_candles("A", MON, EXPECTED_BARS) + _day_candles("A", TUE, EXPECTED_BARS)
                        + _day_candles("B", MON, EXPECTED_BARS), interval=INTERVAL)
    incomplete = mod.check_holdout_data(conn, ["A", "B", "C"], INTERVAL, (MON, TUE), SESSION)
    assert incomplete == 3
    out = capsys.readouterr().out
    assert "holdout data: 2 expected trading days, 3 symbols, 3 incomplete symbol-days of 6" in out


def test_check_holdout_data_counts_a_trading_day_missing_for_every_symbol(capsys):
    """A weekday with no rows for ANY symbol is invisible to a check that takes its days from the
    rows it saw. Mon..Wed is 3 expected days x 2 symbols = 6 symbol-days; Monday and Wednesday are
    complete for both (4), Tuesday has nothing at all: S = 2 more incomplete symbol-days."""
    mod = _load_script()
    conn = connect(":memory:")
    Repo(conn).insert_candles([c for s in ("A", "B") for d in (MON, WED) for c in _day_candles(s, d, EXPECTED_BARS)],
                              interval=INTERVAL)
    incomplete = mod.check_holdout_data(conn, ["A", "B"], INTERVAL, (MON, WED), SESSION)
    assert incomplete == 2
    assert "holdout data: 3 expected trading days, 2 symbols, 2 incomplete symbol-days of 6" in capsys.readouterr().out


def test_check_holdout_data_expects_no_bars_on_weekends_and_holidays(capsys):
    """Sun 16th..Wed 19th with Tuesday a configured holiday: 2 expected days (Mon, Wed), both
    complete, so nothing is incomplete - the empty Sunday and Tuesday are not gaps."""
    mod = _load_script()
    conn = connect(":memory:")
    Repo(conn).insert_candles(_day_candles("A", MON, EXPECTED_BARS) + _day_candles("A", WED, EXPECTED_BARS),
                              interval=INTERVAL)
    session = replace(SESSION, holidays=(TUE.isoformat(),))
    incomplete = mod.check_holdout_data(conn, ["A"], INTERVAL, (date(2026, 8, 16), WED), session)
    assert incomplete == 0
    assert "holdout data: 2 expected trading days, 1 symbols, 0 incomplete symbol-days of 2" in capsys.readouterr().out


def test_check_holdout_data_counts_a_short_day_and_reports_rows_outside_the_expected_days(capsys):
    """One bar short is incomplete; rows on a non-trading day (Sunday) are reported, not scored."""
    mod = _load_script()
    conn = connect(":memory:")
    Repo(conn).insert_candles(_day_candles("A", MON, EXPECTED_BARS - 1) + _day_candles("A", date(2026, 8, 16), 3),
                              interval=INTERVAL)
    incomplete = mod.check_holdout_data(conn, ["A"], INTERVAL, (date(2026, 8, 16), MON), SESSION)
    assert incomplete == 1
    out = capsys.readouterr().out
    assert "holdout data: 1 expected trading days, 1 symbols, 1 incomplete symbol-days of 1" in out
    assert "2026-08-16" in out


def test_check_holdout_data_closes_its_connection(capsys):
    mod = _load_script()
    conn = connect(":memory:")
    Repo(conn).insert_candles(_day_candles("A", MON, EXPECTED_BARS), interval=INTERVAL)
    mod.check_holdout_data(conn, ["A"], INTERVAL, (MON, MON), SESSION)
    import sqlite3
    try:
        conn.execute("SELECT 1")
        assert False, "the connection should have been closed"
    except sqlite3.ProgrammingError:
        pass


def test_check_holdout_data_reports_zero_incomplete_when_the_window_is_complete(capsys):
    mod = _load_script()
    conn = connect(":memory:")
    Repo(conn).insert_candles(_day_candles("A", MON, EXPECTED_BARS) + _day_candles("A", TUE, EXPECTED_BARS),
                              interval=INTERVAL)
    incomplete = mod.check_holdout_data(conn, ["A"], INTERVAL, (MON, TUE), SESSION)
    assert incomplete == 0
    assert "holdout data: 2 expected trading days, 1 symbols, 0 incomplete symbol-days of 2" in capsys.readouterr().out


def test_existing_run_ids_lists_only_the_ids_already_stored_and_closes_its_connection(tmp_path):
    """The regime phase checks every run id it is about to create BEFORE the first run, so a crash
    part-way cannot leave a second invocation to add a partial set of rows on top."""
    mod = _load_script()
    db = str(tmp_path / "t.db")
    conn = connect(db)
    repo = Repo(conn)
    repo.create_run("rg-orb-off-tune", "backtest", 0, "{}")
    conn.commit()
    conn.close()
    wanted = ["rg-orb-off-tune", "rg-orb-on-tune"]
    assert mod.existing_run_ids(db, wanted) == ["rg-orb-off-tune"]
    assert mod.existing_run_ids(db, ["rg-orb-on-tune"]) == []


def test_regime_run_ids_are_the_ids_the_phase_creates():
    mod = _load_script()
    assert mod.regime_run_ids("orb", ["tune"]) == ["rg-orb-off-tune", "rg-orb-on-tune"]
    assert mod.regime_run_ids("confluence", ["tune", "hold"]) == [
        "rg-confluence-off-tune", "rg-confluence-on-tune", "rg-confluence-off-hold", "rg-confluence-on-hold"]
