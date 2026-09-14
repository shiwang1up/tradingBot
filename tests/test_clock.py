from datetime import date

from tradebot.config import SessionConfig
from tradebot.engine.clock import SessionClock, date_of, ist_epoch, to_ist

SESSION = SessionConfig(open="09:15", close="15:30", square_off="15:10",
                        no_new_entries_after="14:45", holidays=("2026-10-02",))


def test_ist_epoch_known_value():
    # 2026-09-14 09:15 IST == 2026-09-14 03:45 UTC
    assert ist_epoch(date(2026, 9, 14), "09:15") == 1789357500


def test_to_ist_and_date_of_roundtrip():
    ts = ist_epoch(date(2026, 9, 14), "15:29")
    dt = to_ist(ts)
    assert (dt.hour, dt.minute) == (15, 29)
    assert date_of(ts) == date(2026, 9, 14)


def test_trading_day_excludes_weekends_and_holidays():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.is_trading_day(date(2026, 9, 14))       # Monday
    assert not clk.is_trading_day(date(2026, 9, 13))   # Sunday
    assert not clk.is_trading_day(date(2026, 10, 2))   # holiday


def test_square_off_bar_is_last_bar_ending_at_or_before_square_off():
    clk = SessionClock(SESSION, interval_minutes=5)
    d = date(2026, 9, 14)
    assert clk.square_off_bar_ts(d) == ist_epoch(d, "15:05")
    assert clk.is_square_off_bar(ist_epoch(d, "15:05"))
    assert not clk.is_square_off_bar(ist_epoch(d, "15:00"))


def test_entries_allowed_cutoff():
    clk = SessionClock(SESSION, interval_minutes=5)
    d = date(2026, 9, 14)
    assert clk.entries_allowed(ist_epoch(d, "14:40"))
    assert not clk.entries_allowed(ist_epoch(d, "14:45"))
    assert not clk.entries_allowed(ist_epoch(d, "09:10"))  # before open


def test_in_session():
    clk = SessionClock(SESSION, interval_minutes=5)
    d = date(2026, 9, 14)
    assert clk.in_session(ist_epoch(d, "09:15"))
    assert clk.in_session(ist_epoch(d, "15:25"))
    assert not clk.in_session(ist_epoch(d, "15:30"))
