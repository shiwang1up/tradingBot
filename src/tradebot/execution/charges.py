"""Statutory and brokerage charges on one closed intraday trade. Pure: no I/O, no state.

Values are taken by side (what was bought, what was sold), not by entry and exit, because STT is
charged on the sell side and stamp duty on the buy side: a short sells first and buys second."""
from __future__ import annotations

from typing import Optional

from tradebot.config import ChargesConfig


def round_trip_charges(buy_value: float, sell_value: float, cfg: Optional[ChargesConfig]) -> float:
    """Rupees charged on a trade that bought `buy_value` and sold `sell_value`, rounded to paise.
    Zero when `cfg` is None or disabled."""
    if cfg is None or not cfg.enabled:
        return 0.0

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
