import pytest

from tradebot.strategy.ema_rsi import build_strategy
from tradebot.strategy.pullback import DONE, IDLE, PULLBACK, PullbackStrategy
from tradebot.types import Candle

# Small periods keep the sequences hand-checkable: EMA3 lags a +1/bar trend by about 1.0, so lows at
# close - 0.3 stay above it and only a deliberate dip touches it.
PARAMS = dict(ema_fast=3, ema_slow=5, atr_period=3, max_pullback_bars=4, reward_risk=2.0,
              min_stop_pct=0.1, max_stop_atr=3.0, product="MIS")
UP = [(c - 1, c + 0.5, c - 0.3, c) for c in range(10, 17)]        # closes 10..16
DOWN = [(c + 1, c + 0.3, c - 0.5, c) for c in range(16, 9, -1)]   # closes 16..10
TOUCH = (16, 16.2, 14.0, 15.0)          # low 14.0 dips through EMA3 (about 15.0): pullback opens
CONFIRM = (15, 16.5, 14.5, 16.4)        # closes back above EMA3 with a higher low (14.5 > 14.0)


def bars(rows, symbol="X"):
    return [Candle(symbol, 1000 + i * 900, o, h, l, c, 100) for i, (o, h, l, c) in enumerate(rows)]


def run(rows, params=None):
    s = PullbackStrategy(params or PARAMS)
    return s, [(c.ts, s.on_candle(c)) for c in bars(rows)]


def signals(out):
    return [(ts, sig.direction, sig.entry_price, sig.stop_price, sig.target_price) for ts, sig in out if sig]


def test_canonical_long_fires_on_the_confirmation_bar():
    s, out = run(UP + [TOUCH, CONFIRM])
    assert signals(out) == [(8200, "LONG", 16.4, 13.95, 21.3)]   # stop one tick under the pullback low 14.0
    ts, sig = out[-1]
    assert sig.symbol == "X" and sig.strategy == "pullback" and sig.product == "MIS" and sig.bar_ts == ts
    assert s.snapshot("X")["phase"] == DONE and s.snapshot("X")["trend"] == 1


def test_confirmation_needs_a_higher_low():
    lower_low = (15, 16.5, 13.9, 16.4)      # closes above the EMA but low 13.9 < 14.0: no signal
    higher_low = (16.4, 16.8, 14.2, 16.6)   # low 14.2 > 13.9: fires, stop under the new pullback low 13.9
    _, out = run(UP + [TOUCH, lower_low, higher_low])
    assert signals(out) == [(9100, "LONG", 16.6, 13.85, 22.1)]


def test_one_trade_per_pullback_until_a_new_swing_high():
    quiet = (16.4, 16.5, 16.2, 16.45)       # high 16.5 does not exceed the pre-pullback swing high 16.5
    breakout = (16.45, 16.8, 16.3, 16.7)    # 16.8 > 16.5: back to idle
    touch2, confirm2 = (16.7, 16.8, 15.0, 16.0), (16, 17, 15.5, 16.9)
    s, out = run(UP + [TOUCH, CONFIRM, quiet, breakout, touch2, confirm2])
    assert signals(out) == [(8200, "LONG", 16.4, 13.95, 21.3), (11800, "LONG", 16.9, 14.95, 20.8)]
    phases = {}
    s2 = PullbackStrategy(PARAMS)
    for c in bars(UP + [TOUCH, CONFIRM, quiet, breakout]):
        s2.on_candle(c)
        phases[c.ts] = s2.snapshot("X").get("phase")   # empty before the indicators are warm
    assert phases[9100] == DONE and phases[10000] == IDLE


def test_pullback_longer_than_max_bars_is_spent_until_a_new_swing_high():
    sinking = [(15, 16.5, 13.9, 16.4), (16.4, 16.5, 13.8, 16.3), (16.3, 16.5, 13.7, 16.2), (16.2, 16.5, 13.6, 16.1)]
    s, out = run(UP + [TOUCH] + sinking)     # every low is lower than the last: never confirms
    assert signals(out) == []
    assert s.snapshot("X")["phase"] == DONE  # 5 pullback bars > max_pullback_bars 4: spent, not re-armable
    # a dip and a would-be confirmation right after the timeout must not trade ...
    _, out2 = run(UP + [TOUCH] + sinking + [(16.1, 16.5, 13.5, 16.0), (16.0, 16.4, 14.0, 16.3)])
    assert signals(out2) == []
    # ... but a new swing high above 16.5 re-arms, and the next pullback trades
    _, out3 = run(UP + [TOUCH] + sinking + [(16.1, 17.0, 16.0, 16.8), (16.8, 17.0, 15.0, 16.2), (16.2, 17.2, 15.6, 17.0)])
    assert [x[1] for x in signals(out3)] == ["LONG"] and signals(out3)[0][0] == 1000 + 14 * 900


def test_trend_flip_resets_and_the_flip_bar_does_not_arm():
    falling = [(15, 15, 13, 13.5), (13.5, 13.6, 12, 12.2), (12.2, 12.3, 11, 11.1)]
    s, out = run(UP + [TOUCH] + falling)
    assert signals(out) == []
    snap = s.snapshot("X")
    assert snap["trend"] == -1 and snap["phase"] == PULLBACK   # the bar after the flip may arm; the flip bar may not


def test_short_mirror():
    touch = (10, 12.0, 9.8, 11.0)            # high 12.0 reaches up through EMA3
    confirm = (11, 11.5, 9.5, 9.6)           # closes back below with a lower high (11.5 < 12.0)
    _, out = run(DOWN + [touch, confirm])
    assert signals(out) == [(8200, "SHORT", 9.6, 12.05, 4.7)]   # stop one tick above the pullback high 12.0


@pytest.mark.parametrize("override", [{"min_stop_pct": 20}, {"max_stop_atr": 0.5}])
def test_stop_guards_drop_the_signal_but_still_consume_the_pullback(override):
    s, out = run(UP + [TOUCH, CONFIRM], dict(PARAMS, **override))
    assert signals(out) == []
    assert s.snapshot("X")["phase"] == DONE


def test_not_ready_until_two_warm_bars_and_first_ready_bar_never_fires():
    s = PullbackStrategy(PARAMS)
    cs = bars(UP)
    for c in cs[:4]:
        assert s.on_candle(c) is None
    assert not s.is_ready("X") and s.snapshot("X") == {}
    assert s.on_candle(cs[4]) is None          # first bar with EMA5 and ATR warm
    assert not s.is_ready("X")
    s.on_candle(cs[5])
    assert s.is_ready("X")
    assert set(s.snapshot("X")) == {"ema_fast", "ema_slow", "atr", "trend", "phase", "swing_high", "swing_low",
                                    "pullback_low", "pullback_high"}


def test_recompute_replays_deterministically():
    s, out = run(UP + [TOUCH, CONFIRM, (16.4, 16.5, 16.2, 16.45), (16.45, 16.8, 16.3, 16.7)])   # ends IDLE after a breakout
    before = s.snapshot("X")
    fresh = PullbackStrategy(PARAMS)
    fresh.recompute("X", bars(UP + [TOUCH, CONFIRM, (16.4, 16.5, 16.2, 16.45), (16.45, 16.8, 16.3, 16.7)]))
    assert fresh.snapshot("X") == before and before["phase"] == IDLE
    s.recompute("X", bars(UP))              # a shorter replay must not keep the old pullback state
    assert s.snapshot("X")["phase"] == IDLE and s.snapshot("X")["pullback_low"] == 0.0


def test_a_new_session_starts_clean_so_yesterdays_pullback_cannot_confirm_on_the_gap():
    from tradebot.engine.clock import ist_epoch
    from datetime import date
    day1 = ist_epoch(date(2026, 9, 14), "09:15")
    day2 = ist_epoch(date(2026, 9, 15), "09:15")
    rows = UP + [TOUCH]                                    # the last bar of day 1 arms a pullback (low 14.0)
    cs = [Candle("X", day1 + i * 900, o, h, l, c, 100) for i, (o, h, l, c) in enumerate(rows)]
    gap = Candle("X", day2, 20.0, 20.5, 19.8, 20.4, 100)   # closes above the EMA with a "higher low"
    s = PullbackStrategy(PARAMS)
    out = [s.on_candle(c) for c in cs] + [s.on_candle(gap)]
    assert all(sig is None for sig in out), "a gap bar must not confirm yesterday's pullback"
    snap = s.snapshot("X")
    assert snap["phase"] == IDLE and snap["swing_high"] == 20.5 and snap["pullback_low"] == 0.0


def test_a_bar_with_equal_emas_has_no_trend_and_places_nothing():
    flat = [(10, 10.5, 9.5, 10)] * 8
    s, out = run(flat)
    assert signals(out) == [] and s.is_ready("X")
    assert s.snapshot("X")["trend"] == 0 and s.snapshot("X")["phase"] == IDLE


@pytest.mark.parametrize("bad, msg", [
    ({"ema_slow": 3}, "ema_fast must be <"),
    ({"max_pullback_bars": 0}, "max_pullback_bars"),
    ({"reward_risk": 0}, "must be > 0"),
    ({"product": "NRML"}, "product"),
    ({"ema_fast": 2.5}, "integer"),
])
def test_parameter_validation(bad, msg):
    with pytest.raises(ValueError, match=msg):
        PullbackStrategy(dict(PARAMS, **bad))


def test_factory_registers_pullback():
    assert isinstance(build_strategy("pullback", PARAMS), PullbackStrategy)
