"""The real timeline, not a fixture. This is the acceptance test for Task 6."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from tradebot.data.membership import constituents_on, expected_size, load_timeline

TIMELINE = Path(__file__).resolve().parents[1] / "index_membership.yaml"
pytestmark = pytest.mark.skipif(not TIMELINE.exists(),
                                reason="index_membership.yaml not built yet (Task 6)")


def test_every_date_in_the_window_yields_exactly_the_index_size():
    """The whole point. If any date returns a different count, a release is missing or
    misparsed, and this names the first date that breaks.

    The size expected on a date is the index size except inside a declared exception
    window, where the index really did carry an extra security -- a DVR line, or a
    demerged entity added at zero price before it listed. Those windows are dated and
    each cites the release that evidences it; anywhere else this still demands 200, so a
    missing release cannot hide behind the mechanism."""
    t = load_timeline(TIMELINE)
    d, bad = date(2020, 1, 1), []
    while d <= t.anchor_as_of:
        try:
            n = len(constituents_on(t, d))
            if n != expected_size(t, d):
                bad.append((d, n, expected_size(t, d)))
        except Exception as e:                       # noqa: BLE001 - report, do not mask
            bad.append((d, str(e)))
        d += timedelta(days=1)
    assert not bad, "first 5 bad dates: %r" % (bad[:5],)


def test_every_size_exception_cites_a_source_and_a_reason():
    """An exception without a release behind it is the invariant switched off."""
    t = load_timeline(TIMELINE)
    unsourced = [(x.start, x.end) for x in t.size_exceptions
                 if not x.source.strip() or not x.reason.strip()]
    assert not unsourced, "size exceptions with no source or reason: %r" % (unsourced,)


def test_the_anchor_date_returns_the_anchor():
    t = load_timeline(TIMELINE)
    assert constituents_on(t, t.anchor_as_of) == tuple(sorted(t.anchor))


def test_every_event_cites_a_source():
    """A number no one can trace back to an NSE document is not evidence."""
    t = load_timeline(TIMELINE)
    missing = [e.effective for e in t.events if not e.source.strip()]
    assert not missing, "events with no source URL: %r" % (missing,)
