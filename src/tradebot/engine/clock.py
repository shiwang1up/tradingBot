"""The only module that knows about IST. Everything else uses UTC epoch seconds."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from tradebot.config import SessionConfig

IST = ZoneInfo("Asia/Kolkata")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


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
    """Session boundaries for a given bar interval. Pure; no wall-clock access."""

    def __init__(self, session: SessionConfig, interval_minutes: int):
        self.session = session
        self.interval_sec = interval_minutes * 60
        self._holidays = set(session.holidays)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in self._holidays

    def open_ts(self, d: date) -> int:
        return ist_epoch(d, self.session.open)

    def close_ts(self, d: date) -> int:
        return ist_epoch(d, self.session.close)

    def in_session(self, ts: int) -> bool:
        d = date_of(ts)
        return self.open_ts(d) <= ts < self.close_ts(d)

    def square_off_bar_ts(self, d: date) -> int:
        """Open time of the last bar whose end is at or before the square-off time."""
        sq = ist_epoch(d, self.session.square_off)
        n_bars = (sq - self.open_ts(d)) // self.interval_sec
        return self.open_ts(d) + (n_bars - 1) * self.interval_sec

    def is_square_off_bar(self, ts: int) -> bool:
        return ts == self.square_off_bar_ts(date_of(ts))

    def entries_allowed(self, ts: int) -> bool:
        d = date_of(ts)
        cutoff = ist_epoch(d, self.session.no_new_entries_after)
        return self.open_ts(d) <= ts < cutoff
