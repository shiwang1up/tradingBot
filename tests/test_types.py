# tests/test_types.py
from tradebot.types import Signal, make_client_id, round_tick


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


def test_signal_risk_per_share():
    assert _sig().risk_per_share == 1.0
    assert _sig(direction="SHORT", stop_price=101.5).risk_per_share == 1.5
