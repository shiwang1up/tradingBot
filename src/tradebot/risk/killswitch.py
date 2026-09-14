from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class KillState:
    active: bool   # file exists (or is unreadable): no new entries
    flatten: bool  # file mentions "flatten": also square off everything


def read_kill_switch(path: Union[str, Path]) -> KillState:
    """Safety interlock: never raises, and fails closed.

    A single read avoids the exists/read race. Any OS error other than "not there"
    (directory, permissions, I/O) blocks new entries rather than being ignored.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return KillState(False, False)
    except OSError:
        return KillState(True, False)
    return KillState(True, "flatten" in text.lower())
