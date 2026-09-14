from __future__ import annotations

import math

_EPS = 1e-9  # absorbs float error on tick-aligned distances so exact integers are not floored down


def compute_quantity(capital: float, per_trade_pct: float, entry: float, stop: float,
                     available_margin: float, lot_size: int) -> int:
    """min(risk-based size, margin-based size), rounded down to lot size. 0 if below one lot."""
    dist = abs(entry - stop)
    if not all(math.isfinite(x) for x in (dist, entry, available_margin, capital)):
        return 0
    if dist <= 0 or entry <= 0 or lot_size <= 0 or available_margin <= 0:
        return 0
    risk_qty = math.floor(capital * per_trade_pct / 100.0 / dist + _EPS)
    margin_qty = math.floor(available_margin / entry + _EPS)
    qty = min(risk_qty, margin_qty)
    qty -= qty % lot_size
    return max(qty, 0)
