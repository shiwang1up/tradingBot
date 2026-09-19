# tests/test_types.py
import math

import pytest

from tradebot.types import Position, Signal, make_client_id, round_tick


def _sig(**kw):
    base = dict(strategy="ema_rsi", symbol="RELIANCE", direction="LONG",
                entry_price=100.0, stop_price=99.0, target_price=102.0,
                product="MIS", bar_ts=1_700_000_000)
    base.update(kw)
    return Signal(**base)


def test_client_id_is_deterministic_and_16_hex():
    a = make_client_id("ema_rsi", "RELIANCE", 1_700_000_000)
    b = make_client_id("ema_rsi", "RELIANCE", 1_700_000_000)
    assert a == b
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_client_id_changes_with_any_component():
    base = make_client_id("ema_rsi", "RELIANCE", 1)
    assert make_client_id("other", "RELIANCE", 1) != base
    assert make_client_id("ema_rsi", "TCS", 1) != base
    assert make_client_id("ema_rsi", "RELIANCE", 2) != base


def test_round_tick():
    assert round_tick(100.03) == 100.05
    assert round_tick(100.02) == 100.0
    assert round_tick(99.98) == 100.0


def test_position_unrealised_signs():
    long = Position("X", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "cid", "s")
    short = Position("X", "MIS", "SHORT", 10, 100.0, 101.0, None, 1, "cid", "s")
    assert long.unrealised(102.0) == 20.0
    assert long.unrealised(98.0) == -20.0
    assert short.unrealised(98.0) == 20.0
    assert short.unrealised(102.0) == -20.0


def test_decision_token_fields_default_to_zero():
    from tradebot.types import Decision
    d = Decision(_sig(), True, "ok", 1.0, "stub")
    assert (d.input_tokens, d.output_tokens, d.cache_read_tokens, d.cache_write_tokens) == (0, 0, 0, 0)


def test_signal_priority_defaults_to_zero_and_is_the_last_field():
    s = Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", 1000)
    assert s.priority == 0.0
    assert Signal("orb", "A", "LONG", 100.0, 99.0, None, "MIS", 1000, priority=2.5).priority == 2.5


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None])
def test_signal_rejects_a_non_finite_priority(bad):
    with pytest.raises(ValueError, match="priority"):
        _sig(priority=bad)


def test_signal_accepts_an_int_priority():
    assert _sig(priority=2).priority == 2
