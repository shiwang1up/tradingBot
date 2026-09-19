"""Statutory and brokerage charges on one closed intraday trade. Pure: no I/O, no state.

Values are taken by side (what was bought, what was sold), not by entry and exit, because STT is
charged on the sell side and stamp duty on the buy side: a short sells first and buys second."""
from __future__ import annotations

import math
from typing import Optional

from tradebot.config import ChargesConfig


def round_trip_charges(buy_value: float, sell_value: float, cfg: Optional[ChargesConfig]) -> float:
    """Rupees charged on a trade that bought `buy_value` and sold `sell_value`. Zero when `cfg` is
    None or disabled, and neither value is validated in that case: a disabled schedule never raises.

    Otherwise both values must be finite and non-negative (zero is allowed) -- a NaN, an infinity or
    a negative number is a caller bug, not a free or undercharged trade, so it raises rather than
    silently producing a wrong or NaN total. The total is rounded once to paise at the end; the
    individual components (brokerage, STT, exchange transaction charge, SEBI fee, stamp duty, GST)
    are not rounded on their own. Every order pays at least `brokerage_min`, including one worth
    zero rupees.

    `position_charges` below always validates its arguments regardless of `cfg`; this function
    validates only when the schedule is enabled.
    """
    if cfg is None or not cfg.enabled:
        return 0.0

    for name, value in (("buy_value", buy_value), ("sell_value", sell_value)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name}: expected a finite, non-negative order value, got {value!r}")

    def brokerage(order_value: float) -> float:
        return min(max(order_value * cfg.brokerage_pct / 100.0, cfg.brokerage_min), cfg.brokerage_max)

    turnover = buy_value + sell_value
    brok = brokerage(buy_value) + brokerage(sell_value)
    stt = sell_value * cfg.stt_sell_pct / 100.0
    txn = turnover * cfg.exchange_txn_pct / 100.0
    sebi = turnover * cfg.sebi_pct / 100.0
    stamp = buy_value * cfg.stamp_buy_pct / 100.0
    gst = (brok + txn + sebi) * cfg.gst_pct / 100.0
    return round(brok + stt + txn + sebi + stamp + gst, 2)


def position_charges(direction: str, entry_price: float, exit_price: float, quantity: int,
                      cfg: Optional[ChargesConfig]) -> float:
    """Charges on one closed position. A LONG buys at entry and sells at exit; a SHORT sells at
    entry and buys at exit.

    quantity, entry_price and exit_price are validated here regardless of whether the fee
    schedule is enabled, like the direction check below: a caller bug must not hide behind a
    disabled or free schedule.
    """
    if quantity <= 0:
        raise ValueError(f"quantity: expected a positive integer, got {quantity!r}")
    for name, value in (("entry_price", entry_price), ("exit_price", exit_price)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name}: expected a finite number greater than 0, got {value!r}")
    entry_value = entry_price * quantity
    exit_value = exit_price * quantity
    if direction == "LONG":
        buy_value, sell_value = entry_value, exit_value
    elif direction == "SHORT":
        buy_value, sell_value = exit_value, entry_value
    else:
        raise ValueError(f"direction: expected LONG or SHORT, got {direction!r}")
    return round_trip_charges(buy_value, sell_value, cfg)
