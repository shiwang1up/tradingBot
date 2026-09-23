"""What a round trip costs, and the excess return per month it demands.

Cost scales with turnover, so the holding period sets the bar before any signal is considered: a
10-day hold on a lakh across eight positions demands roughly 1.43% a month, about double the best
documented anomalies, and no signal can clear it. That arithmetic decided the horizons of every
screen in this repository, and until now it lived as hand-worked tables inside spec documents --
which is how one of them came to be computed against a DP fee that belonged to a different broker.
"""
from __future__ import annotations

from typing import Optional

from tradebot.config import ChargesConfig
from tradebot.execution.charges import round_trip_charges

TRADING_DAYS_PER_MONTH = 21.0


def round_trip_fraction(position_value: float, charges: Optional[ChargesConfig],
                        slippage_pct: float = 0.05) -> float:
    """Cost of buying and selling `position_value` of one name, as a FRACTION of that value.

    Charges are taken at the delivery schedule: this is a swing-holding figure. Slippage is charged
    on both sides, against the trade, as a human percent per side.
    """
    if position_value <= 0:
        raise ValueError(f"position_value: expected a value greater than 0, got {position_value!r}")
    if slippage_pct < 0:
        raise ValueError(f"slippage_pct: expected a non-negative percent, got {slippage_pct!r}")
    charged = round_trip_charges(position_value, position_value, charges, product="CNC")
    return charged / position_value + 2 * slippage_pct / 100.0


def hurdle_per_month(fraction: float, hold_days: int) -> float:
    """The excess return per month a strategy must beat, as a fraction, for a hold of `hold_days`
    trading days. One round trip's cost spread over the months it is held."""
    if hold_days <= 0:
        raise ValueError(f"hold_days: expected a positive number of trading days, got {hold_days!r}")
    return fraction / (hold_days / TRADING_DAYS_PER_MONTH)
