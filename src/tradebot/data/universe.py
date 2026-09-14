from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Universe:
    exchange: str
    symbols: tuple[str, ...]


def load_universe(path: str | Path, as_of=None) -> Universe:
    """Load universe.yaml. `as_of` is reserved for point-in-time constituents and ignored for now."""
    raw = yaml.safe_load(Path(path).read_text())
    return Universe(exchange=str(raw["exchange"]), symbols=tuple(str(s) for s in raw["symbols"]))
