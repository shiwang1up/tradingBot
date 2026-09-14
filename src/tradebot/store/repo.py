"""All SQL lives here. Every method takes and returns plain types or sqlite3.Row."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from tradebot.types import Candle, Position, Signal


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- runs --------------------------------------------------------------
    def create_run(self, run_id: str, mode: str, started_at: int, config_json: str) -> None:
        self.conn.execute(
            "INSERT INTO runs(run_id, mode, started_at, config_json) VALUES (?,?,?,?)",
            (run_id, mode, started_at, config_json),
        )
        self.conn.commit()

    def end_run(self, run_id: str, ended_at: int) -> None:
        self.conn.execute("UPDATE runs SET ended_at=? WHERE run_id=?", (ended_at, run_id))
        self.conn.commit()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    # -- candles -----------------------------------------------------------
    def insert_candles(self, candles: Iterable[Candle], interval: int) -> int:
        rows = [(c.symbol, c.ts, interval, c.open, c.high, c.low, c.close, c.volume, c.source) for c in candles]
        # total_changes is connection-wide; correct here because Repo is single-threaded and
        # nothing else runs between the two reads. Cursor.rowcount would count ignored rows too.
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO candles(symbol, ts, interval, o, h, l, c, v, source) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def delete_candles(self, symbol: str, interval: int, start_ts: int, end_ts: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM candles WHERE symbol=? AND interval=? AND ts BETWEEN ? AND ?",
            (symbol, interval, start_ts, end_ts),
        )
        self.conn.commit()
        return cur.rowcount

    def latest_candle_ts(self, symbol: str, interval: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM candles WHERE symbol=? AND interval=?", (symbol, interval)
        ).fetchone()
        return row["ts"]

    def load_candles(self, symbols: list[str], interval: int, start_ts: int, end_ts: int) -> list[Candle]:
        # One bind variable per symbol; SQLite >= 3.32 allows 32766, older builds 999. NIFTY 200 fits either.
        if len(symbols) > 900:
            raise ValueError("load_candles: more than 900 symbols; chunk the universe")
        marks = ",".join("?" * len(symbols))
        rows = self.conn.execute(
            f"SELECT * FROM candles WHERE symbol IN ({marks}) AND interval=? AND ts BETWEEN ? AND ? "
            "ORDER BY ts, symbol",
            (*symbols, interval, start_ts, end_ts),
        ).fetchall()
        return [Candle(r["symbol"], r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"], r["source"]) for r in rows]

    # -- signals & decisions ----------------------------------------------
    def insert_signal(self, run_id: str, s: Signal) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals(run_id, strategy, symbol, bar_ts, direction, entry, stop, target, product) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, s.strategy, s.symbol, s.bar_ts, s.direction, s.entry_price, s.stop_price, s.target_price, s.product),
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_risk_decision(self, run_id: str, signal_id: int, approved: bool, reason: str, quantity: int) -> None:
        self.conn.execute(
            "INSERT INTO risk_decisions(run_id, signal_id, approved, reason, quantity) VALUES (?,?,?,?,?)",
            (run_id, signal_id, int(approved), reason, quantity),
        )
        self.conn.commit()

    def insert_ai_decision(self, run_id: str, signal_id: int, filter_kind: str, approved: bool, reason: str,
                           confidence: float, latency_ms: int, failure: str | None) -> None:
        self.conn.execute(
            "INSERT INTO ai_decisions(run_id, signal_id, filter_kind, approved, reason, confidence, latency_ms, failure) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, signal_id, filter_kind, int(approved), reason, confidence, latency_ms, failure),
        )
        self.conn.commit()

    def rejection_counts(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS n FROM risk_decisions WHERE run_id=? AND approved=0 GROUP BY reason",
            (run_id,),
        ).fetchall()
        return {r["reason"]: r["n"] for r in rows}

    def ai_rejection_count(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM ai_decisions WHERE run_id=? AND approved=0", (run_id,)
        ).fetchone()
        return row["n"]

    # -- orders & fills ----------------------------------------------------
    def insert_order(self, run_id: str, client_id: str, signal_id: int | None, kind: str, side: str, qty: int,
                     limit_price: float | None, status: str, placed_at: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO orders(run_id, client_id, signal_id, kind, side, qty, limit_price, status, placed_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, client_id, signal_id, kind, side, qty, limit_price, status, placed_at, placed_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_order(self, run_id: str, client_id: str, status: str, updated_at: int) -> None:
        self.conn.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE run_id=? AND client_id=?",
            (status, updated_at, run_id, client_id),
        )
        self.conn.commit()

    def order_status(self, run_id: str, client_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM orders WHERE run_id=? AND client_id=?", (run_id, client_id)
        ).fetchone()
        return row["status"] if row else None

    def order_id(self, run_id: str, client_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM orders WHERE run_id=? AND client_id=?", (run_id, client_id)
        ).fetchone()
        return row["id"] if row else None

    def insert_fill(self, order_id: int, qty: int, price: float, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO fills(order_id, qty, price, ts) VALUES (?,?,?,?)", (order_id, qty, price, ts)
        )
        self.conn.commit()

    # -- positions ---------------------------------------------------------
    def insert_position(self, run_id: str, p: Position) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions(run_id, symbol, product, direction, strategy, client_id, qty, avg_price, stop, target, "
            "opened_at, fill_status, adopted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, p.symbol, p.product, p.direction, p.strategy, p.client_id, p.quantity, p.avg_price,
             p.stop_price, p.target_price, p.opened_ts, p.fill_status, int(p.adopted)),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_position(self, position_id: int, closed_ts: int, exit_price: float, exit_reason: str, pnl: float) -> None:
        self.conn.execute(
            "UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl=? WHERE id=?",
            (closed_ts, exit_price, exit_reason, pnl, position_id),
        )
        self.conn.commit()

    def list_positions(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE run_id=? ORDER BY opened_at, symbol", (run_id,)
        ).fetchall()

    # -- daily pnl ---------------------------------------------------------
    def upsert_daily_pnl(self, run_id: str, date: str, realised: float, unrealised: float,
                         fills: int, entries_placed: int) -> None:
        fill_rate = fills / entries_placed if entries_placed else 0.0
        self.conn.execute(
            "INSERT INTO daily_pnl(run_id, date, realised, unrealised, fills, entries_placed, fill_rate) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id, date) DO UPDATE SET realised=excluded.realised, "
            "unrealised=excluded.unrealised, fills=excluded.fills, entries_placed=excluded.entries_placed, "
            "fill_rate=excluded.fill_rate",
            (run_id, date, realised, unrealised, fills, entries_placed, fill_rate),
        )
        self.conn.commit()

    def daily_pnl(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM daily_pnl WHERE run_id=? ORDER BY date", (run_id,)
        ).fetchall()
