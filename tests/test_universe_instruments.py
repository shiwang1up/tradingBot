from pathlib import Path

from tradebot.data.instruments import load_instruments, resolve_universe
from tradebot.data.universe import Universe, load_universe

FIXTURE = Path(__file__).parent / "fixtures" / "instrument_sample.csv"


def test_load_universe(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - RELIANCE\n  - TCS\n")
    u = load_universe(p)
    assert u == Universe(exchange="NSE", symbols=("RELIANCE", "TCS"))


def test_load_instruments_keys_by_exchange_and_symbol():
    inst = load_instruments(FIXTURE)
    r = inst[("NSE", "RELIANCE")]
    assert r.exchange_token == "2885"
    assert r.lot_size == 1
    assert r.tick_size == 0.05
    assert r.buy_allowed and r.sell_allowed
    assert inst[("BSE", "RELIANCE")].exchange_token == "500325"


def test_resolve_universe_drops_missing_and_untradeable():
    inst = load_instruments(FIXTURE)
    u = Universe(exchange="NSE", symbols=("RELIANCE", "TCS", "SUSPENDED", "NOSUCH", "NIFTY26SEPFUT"))
    resolved, dropped = resolve_universe(u, inst)
    assert sorted(resolved) == ["RELIANCE", "TCS"]
    assert dict(dropped) == {
        "SUSPENDED": "not_tradeable",
        "NOSUCH": "not_found",
        "NIFTY26SEPFUT": "not_cash_equity",
    }
