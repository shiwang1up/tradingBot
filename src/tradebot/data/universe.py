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

    With `as_of`, returns the index constituents on that date instead of today's list. That
    needs a `membership:` key naming a timeline file, resolved relative to this file. Without
    it the call is unchanged, which is what every existing caller does.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"universe file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict) or "exchange" not in raw or "symbols" not in raw:
        raise ValueError(f"{p}: expected a mapping with 'exchange' and 'symbols' keys")
    if not isinstance(raw["symbols"], list) or not raw["symbols"]:
        raise ValueError(f"{p}: 'symbols' must be a non-empty list")
    exchange = str(raw["exchange"]).strip().upper()

    if as_of is not None:
        from tradebot.data.membership import constituents_on, load_timeline
        ref = raw.get("membership")
        if not ref:
            raise ValueError(
                f"{p}: as_of was given but the file has no 'membership:' key naming a "
                f"point-in-time timeline. Refusing to fall back to today's list, which "
                f"would reintroduce the survivorship bias as_of exists to remove.")
        return Universe(exchange=exchange,
                        symbols=constituents_on(load_timeline(p.parent / ref), as_of))

    seen: dict = {}
    for s in raw["symbols"]:
        sym = str(s).strip().upper()
        if sym:
            seen.setdefault(sym, None)
    return Universe(exchange=exchange, symbols=tuple(seen))
