"""The only module that imports growwapi. Plan 1 needs auth + historical candles only.
Order, feed, position, and margin methods are added in Plan 3."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tradebot.engine.clock import IST, to_ist
from tradebot.types import Candle

NO_RETRY_MARKERS = ("Authentication", "Authorisation", "BadRequest")


def with_retry(fn: Callable[[], Any], attempts: int = 3, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> Any:
    """Exponential backoff. Auth/authorisation/bad-request errors are never retried."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - SDK exceptions vary; we inspect the class name
            name = type(e).__name__
            if any(m in name for m in NO_RETRY_MARKERS) or i == attempts - 1:
                raise
            sleep(base_delay * (2 ** i))
    raise RuntimeError("unreachable")


def _to_epoch(v: Any) -> int:
    if isinstance(v, (int, float)):
        v = int(v)
        return v // 1000 if v > 10_000_000_000 else v
    s = str(v).strip()
    if s.isdigit():
        return _to_epoch(int(s))
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp())


def parse_candles(symbol: str, resp: dict | None) -> list[Candle]:
    rows = (resp or {}).get("candles") or []
    out = []
    for r in rows:
        out.append(Candle(symbol, _to_epoch(r[0]), float(r[1]), float(r[2]), float(r[3]),
                          float(r[4]), int(float(r[5] or 0))))
    return out


def _fmt_ist(ts: int) -> str:
    return to_ist(ts).strftime("%Y-%m-%d %H:%M:%S")


# Interval in minutes -> Groww V2 candle_interval string (matches GrowwAPI.CANDLE_INTERVAL_* constants).
_INTERVAL_NAMES = {1: "1minute", 2: "2minute", 3: "3minute", 5: "5minute", 10: "10minute", 15: "15minute",
                   30: "30minute", 60: "1hour", 240: "4hour", 1440: "1day", 10080: "1week"}


def candle_interval_name(interval_minutes: int) -> str:
    try:
        return _INTERVAL_NAMES[interval_minutes]
    except KeyError as e:
        raise ValueError(f"unsupported candle interval: {interval_minutes} minutes") from e


def groww_symbol(exchange: str, trading_symbol: str) -> str:
    """Groww's symbol form for cash equities, e.g. NSE-RELIANCE."""
    return f"{exchange}-{trading_symbol}"


class GrowwAdapter:
    def __init__(self, api_key: str, totp_secret: str):
        if not api_key or not totp_secret:
            raise ValueError("GROWW_API_KEY and GROWW_TOTP_SECRET must be set in .env")
        self._api_key = api_key
        self._totp_secret = totp_secret
        self._client = None

    @property
    def client(self):
        """Authenticate lazily, once. A failed auth raises; never loop on it."""
        if self._client is None:
            import pyotp
            from growwapi import GrowwAPI

            totp = pyotp.TOTP(self._totp_secret).now()
            token = GrowwAPI.get_access_token(api_key=self._api_key, totp=totp)
            self._client = GrowwAPI(token)
        return self._client

    def fetch_candles(self, symbol: str, exchange: str, start_ts: int, end_ts: int, interval: int) -> list[Candle]:
        """Cash-segment candles via the V2 endpoint (get_historical_candle_data is deprecated).

        V2 rows are [iso_timestamp, o, h, l, c, volume, open_interest]; parse_candles reads the
        first six and treats naive timestamps as IST.
        """
        def call():
            return self.client.get_historical_candles(
                exchange=exchange,
                segment=self.client.SEGMENT_CASH,
                groww_symbol=groww_symbol(exchange, symbol),
                start_time=_fmt_ist(start_ts),
                end_time=_fmt_ist(end_ts),
                candle_interval=candle_interval_name(interval),
                timeout=30,
            )

        return parse_candles(symbol, with_retry(call))
