"""The only module that imports growwapi. Plan 1 needs auth + historical candles only.
Order, feed, position, and margin methods are added in the live plan."""
from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tradebot.engine.clock import IST, to_ist
from tradebot.types import Candle

# Errors that must never be retried (spec 11): auth, authorisation, bad request, not found.
# The SDK raises the generic GrowwAPIException for Groww's {"status": "FAILURE"} bodies, so
# we match on the HTTP-style error code as well as the exception class name.
NO_RETRY_MARKERS = ("Authentication", "Authorisation", "Authorization", "BadRequest", "NotFound")
NO_RETRY_CODES = {"400", "401", "403", "404"}


def is_non_retryable(e: BaseException) -> bool:
    name = type(e).__name__
    if any(m in name for m in NO_RETRY_MARKERS):
        return True
    code = getattr(e, "code", None)
    return code is not None and str(code) in NO_RETRY_CODES


def is_rate_limited(e: BaseException) -> bool:
    return "RateLimit" in type(e).__name__ or str(getattr(e, "code", "")) == "429"


def with_retry(fn: Callable[[], Any], attempts: int = 3, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep, rate_limit_delay: float = 10.0) -> Any:
    """Exponential backoff (1s, 2s, ...). Non-retryable errors propagate on the first attempt.
    A rate limit (429) waits `rate_limit_delay` * attempt instead, since Groww's window is
    seconds to a minute, not milliseconds."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - SDK exceptions vary; classified by is_non_retryable
            if is_non_retryable(e) or i == attempts - 1:
                raise
            sleep(rate_limit_delay * (i + 1) if is_rate_limited(e) else base_delay * (2 ** i))
    raise AssertionError("unreachable")


def _to_epoch(v: Any) -> int:
    if isinstance(v, (int, float)):
        v = int(v)
        return v // 1000 if v > 10_000_000_000 else v
    s = str(v).strip()
    if s.isdigit():
        return _to_epoch(int(s))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"  # 3.9's fromisoformat does not accept a Z suffix
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # Groww returns naive IST wall-clock strings
    return int(dt.timestamp())


def parse_candles(symbol: str, resp: dict | None) -> list[Candle]:
    """Rows are [ts, o, h, l, c, volume, ...]; V2 appends open interest, which is ignored.

    Groww emits pre-open rows (09:00, 09:05) whose prices are null, or partly null: those are
    not tradeable bars and are skipped. A malformed row is still an error."""
    rows = (resp or {}).get("candles") or []
    out = []
    skipped = 0
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            raise ValueError(f"{symbol}: malformed candle row {r!r}")
        if any(r[i] is None for i in (1, 2, 3, 4)):
            skipped += 1
            continue
        o, h, l, c = (float(r[i]) for i in (1, 2, 3, 4))
        if not all(math.isfinite(x) for x in (o, h, l, c)) or not (l <= min(o, c) and max(o, c) <= h):
            raise ValueError(f"{symbol}: bad OHLC in candle row {r!r}")
        out.append(Candle(symbol, _to_epoch(r[0]), o, h, l, c, int(float(r[5] or 0))))
    if skipped:
        logging.getLogger("tradebot.groww").debug("%s: skipped %d candle rows with null prices", symbol, skipped)
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


_BASE32 = re.compile(r"^[A-Z2-7]+=*$")


class GrowwAuthenticationError(RuntimeError):
    """Credential problem detected before or during login. Never retried; aborts the run."""


class GrowwAdapter:
    """Two Groww login flows (spec 2):
    - TOTP flow: GROWW_API_KEY is the TOTP api key and GROWW_TOTP_SECRET a base32 secret; no expiry.
    - Approval flow: GROWW_API_KEY is the JWT api key and GROWW_API_SECRET its secret; the key must be
      approved daily on https://groww.in/trade-api/api-keys.
    """

    def __init__(self, api_key: str, totp_secret: str = "", api_secret: str = ""):
        if not api_key:
            raise GrowwAuthenticationError("GROWW_API_KEY must be set in .env")
        if not totp_secret and not api_secret:
            raise GrowwAuthenticationError("set GROWW_TOTP_SECRET (TOTP flow) or GROWW_API_SECRET (approval flow) in .env")
        if totp_secret and not _BASE32.match(totp_secret.replace(" ", "").upper()):
            raise GrowwAuthenticationError(
                "GROWW_TOTP_SECRET is not a base32 TOTP secret. If Groww gave you an API key and an API "
                "secret (approval flow), put the secret in GROWW_API_SECRET instead.")
        self._api_key = api_key
        self._totp_secret = totp_secret.replace(" ", "").upper() if totp_secret else ""
        self._api_secret = api_secret
        self._client = None

    @property
    def flow(self) -> str:
        return "totp" if self._totp_secret else "approval"

    @property
    def client(self):
        """Authenticate lazily, once. A failed auth raises; never loop on it."""
        if self._client is None:
            from growwapi import GrowwAPI

            try:
                if self._totp_secret:
                    import pyotp
                    token = GrowwAPI.get_access_token(api_key=self._api_key, totp=pyotp.TOTP(self._totp_secret).now())
                else:
                    token = GrowwAPI.get_access_token(api_key=self._api_key, secret=self._api_secret)
            except Exception as e:  # noqa: BLE001 - any login failure is fatal and must read clearly
                hint = (" Approval-flow keys must be approved daily at https://groww.in/trade-api/api-keys."
                        if not self._totp_secret else "")
                raise GrowwAuthenticationError(f"Groww login failed ({self.flow} flow): {type(e).__name__}: {e}.{hint}") from e
            if not token:
                raise GrowwAuthenticationError("Groww login returned no access token")
            self._client = GrowwAPI(token)
        return self._client

    def fetch_candles(self, symbol: str, exchange: str, start_ts: int, end_ts: int, interval: int) -> list[Candle]:
        """Cash-segment candles via the V2 endpoint (get_historical_candle_data is deprecated).

        V2 rows are [iso_timestamp, o, h, l, c, volume, open_interest]; parse_candles reads the
        first six and treats naive timestamps as IST.
        """
        client = self.client  # auth happens here, outside the retry loop, so it is never retried
        interval_name = candle_interval_name(interval)
        gsym = groww_symbol(exchange, symbol)

        def call():
            return client.get_historical_candles(
                exchange=exchange,
                segment=client.SEGMENT_CASH,
                groww_symbol=gsym,
                start_time=_fmt_ist(start_ts),
                end_time=_fmt_ist(end_ts),
                candle_interval=interval_name,
                timeout=30,
            )

        return parse_candles(symbol, with_retry(call))
