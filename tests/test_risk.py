import pytest

from tradebot.config import RiskConfig
from tradebot.risk.engine import PortfolioState, evaluate
from tradebot.risk.killswitch import KillState, read_kill_switch
from tradebot.risk.sizing import compute_quantity
from tradebot.types import ApprovedOrder, Rejection, Signal

CFG = RiskConfig(per_trade_pct=1.0, daily_loss_cap_pct=3.0, flatten_on_daily_cap=False,
                 max_entries_per_day=5, max_open_positions=2, cooldown_bars=3, mis_leverage=5.0,
                 adopted_stop_pct=1.5)
OFF = KillState(active=False, flatten=False)


def _sig(direction="LONG", entry=100.0, stop=99.0, product="MIS", symbol="RELIANCE", bar_ts=10_000):
    target = entry + 2 * (entry - stop) if direction == "LONG" else entry - 2 * (stop - entry)
    return Signal("ema_rsi", symbol, direction, entry, stop, target, product, bar_ts)


def _state(**kw):
    base = dict(capital=100_000.0, open_symbols=set(), pending_symbols=set(), realised_today=0.0,
                unrealised=0.0, entries_today=0, cooldown_until={})
    base.update(kw)
    return PortfolioState(**base)


# -- kill switch ---------------------------------------------------------
def test_kill_switch_absent(tmp_path):
    assert read_kill_switch(tmp_path / "KILL") == KillState(False, False)


def test_kill_switch_never_raises_and_fails_closed(tmp_path):
    d = tmp_path / "KILL"
    d.mkdir()
    assert read_kill_switch(d) == KillState(True, False)      # directory: block, don't flatten
    f = tmp_path / "K2"
    f.write_bytes(b"\xff\xfe not utf8")
    assert read_kill_switch(f) == KillState(True, False)      # undecodable: still active
    link = tmp_path / "K3"
    link.symlink_to(tmp_path / "gone")
    assert read_kill_switch(link) == KillState(False, False)  # dangling symlink == absent


def test_kill_switch_flatten_anywhere_in_text(tmp_path):
    (tmp_path / "KILL").write_text("FLATTEN everything now\n")
    assert read_kill_switch(tmp_path / "KILL").flatten


def test_kill_switch_empty_file_blocks_entries_only(tmp_path):
    (tmp_path / "KILL").write_text("")
    assert read_kill_switch(tmp_path / "KILL") == KillState(True, False)


def test_kill_switch_flatten(tmp_path):
    (tmp_path / "KILL").write_text(" Flatten \n")
    assert read_kill_switch(tmp_path / "KILL") == KillState(True, True)


# -- sizing ---------------------------------------------------------------
@pytest.mark.parametrize("capital,pct,entry,stop,margin,lot,expected", [
    (100_000, 1.0, 100.0, 99.0, 500_000, 1, 1000),   # risk-limited: 1000 rs / 1 rs
    (100_000, 1.0, 100.0, 99.0, 50_000, 1, 500),     # capital-limited: 50000 / 100
    (100_000, 1.0, 100.0, 99.0, 500_000, 75, 975),   # rounded down to lot
    (100_000, 1.0, 100.0, 99.0, 5_000, 75, 0),       # 50 < lot => 0
    (100_000, 1.0, 100.0, 100.0, 500_000, 1, 0),     # zero stop distance
    (100_000, 0.5, 250.0, 247.5, 1_000_000, 1, 200), # 500 / 2.5
    (100_000, 0.5, 100.0, 99.8, 1e9, 500, 2500),      # float-exact boundary must not floor to 2000
    (100_000, 1.0, 100.0, 99.0, 0, 1, 0),             # no margin
    (100_000, 1.0, 100.0, 99.0, -5, 1, 0),            # negative margin
    (100_000, 1.0, 100.0, 99.0, 500_000, 5000, 0),    # lot exceeds both candidates
    (100_000, 1.0, float("nan"), 99.0, 500_000, 1, 0),
    (100_000, 1.0, 100.0, 99.0, float("inf"), 1, 0),
])
def test_compute_quantity(capital, pct, entry, stop, margin, lot, expected):
    assert compute_quantity(capital, pct, entry, stop, margin, lot) == expected


# -- evaluate: rejections in spec order ----------------------------------
def test_kill_switch_rejects():
    r = evaluate(_sig(), _state(), CFG, 1, 500_000, KillState(True, False))
    assert isinstance(r, Rejection) and r.reason == "kill_switch"


def test_daily_loss_cap_includes_unrealised():
    r = evaluate(_sig(), _state(realised_today=-1000.0, unrealised=-2000.0), CFG, 1, 500_000, OFF)
    assert r.reason == "daily_loss_cap"
    ok = evaluate(_sig(), _state(realised_today=-1000.0, unrealised=-1999.0), CFG, 1, 500_000, OFF)
    assert isinstance(ok, ApprovedOrder)


def test_entry_cap():
    r = evaluate(_sig(), _state(entries_today=5), CFG, 1, 500_000, OFF)
    assert r.reason == "max_entries_per_day"


def test_max_open_positions_counts_pending():
    r = evaluate(_sig(), _state(open_symbols={"A"}, pending_symbols={"B"}), CFG, 1, 500_000, OFF)
    assert r.reason == "max_open_positions"


def test_symbol_already_open_or_pending():
    assert evaluate(_sig(), _state(open_symbols={"RELIANCE"}), CFG, 1, 500_000, OFF).reason == "symbol_already_open"
    assert evaluate(_sig(), _state(pending_symbols={"RELIANCE"}), CFG, 1, 500_000, OFF).reason == "symbol_already_open"


def test_cooldown_blocks_until_inclusive():
    st = _state(cooldown_until={"RELIANCE": 10_000})
    assert evaluate(_sig(bar_ts=10_000), st, CFG, 1, 500_000, OFF).reason == "cooldown"
    assert isinstance(evaluate(_sig(bar_ts=10_300), st, CFG, 1, 500_000, OFF), ApprovedOrder)


def test_invalid_stop_side():
    assert evaluate(_sig("LONG", 100.0, 101.0), _state(), CFG, 1, 500_000, OFF).reason == "invalid_stop"
    assert evaluate(_sig("SHORT", 100.0, 99.0), _state(), CFG, 1, 500_000, OFF).reason == "invalid_stop"


def test_short_requires_mis():
    r = evaluate(_sig("SHORT", 100.0, 101.0, product="CNC"), _state(), CFG, 1, 500_000, OFF)
    assert r.reason == "short_requires_mis"


def test_insufficient_size():
    r = evaluate(_sig(), _state(), CFG, lot_size=75, available_margin=5_000, kill=OFF)
    assert r.reason == "insufficient_size"


def test_approved_order_has_quantity_and_client_id():
    r = evaluate(_sig(), _state(), CFG, 1, 500_000, OFF)
    assert isinstance(r, ApprovedOrder)
    assert r.quantity == 1000
    assert len(r.client_id) == 16


def test_check_precedence_kill_switch_wins():
    st = _state(realised_today=-9999.0, entries_today=99, open_symbols={"RELIANCE"},
                cooldown_until={"RELIANCE": 99_999})
    r = evaluate(_sig(), st, CFG, 1, 0, KillState(True, False))
    assert r.reason == "kill_switch"
    assert evaluate(_sig(), st, CFG, 1, 0, OFF).reason == "daily_loss_cap"


def test_invalid_price_reason():
    assert evaluate(_sig(entry=0.0, stop=-1.0), _state(), CFG, 1, 500_000, OFF).reason == "invalid_price"


def test_evaluate_does_not_mutate_state():
    st = _state(open_symbols={"A"}, pending_symbols={"B"}, cooldown_until={"C": 5})
    before = (set(st.open_symbols), set(st.pending_symbols), dict(st.cooldown_until), st.entries_today)
    evaluate(_sig(), st, CFG, 1, 500_000, OFF)
    assert before == (st.open_symbols, st.pending_symbols, st.cooldown_until, st.entries_today)


def test_open_count_does_not_double_count_overlap():
    st = _state(open_symbols={"A"}, pending_symbols={"A"})
    assert st.open_count == 1
