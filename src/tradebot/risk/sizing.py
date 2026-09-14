from __future__ import annotations

import math


def compute_quantity(capital: float, per_trade_pct: float, entry: float, stop: float,
                     available_margin: float, lot_size: int) -> int:
    """min(risk-based size, margin-based size), rounded down to lot size. 0 if below one lot."""
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0 or lot_size <= 0:
        return 0
    risk_qty = math.floor(capital * per_trade_pct / 100.0 / dist)
    margin_qty = math.floor(available_margin / entry)
    qty = min(risk_qty, margin_qty)
    qty -= qty % lot_size
    return max(qty, 0)
