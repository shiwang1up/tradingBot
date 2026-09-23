"""Unit tests for scripts/exit_screen.py, loaded from its path like the other script tests."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "exit_screen.py"
_spec = importlib.util.spec_from_file_location("exit_screen", SCRIPT)
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def _bars(closes, highs=None, lows=None, opens=None):
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    opens = opens or list(closes)
    d0 = date(2020, 1, 1)
    return [ex.Bar(d0 + timedelta(days=i), opens[i], highs[i], lows[i], closes[i], 1000)
            for i in range(n)]


def test_hold60_exits_at_the_close_of_the_sixtieth_bar():
    bars = _bars([100.0 + i for i in range(70)])
    out = ex.EXITS["hold60"](bars, 0, atr=5.0, horizon=60)
    assert out.index == 60 and out.price == pytest.approx(bars[60].close)
    assert out.reason == "HOLD"


def test_hold60_equals_the_entry_screens_forward_return():
    """The incumbent must BE the design the entry screen validated. If these ever diverge, this
    screen is not measuring what passed the bar, and the whole comparison is against the wrong
    thing."""
    bars = _bars([100.0 + i for i in range(70)])
    out = ex.EXITS["hold60"](bars, 0, atr=5.0, horizon=60)
    assert out.price / bars[0].close - 1.0 == pytest.approx(bars[60].close / bars[0].close - 1.0)


def test_a_stop_triggers_on_the_first_bar_whose_low_reaches_it():
    """Entry 100, ATR 5, so the 2xATR stop is 90. The low touches 89 on bar 3."""
    bars = _bars([100.0, 99.0, 95.0, 91.0, 99.0], lows=[100.0, 98.0, 94.0, 89.0, 98.0])
    out = ex.EXITS["stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.index == 3 and out.reason == "STOP"
    assert out.price == pytest.approx(90.0), "the stop level, not the bar's low"


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    """Matches check_exit's clamp in execution/backtest.py. Without it a bar that gaps far below
    the stop would book a loss smaller than the one actually taken."""
    bars = _bars([100.0, 80.0], opens=[100.0, 82.0], lows=[100.0, 79.0])
    out = ex.EXITS["stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "STOP" and out.price == pytest.approx(82.0)


def test_a_wider_stop_survives_what_a_narrower_one_does_not():
    """Entry 100, ATR 5: the 2xATR stop is 90 and the 3xATR stop is 85. A dip to 88 takes one."""
    bars = _bars([100.0] + [95.0] * 3 + [110.0] * 60, lows=[100.0, 94.0, 88.0, 94.0] + [109.0] * 60)
    assert ex.EXITS["stop2atr"](bars, 0, atr=5.0, horizon=60).reason == "STOP"
    assert ex.EXITS["stop3atr"](bars, 0, atr=5.0, horizon=60).reason == "HOLD"


def test_the_trail_ratchets_up_and_never_down():
    """Rises to 130, so the 3xATR trail sits at 115 and a fall to 114 exits -- even though the
    entry-based stop at 85 was never threatened."""
    closes = [100.0] + [110.0, 120.0, 130.0] + [114.0] + [130.0] * 60
    bars = _bars(closes, lows=[c * 0.999 for c in closes])
    out = ex.EXITS["trail3atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "TRAIL" and out.index == 4


def test_the_trail_uses_the_highest_close_since_entry_not_the_highest_high():
    """A single spiky high must not ratchet the trail somewhere price never closed."""
    closes = [100.0, 105.0, 104.0] + [104.0] * 60
    bars = _bars(closes, highs=[100.0, 140.0, 104.0] + [104.0] * 60)
    out = ex.EXITS["trail3atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "HOLD", "a 140 high would have trailed to 125 and exited immediately"


def test_a_target_exits_at_the_target_level():
    """Entry 100, ATR 5: risk is 2xATR = 10, so the 2R target is 120.

    Bar 2 closes at 115 (opens default to closes, so it opens below the target too) and spikes
    to a high of 126 -- a plain intrabar touch, not a gap through the target. A close of 125
    here would gap the open above 120 and, per the max(open, target) clamp, fill better than the
    target -- a different scenario, already covered for stops by the dedicated gap test.
    """
    bars = _bars([100.0, 110.0, 115.0] + [100.0] * 60, highs=[100.0, 111.0, 126.0] + [101.0] * 60)
    out = ex.EXITS["target2r_stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "TARGET" and out.index == 2 and out.price == pytest.approx(120.0)


def test_a_stop_and_a_target_on_the_same_bar_resolve_as_a_stop():
    """Matches STOP_FIRST_ON_SAME_BAR in execution/backtest.py, so the screen and the engine cannot
    disagree about an ambiguous bar."""
    bars = _bars([100.0, 105.0], highs=[100.0, 125.0], lows=[100.0, 85.0])
    out = ex.EXITS["target2r_stop2atr"](bars, 0, atr=5.0, horizon=60)
    assert out.reason == "STOP"


def test_an_exit_past_the_end_of_the_series_is_none():
    """A signal too close to the end of the data has no measurable outcome and must not be counted
    as a flat trade."""
    bars = _bars([100.0] * 10)
    assert ex.EXITS["hold60"](bars, 0, atr=5.0, horizon=60) is None


def test_every_cell_in_the_registered_grid_is_present():
    assert set(ex.EXITS) == {"hold60", "stop2atr", "stop3atr", "stop4atr",
                             "trail3atr", "target2r_stop2atr"}
    assert ex.INCUMBENT == "hold60"
    assert ex.BONFERRONI_T == pytest.approx(2.58)


def test_every_cell_agrees_on_when_there_is_no_measurable_outcome():
    """If one cell can resolve a signal another cannot, the comparison stops being paired and the
    per-date difference compares different trades."""
    bars = _bars([100.0] * 10)
    for name, rule in ex.EXITS.items():
        assert rule(bars, 0, atr=5.0, horizon=60) is None, name
