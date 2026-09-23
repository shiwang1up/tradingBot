"""Named charge schedules, loaded from brokers.yaml.

The rates in this repository were typed from a spec and went unverified for the whole life of the
project, while every go/no-go decision rested on them. A schedule here must say where its numbers
came from and when someone looked: `verified_on` and `source` are mandatory, and a schedule without
them fails to load rather than quietly becoming another unsourced assumption.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Union

import yaml

from tradebot.config import ChargesConfig


class BrokerScheduleError(ValueError):
    """A brokers.yaml that is missing, malformed, or missing provenance."""


@dataclass(frozen=True)
class Broker:
    name: str
    verified_on: date
    source: str
    charges: ChargesConfig


_RATE_FIELDS = tuple(f.name for f in dataclasses.fields(ChargesConfig)
                     if f.name not in ("enabled", "broker"))


def load_brokers(path: Union[str, Path] = "brokers.yaml") -> Dict[str, Broker]:
    """{name: Broker} from a brokers.yaml. Raises BrokerScheduleError with the offending name."""
    p = Path(path)
    if not p.exists():
        raise BrokerScheduleError(f"broker schedules not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise BrokerScheduleError(f"{p}: expected a mapping of broker name to schedule")
    out: Dict[str, Broker] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise BrokerScheduleError(f"{p}: schedule '{name}' must be a mapping, got {entry!r}")
        for required in ("verified_on", "source"):
            if not entry.get(required):
                raise BrokerScheduleError(
                    f"{p}: schedule '{name}' is missing '{required}'. Every rate must be traceable "
                    f"to a page someone read on a date; that is the point of this file.")
        rates = {k: v for k, v in entry.items() if k not in ("verified_on", "source")}
        unknown = sorted(set(rates) - set(_RATE_FIELDS))
        if unknown:
            raise BrokerScheduleError(f"{p}: schedule '{name}' has unknown keys: {unknown}")
        verified = entry["verified_on"]
        if not isinstance(verified, date):
            raise BrokerScheduleError(
                f"{p}: schedule '{name}' verified_on must be an unquoted ISO date, got {verified!r}")
        out[name] = Broker(name=name, verified_on=verified, source=str(entry["source"]),
                           charges=ChargesConfig(**{k: float(v) for k, v in rates.items()}))
    return out
