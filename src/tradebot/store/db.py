"""SQLite connection factory. Applies schema.sql on every connect (idempotent) and
refuses to open a database written by a different schema version."""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 2


class SchemaVersionError(RuntimeError):
    pass


# Forward migrations keyed by the version they upgrade FROM. Each list runs inside executescript.
MIGRATIONS = {
    1: [
        "ALTER TABLE ai_decisions ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_decisions ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_decisions ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_decisions ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0",
    ],
}


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    is_file = str(path) != ":memory:"
    if is_file:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if is_file:
        conn.execute("PRAGMA journal_mode = WAL")
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found > SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"{path} has schema version {found}, code expects {SCHEMA_VERSION}; upgrade the code or delete the file"
        )
    if 0 < found < SCHEMA_VERSION:  # a fresh database (0) is created at the current version by schema.sql
        for v in range(found, SCHEMA_VERSION):
            for stmt in MIGRATIONS.get(v, []):
                conn.execute(stmt)
        conn.commit()
    schema = resources.files("tradebot.store").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
