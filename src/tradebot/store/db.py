"""SQLite connection factory. Applies schema.sql on every connect (idempotent) and
refuses to open a database written by a different schema version."""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    pass


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
    if found not in (0, SCHEMA_VERSION):
        conn.close()
        raise SchemaVersionError(
            f"{path} has schema version {found}, code expects {SCHEMA_VERSION}; migrate or delete the file"
        )
    schema = resources.files("tradebot.store").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
