"""Unit tests for scripts/orb_experiment.py. The script lives outside the package and has no
`from __future__ import annotations`, so it is loaded straight from its path rather than imported
as `tradebot.*` (amendment D7)."""
import importlib.util
from datetime import date
from pathlib import Path

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


def test_check_holdout_data_counts_a_zero_bar_symbol_day_as_incomplete(capsys):
    """D7: a symbol-day with ZERO bars (B on day 2, C on both days) must count as incomplete -
    the pre-fix version only tallied symbol-days that had at least one row, so it missed exactly
    this case. A complete: 2 days; B: day 1 only; C: absent entirely -> 2 days, 3 incomplete of 6."""
    mod = _load_script()
    conn = connect(":memory:")
    repo = Repo(conn)
    day1, day2 = date(2026, 8, 16), date(2026, 8, 17)
    repo.insert_candles(_day_candles("A", day1, EXPECTED_BARS) + _day_candles("A", day2, EXPECTED_BARS)
                        + _day_candles("B", day1, EXPECTED_BARS), interval=INTERVAL)
    # B has no candles at all on day2; C has none on either day.
    incomplete = mod.check_holdout_data(conn, ["A", "B", "C"], INTERVAL, (day1, day2), SESSION)
    assert incomplete == 3
    out = capsys.readouterr().out
    assert "holdout data: 2 days, 3 symbols, 3 incomplete symbol-days of 6" in out


def test_check_holdout_data_closes_its_connection(capsys):
    mod = _load_script()
    conn = connect(":memory:")
    day = date(2026, 8, 16)
    Repo(conn).insert_candles(_day_candles("A", day, EXPECTED_BARS), interval=INTERVAL)
    mod.check_holdout_data(conn, ["A"], INTERVAL, (day, day), SESSION)
    import sqlite3
    try:
        conn.execute("SELECT 1")
        assert False, "the connection should have been closed"
    except sqlite3.ProgrammingError:
        pass


def test_check_holdout_data_reports_zero_incomplete_when_the_window_is_complete(capsys):
    mod = _load_script()
    conn = connect(":memory:")
    day1, day2 = date(2026, 8, 16), date(2026, 8, 17)
    Repo(conn).insert_candles(_day_candles("A", day1, EXPECTED_BARS) + _day_candles("A", day2, EXPECTED_BARS),
                              interval=INTERVAL)
    incomplete = mod.check_holdout_data(conn, ["A"], INTERVAL, (day1, day2), SESSION)
    assert incomplete == 0
    assert "holdout data: 2 days, 1 symbols, 0 incomplete symbol-days of 2" in capsys.readouterr().out
