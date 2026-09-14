from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KillState:
    active: bool   # file exists: no new entries
    flatten: bool  # file contains "flatten": also square off everything


def read_kill_switch(path: str | Path) -> KillState:
    p = Path(path)
    if not p.exists():
        return KillState(False, False)
    return KillState(True, p.read_text().strip().lower() == "flatten")
