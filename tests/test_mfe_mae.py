"""Unit tests for scripts/mfe_mae.py, loaded from its path like the other script tests."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mfe_mae.py"
_spec = importlib.util.spec_from_file_location("mfe_mae", SCRIPT)
mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mm)


def _bars(rows):
    """rows: (high, low) per bar; open and close are set to the midpoint, which these
    functions never read."""
    return [mm.Bar(ts=i, open=(h + l) / 2, high=h, low=l, close=(h + l) / 2)
            for i, (h, l) in enumerate(rows)]


def test_excursions_measure_a_long_in_units_of_risk():
    """Entry 100, stop 90, so R is 10. A high of 115 is +1.5R and a low of 95 is -0.5R."""
    bars = _bars([(105, 98), (115, 95)])
    mfe, mae = mm.excursions(bars, "LONG", entry=100.0, risk=10.0)
    assert mfe == pytest.approx(1.5)
    assert mae == pytest.approx(-0.5)


def test_excursions_invert_for_a_short():
    """For a short, favourable is DOWN. Entry 100, R 10: a low of 85 is +1.5R in our favour
    and a high of 105 is -0.5R against. Getting this backwards would flip every short."""
    bars = _bars([(102, 95), (105, 85)])
    mfe, mae = mm.excursions(bars, "SHORT", entry=100.0, risk=10.0)
    assert mfe == pytest.approx(1.5)
    assert mae == pytest.approx(-0.5)


def test_a_target_reached_before_the_stop_pays_the_target():
    bars = _bars([(112, 99), (100, 88)])          # +1.2R first, only then down through the stop
    r, why = mm.replay_target(bars, "LONG", entry=100.0, risk=10.0, target_r=1.0)
    assert (r, why) == (1.0, "TARGET")


def test_a_stop_reached_before_the_target_pays_minus_one():
    bars = _bars([(103, 89), (120, 100)])         # stopped on bar 1, so bar 2 never happens
    r, why = mm.replay_target(bars, "LONG", entry=100.0, risk=10.0, target_r=1.0)
    assert (r, why) == (-1.0, "STOP")


def test_a_bar_touching_both_is_resolved_as_the_stop():
    """OHLC cannot order the two within one bar. Assuming the target came first would invent
    profit, so the stop wins -- every target figure is then pessimistic, never flattering."""
    bars = _bars([(115, 85)])                     # reaches +1.5R and -1.5R in the same bar
    r, why = mm.replay_target(bars, "LONG", entry=100.0, risk=10.0, target_r=1.0)
    assert (r, why) == (-1.0, "STOP")


def test_never_reaching_either_exits_at_the_last_close():
    bars = [mm.Bar(ts=0, open=100, high=104, low=97, close=103)]
    r, why = mm.replay_target(bars, "LONG", entry=100.0, risk=10.0, target_r=1.0)
    assert why == "SQUARE_OFF"
    assert r == pytest.approx(0.3)


def test_a_short_stop_is_above_the_entry():
    """A short's stop is breached by a rising price. Reusing the long comparison would make
    shorts unstoppable, which is the sort of error a PnL table would hide."""
    bars = _bars([(111, 100)])                    # +1.1R against a short entered at 100
    r, why = mm.replay_target(bars, "SHORT", entry=100.0, risk=10.0, target_r=1.0)
    assert (r, why) == (-1.0, "STOP")
