from datetime import date
from pathlib import Path

import pytest

from tradebot.data import instruments as instruments_mod
from tradebot.data.instruments import download_instruments, load_instruments, resolve_universe
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


HEADER = FIXTURE.read_text().splitlines()[0]


def test_load_universe_normalises_and_dedupes(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: nse\nsymbols:\n  - reliance\n  - RELIANCE\n  - ' TCS '\n  - RELIANCE\n")
    assert load_universe(p) == Universe(exchange="NSE", symbols=("RELIANCE", "TCS"))


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "exchange: NSE\n", "exchange: NSE\nsymbols: []\n"])
def test_load_universe_rejects_malformed(tmp_path, text):
    p = tmp_path / "universe.yaml"
    p.write_text(text)
    with pytest.raises(ValueError):
        load_universe(p)


def test_load_universe_as_of_is_not_silently_ignored(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    with pytest.raises(NotImplementedError):
        load_universe(p, as_of=date(2024, 1, 1))


def test_load_instruments_missing_column_is_clear_error(tmp_path):
    p = tmp_path / "i.csv"
    p.write_text(HEADER.replace("buy_allowed,", "is_buy_allowed,") + "\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_instruments(p)


def test_load_instruments_tolerates_bom_and_empty_numeric_cells(tmp_path):
    p = tmp_path / "i.csv"
    row = "NSE,1,X,NSE-X,X Co,EQ,CASH,EQ,IN,,,,,,,,0,1,1,K"  # empty lot_size and tick_size
    p.write_bytes(("\ufeff" + HEADER + "\n" + row + "\n").encode("utf-8"))
    inst = load_instruments(p)[("NSE", "X")]
    assert inst.lot_size == 1 and inst.tick_size == 0.05


def test_load_instruments_bad_numeric_names_symbol(tmp_path):
    p = tmp_path / "i.csv"
    row = "NSE,1,BADLOT,NSE-BADLOT,X Co,EQ,CASH,EQ,IN,,,,,N/A,0.05,,0,1,1,K"
    p.write_text(HEADER + "\n" + row + "\n")
    with pytest.raises(ValueError, match="BADLOT"):
        load_instruments(p)


def test_empty_tradeability_cell_fails_closed(tmp_path):
    p = tmp_path / "i.csv"
    row = "NSE,1,X,NSE-X,X Co,EQ,CASH,EQ,IN,,,,,1,0.05,,0,,1,K"
    p.write_text(HEADER + "\n" + row + "\n")
    _, dropped = resolve_universe(Universe("NSE", ("X",)), load_instruments(p))
    assert dropped == [("X", "not_tradeable")]


def test_download_is_atomic_and_validated(tmp_path, monkeypatch):
    dest = tmp_path / "data" / "instruments.csv"
    dest.parent.mkdir()
    dest.write_text("old-good-cache")

    class Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(instruments_mod.requests, "get", lambda url, timeout: Resp(b"<html>blocked</html>"))
    with pytest.raises(ValueError, match="does not look like"):
        download_instruments(dest)
    assert dest.read_text() == "old-good-cache"
    assert not dest.with_suffix(".csv.tmp").exists()

    monkeypatch.setattr(instruments_mod.requests, "get", lambda url, timeout: Resp(FIXTURE.read_bytes()))
    download_instruments(dest)
    assert ("NSE", "RELIANCE") in load_instruments(dest)
