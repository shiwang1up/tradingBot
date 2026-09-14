"""Groww instrument master: download, parse, and resolve universe symbols."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import requests

from tradebot.data.universe import Universe

INSTRUMENTS_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"


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


def download_instruments(dest: str | Path, url: str = INSTRUMENTS_URL) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def load_instruments(path: str | Path) -> dict[tuple[str, str], Instrument]:
    out: dict[tuple[str, str], Instrument] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            inst = Instrument(
                exchange=row["exchange"],
                trading_symbol=row["trading_symbol"],
                exchange_token=row["exchange_token"],
                segment=row["segment"],
                instrument_type=row["instrument_type"],
                lot_size=int(float(row["lot_size"] or 1)),
                tick_size=float(row["tick_size"] or 0.05),
                buy_allowed=_truthy(row.get("buy_allowed", "1")),
                sell_allowed=_truthy(row.get("sell_allowed", "1")),
            )
            out[(inst.exchange, inst.trading_symbol)] = inst
    return out


def resolve_universe(universe: Universe, instruments: dict[tuple[str, str], Instrument]
                     ) -> tuple[dict[str, Instrument], list[tuple[str, str]]]:
    """Return (resolved symbol -> Instrument, [(symbol, drop_reason)])."""
    resolved: dict[str, Instrument] = {}
    dropped: list[tuple[str, str]] = []
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
