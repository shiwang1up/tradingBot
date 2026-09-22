from dataclasses import replace

import pytest

from tradebot.config import ChargesConfig
from tradebot.execution.charges import position_charges, round_trip_charges

CFG = ChargesConfig()


def test_long_round_trip_by_hand():
    # buy 100 x 100.00 = 10,000; sell 100 x 102.00 = 10,200
    # brokerage 10.00 + 10.20 = 20.20; STT 10,200 x 0.025% = 2.55; txn 20,200 x 0.00297% = 0.59994
    # SEBI 20,200 x 0.0001% = 0.0202; stamp 10,000 x 0.003% = 0.30
    # GST 18% x (20.20 + 0.59994 + 0.0202) = 3.7476252; total 27.4177652
    assert round_trip_charges(10_000.0, 10_200.0, CFG) == pytest.approx(27.42)


def test_short_round_trip_takes_values_by_side_not_by_order():
    # short 100 @ 100.00 (sell 10,000), cover @ 98.00 (buy 9,800)
    # brokerage 9.80 + 10.00 = 19.80; STT 2.50; txn 0.58806; SEBI 0.0198; stamp 0.294
    # GST 18% x 20.40786 = 3.6734148; total 26.8752748
    assert round_trip_charges(9_800.0, 10_000.0, CFG) == pytest.approx(26.88)


def test_brokerage_is_capped_per_order():
    # 65,000 each side: 0.1% would be 65 per order, capped at 20 -> 40
    # STT 16.25; txn 3.861; SEBI 0.13; stamp 1.95; GST 18% x 43.991 = 7.91838; total 70.10938
    assert round_trip_charges(65_000.0, 65_000.0, CFG) == pytest.approx(70.11)


def test_brokerage_is_floored_per_order():
    # 1,000 each side: 0.1% would be 1 per order, floored at 5 -> 10
    # STT 0.25; txn 0.0594; SEBI 0.002; stamp 0.03; GST 18% x 10.0614 = 1.811052; total 12.152452
    assert round_trip_charges(1_000.0, 1_000.0, CFG) == pytest.approx(12.15)


def test_disabled_or_absent_config_costs_nothing():
    assert round_trip_charges(10_000.0, 10_200.0, ChargesConfig(enabled=False)) == 0.0
    assert round_trip_charges(10_000.0, 10_200.0, None) == 0.0


def test_result_is_rounded_to_paise():
    # buy 12,345.67 x 0.1% = 12.34567; sell 12,400.10 x 0.1% = 12.40010; brok 24.74577
    # STT 12,400.10 x 0.025% = 3.100025; turnover 24,745.77
    # txn 24,745.77 x 0.00297% = 0.734949369; SEBI 24,745.77 x 0.0001% = 0.02474577
    # stamp 12,345.67 x 0.003% = 0.3703701
    # GST 18% x (24.74577 + 0.734949369 + 0.02474577) = 4.590983725
    # total 24.74577 + 3.100025 + 0.734949369 + 0.02474577 + 0.3703701 + 4.590983725 = 33.566843964
    assert round_trip_charges(12_345.67, 12_400.10, CFG) == pytest.approx(33.57)


def test_brokerage_floor_and_cap_can_land_on_different_legs():
    # buy 1,000: 0.1% = 1.00, floored to 5.00; sell 65,000: 0.1% = 65.00, capped to 20.00; brok 25.00
    # STT 65,000 x 0.025% = 16.25; turnover 66,000; txn 66,000 x 0.00297% = 1.9602
    # SEBI 66,000 x 0.0001% = 0.066; stamp 1,000 x 0.003% = 0.03
    # GST 18% x (25.00 + 1.9602 + 0.066) = 4.864716
    # total 25.00 + 16.25 + 1.9602 + 0.066 + 0.03 + 4.864716 = 48.170916
    assert round_trip_charges(1_000.0, 65_000.0, CFG) == pytest.approx(48.17)


@pytest.mark.parametrize("buy,sell", [
    (float("nan"), 10_200.0),
    (10_000.0, float("nan")),
    (float("inf"), 10_200.0),
    (10_000.0, float("inf")),
    (-10_000.0, 10_200.0),
    (10_000.0, -10_200.0),
])
def test_non_finite_or_negative_values_raise(buy, sell):
    with pytest.raises(ValueError):
        round_trip_charges(buy, sell, CFG)


def test_disabled_or_none_config_never_validates():
    # a disabled schedule must not raise even on garbage values
    assert round_trip_charges(float("nan"), float("-inf"), ChargesConfig(enabled=False)) == 0.0
    assert round_trip_charges(float("nan"), float("-inf"), None) == 0.0


def test_position_charges_long_buys_at_entry_and_sells_at_exit():
    # LONG 100 @ 100.00 -> 102.00: buy 10,000, sell 10,200, same as test_long_round_trip_by_hand
    assert position_charges("LONG", 100.0, 102.0, 100, CFG) == pytest.approx(27.42)


def test_position_charges_short_sells_at_entry_and_buys_at_exit():
    # SHORT 100 @ 100.00 -> 98.00: sell 10,000, buy 9,800, same as the short round-trip test
    assert position_charges("SHORT", 100.0, 98.0, 100, CFG) == pytest.approx(26.88)


def test_position_charges_rejects_unknown_direction():
    with pytest.raises(ValueError):
        position_charges("FLAT", 100.0, 102.0, 100, CFG)


def test_position_charges_with_no_config_is_free():
    assert position_charges("LONG", 100.0, 102.0, 100, None) == 0.0


@pytest.mark.parametrize("quantity,entry_price,exit_price,match", [
    (0, 100.0, 102.0, "quantity"),
    (-5, 100.0, 102.0, "quantity"),
    (100, 0.0, 102.0, "entry_price"),
    (100, 100.0, float("nan"), "exit_price"),
])
def test_position_charges_validates_quantity_and_prices(quantity, entry_price, exit_price, match):
    # these checks run whether or not the schedule is enabled, like the direction check above
    with pytest.raises(ValueError, match=match):
        position_charges("LONG", entry_price, exit_price, quantity, CFG)


def test_delivery_charges_more_than_intraday_on_the_same_trade():
    """The point of the whole task: CNC pays STT on both sides plus a DP charge, so a delivery
    round trip must cost materially more than the same trade intraday. If these ever come out
    equal, the product argument is not reaching the schedule."""
    mis = round_trip_charges(10_000.0, 10_200.0, CFG, product="MIS")
    cnc = round_trip_charges(10_000.0, 10_200.0, CFG, product="CNC")
    assert cnc > mis
    assert cnc - mis > 25.0          # STT alone is ~0.175% of 20,200 = ~35


def test_the_default_product_is_intraday_and_unchanged():
    """Every existing caller omits the argument and must get exactly what it got before."""
    assert round_trip_charges(10_000.0, 10_200.0, CFG) == round_trip_charges(
        10_000.0, 10_200.0, CFG, product="MIS")
    assert round_trip_charges(10_000.0, 10_200.0, CFG) == pytest.approx(27.42)


def test_raising_the_buy_leg_costs_more_under_delivery_than_intraday():
    """Two things make a delivery BUY dearer: STT is charged on both sides rather than the
    sell alone, and stamp duty is 0.015% rather than 0.003%. So the differential is the sum of
    both, not STT alone -- an earlier version of this test counted only the STT and contradicted
    the DP-charge test two below it, which does compute the stamp term."""
    base_mis = round_trip_charges(10_000.0, 10_000.0, CFG, product="MIS")
    more_mis = round_trip_charges(20_000.0, 10_000.0, CFG, product="MIS")
    base_cnc = round_trip_charges(10_000.0, 10_000.0, CFG, product="CNC")
    more_cnc = round_trip_charges(20_000.0, 10_000.0, CFG, product="CNC")
    extra_stt = 10_000.0 * CFG.delivery_stt_pct / 100.0
    extra_stamp = 10_000.0 * (CFG.delivery_stamp_buy_pct - CFG.stamp_buy_pct) / 100.0
    assert (more_cnc - base_cnc) - (more_mis - base_mis) == pytest.approx(
        extra_stt + extra_stamp, rel=1e-6)


def test_the_dp_charge_is_flat_and_applied_once():
    """A fixed rupee fee per sell, so it does not scale with value and appears exactly once in
    a round trip."""
    small = round_trip_charges(1_000.0, 1_000.0, CFG, product="CNC")
    small_mis = round_trip_charges(1_000.0, 1_000.0, CFG, product="MIS")
    stt_delta = 2_000.0 * CFG.delivery_stt_pct / 100.0 - 1_000.0 * CFG.stt_sell_pct / 100.0
    stamp_delta = 1_000.0 * (CFG.delivery_stamp_buy_pct - CFG.stamp_buy_pct) / 100.0
    assert small - small_mis == pytest.approx(stt_delta + stamp_delta + CFG.dp_charge, abs=0.02)


def test_a_disabled_schedule_is_free_for_delivery_too():
    disabled = replace(CFG, enabled=False)
    assert round_trip_charges(10_000.0, 10_200.0, disabled, product="CNC") == 0.0


def test_an_unknown_product_raises():
    """Silently falling back to the cheaper schedule is the failure mode that matters."""
    with pytest.raises(ValueError) as e:
        round_trip_charges(10_000.0, 10_200.0, CFG, product="NRML")
    assert "NRML" in str(e.value)
