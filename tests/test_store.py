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
    repo.close_position(p.db_id, closed_ts=9, exit_price=102.0, exit_reason="TARGET", pnl=20.0, charges=None)
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
    repo.close_position(pid, closed_ts=2, exit_price=None, exit_reason=None, pnl=None, charges=None)
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


def test_v1_database_is_migrated_to_current(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL);
        CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, signal_id INTEGER NOT NULL,
                                   filter_kind TEXT NOT NULL, approved INTEGER NOT NULL, reason TEXT NOT NULL,
                                   confidence REAL NOT NULL, latency_ms INTEGER NOT NULL, failure TEXT);
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                                product TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
                                client_id TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
                                stop REAL NOT NULL, target REAL, exit_ids_json TEXT NOT NULL DEFAULT '[]',
                                opened_at INTEGER NOT NULL, closed_at INTEGER, exit_price REAL, exit_reason TEXT,
                                pnl REAL, fill_status TEXT NOT NULL DEFAULT 'full', adopted INTEGER NOT NULL DEFAULT 0);
        PRAGMA user_version = 1;
    """)
    raw.commit()
    raw.close()
    conn = connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    assert {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4


def test_v2_database_is_migrated_to_v3(tmp_path):
    path = tmp_path / "v2.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL);
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                                product TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
                                client_id TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
                                stop REAL NOT NULL, target REAL, exit_ids_json TEXT NOT NULL DEFAULT '[]',
                                opened_at INTEGER NOT NULL, closed_at INTEGER, exit_price REAL, exit_reason TEXT,
                                pnl REAL, fill_status TEXT NOT NULL DEFAULT 'full', adopted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO runs VALUES ('r', 'backtest', 0, NULL, '{}');
        PRAGMA user_version = 2;
    """)
    raw.commit()
    raw.close()
    conn = connect(path)
    assert "last_bar_ts" in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert conn.execute("SELECT last_bar_ts FROM runs WHERE run_id='r'").fetchone()[0] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    conn.close()


def _make_v3_database(path) -> None:
    """A schema-v3 file: positions has no `charges` column yet. Shared by the migration test and
    the column-order test below."""
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL, last_bar_ts INTEGER);
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                                product TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
                                client_id TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
                                stop REAL NOT NULL, target REAL, exit_ids_json TEXT NOT NULL DEFAULT '[]',
                                opened_at INTEGER NOT NULL, closed_at INTEGER, exit_price REAL, exit_reason TEXT,
                                pnl REAL, fill_status TEXT NOT NULL DEFAULT 'full', adopted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO runs VALUES ('r', 'backtest', 0, NULL, '{}', NULL);
        INSERT INTO positions(run_id, symbol, product, direction, strategy, client_id, qty, avg_price, stop,
                              opened_at, closed_at, exit_price, exit_reason, pnl)
               VALUES ('r', 'A', 'MIS', 'LONG', 'ema_rsi', 'c1', 10, 100.0, 99.0, 1, 2, 102.0, 'TARGET', 20.0);
        PRAGMA user_version = 3;
    """)
    raw.commit()
    raw.close()


def test_v3_database_is_migrated_to_v4(tmp_path):
    path = tmp_path / "v3.db"
    _make_v3_database(path)
    conn = connect(path)
    assert "charges" in {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    row = conn.execute("SELECT pnl, charges FROM positions WHERE run_id='r'").fetchone()
    assert row["pnl"] == 20.0 and row["charges"] is None      # old rows keep NULL: the report estimates them
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    conn.close()


def test_fresh_and_migrated_databases_have_identical_positions_columns(tmp_path):
    """ALTER TABLE ADD COLUMN appends, so a fresh database's positions columns must be declared in
    the same order schema.sql's charges-last ordering gives a migrated (v3 -> v4) database."""
    def cols(conn):
        return [(r["name"], r["type"], r["notnull"], r["dflt_value"])
               for r in conn.execute("PRAGMA table_info(positions)")]

    fresh = connect(":memory:")
    fresh_cols = cols(fresh)
    fresh.close()

    path = tmp_path / "v3_for_columns.db"
    _make_v3_database(path)
    migrated = connect(path)
    migrated_cols = cols(migrated)
    migrated.close()

    assert fresh_cols == migrated_cols


def test_close_position_stores_charges(repo):
    repo.create_run("r", "backtest", 0, "{}")
    pid = repo.insert_position("r", Position("A", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c1", "ema_rsi"))
    repo.close_position(pid, 9, 102.0, "TARGET", 20.0, charges=3.21)
    assert repo.list_positions("r")[0]["charges"] == 3.21
    pid2 = repo.insert_position("r", Position("B", "MIS", "LONG", 10, 100.0, 99.0, 102.0, 1, "c2", "ema_rsi"))
    repo.close_position(pid2, 9, 102.0, "TARGET", 20.0, charges=None)     # a row with no recorded charges
    assert repo.list_positions("r")[1]["charges"] is None


def test_last_bar_ts_open_positions_and_pending_orders(repo):
    repo.create_run("p", "paper", 0, "{}")
    assert repo.get_run("p")["last_bar_ts"] is None
    repo.set_last_bar_ts("p", 1000)
    assert repo.get_run("p")["last_bar_ts"] == 1000

    sid = repo.insert_signal("p", Signal("ema_rsi", "A", "LONG", 100.0, 99.0, 102.0, "MIS", 900))
    repo.insert_order("p", "c1", sid, "ENTRY", "BUY", 10, 100.0, "PENDING", 900)
    sid2 = repo.insert_signal("p", Signal("ema_rsi", "B", "SHORT", 50.0, 51.0, 48.0, "MIS", 900))
    repo.insert_order("p", "c2", sid2, "ENTRY", "SELL", 5, 50.0, "PENDING", 900)
    repo.update_order("p", "c2", "FILLED", 1200)
    pend = repo.pending_orders("p")
    assert [(r["client_id"], r["strategy"], r["symbol"], r["direction"], r["qty"], r["entry"], r["stop"],
             r["target"], r["product"], r["bar_ts"]) for r in pend] == [
        ("c1", "ema_rsi", "A", "LONG", 10, 100.0, 99.0, 102.0, "MIS", 900)]

    open_id = repo.insert_position("p", Position("B", "MIS", "SHORT", 5, 50.0, 51.0, 48.0, 1200, "c2", "ema_rsi"))
    done_id = repo.insert_position("p", Position("C", "MIS", "LONG", 1, 10.0, 9.0, 12.0, 600, "c3", "ema_rsi"))
    repo.close_position(done_id, 900, 9.0, "STOP", -1.0, charges=None)
    assert [r["id"] for r in repo.open_positions("p")] == [open_id]


def test_ai_cache_roundtrip(repo):
    assert repo.get_ai_cache("A", 100, "h") is None
    repo.put_ai_cache("A", 100, "h", '{"approve": true}', created_at=5)
    repo.put_ai_cache("A", 100, "h", '{"approve": false}', created_at=6)  # replace
    assert repo.get_ai_cache("A", 100, "h") == '{"approve": false}'


def test_ai_usage_and_rejected_signals(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    s1 = repo.insert_signal("r1", _signal("A", 100))
    s2 = repo.insert_signal("r1", _signal("B", 100))
    repo.insert_ai_decision("r1", s1, "claude", False, "noise", 0.8, 900, None, input_tokens=1200, output_tokens=80)
    repo.insert_ai_decision("r1", s2, "claude", True, "fine", 0.6, 0, None)
    u = repo.ai_usage("r1")
    assert (u["calls"], u["input_tokens"], u["output_tokens"], u["decisions"]) == (1, 1200, 80, 2)
    assert u["avg_latency_ms"] == 900
    rej = repo.ai_rejected_signals("r1")
    assert [(r["symbol"], r["reason"]) for r in rej] == [("A", "noise")]


def test_positions_by_client_id(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 1, 100.0, 99.0, None, 5, "cid-a", "ema_rsi")
    pid = repo.insert_position("r1", p)
    repo.close_position(pid, 9, 101.0, "TARGET", 1.0, charges=None)
    m = repo.positions_by_client_id("r1")
    assert m["cid-a"]["pnl"] == 1.0


def _v1_db(path, extra_ddl=""):
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at INTEGER NOT NULL,
                           ended_at INTEGER, config_json TEXT NOT NULL);
        CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, signal_id INTEGER NOT NULL,
                                   filter_kind TEXT NOT NULL, approved INTEGER NOT NULL, reason TEXT NOT NULL,
                                   confidence REAL NOT NULL, latency_ms INTEGER NOT NULL, failure TEXT);
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                                product TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
                                client_id TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
                                stop REAL NOT NULL, target REAL, exit_ids_json TEXT NOT NULL DEFAULT '[]',
                                opened_at INTEGER NOT NULL, closed_at INTEGER, exit_price REAL, exit_reason TEXT,
                                pnl REAL, fill_status TEXT NOT NULL DEFAULT 'full', adopted INTEGER NOT NULL DEFAULT 0);
        PRAGMA user_version = 1;
    """ + extra_ddl)
    raw.commit()
    raw.close()


def test_migration_is_atomic_and_a_failed_one_leaves_the_old_version(tmp_path, monkeypatch):
    from tradebot.store import db as db_mod
    path = tmp_path / "v1.db"
    _v1_db(path)
    monkeypatch.setitem(db_mod.MIGRATIONS, 1, db_mod.MIGRATIONS[1] + ["ALTER TABLE nope ADD COLUMN x INTEGER"])
    with pytest.raises(sqlite3.OperationalError):
        connect(path)
    raw = sqlite3.connect(str(path))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "input_tokens" not in {r[1] for r in raw.execute("PRAGMA table_info(ai_decisions)")}
    raw.close()
    monkeypatch.undo()
    conn = connect(path)  # retry after the fix succeeds cleanly
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_missing_migration_entry_refuses_to_open(tmp_path, monkeypatch):
    from tradebot.store import db as db_mod
    path = tmp_path / "v1.db"
    _v1_db(path)
    monkeypatch.delitem(db_mod.MIGRATIONS, 1)
    with pytest.raises(SchemaVersionError, match="no migration defined"):
        connect(path)


def test_ai_usage_latency_includes_failed_calls(repo):
    repo.create_run("r1", "backtest", 0, "{}")
    s1 = repo.insert_signal("r1", _signal("A", 100))
    s2 = repo.insert_signal("r1", _signal("B", 200))
    repo.insert_ai_decision("r1", s1, "claude", True, "ok", 0.5, 400, None, input_tokens=1000)
    repo.insert_ai_decision("r1", s2, "claude", False, "ai_failure: timeout", 0.0, 60000, "timeout")
    s3 = repo.insert_signal("r1", _signal("C", 300))
    repo.insert_ai_decision("r1", s3, "claude", False, "ai_failure: max_calls", 0.0, 0, "max_calls_per_run")  # no call
    s4 = repo.insert_signal("r1", _signal("D", 400))
    repo.insert_ai_decision("r1", s4, "claude", True, "ok", 0.5, 300, None, cache_read_tokens=900)  # fully cached call
    u = repo.ai_usage("r1")
    assert u["calls"] == 2 and u["failures"] == 2 and u["avg_latency_ms"] == pytest.approx((400 + 60000 + 300) / 3)
