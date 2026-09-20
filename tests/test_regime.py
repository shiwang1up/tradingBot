import pytest

from tradebot.risk.regime import DOWN, NOT_READY, UP, RegimeFilter


def test_not_ready_until_the_ema_is_warm_and_everything_is_held_back():
    f = RegimeFilter(3)
    assert f.state == NOT_READY
    assert f.update(10.0) == NOT_READY and f.update(11.0) == NOT_READY
    assert f.rejection("LONG") == "regime_not_ready" and f.rejection("SHORT") == "regime_not_ready"


def test_up_above_the_ema_and_down_at_or_below_it():
    f = RegimeFilter(3)
    for x in (10.0, 11.0):
        f.update(x)
    assert f.update(12.0) == UP          # EMA seeds at the mean 11.0; 12 is above
    assert f.rejection("LONG") is None and f.rejection("SHORT") == "regime"
    assert f.update(9.0) == DOWN         # EMA moves to 10.0; 9 is below
    assert f.rejection("SHORT") is None and f.rejection("LONG") == "regime"


def test_a_close_equal_to_the_ema_counts_as_down():
    f = RegimeFilter(3)
    for x in (10.0, 10.0, 10.0):
        f.update(x)
    assert f.state == DOWN


def test_bad_input_is_loud():
    with pytest.raises(ValueError):
        RegimeFilter(0)
    with pytest.raises(ValueError):
        RegimeFilter(3).update(float("nan"))


def test_rejection_rejects_an_unknown_direction_loudly():
    with pytest.raises(ValueError):
        RegimeFilter(3).rejection("BUY")
