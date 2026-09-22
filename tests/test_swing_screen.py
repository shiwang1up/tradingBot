"""Unit tests for scripts/swing_screen.py, loaded from its path like the other script tests."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "swing_screen.py"
_spec = importlib.util.spec_from_file_location("swing_screen", SCRIPT)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

INSAMPLE_TEST = (date(2020, 1, 1), date(2021, 12, 31))


def _bars(closes, highs=None, lows=None, opens=None, vols=None):
    """Daily bars from a close series; the other fields default to something inert."""
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    opens = opens or list(closes)
    vols = vols or [1000] * n
    d0 = date(2020, 1, 1)
    return [sw.Bar(d0 + timedelta(days=i), opens[i], highs[i], lows[i], closes[i], vols[i])
            for i in range(n)]


def test_pullback_needs_both_an_uptrend_and_an_oversold_reading():
    """close > SMA200 AND RSI2 < 10. Either alone must not fire, or the setup is just
    'the market went up', which the baseline already accounts for."""
    up = [100.0 + i for i in range(230)]          # steady rise: above SMA200, RSI2 high
    ind = sw.Indicators(_bars(up))
    assert not sw.pullback(ind, 229)
    dipped = up[:]                                 # three sharp down closes at the end
    for k in (227, 228, 229):
        dipped[k] = up[226] * 0.94
    ind2 = sw.Indicators(_bars(dipped))
    assert sw.pullback(ind2, 229)


def test_breakout_needs_a_new_hundred_day_high():
    closes = [100.0] * 220 + [101.0]
    ind = sw.Indicators(_bars(closes))
    assert sw.breakout(ind, 220)
    assert not sw.breakout(ind, 219)


def test_trend_dip_needs_price_between_sma50_and_sma20():
    """Below SMA20 but still above SMA50, in an established uptrend. Below both is a
    breakdown, not a dip, and must not fire."""
    closes = [100.0 + i * 0.5 for i in range(230)]
    ind = sw.Indicators(_bars(closes))
    i = 229
    assert ind.sma20[i] is not None and ind.sma50[i] is not None
    assert not sw.trend_dip(ind, i)                # riding above SMA20
    dipped = closes[:]
    dipped[229] = (ind.sma20[229] + ind.sma50[229]) / 2.0
    assert sw.trend_dip(sw.Indicators(_bars(dipped)), 229)


def test_squeeze_needs_contraction_then_an_expansion_bar():
    """Quiet YESTERDAY, then a break today. The contraction is measured on the prior bar
    because the expansion bar's own range is what lifts ATR -- measuring both on the same
    bar makes a genuine squeeze-then-break disqualify itself."""
    closes = [100.0 + (i % 7) * 3.0 for i in range(200)] + [100.0] * 30
    bars = _bars(closes)
    ind = sw.Indicators(bars)
    assert not sw.squeeze(ind, 229)                # quiet, but no expansion
    closes2 = closes[:]
    closes2[229] = bars[228].high * 1.02           # break out of the quiet range
    ind2 = sw.Indicators(_bars(closes2))
    assert ind2.atrpct[228] <= ind2.atrpct_min100_prev[228], (
        "the fixture must actually be quiet on bar 228, or this proves nothing")
    assert sw.squeeze(ind2, 229)


def test_gap_vol_needs_a_gap_volume_and_a_green_close():
    n = 230
    closes = [100.0] * n
    opens = [100.0] * n
    vols = [1000] * n
    opens[229] = 102.0                              # gap up 2%
    closes[229] = 103.0                             # closes green
    vols[229] = 3000                                # 3x the 20-day average
    assert sw.gap_vol(sw.Indicators(_bars(closes, opens=opens, vols=vols)), 229)
    red = closes[:]
    red[229] = 101.0                                # still above prior close but below its open
    assert not sw.gap_vol(sw.Indicators(_bars(red, opens=opens, vols=vols)), 229)


def test_three_down_needs_three_lower_closes_inside_an_uptrend():
    closes = [100.0 + i for i in range(227)] + [320.0, 319.0, 318.0]
    ind = sw.Indicators(_bars(closes))
    assert sw.three_down(ind, 229)
    assert not sw.three_down(sw.Indicators(_bars([100.0 + i for i in range(230)])), 229)


def test_every_setup_declines_before_warmup():
    """SMA200 needs 200 bars. A setup that fires on bar 10 is reading a None as a number."""
    ind = sw.Indicators(_bars([100.0 + i for i in range(30)]))
    for name, fn in sw.SETUPS:
        if name == "random":
            continue
        assert not fn(ind, 10), "%s fired before warm-up" % name


def test_forward_return_is_close_to_close_over_the_horizon():
    closes = [100.0] * 220 + [110.0]
    bars = _bars(closes)
    assert sw.forward_return(bars, 200, 20) == pytest.approx(0.10)
    assert sw.forward_return(bars, 210, 20) is None      # exit lands past the end


def test_an_observation_needs_the_symbol_to_be_an_index_member_that_day():
    """The whole point of the point-in-time work. A name-day for a symbol that was not in
    the index that morning is a name-day you could not have traded."""
    members = {date(2021, 3, 1): ("AAA",), date(2021, 3, 2): ("BBB",)}
    assert sw.is_member(members, "AAA", date(2021, 3, 1))
    assert not sw.is_member(members, "AAA", date(2021, 3, 2))
    assert not sw.is_member(members, "AAA", date(2099, 1, 1))


def test_a_masked_date_anywhere_in_the_hold_disqualifies_the_observation():
    """Same inclusive check the daily screen applies to a trade: an unadjusted split inside
    the holding window makes the return fiction, wherever in the window it falls."""
    bars = _bars([100.0] * 230)
    masked = {bars[205].date}
    assert sw.hold_is_clean(bars, 200, 20, set())
    assert not sw.hold_is_clean(bars, 200, 20, masked)
    assert sw.hold_is_clean(bars, 100, 20, masked)        # window ends before the mask


def test_a_masked_entry_or_exit_date_also_disqualifies():
    bars = _bars([100.0] * 230)
    assert not sw.hold_is_clean(bars, 200, 20, {bars[200].date})
    assert not sw.hold_is_clean(bars, 200, 20, {bars[220].date})


def test_observations_carry_symbol_date_horizon_and_return():
    bars = _bars([100.0 + i for i in range(230)])
    ind = sw.Indicators(bars)
    members = {b.date: ("AAA",) for b in bars}
    obs = sw.observations("AAA", bars, ind, set(), members, INSAMPLE_TEST, ("breakout", sw.breakout), (20,))
    assert obs, "the fixture rises monotonically, so breakout must fire somewhere"
    o = obs[0]
    assert o.symbol == "AAA" and o.horizon == 20
    assert o.entry_date in [b.date for b in bars]
    assert isinstance(o.ret, float)


def test_time_series_baseline_is_the_mean_hold_in_that_symbol():
    """Every eligible entry in the window, held h days. A steady 1%-a-bar riser held 2 bars
    returns 2.01% from any entry, so the mean is that."""
    closes = [100.0 * (1.01 ** i) for i in range(230)]
    bars = _bars(closes)
    b = sw.ts_baseline(bars, (bars[200].date, bars[229].date), 2, set())
    assert b == pytest.approx(1.01 ** 2 - 1.0)


def test_time_series_baseline_skips_masked_windows():
    closes = [100.0 * (1.01 ** i) for i in range(230)]
    bars = _bars(closes)
    clean = sw.ts_baseline(bars, (bars[200].date, bars[229].date), 2, set())
    masked = sw.ts_baseline(bars, (bars[200].date, bars[229].date), 2, {bars[210].date})
    assert clean == pytest.approx(masked)     # same value, but computed over fewer holds
    assert sw.ts_baseline(bars, (bars[200].date, bars[202].date), 2, {bars[201].date}) is None


def test_cross_sectional_baseline_is_the_mean_over_that_days_members():
    """Two members on the date, one returning 10% and one 0%, gives 5%."""
    rets = {("AAA", date(2021, 3, 1), 20): 0.10, ("BBB", date(2021, 3, 1), 20): 0.0}
    members = {date(2021, 3, 1): ("AAA", "BBB")}
    assert sw.xs_baseline(rets, members, date(2021, 3, 1), 20) == pytest.approx(0.05)


def test_cross_sectional_baseline_covers_only_that_days_members():
    """A symbol not in the index that day must not enter the comparison, or the baseline is
    computed over a universe you could not have chosen from."""
    rets = {("AAA", date(2021, 3, 1), 20): 0.10, ("ZZZ", date(2021, 3, 1), 20): 1.00}
    members = {date(2021, 3, 1): ("AAA",)}
    assert sw.xs_baseline(rets, members, date(2021, 3, 1), 20) == pytest.approx(0.10)


def test_cross_sectional_baseline_is_none_without_members():
    assert sw.xs_baseline({}, {}, date(2021, 3, 1), 20) is None


def test_t_is_computed_across_dates_not_observations():
    """Forty names firing on one dip is ONE observation, not forty. Counting them
    separately inflates t by about the square root of the cluster size, which is how the
    last screen produced a positive mean beside a negative t."""
    one_day = [sw.Ex("S%d" % k, date(2021, 3, 1), 60, 0.05, 0.05) for k in range(40)]
    two_more = [sw.Ex("A", date(2021, 4, 1), 60, -0.05, -0.05),
                sw.Ex("A", date(2021, 5, 1), 60, -0.04, -0.04)]
    t = sw.t_across_dates(one_day + two_more)
    assert t is not None and t < 1.0, "clustered day must not dominate"
    assert sw.t_across_dates(one_day) is None        # a single date has no variance


def test_date_means_average_within_a_date_first():
    exs = [sw.Ex("A", date(2021, 3, 1), 60, 0.10, 0.10),
           sw.Ex("B", date(2021, 3, 1), 60, 0.00, 0.00),
           sw.Ex("C", date(2021, 4, 1), 60, 0.04, 0.04)]
    assert sw.date_means(exs) == pytest.approx([0.05, 0.04])


def test_the_hurdle_scales_with_turnover():
    """Cost per year is the round trip times the rotations per year, so a longer hold is a
    lower bar. This is the single biggest lever in the design."""
    h20 = sw.hurdle_per_month(20)
    h60 = sw.hurdle_per_month(60)
    h120 = sw.hurdle_per_month(120)
    assert h20 > h60 > h120
    assert h60 == pytest.approx(0.00238, abs=2e-4)


def test_the_hurdle_is_per_month_not_per_trade():
    """A 60-day hold turns over about 4.2 times a year, so its annual cost spread over
    twelve months is much smaller than one round trip."""
    assert sw.hurdle_per_month(60) < sw.round_trip_cost(sw.CAPITAL / sw.POSITIONS)


def test_summarise_reports_both_excesses_and_their_ts():
    exs = [sw.Ex("A", date(2021, 3, 1), 60, 0.02, 0.01),
           sw.Ex("B", date(2021, 4, 1), 60, -0.01, 0.03),
           sw.Ex("C", date(2021, 5, 1), 60, 0.03, -0.02)]
    s = sw.summarise(exs, 60)
    assert s["n"] == 3 and s["dates"] == 3
    assert s["ts_excess"] == pytest.approx((0.02 - 0.01 + 0.03) / 3)
    assert s["xs_excess"] == pytest.approx((0.01 + 0.03 - 0.02) / 3)
    assert s["xs_t"] is not None


def test_summarise_of_nothing_does_not_divide_by_zero():
    s = sw.summarise([], 60)
    assert s["n"] == 0 and s["xs_t"] is None


def test_the_verdict_requires_both_a_positive_excess_and_the_t_bar():
    """Pre-registered: excess > 0 AND t >= 2.64. Either alone is not a pass, and a pass is
    separately reported as tradable only if it also clears the turnover hurdle."""
    assert sw.verdict(0.01, 3.0, 60) == "PASS"
    assert sw.verdict(-0.01, 3.0, 60) == "fail"        # negative excess
    assert sw.verdict(0.01, 1.0, 60) == "fail"         # under the t bar
    assert sw.verdict(0.0001, 3.0, 60) == "PASS but below the cost hurdle"
