from datetime import date

from click.testing import CliRunner

from tests.helpers import make_config, synth_candles
from tradebot import cli
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.types import Candle


def _setup(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B]\n")
    repo = Repo(connect(cfg.paths.db))
    days = [date(2026, 9, 14), date(2026, 9, 15)]
    repo.insert_candles(synth_candles("A", days) + synth_candles("B", days, phase=4.0, seed=99), interval=5)
    repo.conn.close()
    return cfg


def test_backtest_then_report(tmp_path):
    _setup(tmp_path)
    r = CliRunner()
    res = r.invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                              "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "cli1"])
    assert res.exit_code == 0, res.output
    assert "Trades" in res.output
    rep = r.invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "report", "--run", "cli1"])
    assert rep.exit_code == 0, rep.output
    assert "Run cli1" in rep.output


def test_backtest_without_data_fails_clearly(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                                        "--start", "2026-09-14", "--end", "2026-09-15"])
    assert res.exit_code != 0
    assert "fetch-data" in res.output


def test_fetch_data_uses_adapter_and_instruments(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE, NOSUCH]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
        "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
        "freeze_quantity,is_reserved,buy_allowed,sell_allowed,feed_key\n"
        "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,,,,,1,0.05,,0,1,1,NSE_CASH_2885\n")
    calls = []

    class FakeAdapter:
        def __init__(self, key, secret):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            calls.append((symbol, exchange, interval))
            return [Candle(symbol, end_ts - (end_ts % 300), 1, 2, 0.5, 1.5, 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "fetch-data", "--days", "20"])
    assert res.exit_code == 0, res.output
    assert calls and all(c[0] == "RELIANCE" and c[1] == "NSE" and c[2] == 5 for c in calls)
    assert "dropping NOSUCH: not_found" in res.output
