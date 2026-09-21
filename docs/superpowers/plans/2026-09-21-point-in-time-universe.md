# Point-in-time universe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `load_universe(path, as_of=date)` return the true NIFTY 200 constituents on any date from 2020-01-01, with daily candles for every name that was ever in the index and a slippage estimate that varies by each name's own liquidity.

**Architecture:** A membership timeline anchored on today's published NIFTY 200 list and replayed BACKWARD through dated NSE press-release events, because NSE publishes changes rather than snapshots. The index always holds exactly 200 names, so a count check after every undo step detects a missed or misparsed release and names the date that broke. Slippage comes from each name's own median traded value, which the database already stores.

**Tech Stack:** Python 3.9 (no `X | None`, no builtin generics, no `match`, no `from __future__ import annotations`), PyYAML, sqlite3, pytest, curl for NSE fetches.

**Spec:** `docs/superpowers/specs/2026-09-21-point-in-time-universe-design.md`

**Baseline:** `.venv/bin/pytest -q` from the repo root gives `629 passed`.

**Branch:** `dev-nonprice-signals`, already checked out.

---

## Standing constraints for every task

- **Never write to anything under `data/`** except the candle fetch in Task 8, which takes a backup first. Read the database only via `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`.
- **Do not run** `tradebot backtest`, `tradebot paper`, `scripts/orb_experiment.py`, or `scripts/daily_screen.py`. Two backtest holdout windows are deliberately unspent.
- Python 3.9 only. Use `Optional[X]`, `List[str]`, `Dict[str, int]` from `typing`.
- Use `.venv/bin/pytest` and `.venv/bin/python`.
- End every commit message with, on its own last line:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Report the REAL test count. If it differs from the plan, say so; the plan's arithmetic may be wrong and that is the planner's error to fix, not the implementer's to hide.

## File structure

| File | Responsibility |
|---|---|
| `src/tradebot/data/membership.py` | Load the timeline, replay backward, enforce the count invariant. New. |
| `src/tradebot/data/universe.py` | `load_universe` gains a working `as_of`; delegates to `membership`. Modify. |
| `src/tradebot/data/liquidity.py` | Median traded value → slippage tier. New. |
| `scripts/build_membership.py` | Fetch the anchor, parse NSE release PDFs, emit the timeline. New. |
| `index_membership.yaml` | The timeline itself, at the repo root beside `universe.yaml`. New, committed. |
| `tests/test_membership.py` | Unit tests for replay, invariant, renames, ISIN. New. |
| `tests/test_liquidity.py` | Unit tests for the tiers, at boundaries. New. |
| `tests/test_membership_real.py` | Integration: every date yields 200. New, added in Task 7. |

Test counts: Task 1 +6, Task 2 +6, Task 3 +5, Task 4 +6, Task 5 +5, Task 6 (no tests, data only), Task 7 +3, Task 8 (no tests). From 629: 635, 641, 646, 652, 657, 657, 660, 660.

---

### Task 1: the timeline file format and its loader

**Files:**
- Create: `src/tradebot/data/membership.py`
- Test: `tests/test_membership.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_membership.py`:

```python
from datetime import date
from pathlib import Path

import pytest

from tradebot.data.membership import Timeline, load_timeline

SAMPLE = """
index: TEST 4
anchor:
  as_of: 2024-01-01
  source: https://example.test/list.csv
  fetched: 2024-01-01
  size: 4
  symbols: [AAA, BBB, CCC, DDD]
events:
  - effective: 2023-07-01
    source: https://example.test/jul2023.pdf
    include: [DDD]
    exclude: [EEE]
  - effective: 2023-01-01
    source: https://example.test/jan2023.pdf
    include: [CCC]
    exclude: [FFF]
renames: []
"""


def _write(tmp_path, text=SAMPLE):
    p = tmp_path / "membership.yaml"
    p.write_text(text)
    return p


def test_load_timeline_reads_the_anchor(tmp_path):
    t = load_timeline(_write(tmp_path))
    assert isinstance(t, Timeline)
    assert t.index == "TEST 4"
    assert t.size == 4
    assert t.anchor_as_of == date(2024, 1, 1)
    assert t.anchor == ("AAA", "BBB", "CCC", "DDD")


def test_events_are_sorted_newest_first_whatever_the_file_order(tmp_path):
    """The replay walks newest first. Relying on the file being written in that order
    would make a hand-edited timeline silently wrong."""
    scrambled = SAMPLE.replace(
        "  - effective: 2023-07-01\n"
        "    source: https://example.test/jul2023.pdf\n"
        "    include: [DDD]\n"
        "    exclude: [EEE]\n"
        "  - effective: 2023-01-01\n"
        "    source: https://example.test/jan2023.pdf\n"
        "    include: [CCC]\n"
        "    exclude: [FFF]\n",
        "  - effective: 2023-01-01\n"
        "    source: https://example.test/jan2023.pdf\n"
        "    include: [CCC]\n"
        "    exclude: [FFF]\n"
        "  - effective: 2023-07-01\n"
        "    source: https://example.test/jul2023.pdf\n"
        "    include: [DDD]\n"
        "    exclude: [EEE]\n")
    t = load_timeline(_write(tmp_path, scrambled))
    assert [e.effective for e in t.events] == [date(2023, 7, 1), date(2023, 1, 1)]


def test_symbols_are_upper_cased_and_stripped(tmp_path):
    t = load_timeline(_write(tmp_path, SAMPLE.replace("[AAA, BBB, CCC, DDD]",
                                                      "[' aaa ', BBB, CCC, DDD]")))
    assert t.anchor[0] == "AAA"


def test_anchor_size_must_match_the_declared_size(tmp_path):
    """The declared size is what the invariant checks against. If the anchor itself does
    not match it, every reconstructed date is wrong and nothing downstream can tell."""
    bad = SAMPLE.replace("size: 4", "size: 5")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, bad))
    assert "5" in str(e.value) and "4" in str(e.value)


def test_duplicate_symbols_in_the_anchor_are_rejected(tmp_path):
    bad = SAMPLE.replace("[AAA, BBB, CCC, DDD]", "[AAA, AAA, CCC, DDD]")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, bad))
    assert "AAA" in str(e.value)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(ValueError) as e:
        load_timeline(tmp_path / "nope.yaml")
    assert "not found" in str(e.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_membership.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.data.membership'`

- [ ] **Step 3: Write the implementation**

Create `src/tradebot/data/membership.py`:

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_membership.py -q` → `6 passed`.
Then `.venv/bin/pytest -q` → `635 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/data/membership.py tests/test_membership.py
git commit -m "data(membership): timeline file format and a validating loader"
```

---

### Task 2: backward replay and the count invariant

> **Amended 2026-09-21, after the implementer reported a contradiction.** This task's
> original tests and code disagreed on the range boundary: the test expected a date one day
> before the earliest event to resolve, the code raised for it. The test was right and the
> spec sentence was wrong -- but the obvious repair, allowing exactly one day of reach, is
> also wrong. The real file's first recorded review is around 2020-03-31 and it must still
> answer for 2020-01-01, which it legitimately can, since NSE reviews semi-annually and
> nothing changed in the gap. What bounds the answer is whether the event record is COMPLETE
> back to the queried date, which is a property of how the file was compiled rather than
> anything the events imply. The timeline therefore carries a required `covers_from` key and
> `constituents_on` refuses dates before it. See spec section 6.

**Files:**
- Modify: `src/tradebot/data/membership.py`
- Test: `tests/test_membership.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_membership.py`:

```python
from tradebot.data.membership import MembershipError, constituents_on


def test_a_date_at_or_after_the_anchor_returns_the_anchor(tmp_path):
    t = load_timeline(_write(tmp_path))
    assert constituents_on(t, date(2024, 1, 1)) == ("AAA", "BBB", "CCC", "DDD")
    assert constituents_on(t, date(2025, 6, 1)) == ("AAA", "BBB", "CCC", "DDD")


def test_replay_undoes_one_event(tmp_path):
    """Between the two events: the July change has been undone, the January one has not.
    DDD came in and EEE went out in July, so before July it is EEE that is a member."""
    t = load_timeline(_write(tmp_path))
    assert constituents_on(t, date(2023, 3, 1)) == ("AAA", "BBB", "CCC", "EEE")


def test_replay_undoes_every_event_before_the_earliest(tmp_path):
    t = load_timeline(_write(tmp_path))
    assert constituents_on(t, date(2022, 12, 31)) == ("AAA", "BBB", "EEE", "FFF")


def test_a_broken_count_raises_and_names_the_date_and_source(tmp_path):
    """An event that removes two and adds one leaves the index one short. That means a
    release was missed or misparsed, and the error must say which."""
    bad = SAMPLE.replace("    include: [DDD]\n    exclude: [EEE]\n",
                         "    include: [DDD]\n    exclude: [EEE, GGG]\n")
    t = load_timeline(_write(tmp_path, bad))
    with pytest.raises(MembershipError) as e:
        constituents_on(t, date(2023, 3, 1))
    msg = str(e.value)
    assert "2023-07-01" in msg
    assert "jul2023.pdf" in msg
    assert "5" in msg and "4" in msg


def test_a_date_before_the_earliest_event_raises(tmp_path):
    """The oldest reconstructed list is only valid back to the earliest event. Returning it
    for any earlier date would silently claim knowledge the timeline does not have."""
    t = load_timeline(_write(tmp_path))
    with pytest.raises(MembershipError) as e:
        constituents_on(t, date(2020, 1, 1))
    assert "2023-01-01" in str(e.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_membership.py -q`
Expected: `ImportError: cannot import name 'MembershipError'`

- [ ] **Step 3: Write the implementation**

Add to `src/tradebot/data/membership.py`, after `load_timeline`:

```python
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

    oldest = timeline.events[-1].effective if timeline.events else timeline.anchor_as_of
    if as_of < oldest:
        raise MembershipError(
            "as_of %s is before the earliest event %s; the timeline cannot reach it. "
            "Add the releases covering that period, or query a later date."
            % (as_of, oldest))

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
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_membership.py -q` → `11 passed`.
Then `.venv/bin/pytest -q` → `640 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/data/membership.py tests/test_membership.py
git commit -m "data(membership): backward replay with a count invariant per undo step"
```

---

### Task 3: renames, ISIN, and mergers

**Files:**
- Modify: `src/tradebot/data/membership.py`
- Test: `tests/test_membership.py`

The anchor CSV carries an ISIN per name. ISIN survives a ticker rename where the symbol does
not, so where a name's ISIN is known it is the better key. A merger is different: the ISIN
disappears rather than changing, so a merged-away name must still be reachable by symbol.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_membership.py`:

```python
RENAMED = SAMPLE.replace("renames: []", """renames:
  - from: OLDNAME
    to: DDD
    effective: 2023-09-01
    kind: rename
    source: https://example.test/rename.pdf
""")


def test_a_rename_resolves_to_the_old_symbol_before_its_effective_date(tmp_path):
    """DDD was called OLDNAME until 2023-09-01. A screen asking for August 2023 needs
    OLDNAME, because that is the symbol the candles are stored under."""
    t = load_timeline(_write(tmp_path, RENAMED))
    after = constituents_on(t, date(2023, 10, 1))
    assert "DDD" in after and "OLDNAME" not in after


def test_a_rename_is_not_applied_after_its_effective_date(tmp_path):
    t = load_timeline(_write(tmp_path, RENAMED))
    assert "DDD" in constituents_on(t, date(2024, 1, 1))


def test_renames_do_not_change_the_member_count(tmp_path):
    """A rename swaps one symbol for another. If it ever changes the count, the rename
    table has an entry that is really an inclusion or exclusion in disguise."""
    t = load_timeline(_write(tmp_path, RENAMED))
    for d in (date(2023, 3, 1), date(2023, 10, 1), date(2024, 1, 1)):
        assert len(constituents_on(t, d)) == 4


def test_a_merger_is_recorded_with_its_kind(tmp_path):
    merged = SAMPLE.replace("renames: []", """renames:
  - from: HDFC
    to: HDFCBANK
    effective: 2023-07-13
    kind: merger
    source: https://example.test/merger.pdf
""")
    t = load_timeline(_write(tmp_path, merged))
    assert t.renames[0].kind == "merger"
    assert t.renames[0].old == "HDFC"
```

- [ ] **Step 2: Run them — expect the four rename tests to PASS already**

Run: `.venv/bin/pytest tests/test_membership.py -q`

Expected: the four rename tests pass, and only `test_rename_carries_an_isin` (added in Step 3
below) fails. **This is deliberate and is not a TDD violation to hide.** Task 2's rename loop
already implements the behaviour correctly; these four are CHARACTERIZATION tests that lock it
in so a later refactor cannot quietly break it. Writing them as if they were driving new code
would be a lie about what the task does.

Verify they are not vacuous before accepting them: temporarily change the rename condition in
`constituents_on` from `r.effective > as_of` to `r.effective < as_of`, confirm
`test_a_rename_resolves_to_the_old_symbol_before_its_effective_date` fails, then revert. Report
that you did this. A characterization test that cannot fail is worse than no test, because it
reads like coverage.

- [ ] **Step 3: Add the ISIN field — this is the task's real new behaviour**

Add this test to `tests/test_membership.py`:

```python
def test_rename_carries_an_isin(tmp_path):
    """ISIN survives a ticker rename where the symbol does not, so where the anchor gives
    one it is the better key. Absent is allowed: press releases often give only a symbol."""
    with_isin = SAMPLE.replace("renames: []", """renames:
  - from: OLDNAME
    to: DDD
    effective: 2023-09-01
    kind: rename
    source: https://example.test/rename.pdf
    isin: INE123A01010
""")
    t = load_timeline(_write(tmp_path, with_isin))
    assert t.renames[0].isin == "INE123A01010"
    plain = load_timeline(_write(tmp_path, RENAMED))
    assert plain.renames[0].isin is None
```

Run it and confirm it fails with `AttributeError: 'Rename' object has no attribute 'isin'`.
Then change `Rename` to:

```python
@dataclass(frozen=True)
class Rename:
    old: str
    new: str
    effective: date
    kind: str               # "rename" (symbol changed, ISIN survived) or "merger" (ISIN gone)
    source: str
    isin: Optional[str] = None
```

and in `load_timeline`'s rename loop pass `isin=(str(r["isin"]).strip() if r.get("isin") else None)`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_membership.py -q` → `16 passed`.
Then `.venv/bin/pytest -q` → `645 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/data/membership.py tests/test_membership.py
git commit -m "data(membership): renames carry an ISIN and never change the member count"
```

---

### Task 4: `as_of` in `load_universe`

**Files:**
- Modify: `src/tradebot/data/universe.py`
- Test: `tests/test_universe_instruments.py`

`load_universe` currently raises `NotImplementedError` when `as_of` is passed. It now
delegates to the timeline. The existing signature and the `Universe` dataclass do not change,
so every current caller keeps working.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_universe_instruments.py`:

```python
from tradebot.data.membership import MembershipError

TIMELINE = """
index: TEST 3
anchor:
  as_of: 2024-01-01
  source: https://example.test/list.csv
  fetched: 2024-01-01
  size: 3
  symbols: [AAA, BBB, CCC]
events:
  - effective: 2023-06-01
    source: https://example.test/jun2023.pdf
    include: [CCC]
    exclude: [ZZZ]
renames: []
"""


def test_load_universe_without_as_of_is_unchanged(tmp_path):
    """Every existing caller passes no as_of and must keep getting today's file verbatim."""
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - RELIANCE\n  - TCS\n")
    assert load_universe(p) == Universe(exchange="NSE", symbols=("RELIANCE", "TCS"))


def test_load_universe_with_as_of_returns_point_in_time_members(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - AAA\n  - BBB\n  - CCC\n"
                 "membership: membership.yaml\n")
    (tmp_path / "membership.yaml").write_text(TIMELINE)
    u = load_universe(p, as_of=date(2023, 1, 15))
    assert u.symbols == ("AAA", "BBB", "ZZZ")
    assert u.exchange == "NSE"


def test_as_of_without_a_membership_file_raises_clearly(tmp_path):
    """Silently falling back to today's list is the exact bug this whole change exists to
    remove, so the absence of a timeline must be loud."""
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - RELIANCE\n")
    with pytest.raises(ValueError) as e:
        load_universe(p, as_of=date(2023, 1, 15))
    assert "membership" in str(e.value).lower()


def test_as_of_outside_the_timeline_propagates_the_membership_error(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - AAA\nmembership: membership.yaml\n")
    (tmp_path / "membership.yaml").write_text(TIMELINE)
    with pytest.raises(MembershipError):
        load_universe(p, as_of=date(2019, 1, 1))


def test_membership_path_is_resolved_next_to_the_universe_file(tmp_path):
    """So that a universe file can be loaded from any working directory."""
    sub = tmp_path / "cfg"
    sub.mkdir()
    p = sub / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - AAA\nmembership: membership.yaml\n")
    (sub / "membership.yaml").write_text(TIMELINE)
    assert load_universe(p, as_of=date(2023, 12, 1)).symbols == ("AAA", "BBB", "CCC")


def test_as_of_none_does_not_require_a_membership_file(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text("exchange: NSE\nsymbols:\n  - AAA\nmembership: missing.yaml\n")
    assert load_universe(p, as_of=None).symbols == ("AAA",)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_universe_instruments.py -q`
Expected: FAIL with `NotImplementedError: point-in-time constituents are not implemented; omit as_of`

- [ ] **Step 3: Write the implementation**

Replace the body of `load_universe` in `src/tradebot/data/universe.py`. Keep the module's
existing `from __future__ import annotations` — that file already has it and it is fine there.

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_universe_instruments.py -q` → all pass.
Then `.venv/bin/pytest -q` → `651 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/data/universe.py tests/test_universe_instruments.py
git commit -m "data(universe): as_of returns point-in-time members, or raises"
```

---

### Task 5: liquidity-tiered slippage

**Files:**
- Create: `src/tradebot/data/liquidity.py`
- Test: `tests/test_liquidity.py`

Spec section 5. At ₹10,000 a position a trade is about 0.00065% of daily turnover even in the
thinnest current name, so this models the BID-ASK SPREAD, not market impact. Turnover is a
proxy for spread, not a measure of it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_liquidity.py`:

```python
import sqlite3
from datetime import date

import pytest

from tradebot.data.liquidity import TIERS, median_traded_value, slippage_for, slippage_pct_for_value


def test_each_tier_boundary_maps_as_specified():
    """Boundaries are inclusive at the bottom of each band: exactly 500 cr is the cheap tier."""
    cr = 1e7
    assert slippage_pct_for_value(600 * cr) == 0.05
    assert slippage_pct_for_value(500 * cr) == 0.05
    assert slippage_pct_for_value(499 * cr) == 0.08
    assert slippage_pct_for_value(100 * cr) == 0.08
    assert slippage_pct_for_value(99 * cr) == 0.15
    assert slippage_pct_for_value(25 * cr) == 0.15
    assert slippage_pct_for_value(24 * cr) == 0.30


def test_tiers_are_monotonic_and_cover_zero():
    """A thinner name must never be charged less than a thicker one."""
    vals = [t[0] for t in TIERS]
    assert vals == sorted(vals, reverse=True)
    assert slippage_pct_for_value(0.0) == 0.30


def test_no_history_gets_the_most_conservative_tier():
    """Never the cheapest: an unknown name is assumed thin until shown otherwise."""
    assert slippage_pct_for_value(None) == 0.30


def test_median_traded_value_uses_close_times_volume_over_the_window(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL,"
                 " h REAL, l REAL, c REAL, v INTEGER, source TEXT)")
    base = 1577836800                                   # 2020-01-01 00:00 UTC
    for k in range(10):
        conn.execute("INSERT INTO candles VALUES ('A',?,1440,1,1,1,?,?, 'official')",
                     (base + k * 86400, 100.0, 1000 + k))
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    # closes are 100, volumes 1000..1009 -> traded values 100000..100900, median 100450
    assert median_traded_value(ro, "A", date(2020, 1, 15), window=10) == pytest.approx(100450.0)


def test_median_traded_value_is_none_for_a_symbol_with_no_bars(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL,"
                 " h REAL, l REAL, c REAL, v INTEGER, source TEXT)")
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    assert median_traded_value(ro, "NOPE", date(2020, 1, 15)) is None
    assert slippage_for(ro, "NOPE", date(2020, 1, 15)) == 0.30
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_liquidity.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.data.liquidity'`

- [ ] **Step 3: Write the implementation**

Create `src/tradebot/data/liquidity.py`:

```python
"""Per-name slippage, from the name's own traded value.

The flat 5 bps used until now was calibrated on NIFTY 50 large caps. NIFTY 200 includes
midcaps whose spreads are several times wider, and momentum premia are largest in exactly
those names, so a flat rate would flatter the thin end of the universe most.

At the position sizes this project trades -- 1 lakh across ten names is 10,000 each, about
0.00065% of daily turnover in even the thinnest current name -- this is the BID-ASK SPREAD,
not market impact. Traded value is a proxy for spread, not a measurement of it. See section
5.3 of the spec: the tiers are unvalidated against quote data, and may not be changed after
seeing a result.
"""
import statistics
from datetime import date
from typing import Optional

INTERVAL = 1440
IST_OFFSET = 19800
WINDOW = 60                                   # trading days of history behind the estimate
CRORE = 1e7

# (minimum median daily traded value in rupees, percent slippage per side), richest first.
TIERS = (
    (500 * CRORE, 0.05),
    (100 * CRORE, 0.08),
    (25 * CRORE, 0.15),
    (0.0, 0.30),
)
UNKNOWN_SLIPPAGE_PCT = 0.30                   # never the cheapest tier


def slippage_pct_for_value(value):
    """Percent per side for a median daily traded value in rupees. None means no history,
    which is charged at the most conservative tier rather than assumed liquid."""
    if value is None:
        return UNKNOWN_SLIPPAGE_PCT
    for floor, pct in TIERS:
        if value >= floor:
            return pct
    return UNKNOWN_SLIPPAGE_PCT


def median_traded_value(conn, symbol, as_of, window=WINDOW, interval=INTERVAL):
    """Median of close x volume over the `window` daily bars ending the day BEFORE `as_of`.
    None when the symbol has no bars in that span. Strictly before `as_of` so an estimate
    never uses the day it is applied to."""
    # Daily bars are stamped 00:00 IST, so the bar FOR `as_of` sits at exactly this ts.
    # Using a strict < is the whole guard: an estimate must never see the day it prices.
    as_of_ts = (as_of - date(1970, 1, 1)).days * 86400 - IST_OFFSET
    rows = conn.execute(
        "SELECT c * v FROM candles WHERE symbol=? AND interval=? AND ts<? AND v>0"
        " ORDER BY ts DESC LIMIT ?", (symbol, interval, as_of_ts, window)).fetchall()
    vals = [r[0] for r in rows if r[0] is not None and r[0] > 0]
    if not vals:
        return None
    return statistics.median(vals)


def slippage_for(conn, symbol, as_of, window=WINDOW, interval=INTERVAL):
    """Percent per side for `symbol` on `as_of`."""
    return slippage_pct_for_value(median_traded_value(conn, symbol, as_of, window, interval))


def describe_tiers():
    """One line per tier, for printing beside any result that used them."""
    out = ["slippage tiers (percent per side, by median daily traded value):"]
    prev = None
    for floor, pct in TIERS:
        if prev is None:
            out.append("  >= %5.0f cr   %.2f%%" % (floor / CRORE, pct))
        else:
            out.append("  %5.0f-%5.0f cr  %.2f%%" % (floor / CRORE, prev / CRORE, pct))
        prev = floor
    out.append("  unvalidated against quote data; see spec 5.3")
    return "\n".join(out)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_liquidity.py -q` → `5 passed`.
Then `.venv/bin/pytest -q` → `656 passed`.

- [ ] **Step 5: Sanity-check against the real database, read-only**

Run:

```bash
.venv/bin/python -c "
import sqlite3
from datetime import date
from tradebot.data.liquidity import slippage_for, describe_tiers
c = sqlite3.connect('file:data/tradebot.db?mode=ro', uri=True)
import collections
t = collections.Counter(slippage_for(c, s, date(2025, 1, 1))
                        for (s,) in c.execute('SELECT DISTINCT symbol FROM candles WHERE interval=1440'))
print(describe_tiers()); print(); print('current 50 names by tier:', dict(t))
"
```

Expected: every one of the 50 current names lands in the 0.05% or 0.08% tier, because their
median daily traded values run ₹154 cr to ₹2,668 cr. **If any current NIFTY 50 name lands in
0.15% or 0.30%, stop and report** — either the window or the query is wrong.

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/data/liquidity.py tests/test_liquidity.py
git commit -m "data(liquidity): per-name slippage from median traded value"
```

---

### Task 6: build the real timeline

**Files:**
- Create: `scripts/build_membership.py`
- Create: `index_membership.yaml`

This is the research task. It has no unit tests of its own; Task 7's integration test is what
validates its output, and that test is the acceptance criterion.

- [ ] **Step 1: Fetch and store the anchor**

Write `scripts/build_membership.py` with an `anchor` subcommand that fetches the constituent
CSV with `curl` (the WebFetch tool times out against NSE hosts; curl does not) and writes the
anchor block:

```python
"""Build index_membership.yaml from NSE's published constituent list and press releases.

    .venv/bin/python scripts/build_membership.py anchor      # fetch today's NIFTY 200 list
    .venv/bin/python scripts/build_membership.py parse URL   # print NIFTY 200 rows from a release

Sources of record are NSE's own documents. niftyhistory.in publishes the same data but
discloses no operator, source or methodology, so it is a cross-check only -- never the
authority. Any disagreement is resolved in favour of the NSE release and recorded.
"""
import argparse
import csv
import io
import re
import subprocess
import sys
import zlib
from datetime import date

ANCHOR_URLS = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
EXPECTED = 200


def fetch(url, binary=False):
    out = subprocess.run(["curl", "-sS", "--max-time", "45", "-A", UA, url],
                         capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8", "replace")


def anchor():
    for url in ANCHOR_URLS:
        try:
            text = fetch(url)
        except subprocess.CalledProcessError:
            continue
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) != EXPECTED:
            print("%s returned %d rows, expected %d" % (url, len(rows), EXPECTED),
                  file=sys.stderr)
            continue
        syms = sorted(r["Symbol"].strip().upper() for r in rows)
        isins = {r["Symbol"].strip().upper(): r["ISIN Code"].strip() for r in rows}
        if len(set(syms)) != EXPECTED:
            sys.exit("duplicate symbols in %s" % url)
        print("index: NIFTY 200")
        print("anchor:")
        print("  as_of: %s" % date.today().isoformat())
        print("  source: %s" % url)
        print("  fetched: %s" % date.today().isoformat())
        print("  size: %d" % EXPECTED)
        print("  symbols: [%s]" % ", ".join(syms))
        print("  isins:")
        for s in syms:
            print("    %s: %s" % (s, isins[s]))
        return
    sys.exit("no anchor URL returned %d rows; refusing to fall back to a third party"
             % EXPECTED)
```

- [ ] **Step 2: Add the release parser**

NSE release PDFs have an extractable text layer. Add to `scripts/build_membership.py`:

```python
def pdf_text(data):
    """Text from a PDF's content streams. NSE releases are not scanned images, so the text
    layer is present and this is enough -- no PDF library is needed."""
    parts = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            parts.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except Exception:
            continue
    blob = "\n".join(parts)
    shown = re.findall(r"\((?:[^()\\]|\\.)*\)", blob)
    return "".join(x[1:-1] for x in shown).replace("\\(", "(").replace("\\)", ")")


def parse(url):
    """Print the NIFTY 200 inclusions and exclusions in one release, for hand-checking
    before they go into the timeline."""
    text = pdf_text(fetch(url, binary=True))
    text = re.sub(r"en-(US|IN)", " ", text)
    print(text[:4000])
    print("\n--- rows mentioning Nifty 200 ---")
    for m in re.finditer(r"Nifty 200(.{0,400}?)(?=Nifty |\Z)", text, re.S | re.I):
        print(re.sub(r"\s+", " ", m.group(0))[:400])
```

with a `main` dispatching `anchor` and `parse`.

- [ ] **Step 3: Find the releases**

Locate every NSE index-review press release effective between 2020-01-01 and today. There are
roughly fourteen semi-annual reviews plus off-cycle changes. Start from
`https://www.nseindia.com/regulations/exchange-communication-circulars` and
`nsearchives.nseindia.com`, and search for `"Replacements in indices"` with each year.

For each: run `parse URL`, read the NIFTY 200 section, and record the inclusions and
exclusions by hand into `index_membership.yaml` with the source URL.

**Do not guess a release you cannot find.** A hole in the timeline makes Task 7's test fail at
a specific date, which is the correct and intended outcome. Report which reviews you could not
locate rather than inventing their contents.

- [ ] **Step 4: Write `index_membership.yaml`**

At the repository root, beside `universe.yaml`. NOT under `data/`, which is gitignored in its
entirety. Format exactly as Task 1's loader expects: `index`, `anchor` (with `as_of`, `source`,
`fetched`, `size`, `symbols`), `events` (each with `effective`, `source`, `include`, `exclude`),
`renames`.

- [ ] **Step 5: Point `universe.yaml` at it**

Add one line to `universe.yaml`:

```yaml
membership: index_membership.yaml
```

Leave the existing `symbols:` list untouched. Callers passing no `as_of` keep getting exactly
what they get today.

- [ ] **Step 6: Commit**

```bash
git add scripts/build_membership.py index_membership.yaml universe.yaml
git commit -m "data(membership): the real NIFTY 200 timeline from NSE releases"
```

---

### Task 7: integration test against the real timeline

**Files:**
- Create: `tests/test_membership_real.py`

- [ ] **Step 1: Write the test**

```python
"""The real timeline, not a fixture. This is the acceptance test for Task 6."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from tradebot.data.membership import constituents_on, load_timeline

TIMELINE = Path(__file__).resolve().parents[1] / "index_membership.yaml"
pytestmark = pytest.mark.skipif(not TIMELINE.exists(),
                                reason="index_membership.yaml not built yet (Task 6)")


def test_every_date_in_the_window_yields_exactly_the_index_size():
    """The whole point. If any date returns a different count, a release is missing or
    misparsed, and this names the first date that breaks."""
    t = load_timeline(TIMELINE)
    d, bad = date(2020, 1, 1), []
    while d <= t.anchor_as_of:
        try:
            n = len(constituents_on(t, d))
            if n != t.size:
                bad.append((d, n))
        except Exception as e:                       # noqa: BLE001 - report, do not mask
            bad.append((d, str(e)))
        d += timedelta(days=1)
    assert not bad, "first 5 bad dates: %r" % (bad[:5],)


def test_the_anchor_date_returns_the_anchor():
    t = load_timeline(TIMELINE)
    assert constituents_on(t, t.anchor_as_of) == tuple(sorted(t.anchor))


def test_every_event_cites_a_source():
    """A number no one can trace back to an NSE document is not evidence."""
    t = load_timeline(TIMELINE)
    missing = [e.effective for e in t.events if not e.source.strip()]
    assert not missing, "events with no source URL: %r" % (missing,)
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_membership_real.py -q`

If the first test fails, it names the earliest date whose count is wrong. Go back to Task 6,
find the release covering that date, add it, and rerun. Repeat until green. **Do not adjust
`size`, widen the assertion, or skip dates to make it pass.**

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/pytest -q` → `659 passed`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_membership_real.py
git commit -m "tests(membership): every date in the window yields exactly 200 names"
```

---

### Task 8: fetch candles for the historical members

**Files:**
- Modify: `data/tradebot.db` (the only task permitted to write there)

**This task needs the Groww approval-flow key approved that morning, and takes hours. Do not
start it until Task 7 is green — fetching against a wrong symbol list wastes the whole run.**

- [ ] **Step 1: Back up the database**

```bash
cp "data/tradebot.db" "data/tradebot.pre-universe.bak.db"
ls -la data/*.bak.db
```

Never delete an existing `.bak.db`.

- [ ] **Step 2: Compute the symbols needed**

```bash
.venv/bin/python -c "
import sqlite3
from tradebot.data.membership import load_timeline
t = load_timeline('index_membership.yaml')
need = set(t.anchor)
for e in t.events: need |= set(e.include) | set(e.exclude)
for r in t.renames: need |= {r.old, r.new}
c = sqlite3.connect('file:data/tradebot.db?mode=ro', uri=True)
have = set(s for (s,) in c.execute('SELECT DISTINCT symbol FROM candles WHERE interval=1440'))
print('needed %d, have %d, to fetch %d' % (len(need), len(have), len(need - have)))
print(' '.join(sorted(need - have)))
"
```

- [ ] **Step 3: Fetch**

Use the existing `tradebot fetch-data` path at `interval=1440` from 2020-01-01. Do not write
new fetch machinery. The run must be resumable: if it fails part way, rerunning must not
restart from the beginning.

- [ ] **Step 4: Record what could not be fetched**

Write `data/missing_symbols.txt`, one `SYMBOL reason` per line, for every needed symbol with
no candles. Do not skip them silently — the second spec decides how to treat them, and it can
only do that if they are visible.

- [ ] **Step 5: Verify**

```bash
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('file:data/tradebot.db?mode=ro', uri=True)
n, lo, hi = c.execute('SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE interval=1440').fetchone()
s = c.execute('SELECT COUNT(DISTINCT symbol) FROM candles WHERE interval=1440').fetchone()[0]
print('daily bars %d across %d symbols' % (n, s))
"
```

Expected: roughly 200-250 symbols and 300-360k rows.

- [ ] **Step 6: Run the suite and commit**

```bash
.venv/bin/pytest -q          # expect 659 passed
git add data/missing_symbols.txt 2>/dev/null || true
git status --short
```

`data/` is gitignored, so there is nothing to commit from this task unless
`missing_symbols.txt` is force-added. Report the fetch result in the task report instead.

---

## Self-review

**Spec coverage.** Section 3.1 sourcing → Task 6 Step 3. 3.2 anchor → Task 6 Step 1. 3.2.1
ISIN → Task 3 and Task 6 Step 1 (`isins:` block). 3.2.2 scale → Task 8 Step 2. 3.3 format →
Task 1. 3.4 replay and invariant → Task 2, verified in Task 7. 3.5 mergers → Task 3. Section 4
data → Task 8. Section 5 slippage → Task 5. Section 6 error handling → Tasks 1, 2 and 4.
Section 7 testing → Tasks 1-5 unit, Task 7 integration.

One deliberate gap: spec 3.4's second uncatchable failure mode, "assert each parsed release has
equal include and exclude counts for NIFTY 200", is not a separate task. It is a property of
the hand-built timeline and is checked implicitly by Task 7's count test, which fails if the
counts ever disagree. Adding a redundant assertion would not catch anything Task 7 misses.

**Placeholders.** None. Task 6 Step 3 is research rather than code, and says explicitly what to
do when a release cannot be found rather than leaving it open.

**Type consistency.** `Timeline`, `Event`, `Rename`, `MembershipError`, `constituents_on`,
`load_timeline`, `slippage_for`, `median_traded_value`, `slippage_pct_for_value`, `TIERS`,
`describe_tiers` are each defined once and used with the same signature throughout. `Timeline`
gains no fields after Task 1; `Rename` gains `isin` in Task 3, which is additive with a default
so Task 1's tests keep passing.
