import pytest

from tradebot.config import ChargesConfig
from tradebot.execution.charges import round_trip_charges

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
    v = round_trip_charges(12_345.67, 12_400.10, CFG)
    assert v == round(v, 2) and v > 0
