from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Union

import yaml


@dataclass(frozen=True)
class Universe:
    exchange: str
    symbols: tuple  # of upper-case trading symbols, de-duplicated, order preserved


def load_universe(path: Union[str, Path], as_of: Optional[date] = None) -> Universe:
    """Load universe.yaml.

    `as_of` is the hook for point-in-time constituents (spec 4.1). It is not implemented,
    so passing it raises rather than silently returning today's list.
    """
    if as_of is not None:
        raise NotImplementedError("point-in-time constituents are not implemented; omit as_of")
    p = Path(path)
    if not p.exists():
        raise ValueError(f"universe file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict) or "exchange" not in raw or "symbols" not in raw:
        raise ValueError(f"{p}: expected a mapping with 'exchange' and 'symbols' keys")
    if not isinstance(raw["symbols"], list) or not raw["symbols"]:
        raise ValueError(f"{p}: 'symbols' must be a non-empty list")
    seen: dict = {}
    for s in raw["symbols"]:
        sym = str(s).strip().upper()
        if sym:
            seen.setdefault(sym, None)
    return Universe(exchange=str(raw["exchange"]).strip().upper(), symbols=tuple(seen))
