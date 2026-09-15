# tests/test_clock.py
from dataclasses import replace
from datetime import date

import pytest

from tradebot.config import SessionConfig
from tradebot.engine.clock import SessionClock, date_of, iso_ist, ist_epoch, to_ist

SESSION = SessionConfig(open="09:15", close="15:30", square_off="15:10",
                        no_new_entries_after="14:45", holidays=("2026-10-02", "2026-09-12"))
D = date(2026, 9, 14)  # Monday


def test_ist_epoch_known_values():
    assert ist_epoch(D, "09:15") == 1789357500          # 2026-09-14 03:45 UTC
    assert ist_epoch(date(2026, 1, 5), "09:15") == 1767584700  # no DST in IST: same offset in January


def test_to_ist_date_of_and_iso_roundtrip():
    ts = ist_epoch(D, "15:29")
    dt = to_ist(ts)
    assert (dt.hour, dt.minute) == (15, 29)
    assert date_of(ts) == D
    assert iso_ist(ts) == "2026-09-14T15:29:00+05:30"


def test_trading_day_excludes_weekends_and_holidays():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.is_trading_day(D)
    assert not clk.is_trading_day(date(2026, 9, 13))   # Sunday
    assert not clk.is_trading_day(date(2026, 10, 2))   # holiday on a Friday
    assert not clk.is_trading_day(date(2026, 9, 12))   # holiday that is also a Saturday


def test_square_off_bar_is_last_bar_ending_at_or_before_square_off():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.square_off_bar_ts(D) == ist_epoch(D, "15:05")
    assert clk.is_square_off_bar(ist_epoch(D, "15:05"))
    assert not clk.is_square_off_bar(ist_epoch(D, "15:00"))
    assert not clk.is_square_off_bar(ist_epoch(D, "15:10"))  # a bar opening at 15:10 ends after square-off


@pytest.mark.parametrize("interval, expected", [(1, "15:09"), (5, "15:05"), (15, "14:45")])
def test_square_off_bar_other_intervals(interval, expected):
    clk = SessionClock(SESSION, interval_minutes=interval)
    assert clk.square_off_bar_ts(D) == ist_epoch(D, expected)


def test_square_off_due_is_true_from_the_bar_onward():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert not clk.square_off_due(ist_epoch(D, "15:00"))
    assert clk.square_off_due(ist_epoch(D, "15:05"))
    assert clk.square_off_due(ist_epoch(D, "15:20"))


def test_entries_allowed_cutoff():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.entries_allowed(ist_epoch(D, "14:40"))
    assert not clk.entries_allowed(ist_epoch(D, "14:45"))
    assert not clk.entries_allowed(ist_epoch(D, "09:10"))  # before open


def test_in_session():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.in_session(ist_epoch(D, "09:15"))
    assert clk.in_session(ist_epoch(D, "15:25"))
    assert not clk.in_session(ist_epoch(D, "15:30"))


def test_rejects_interval_that_does_not_fit():
    with pytest.raises(ValueError, match="does not fit"):
        SessionClock(SESSION, interval_minutes=360)


def test_rejects_square_off_bar_before_entry_cutoff():
    # 30m bars: square-off bar would open 14:15, before the 14:45 cutoff
    with pytest.raises(ValueError, match="before no_new_entries_after"):
        SessionClock(SESSION, interval_minutes=30)


def test_rejects_unordered_session_times():
    with pytest.raises(ValueError, match="open < no_new_entries_after"):
        SessionClock(replace(SESSION, square_off="14:00"), interval_minutes=5)


def test_rejects_malformed_holiday_and_time():
    with pytest.raises(ValueError):
        SessionClock(replace(SESSION, holidays=("2026-10-02 00:00:00",)), interval_minutes=5)
    with pytest.raises(ValueError, match="HH:MM"):
        ist_epoch(D, "09:15:00")


def test_last_bar_and_latest_complete_bar():
    clk = SessionClock(SESSION, interval_minutes=5)
    assert clk.last_bar_ts(D) == ist_epoch(D, "15:25")
    o = ist_epoch(D, "09:15")
    assert clk.latest_complete_bar(o + 299, 0) is None            # first bar still open
    assert clk.latest_complete_bar(o + 300, 0) == o                # closed exactly now
    assert clk.latest_complete_bar(o + 304, grace_sec=5) is None   # closed, but inside the grace period
    assert clk.latest_complete_bar(o + 305, grace_sec=5) == o
    assert clk.latest_complete_bar(ist_epoch(D, "12:00"), 0) == ist_epoch(D, "11:55")
    assert clk.latest_complete_bar(ist_epoch(D, "12:00") + 3, grace_sec=5) == ist_epoch(D, "11:50")
    assert clk.latest_complete_bar(ist_epoch(D, "16:00"), 0) == ist_epoch(D, "15:25")  # capped at the last bar
