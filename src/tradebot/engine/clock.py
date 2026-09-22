"""The only module that knows about IST. Everything else uses UTC epoch seconds.

Bar-time convention: every ``ts`` handed to this class is a bar's OPEN time. So
``entries_allowed`` is true for the bar that opens strictly before the cutoff, and
``square_off_bar_ts`` is the open of the last bar that CLOSES at or before square-off.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from tradebot.config import SessionConfig

IST = ZoneInfo("Asia/Kolkata")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(s: str) -> time:
    m = _HHMM.match(s)
    if not m:
        raise ValueError(f"expected a time in HH:MM form, got {s!r}")
    return time(int(m.group(1)), int(m.group(2)))


def _minute_of_day(s: str) -> int:
    t = _parse_hhmm(s)
    return t.hour * 60 + t.minute


def ist_epoch(d: date, hhmm: str) -> int:
    """Epoch seconds for the given IST wall-clock time on date d."""
    return int(datetime.combine(d, _parse_hhmm(hhmm), tzinfo=IST).timestamp())


def to_ist(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=IST)


def date_of(ts: int) -> date:
    return to_ist(ts).date()


def iso_ist(ts: int) -> str:
    return to_ist(ts).isoformat()


class SessionClock:
    """Session boundaries for a given bar interval. Pure; no wall-clock access.

    Validates at construction that the session times are ordered and that at least one
    bar fits between open and square-off, and that the square-off bar does not precede
    the entry cutoff (otherwise an entry could be opened after square-off already ran).

    An interval of a day or more sets ``daily``, and only the ordering check applies: a daily
    bar is stamped 00:00 IST, before the open, so the intra-session window would reject every
    one of them, and a position held for weeks is never squared off at the close.
    """

    def __init__(self, session: SessionConfig, interval_minutes: int):
        self.session = session
        self.interval_sec = interval_minutes * 60
        self._holidays = {date.fromisoformat(h) for h in session.holidays}
        o, cut, sq, c = (_minute_of_day(x) for x in
                         (session.open, session.no_new_entries_after, session.square_off, session.close))
        if not (o < cut <= sq <= c):
            raise ValueError("session times must satisfy open < no_new_entries_after <= square_off <= close")
        self.daily = interval_minutes >= 1440
        if self.daily:
            # One bar a day, stamped 00:00 IST. The intra-session window and the square-off do
            # not apply: a CNC position is meant to survive the close, which is the entire point.
            self._square_off_offset_sec = 0
            return
        n_bars = (sq - o) // interval_minutes
        if n_bars < 1:
            raise ValueError(f"interval {interval_minutes}m does not fit between {session.open} and {session.square_off}")
        self._square_off_offset_sec = (n_bars - 1) * self.interval_sec
        if o + (n_bars - 1) * interval_minutes < cut:
            raise ValueError("square-off bar would open before no_new_entries_after; shorten the interval or move the cutoff")

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._holidays

    def open_ts(self, d: date) -> int:
        return ist_epoch(d, self.session.open)

    def close_ts(self, d: date) -> int:
        return ist_epoch(d, self.session.close)

    def in_session(self, ts: int) -> bool:
        d = date_of(ts)
        if self.daily:
            return self.is_trading_day(d)
        return self.open_ts(d) <= ts < self.close_ts(d)

    def square_off_bar_ts(self, d: date) -> int:
        """Open time of the last bar whose end is at or before the square-off time."""
        return self.open_ts(d) + self._square_off_offset_sec

    def is_square_off_bar(self, ts: int) -> bool:
        if self.daily:
            return False
        return ts == self.square_off_bar_ts(date_of(ts))

    def square_off_due(self, ts: int) -> bool:
        """True from the square-off bar onward. Callers latch once per day so a missing
        bar at exactly the square-off time cannot skip the square-off."""
        if self.daily:
            return False
        return ts >= self.square_off_bar_ts(date_of(ts))

    def entries_allowed(self, ts: int) -> bool:
        d = date_of(ts)
        if self.daily:
            return self.is_trading_day(d)
        cutoff = ist_epoch(d, self.session.no_new_entries_after)
        return self.open_ts(d) <= ts < cutoff

    def last_bar_ts(self, d: date) -> int:
        """Open time of the session's last bar (the one that ends at the close)."""
        return self.close_ts(d) - self.interval_sec

    def latest_complete_bar(self, now_ts: int, grace_sec: int = 0) -> Optional[int]:
        """Open time of the most recent bar of now_ts's day whose close plus grace is at or before
        now_ts, capped at the session's last bar. None before the first bar has completed. Paper
        mode uses it to decide which bars are ready to fetch."""
        d = date_of(now_ts)
        o = self.open_ts(d)
        closed_bars = (now_ts - grace_sec - o) // self.interval_sec
        if closed_bars < 1:
            return None
        return min(o + (closed_bars - 1) * self.interval_sec, self.last_bar_ts(d))
