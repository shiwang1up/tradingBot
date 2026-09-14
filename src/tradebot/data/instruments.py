"""Groww instrument master: download, parse, and resolve universe symbols."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import requests

from tradebot.data.universe import Universe

INSTRUMENTS_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
REQUIRED_COLUMNS = frozenset({
    "exchange", "exchange_token", "trading_symbol", "segment", "instrument_type",
    "lot_size", "tick_size", "buy_allowed", "sell_allowed",
})


@dataclass(frozen=True)
class Instrument:
    exchange: str
    trading_symbol: str
    exchange_token: str
    segment: str
    instrument_type: str
    lot_size: int
    tick_size: float
    buy_allowed: bool
    sell_allowed: bool


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "y", "yes")


def download_instruments(dest: Union[str, Path], url: str = INSTRUMENTS_URL) -> Path:
    """Download the master atomically: a failed or truncated download never replaces a good cache."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    head = resp.content[:4096].decode("utf-8-sig", errors="replace").splitlines()[:1]
    if not head or "trading_symbol" not in head[0]:
        raise ValueError(f"instrument download from {url} does not look like the instrument CSV")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    os.replace(tmp, dest)
    return dest


def _num(row: dict, col: str, cast, default, path: Path):
    raw = (row.get(col) or "").strip()
    if raw == "":
        return default
    try:
        return cast(float(raw))
    except ValueError as e:
        raise ValueError(f"{path}: bad {col}={raw!r} for {row.get('trading_symbol')!r}") from e


def load_instruments(path: Union[str, Path]) -> dict:
    """Return {(exchange, trading_symbol): Instrument}. Fails loudly on schema drift."""
    path = Path(path)
    out: dict = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: instrument CSV missing columns {sorted(missing)}")
        for row in reader:
            inst = Instrument(
                exchange=row["exchange"].strip().upper(),
                trading_symbol=row["trading_symbol"].strip().upper(),
                exchange_token=row["exchange_token"].strip(),
                segment=row["segment"].strip().upper(),
                instrument_type=row["instrument_type"].strip().upper(),
                lot_size=_num(row, "lot_size", int, 1, path),
                tick_size=_num(row, "tick_size", float, 0.05, path),
                buy_allowed=_truthy(row["buy_allowed"]),    # empty cell => not tradeable (fail closed)
                sell_allowed=_truthy(row["sell_allowed"]),
            )
            out[(inst.exchange, inst.trading_symbol)] = inst
    return out


def resolve_universe(universe: Universe, instruments: dict) -> tuple:
    """Return (resolved symbol -> Instrument, [(symbol, drop_reason)])."""
    resolved: dict = {}
    dropped: list = []
    for sym in universe.symbols:
        inst = instruments.get((universe.exchange, sym))
        if inst is None:
            dropped.append((sym, "not_found"))
        elif inst.segment != "CASH" or inst.instrument_type != "EQ":
            dropped.append((sym, "not_cash_equity"))
        elif not (inst.buy_allowed and inst.sell_allowed):
            dropped.append((sym, "not_tradeable"))
        else:
            resolved[sym] = inst
    return resolved, dropped
