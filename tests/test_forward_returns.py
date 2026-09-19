import importlib.util
import json
from pathlib import Path

from tradebot.types import Candle, Signal

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "signal_forward_returns.py"
spec = importlib.util.spec_from_file_location("signal_forward_returns", SCRIPT)
fwd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fwd)


def _cfg(interval):
    return json.dumps({"execution": {"interval_minutes": interval}})


def test_forward_returns_use_the_runs_interval(repo):
    t0 = 1_000_000
    fifteen = [Candle("A", t0 + i * 900, 100 + i, 100 + i, 100 + i, 100 + i, 1) for i in range(30)]     # +1 per bar
    five = [Candle("A", t0 + i * 300, 100 - i, 100 - i, 100 - i, 100 - i, 1) for i in range(30)]        # -1 per bar
    repo.insert_candles(fifteen, 15)
    repo.insert_candles(five, 5)
    repo.create_run("r15", "backtest", 0, _cfg(15))
    repo.create_run("r5", "backtest", 0, _cfg(5))
    repo.create_run("legacy", "backtest", 0, "{}")
    for rid in ("r15", "r5", "legacy"):
        repo.insert_signal(rid, Signal("s", "A", "LONG", 105.0, 104.0, 107.0, "MIS", t0 + 5 * 900))  # bar index 5 on 15m
    assert fwd.run_interval(repo.conn, "r15") == 15
    assert fwd.run_interval(repo.conn, "legacy") == 5
    up = fwd.forward_returns(repo.conn, "r15")
    assert up["ALL"][1] == [((106.0 / 105.0) - 1) * 1e4]        # the 15-minute series rose one point
    assert fwd.forward_returns(repo.conn, "r5")["ALL"][1][0] < 0     # 4500 s is also a 5-minute bar; that series falls
    table = fwd.format_table(up)
    assert table.splitlines()[0].startswith("group") and "ALL" in table
