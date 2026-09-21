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
