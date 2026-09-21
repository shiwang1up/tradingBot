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
from typing import Optional, Tuple

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
    kind: str               # "rename" (symbol changed, ISIN survived) or "merger" (ISIN gone)
    source: str
    isin: Optional[str] = None


@dataclass(frozen=True)
class Timeline:
    index: str
    size: int                       # the invariant: how many names the index always holds
    covers_from: date          # every change effective on or after this date is recorded
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
    for key in ("index", "covers_from", "anchor"):
        if key not in raw:
            raise ValueError("%s: missing top-level key %r" % (p, key))
    covers_from = _date(raw["covers_from"], "%s: covers_from" % p)
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
    if events and events[-1].effective < covers_from:
        raise ValueError(
            "%s: covers_from is %s but an event is effective %s, before it. Either the "
            "coverage claim is too late or that event does not belong here."
            % (p, covers_from, events[-1].effective))

    renames = []
    for r in (raw.get("renames") or []):
        renames.append(Rename(
            old=_sym(r["from"]), new=_sym(r["to"]),
            effective=_date(r["effective"], "%s: rename effective" % p),
            kind=str(r.get("kind", "rename")), source=str(r.get("source", "")),
            isin=(str(r["isin"]).strip() if r.get("isin") else None)))

    return Timeline(index=str(raw["index"]), size=size, covers_from=covers_from,
                    anchor_as_of=_date(a["as_of"], "%s: anchor as_of" % p),
                    anchor_source=str(a["source"]), anchor=tuple(symbols),
                    events=tuple(events), renames=tuple(renames))


class MembershipError(Exception):
    """The timeline cannot answer for this date: a release is missing, misparsed, or the
    date is outside the reconstructable range."""


def constituents_on(timeline, as_of):
    """The index members on `as_of`, as a sorted tuple.

    Walks backward from the anchor, undoing every event effective AFTER `as_of`: the names
    that event brought in are removed, and the names it took out are put back. The count is
    checked after each undo, because the index always holds exactly `timeline.size` names
    and anything else means the timeline is wrong rather than the market.
    """
    if as_of > date.today():
        raise MembershipError("as_of %s is in the future" % as_of)
    if as_of >= timeline.anchor_as_of:
        return tuple(sorted(timeline.anchor))

    if as_of < timeline.covers_from:
        raise MembershipError(
            "as_of %s is before %s, the date this timeline's event record is complete from. "
            "Earlier dates are not merely unrecorded but unknowable: an index change may sit "
            "in the gap. Add the releases covering that period to extend coverage."
            % (as_of, timeline.covers_from))

    members = set(timeline.anchor)
    for e in timeline.events:                      # newest first
        if e.effective <= as_of:
            break
        members -= set(e.include)
        members |= set(e.exclude)
        if len(members) != timeline.size:
            raise MembershipError(
                "membership count is %d, expected %d, after undoing the event effective "
                "%s from %s. A release is missing, misparsed, or applied twice."
                % (len(members), timeline.size, e.effective, e.source or "(no source)"))

    for r in timeline.renames:                     # undo renames across the same span
        if r.effective > as_of and r.new in members:
            members.discard(r.new)
            members.add(r.old)

    return tuple(sorted(members))
