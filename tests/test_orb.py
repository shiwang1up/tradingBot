from datetime import date

import pytest

from tradebot.engine.clock import ist_epoch
from tradebot.strategy.ema_rsi import build_strategy
from tradebot.strategy.orb import OrbStrategy
from tradebot.types import Candle

PARAMS = dict(range_minutes=30, reward_risk=2.0, min_range_pct=0.4, max_range_pct=1.5, product="MIS",
              session_open="09:15", interval_minutes=15)
D1, D2 = date(2026, 9, 14), date(2026, 9, 15)
# Two range bars: high 100.8, low 99.8, width 1.0 on a 100.3 midpoint = 0.997%; mean volume 2000.
RANGE = [(100.0, 100.6, 99.8, 100.4, 1000), (100.4, 100.8, 100.0, 100.2, 3000)]
INSIDE = (100.2, 100.7, 100.1, 100.5, 500)
UP = (100.5, 101.3, 100.4, 101.2, 5000)      # closes above 100.8
DOWN = (100.0, 100.1, 99.2, 99.4, 4000)      # closes below 99.8


def bars(rows, day=D1, start="09:15", symbol="X"):
    t0 = ist_epoch(day, start)
    return [Candle(symbol, t0 + i * 900, o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]


def run(candles, params=None):
    s = OrbStrategy(params or PARAMS)
    return s, [s.on_candle(c) for c in candles]


def test_long_breakout_fires_on_the_first_close_above_the_range():
    s, out = run(bars(RANGE + [INSIDE, UP]))
    assert out[:3] == [None, None, None]
    sig = out[3]
    assert (sig.strategy, sig.symbol, sig.direction, sig.product) == ("orb", "X", "LONG", "MIS")
    assert sig.entry_price == pytest.approx(101.2) and sig.stop_price == pytest.approx(99.8)
    assert sig.target_price == pytest.approx(104.0)          # 101.2 + 2 x (101.2 - 99.8)
    assert sig.priority == pytest.approx(2.5)                # 5000 / mean(1000, 3000)
    assert sig.bar_ts == ist_epoch(D1, "10:00")


def test_rounding_direction_on_stop_and_target_is_away_from_and_toward_entry():
    # Range bars: high 152.97, low 152.03 (width 0.94 on a 152.50 midpoint = 0.616%, inside
    # [0.4, 1.5]); reward_risk 2.0.
    #
    # LONG closing at 153.47: stop = round_tick_down(152.03) = 152.00 (away from the range, i.e. the
    # entry's loss side is widened, never narrowed by rounding); risk = 153.47 - 152.00 = 1.47; target
    # = round_tick_down(153.47 + 2 x 1.47 = 156.41) = 156.40 (toward the entry: conservative).
    #
    # SHORT closing at 151.53: stop = round_tick_up(152.97) = 153.00; risk = 153.00 - 151.53 = 1.47;
    # target = round_tick_up(151.53 - 2 x 1.47 = 148.59) = 148.60.
    rng = [(152.50, 152.97, 152.30, 152.60, 1000), (152.60, 152.80, 152.03, 152.50, 1000)]
    long_close = (153.00, 153.60, 152.90, 153.47, 2000)
    short_close = (152.00, 152.10, 151.40, 151.53, 2000)

    _, out = run(bars(rng + [long_close]))
    sig = out[2]
    assert sig.direction == "LONG"
    assert sig.stop_price == pytest.approx(152.00)
    assert sig.target_price == pytest.approx(156.40)

    _, out = run(bars(rng + [short_close]))
    sig = out[2]
    assert sig.direction == "SHORT"
    assert sig.stop_price == pytest.approx(153.00)
    assert sig.target_price == pytest.approx(148.60)


def test_short_breakout_mirrors():
    _, out = run(bars(RANGE + [DOWN]))
    sig = out[2]
    assert sig.direction == "SHORT" and sig.entry_price == pytest.approx(99.4)
    assert sig.stop_price == pytest.approx(100.8) and sig.target_price == pytest.approx(96.6)
    assert sig.priority == pytest.approx(2.0)


def test_one_signal_per_symbol_per_day_and_a_new_day_starts_over():
    higher = (101.2, 102.0, 101.0, 101.9, 9000)
    s, out = run(bars(RANGE + [UP, higher, DOWN]) + bars(RANGE + [UP], day=D2))
    assert [o is not None for o in out] == [False, False, True, False, False, False, False, True]
    assert out[-1].bar_ts == ist_epoch(D2, "09:45")


def test_symbols_keep_separate_ranges():
    s = OrbStrategy(PARAMS)
    out = [s.on_candle(c) for pair in zip(bars(RANGE + [UP], symbol="X"), bars(RANGE + [INSIDE], symbol="Y")) for c in pair]
    assert [o.symbol for o in out if o] == ["X"]


def test_a_range_that_is_too_narrow_or_too_wide_trades_nothing_that_day():
    narrow = [(100.0, 100.2, 100.0, 100.1, 1000), (100.1, 100.3, 100.05, 100.2, 1000)]   # 0.30%
    wide = [(100.0, 102.0, 100.0, 101.5, 1000), (101.5, 101.8, 100.5, 101.0, 1000)]      # 1.98%
    breakout = (101.0, 103.5, 100.9, 103.0, 9000)
    for rng in (narrow, wide):
        s, out = run(bars(rng + [breakout, breakout]))
        assert out == [None] * 4
        assert s.is_ready("X") and s.snapshot("X")["range_width_pct"] > 0


def test_a_short_range_is_skipped():
    # The 09:15 bar is missing: one range bar where two are needed.
    _, out = run(bars([RANGE[1], UP], start="09:30"))
    assert out == [None, None]


def test_a_close_exactly_on_the_range_boundary_does_not_fire():
    # Closing exactly at range_high (or range_low) is inside, not a breakout: > and < are strict. The
    # non-breakout bar must not mark the symbol done, so a later real breakout still fires.
    at_high = (100.7, 100.9, 100.6, 100.8, 1000)   # closes exactly on range_high (100.8)
    _, out = run(bars(RANGE + [at_high, UP]))
    assert out[:3] == [None, None, None]
    assert out[3] is not None and out[3].direction == "LONG"

    at_low = (99.9, 100.0, 99.7, 99.8, 1000)       # closes exactly on range_low (99.8)
    _, out = run(bars(RANGE + [at_low, DOWN]))
    assert out[:3] == [None, None, None]
    assert out[3] is not None and out[3].direction == "SHORT"


def test_range_width_exactly_at_a_bound_is_tradable():
    # high 101.0, low 100.0: width 1.0 on a 100.5 midpoint. Compute the width the same way the
    # strategy does, so the bound equals it exactly (bit for bit) rather than by decimal rounding.
    rng = [(100.0, 100.6, 100.0, 100.4, 1000), (100.4, 101.0, 100.2, 100.8, 1000)]
    high, low = 101.0, 100.0
    width_pct = (high - low) / ((high + low) / 2.0) * 100.0
    breakout = (101.0, 103.0, 100.9, 102.5, 9000)

    _, out = run(bars(rng + [breakout]), dict(PARAMS, min_range_pct=width_pct, max_range_pct=5.0))
    assert out[2] is not None                          # width == min_range_pct: inclusive

    _, out = run(bars(rng + [breakout]), dict(PARAMS, min_range_pct=0.01, max_range_pct=width_pct))
    assert out[2] is not None                          # width == max_range_pct: inclusive


def test_duplicate_range_bar_is_not_double_counted():
    # The 09:15 bar arrives twice (identical ts) and 09:30 never arrives: one distinct range bar,
    # one short of the two needed, so the range never completes and nothing fires.
    t0 = ist_epoch(D1, "09:15")
    o, h, l, c, v = RANGE[0]
    dup = [Candle("X", t0, o, h, l, c, v), Candle("X", t0, o, h, l, c, v)]
    after_range = Candle("X", t0 + 1800, *UP)  # 09:45: the first bar after a 30-minute range
    s = OrbStrategy(PARAMS)
    out = [s.on_candle(c) for c in dup + [after_range]]
    assert out == [None, None, None]


def test_a_late_range_bar_after_completion_does_not_widen_the_range():
    # A stray range-window bar (an off-grid timestamp, as from a data glitch) that arrives after the
    # range was already declared complete must not retroactively widen it, even though its ts is
    # newer than last_range_ts and would otherwise be accepted as a fresh range bar.
    s = OrbStrategy(PARAMS)
    for c in bars(RANGE + [INSIDE]):
        s.on_candle(c)
    before = s.snapshot("X")
    stray_ts = ist_epoch(D1, "09:15") + 1000   # inside the range window, after last_range_ts (09:30)
    stray = Candle("X", stray_ts, 200.0, 300.0, 50.0, 250.0, 1000)
    assert s.on_candle(stray) is None
    assert s.snapshot("X") == before


def test_bars_before_the_open_are_ignored():
    pre = Candle("X", ist_epoch(D1, "09:00"), 90.0, 120.0, 80.0, 100.0, 1)
    s = OrbStrategy(PARAMS)
    assert s.on_candle(pre) is None
    out = [s.on_candle(c) for c in bars(RANGE + [UP])]
    assert out[2] is not None and out[2].stop_price == pytest.approx(99.8)    # 80.0 never entered the range


def test_no_target_when_reward_risk_is_null():
    _, out = run(bars(RANGE + [UP]), dict(PARAMS, reward_risk=None))
    assert out[2].target_price is None and out[2].stop_price == pytest.approx(99.8)


def test_zero_volume_range_gives_priority_zero():
    rng = [(o, h, l, c, 0) for o, h, l, c, _ in RANGE]
    _, out = run(bars(rng + [UP]))
    assert out[2].priority == 0.0


def test_ready_and_snapshot_follow_the_range():
    s = OrbStrategy(PARAMS)
    cs = bars(RANGE + [UP])
    s.on_candle(cs[0]); s.on_candle(cs[1])
    assert not s.is_ready("X") and s.snapshot("X") == {}
    s.on_candle(cs[2])
    snap = s.snapshot("X")
    assert s.is_ready("X")
    assert snap["range_high"] == pytest.approx(100.8) and snap["range_low"] == pytest.approx(99.8)
    assert snap["range_width_pct"] == pytest.approx(1.0 / 100.3 * 100) and snap["rel_volume"] == pytest.approx(2.5)
    s.reset("X")
    assert not s.is_ready("X")


def test_sixty_minute_range_needs_four_bars():
    four = RANGE + [(100.2, 100.7, 100.1, 100.5, 2000), (100.5, 100.75, 100.2, 100.6, 2000)]
    _, out = run(bars(four + [UP]), dict(PARAMS, range_minutes=60))
    assert [o is not None for o in out] == [False] * 4 + [True]


def test_params_are_validated():
    for bad, match in [(dict(range_minutes=20), "multiple"), (dict(range_minutes=0), "range_minutes"),
                       (dict(min_range_pct=2.0), "min_range_pct"), (dict(reward_risk=0), "reward_risk"),
                       (dict(reward_risk=float("nan")), "reward_risk"),
                       (dict(reward_risk=float("inf")), "reward_risk"),
                       (dict(max_range_pct=float("inf")), "max_range_pct"),
                       (dict(product="CNC"), "MIS"),
                       (dict(session_open="9:15"), r"orb\.session_open: expected a time in HH:MM")]:
        with pytest.raises(ValueError, match=match):
            OrbStrategy(dict(PARAMS, **bad))


def test_build_strategy_knows_orb():
    assert isinstance(build_strategy("orb", PARAMS), OrbStrategy)
