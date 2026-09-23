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


def _dip_then_flat_bars():
    """A steady uptrend that dips to sit between SMA20 and SMA50 at bar 229 -- the same fixture
    shape trend_dip's own unit test in test_swing_screen.py uses -- then keeps rising calmly for
    exactly 60 more bars, so hold60's horizon lands on the series' last bar and every cell falls
    through to a real exit with nothing to drop. The complement of the fixture in
    test_an_observation_any_cell_cannot_resolve_is_dropped_from_all_of_them below, where the
    series ends BEFORE the horizon instead."""
    closes = [100.0 + i * 0.5 for i in range(230)]
    probe = ex.Indicators(_bars(closes))
    dipped = closes[:]
    dipped[229] = (probe.sma20[229] + probe.sma50[229]) / 2.0
    tail = [dipped[229] + 0.5 * k for k in range(1, 61)]
    return _bars(dipped + tail)


def test_every_cell_in_the_registered_grid_is_present():
    assert set(ex.EXITS) == {"hold60", "stop2atr", "stop3atr", "stop4atr",
                             "trail3atr", "target2r_stop2atr"}
    assert ex.INCUMBENT == "hold60"
    assert ex.BONFERRONI_T == pytest.approx(2.58)


def test_every_cell_agrees_when_nothing_triggers_before_the_end_of_the_data():
    """On flat bars nothing triggers early, so every rule falls through to the same
    end-of-series None. This is NOT the general invariant -- the rules disagree with each other
    once something triggers early enough that some can resolve and others cannot; that case is
    exercised in test_an_observation_any_cell_cannot_resolve_is_dropped_from_all_of_them below.
    The general invariant (an unresolvable name-day is dropped from every cell) lives in
    observations(), not here."""
    bars = _bars([100.0] * 10)
    for name, rule in ex.EXITS.items():
        assert rule(bars, 0, atr=5.0, horizon=60) is None, name


def test_every_cell_reports_the_same_entries():
    """The pairing IS the method. If cells ever see different entries, the per-date difference is
    comparing different trades and the t is meaningless."""
    bars = _dip_then_flat_bars()
    ind = ex.Indicators(bars)
    uni = ex.swing_screen.Universe(
        bars={"AAA": bars, "BBB": bars}, masked={"AAA": set(), "BBB": set()},
        inds={"AAA": ind, "BBB": ind}, members_by_date={b.date: ("AAA", "BBB") for b in bars},
        all_dates=[b.date for b in bars], returns={})
    window = (bars[0].date, bars[-1].date)

    obs = ex.observations(uni, window)
    assert obs, "the fixture must produce at least one observation, or this proves nothing"

    entries_by_cell = {}
    for o in obs:
        entries_by_cell.setdefault(o.cell, set()).add((o.symbol, o.entry_date))
    assert set(entries_by_cell) == set(ex.EXITS), "every registered cell must report something"
    common = next(iter(entries_by_cell.values()))
    for cell, entries in entries_by_cell.items():
        assert entries == common, "%s saw a different set of entries than the rest" % cell
    assert common == {("AAA", bars[229].date), ("BBB", bars[229].date)}


def test_an_observation_any_cell_cannot_resolve_is_dropped_from_all_of_them():
    """The rules disagree near the end of a series: a stop can trigger where the 60-bar hold has
    no outcome. Keeping those would compare a stopped trade against nothing."""
    closes = [100.0 + i * 0.5 for i in range(230)]
    probe = ex.Indicators(_bars(closes))
    dipped = closes[:]
    dipped[229] = (probe.sma20[229] + probe.sma50[229]) / 2.0
    prefix = _bars(dipped)
    ind = ex.Indicators(prefix)
    assert ex.trend_dip(ind, 229), "fixture must fire trend_dip, or this proves nothing"
    atr = ind.atr20[229]
    entry_close = prefix[229].close
    # Between the 3x and 4x ATR stop levels: breaches stop2atr/stop3atr/trail3atr/target's stop,
    # but not stop4atr -- and the series ends right there, one bar later, well short of bar 60.
    crash_low = entry_close - 3.5 * atr
    crash = ex.Bar(prefix[-1].date + timedelta(days=1), entry_close, entry_close * 1.001,
                   crash_low, crash_low, 1000)
    bars = prefix + [crash]

    # The fixture must actually reproduce the rules' disagreement, or the rest proves nothing.
    outcomes = {name: rule(bars, 229, atr=atr, horizon=60) for name, rule in ex.EXITS.items()}
    assert outcomes["hold60"] is None and outcomes["stop4atr"] is None, (
        "these two must have no measurable outcome this close to the end of the series")
    assert outcomes["stop2atr"] is not None and outcomes["stop3atr"] is not None
    assert outcomes["trail3atr"] is not None and outcomes["target2r_stop2atr"] is not None

    ind_full = ex.Indicators(bars)
    uni = ex.swing_screen.Universe(
        bars={"AAA": bars}, masked={"AAA": set()}, inds={"AAA": ind_full},
        members_by_date={b.date: ("AAA",) for b in bars}, all_dates=[b.date for b in bars],
        returns={})
    window = (bars[0].date, bars[-1].date)
    assert ex.observations(uni, window) == [], (
        "four of six cells could resolve this name-day; keeping it anywhere unpairs the cells")


def test_a_signal_whose_hold_spans_a_corporate_action_is_dropped_from_every_cell():
    """An unadjusted split makes the return fiction wherever it lands. Dropping it from one cell
    and not another would silently unpair them."""
    bars = _dip_then_flat_bars()
    ind = ex.Indicators(bars)
    entry_date = bars[229].date
    masked_date = bars[229 + 30].date       # well inside the hold, entry (229) to horizon (289)
    uni = ex.swing_screen.Universe(
        bars={"AAA": bars, "BBB": bars}, masked={"AAA": {masked_date}, "BBB": set()},
        inds={"AAA": ind, "BBB": ind}, members_by_date={b.date: ("AAA", "BBB") for b in bars},
        all_dates=[b.date for b in bars], returns={})
    window = (bars[0].date, bars[-1].date)

    obs = ex.observations(uni, window)
    aaa_cells = {o.cell for o in obs if o.symbol == "AAA" and o.entry_date == entry_date}
    bbb_cells = {o.cell for o in obs if o.symbol == "BBB" and o.entry_date == entry_date}
    assert aaa_cells == set(), "the masked symbol's entry must be gone from EVERY cell, not some"
    assert bbb_cells == set(ex.EXITS), "an unrelated, unmasked symbol's entry must be unaffected"


def test_returns_are_net_of_costs():
    """The screen's figures must be comparable with the entry screen's, which charges the same."""
    bars = _dip_then_flat_bars()
    ind = ex.Indicators(bars)
    uni = ex.swing_screen.Universe(
        bars={"AAA": bars}, masked={"AAA": set()}, inds={"AAA": ind},
        members_by_date={b.date: ("AAA",) for b in bars}, all_dates=[b.date for b in bars],
        returns={})
    window = (bars[0].date, bars[-1].date)

    obs = ex.observations(uni, window)
    hold = next(o for o in obs if o.cell == "hold60")
    raw_ratio = bars[229 + 60].close / bars[229].close - 1.0
    cost = ex.round_trip_cost()
    assert cost > 0.0, "the fixture proves nothing if the cost model itself charges nothing"
    assert hold.ret == pytest.approx(raw_ratio - cost)
