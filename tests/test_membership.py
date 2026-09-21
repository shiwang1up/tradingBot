from datetime import date
from pathlib import Path

import pytest

from tradebot.data.membership import Timeline, load_timeline

SAMPLE = """
index: TEST 4
covers_from: 2022-01-01
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


def test_a_date_before_the_declared_coverage_raises(tmp_path):
    """The timeline declares how far back its event record is complete. Before that, an
    unrecorded change may sit in the gap, so the answer is unknowable rather than merely
    unrecorded -- and returning the oldest list anyway would be a confident wrong answer."""
    t = load_timeline(_write(tmp_path))
    with pytest.raises(MembershipError) as e:
        constituents_on(t, date(2021, 6, 1))
    assert "2022-01-01" in str(e.value)


def test_coverage_starting_after_an_event_is_rejected(tmp_path):
    """A timeline claiming completeness from a date later than one of its own events is
    internally inconsistent; that is a compiler bug and must not load."""
    bad = SAMPLE.replace("covers_from: 2022-01-01", "covers_from: 2023-06-01")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, bad))
    assert "2023-01-01" in str(e.value)


RENAMED = SAMPLE.replace("renames: []", """renames:
  - from: OLDNAME
    to: DDD
    effective: 2023-09-01
    kind: rename
    source: https://example.test/rename.pdf
""")


def test_a_rename_resolves_to_the_old_symbol_before_its_effective_date(tmp_path):
    """DDD was called OLDNAME until 2023-09-01, so a query for August 2023 must return
    OLDNAME -- that is the symbol its candles are stored under, and a screen given DDD
    for that date would find no prices. This is the case the rename loop exists for."""
    t = load_timeline(_write(tmp_path, RENAMED))
    before = constituents_on(t, date(2023, 8, 15))
    assert "OLDNAME" in before and "DDD" not in before


def test_a_rename_is_not_applied_after_its_effective_date(tmp_path):
    """Between the rename and the anchor the new symbol stands. The date must be strictly
    before anchor_as_of, or constituents_on returns the anchor at its early exit and this
    never exercises the rename loop at all."""
    t = load_timeline(_write(tmp_path, RENAMED))
    assert t.anchor_as_of == date(2024, 1, 1)          # guards the point above
    after = constituents_on(t, date(2023, 10, 1))
    assert "DDD" in after and "OLDNAME" not in after


def test_renames_do_not_change_the_member_count(tmp_path):
    """A rename swaps one symbol for another. If it ever changes the count, the rename
    table holds an entry that is really an inclusion or exclusion in disguise."""
    t = load_timeline(_write(tmp_path, RENAMED))
    for d in (date(2023, 3, 1), date(2023, 8, 15), date(2023, 10, 1), date(2024, 1, 1)):
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


CANCELLING = """
index: TEST CANCEL
covers_from: 2022-01-01
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
    exclude: [EEE, FFF]
  - effective: 2023-03-01
    source: https://example.test/mar2023.pdf
    include: [EEE, FFF]
    exclude: [GGG]
renames: []
"""


def test_the_count_is_checked_after_every_undo_not_once_at_the_end(tmp_path):
    """Two events whose count errors cancel. Undoing the July event leaves five names;
    undoing the March event brings it back to four. A check that ran only after the whole
    replay would see a valid count and return a silently wrong list.

    Per-step checking is also what lets the error name the release that actually broke --
    2023-07-01 here -- which is what makes the invariant usable for finding a missing NSE
    release rather than merely knowing one is missing."""
    t = load_timeline(_write(tmp_path, CANCELLING))
    with pytest.raises(MembershipError) as e:
        constituents_on(t, date(2023, 1, 1))
    msg = str(e.value)
    assert "2023-07-01" in msg, "must name the release that broke, not the query date"
    assert "jul2023.pdf" in msg
    assert "5" in msg and "4" in msg


def test_an_event_effective_after_the_anchor_is_rejected(tmp_path):
    """The anchor is a snapshot: it already reflects every change up to its own date and
    none after it. Replaying a later change backward from it would undo something the
    anchor never contained, corrupting every query rather than just that date's -- and
    nothing downstream could tell. NSE announces reviews a month before they take effect,
    so this file WILL be handed one; it must refuse to load rather than quietly rewrite
    history."""
    future = SAMPLE.replace("  - effective: 2023-07-01", "  - effective: 2024-07-01")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, future))
    msg = str(e.value)
    assert "2024-07-01" in msg, "must name the offending event"
    assert "2024-01-01" in msg, "must name the anchor date it is after"
    assert "jul2023.pdf" in msg, "must name the source so it can be checked"


def test_an_event_effective_on_the_anchor_date_is_allowed(tmp_path):
    """The boundary is 'after', not 'on': a change effective on the anchor date is already
    reflected in the anchor, and constituents_on never undoes it."""
    same_day = SAMPLE.replace("  - effective: 2023-07-01", "  - effective: 2024-01-01")
    t = load_timeline(_write(tmp_path, same_day))
    assert t.events[0].effective == date(2024, 1, 1)
    assert constituents_on(t, date(2024, 1, 1)) == ("AAA", "BBB", "CCC", "DDD")


ALIASED = """
index: TEST ALIAS
covers_from: 2022-01-01
aliases:
  OLDCCC: CCC
anchor:
  as_of: 2024-01-01
  source: https://example.test/list.csv
  fetched: 2024-01-01
  size: 4
  symbols: [AAA, BBB, CCC, DDD]
events:
  - effective: 2023-07-01
    source: https://example.test/jul2023.pdf
    include: [OLDCCC]
    exclude: [EEE]
renames: []
"""


def test_an_alias_canonicalises_the_anchor_and_every_event_at_load_time(tmp_path):
    """One entity, two symbols. The replay is set arithmetic, so it must see one name."""
    t = load_timeline(_write(tmp_path, ALIASED))
    assert t.events[0].include == ("CCC",)
    assert t.aliases == (("OLDCCC", "CCC"),)
    aliased_anchor = load_timeline(
        _write(tmp_path, ALIASED.replace("[AAA, BBB, CCC, DDD]", "[AAA, BBB, OLDCCC, DDD]")))
    assert aliased_anchor.anchor == ("AAA", "BBB", "CCC", "DDD")


def test_an_alias_makes_an_otherwise_unbalanced_pair_cancel(tmp_path):
    """The case this exists for. NSE takes an entity out under whatever symbol it carries
    at the time, which need not be the one it came in under: DUMMYITC in, ITCHOTELS out.
    Without the alias the undo removes a symbol that is not in the set and puts back one
    that is, the count grows by one, and every earlier date is silently wrong."""
    t = load_timeline(_write(tmp_path, ALIASED))
    assert constituents_on(t, date(2023, 3, 1)) == ("AAA", "BBB", "DDD", "EEE")
    unaliased = ALIASED.replace("aliases:\n  OLDCCC: CCC\n", "")
    with pytest.raises(MembershipError) as e:
        constituents_on(load_timeline(_write(tmp_path, unaliased)), date(2023, 3, 1))
    assert "5" in str(e.value) and "4" in str(e.value)


def test_a_chained_alias_is_rejected(tmp_path):
    """A -> B -> C would need two passes; one pass leaves a retired symbol in the timeline
    and the count breaks exactly as if there were no alias at all."""
    chained = ALIASED.replace("aliases:\n  OLDCCC: CCC\n",
                              "aliases:\n  OLDCCC: MIDCCC\n  MIDCCC: CCC\n")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, chained))
    assert "MIDCCC" in str(e.value)


def test_an_alias_to_itself_is_rejected(tmp_path):
    same = ALIASED.replace("  OLDCCC: CCC", "  CCC: CCC")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, same))
    assert "CCC" in str(e.value)


def test_a_timeline_with_no_aliases_block_still_loads(tmp_path):
    t = load_timeline(_write(tmp_path))
    assert t.aliases == ()


from tradebot.data.membership import expected_size

EXCEPTED = """
index: TEST EXC
covers_from: 2022-01-01
size_exceptions:
  - from: 2023-03-01
    to: 2023-06-30
    size: 5
    reason: a demerged entity joined at zero price and had not yet listed
    source: https://example.test/exc.pdf
anchor:
  as_of: 2024-01-01
  source: https://example.test/list.csv
  fetched: 2024-01-01
  size: 4
  symbols: [AAA, BBB, CCC, DDD]
events:
  - effective: 2023-07-01
    source: https://example.test/jul2023.pdf
    include: []
    exclude: [EEE]
  - effective: 2023-03-01
    source: https://example.test/mar2023.pdf
    include: [EEE]
    exclude: []
renames: []
"""


def test_a_size_exception_permits_the_extra_name_inside_its_window(tmp_path):
    """The index held five securities for four companies while the placeholder sat in it.
    Without the window this is exactly what a missing release looks like, which is why the
    window has to be dated and sourced rather than a tolerance."""
    t = load_timeline(_write(tmp_path, EXCEPTED))
    assert expected_size(t, date(2023, 5, 1)) == 5
    assert constituents_on(t, date(2023, 5, 1)) == ("AAA", "BBB", "CCC", "DDD", "EEE")


def test_outside_the_window_the_plain_size_is_still_demanded(tmp_path):
    t = load_timeline(_write(tmp_path, EXCEPTED))
    assert expected_size(t, date(2023, 2, 1)) == 4
    assert constituents_on(t, date(2023, 2, 1)) == ("AAA", "BBB", "CCC", "DDD")


def test_the_window_boundaries_are_inside_the_window(tmp_path):
    """from and to are inclusive. Tested at the boundary values themselves, because an
    off-by-one here moves a real trading day into or out of the exception."""
    t = load_timeline(_write(tmp_path, EXCEPTED))
    assert expected_size(t, date(2023, 3, 1)) == 5      # first day
    assert expected_size(t, date(2023, 6, 30)) == 5     # last day
    assert expected_size(t, date(2023, 2, 28)) == 4     # day before
    assert expected_size(t, date(2023, 7, 1)) == 4      # day after
    assert len(constituents_on(t, date(2023, 3, 1))) == 5
    assert len(constituents_on(t, date(2023, 6, 30))) == 5


def test_without_the_exception_the_same_timeline_is_an_error(tmp_path):
    """The mechanism must be what permits the count, not the count that permits itself."""
    plain = EXCEPTED[:EXCEPTED.index("size_exceptions:")] + EXCEPTED[EXCEPTED.index("anchor:"):]
    t = load_timeline(_write(tmp_path, plain))
    with pytest.raises(MembershipError) as e:
        constituents_on(t, date(2023, 5, 1))
    assert "jul2023.pdf" in str(e.value)


def test_a_size_exception_without_a_source_is_a_load_error(tmp_path):
    """An exception nobody can trace to a release is the invariant switched off."""
    bad = EXCEPTED.replace("    source: https://example.test/exc.pdf\n", "")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, bad))
    assert "source" in str(e.value)


def test_a_size_exception_missing_any_other_field_is_a_load_error(tmp_path):
    for line in ("    to: 2023-06-30\n", "    size: 5\n",
                 "    reason: a demerged entity joined at zero price and had not yet listed\n"):
        with pytest.raises(ValueError):
            load_timeline(_write(tmp_path, EXCEPTED.replace(line, "")))


def test_a_backwards_size_exception_is_rejected(tmp_path):
    bad = EXCEPTED.replace("  - from: 2023-03-01\n    to: 2023-06-30\n",
                           "  - from: 2023-06-30\n    to: 2023-03-01\n")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, bad))
    assert "2023-06-30" in str(e.value) and "2023-03-01" in str(e.value)


def test_overlapping_size_exceptions_are_rejected(tmp_path):
    """Two windows covering one date disagree about how many names the index held, and
    whichever is consulted first silently wins."""
    bad = EXCEPTED.replace("""    source: https://example.test/exc.pdf
""", """    source: https://example.test/exc.pdf
  - from: 2023-06-01
    to: 2023-08-31
    size: 6
    reason: a second window that overlaps the first
    source: https://example.test/exc2.pdf
""")
    with pytest.raises(ValueError) as e:
        load_timeline(_write(tmp_path, bad))
    assert "overlap" in str(e.value)


def test_the_error_names_the_exception_in_force(tmp_path):
    """A wrong window must be as diagnosable as a missing release, so the error says which
    exception set the number it was checking against."""
    wrong = EXCEPTED.replace("    size: 5\n    reason: a demerged",
                             "    size: 6\n    reason: a demerged")
    t = load_timeline(_write(tmp_path, wrong))
    with pytest.raises(MembershipError) as e:
        constituents_on(t, date(2023, 5, 1))
    msg = str(e.value)
    assert "exc.pdf" in msg and "2023-03-01" in msg and "2023-06-30" in msg


def test_a_timeline_with_no_exceptions_expects_the_plain_size(tmp_path):
    t = load_timeline(_write(tmp_path))
    assert t.size_exceptions == ()
    assert expected_size(t, date(2023, 5, 1)) == 4
