"""SQLite connection factory. Applies schema.sql on every connect (idempotent)."""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    schema = resources.files("tradebot.store").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    return conn
