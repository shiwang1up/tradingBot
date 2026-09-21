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
