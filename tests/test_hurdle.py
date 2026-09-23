import pytest

from tradebot.brokers import load_brokers
from tradebot.report.hurdle import hurdle_per_month, round_trip_fraction

GROWW = load_brokers("brokers.yaml")["groww"].charges
ZERODHA = load_brokers("brokers.yaml")["zerodha"].charges


def test_round_trip_fraction_is_charges_plus_slippage_on_both_sides():
    """80.88 of charges on a 12,500 position is 0.647%, plus 0.05% of slippage each side."""
    assert round_trip_fraction(12_500.0, GROWW, slippage_pct=0.05) == pytest.approx(
        0.0064704 + 0.001, rel=1e-3)


def test_a_bigger_position_costs_proportionally_less():
    """Brokerage caps at 20 a leg and the DP fee is flat, so the fraction falls as size rises.
    This is the entire reason concentration changes whether a strategy pays for itself."""
    small = round_trip_fraction(12_500.0, GROWW, slippage_pct=0.05)
    large = round_trip_fraction(50_000.0, GROWW, slippage_pct=0.05)
    assert large < small


def test_zerodha_costs_less_than_groww_at_every_size():
    for v in (12_500.0, 25_000.0, 100_000.0):
        assert (round_trip_fraction(v, ZERODHA, slippage_pct=0.05)
                < round_trip_fraction(v, GROWW, slippage_pct=0.05))


def test_hurdle_per_month_divides_the_round_trip_over_the_hold():
    """A 60-trading-day hold is 60/21 months, so a 0.681% round trip is 0.238%/mo -- the figure
    the swing screen set its bar against."""
    assert hurdle_per_month(0.00681, hold_days=60) == pytest.approx(0.002384, rel=1e-3)


def test_a_longer_hold_lowers_the_hurdle_proportionally():
    assert hurdle_per_month(0.00681, hold_days=120) == pytest.approx(
        hurdle_per_month(0.00681, hold_days=60) / 2, rel=1e-6)


def test_the_groww_eight_slot_hurdle_is_above_the_measured_edge():
    """The finding this whole plan rests on: trend_dip's 0.216%/mo does not clear Groww at eight
    positions on a lakh, and does clear zerodha there."""
    groww = hurdle_per_month(round_trip_fraction(12_500.0, GROWW, 0.05), 60)
    zerodha = hurdle_per_month(round_trip_fraction(12_500.0, ZERODHA, 0.05), 60)
    assert groww > 0.00216 > zerodha


def test_a_zero_or_negative_hold_raises():
    with pytest.raises(ValueError):
        hurdle_per_month(0.00681, hold_days=0)


def test_a_zero_or_negative_position_raises():
    with pytest.raises(ValueError):
        round_trip_fraction(0.0, GROWW, slippage_pct=0.05)
