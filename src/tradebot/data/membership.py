"""Point-in-time index membership, reconstructed backward from a published anchor.

NSE publishes index CHANGES, not snapshots, so the only trustworthy starting point is
today's constituent list. `constituents_on(timeline, d)` walks the change events from the
anchor back to `d`, undoing each one.

The index holds a fixed number of names -- 200 for NIFTY 200 -- so the count after every
undo step is a free correctness check. A missed, misparsed or double-applied release
breaks it, and the error names the date and the source URL that broke it.
"""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import yaml


@dataclass(frozen=True)
class Event:
    effective: date         # first trading date on which the new list applies
    source: str             # URL of the NSE release this came from
    include: Tuple[str, ...]
    exclude: Tuple[str, ...]


@dataclass(frozen=True)
class Rename:
    old: str
    new: str
    effective: date
    kind: str               # "rename" or "merger"
    source: str


@dataclass(frozen=True)
class Timeline:
    index: str
    size: int                       # the invariant: how many names the index always holds
    anchor_as_of: date
    anchor_source: str
    anchor: Tuple[str, ...]
    events: Tuple[Event, ...]       # newest first
    renames: Tuple[Rename, ...]


def _sym(x):
    return str(x).strip().upper()


def _date(x, what):
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x))
    except ValueError:
        raise ValueError("%s: expected an ISO date, got %r" % (what, x))


def load_timeline(path):
    """Read and validate the timeline file. Raises ValueError on anything that would make
    a reconstructed date silently wrong."""
    p = Path(path)
    if not p.exists():
        raise ValueError("membership timeline not found: %s" % p)
    raw = yaml.safe_load(p.read_text()) or {}
    for key in ("index", "anchor"):
        if key not in raw:
            raise ValueError("%s: missing top-level key %r" % (p, key))
    a = raw["anchor"]
    for key in ("as_of", "source", "symbols", "size"):
        if key not in a:
            raise ValueError("%s: anchor is missing %r" % (p, key))

    symbols = [_sym(s) for s in a["symbols"]]
    dupes = sorted(set(s for s in symbols if symbols.count(s) > 1))
    if dupes:
        raise ValueError("%s: anchor lists duplicate symbols: %s" % (p, ", ".join(dupes)))
    size = int(a["size"])
    if len(symbols) != size:
        raise ValueError("%s: anchor declares size %d but lists %d symbols"
                         % (p, size, len(symbols)))

    events = []
    for e in (raw.get("events") or []):
        events.append(Event(
            effective=_date(e["effective"], "%s: event effective" % p),
            source=str(e.get("source", "")),
            include=tuple(_sym(s) for s in (e.get("include") or [])),
            exclude=tuple(_sym(s) for s in (e.get("exclude") or []))))
    events.sort(key=lambda e: e.effective, reverse=True)

    renames = []
    for r in (raw.get("renames") or []):
        renames.append(Rename(
            old=_sym(r["from"]), new=_sym(r["to"]),
            effective=_date(r["effective"], "%s: rename effective" % p),
            kind=str(r.get("kind", "rename")), source=str(r.get("source", ""))))

    return Timeline(index=str(raw["index"]), size=size,
                    anchor_as_of=_date(a["as_of"], "%s: anchor as_of" % p),
                    anchor_source=str(a["source"]), anchor=tuple(symbols),
                    events=tuple(events), renames=tuple(renames))
