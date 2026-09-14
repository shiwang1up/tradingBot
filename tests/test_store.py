import json
import sqlite3

import pytest

from tradebot.store.db import SCHEMA_VERSION, SchemaVersionError, connect
from tradebot.types import Candle, Position, Signal


def _candle(sym="RELIANCE", ts=1_700_000_000, c=100.0):
    return Candle(sym, ts, c, c + 1, c - 1, c, 1000)


def _signal(sym="RELIANCE", ts=1_700_000_000):
    return Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", ts)


def test_run_lifecycle(repo):
    repo.create_run("r1", "backtest", 10, json.dumps({"a": 1}))
    run = repo.get_run("r1")
    assert run["mode"] == "backtest"
    assert run["ended_at"] is None
    repo.end_run("r1", 20)
    assert repo.get_run("r1")["ended_at"] == 20


def test_candles_insert_is_idempotent(repo):
    cs = [_candle(ts=1_700_000_000), _candle(ts=1_700_000_300)]
    assert repo.insert_candles(cs, interval=5) == 2
    assert repo.insert_candles(cs, interval=5) == 0
    assert repo.insert_candles(cs + [_candle(ts=1_700_000_600)], interval=5) == 1  # mixed batch
    assert repo.latest_candle_ts("RELIANCE", 5) == 1_700_000_600
    assert repo.latest_candle_ts("TCS", 5) is None


def test_load_candles_filters_and_orders(repo):
    repo.insert_candles([_candle(ts=3), _candle(ts=1), _candle(ts=2), _candle("TCS", ts=2)], interval=5)
    out = repo.load_candles(["RELIANCE"], interval=5, start_ts=1, end_ts=2)
    assert [c.ts for c in out] == [1, 2]
    both = repo.load_candles(["RELIANCE", "TCS"], interval=5, start_ts=0, end_ts=10)
    assert [(c.ts, c.symbol) for c in both] == [(1, "RELIANCE"), (2, "RELIANCE"), (2, "TCS"), (3, "RELIANCE")]


def test_signal_and_decisions(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    sid = repo.insert_signal("r1", _signal())
    repo.insert_risk_decision("r1", sid, approved=False, reason="max_open_positions", quantity=0)
    repo.insert_risk_decision("r1", repo.insert_signal("r1", _signal("TCS")), approved=True, reason="ok", quantity=10)
    repo.insert_ai_decision("r1", sid, "stub", True, "stub", 1.0, 0, None)
    assert repo.rejection_counts("r1") == {"max_open_positions": 1}


def test_orders_and_fills(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    sid = repo.insert_signal("r1", _signal())
    oid = repo.insert_order("r1", "abc123abc123abc1", sid, "ENTRY", "BUY", 10, 100.0, "PENDING", 5)
    repo.update_order("r1", "abc123abc123abc1", "FILLED", 6)
    repo.insert_fill(oid, 10, 100.05, 6)
    assert repo.order_status("r1", "abc123abc123abc1") == "FILLED"


def test_positions_roundtrip(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    p = Position("RELIANCE", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 5, "cid", "ema_rsi")
    p.db_id = repo.insert_position("r1", p)
    repo.close_position(p.db_id, closed_ts=9, exit_price=102.0, exit_reason="TARGET", pnl=20.0)
    rows = repo.list_positions("r1")
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "TARGET"
    assert rows[0]["pnl"] == 20.0


def test_daily_pnl_upsert(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    repo.upsert_daily_pnl("r1", "2026-09-14", realised=10.0, unrealised=0.0, fills=1, entries_placed=2)
    repo.upsert_daily_pnl("r1", "2026-09-14", realised=15.0, unrealised=1.0, fills=2, entries_placed=2)
    rows = repo.daily_pnl("r1")
    assert len(rows) == 1
    assert rows[0]["realised"] == 15.0
    assert rows[0]["fill_rate"] == 1.0


def test_duplicate_client_id_is_rejected_and_original_survives(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    repo.insert_order("r1", "dupdupdupdupdup1", None, "ENTRY", "BUY", 10, 100.0, "PENDING", 5)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_order("r1", "dupdupdupdupdup1", None, "ENTRY", "BUY", 99, 1.0, "PENDING", 6)
    assert repo.order_status("r1", "dupdupdupdupdup1") == "PENDING"
    assert repo.order_id("r1", "nope") is None
    assert repo.order_status("r1", "nope") is None


def test_foreign_keys_enforced(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_fill(999_999, 1, 1.0, 1)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_signal("no-such-run", _signal())


def test_close_position_accepts_null_pnl(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    pid = repo.insert_position("r1", Position("X", "MIS", "LONG", 1, 1.0, 0.5, None, 1, "c", "s"))
    repo.close_position(pid, closed_ts=2, exit_price=None, exit_reason=None, pnl=None)
    assert repo.list_positions("r1")[0]["pnl"] is None


def test_file_backed_connect_uses_wal_persists_and_versions(tmp_path):
    path = tmp_path / "nested" / "dir" / "t.db"
    conn = connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.execute("INSERT INTO runs(run_id, mode, started_at, config_json) VALUES ('r','backtest',0,'{}')")
    conn.commit()
    conn.close()
    conn2 = connect(path)  # schema re-applied idempotently, data intact
    assert conn2.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn2.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn2.commit()
    conn2.close()
    with pytest.raises(SchemaVersionError):
        connect(path)
