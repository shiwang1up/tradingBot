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


def test_a_partial_window_gets_the_conservative_tier(tmp_path):
    """Five bars of heavy trading is not sixty bars of evidence. Spec 5.2: insufficient
    history takes the most conservative tier, never the cheapest."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL,"
                 " h REAL, l REAL, c REAL, v INTEGER, source TEXT)")
    base = 1577836800
    for k in range(5):                                  # only 5 of the 60 needed
        conn.execute("INSERT INTO candles VALUES ('THIN',?,1440,1,1,1,?,?, 'official')",
                     (base + k * 86400, 1000.0, 10_000_000))    # heavy: 1000 cr a day
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    assert median_traded_value(ro, "THIN", date(2020, 3, 1)) is None
    assert slippage_for(ro, "THIN", date(2020, 3, 1)) == 0.30


def test_the_as_of_bar_itself_is_never_included(tmp_path):
    """An estimate must not see the day it prices. This fails loudly if the comparison ever
    loosens from < to <=, which needs two things the obvious fixture does not give you: the
    as_of bar must sit at EXACTLY the boundary ts, and the window must be even so that one
    leaked outlier is half the sample. A daily bar is stamped 00:00 IST -- 18:30 UTC the day
    before -- so 2020-01-01's bar is 1577817000, not UTC midnight's 1577836800. With an odd
    window the outlier lands at one end and the median does not move at all.
    """
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE candles (symbol TEXT, ts INTEGER, interval INTEGER, o REAL,"
                 " h REAL, l REAL, c REAL, v INTEGER, source TEXT)")
    ist_midnight = 1577817000                           # 2020-01-01 00:00 IST
    for k in range(3):                                  # 2020-01-01..03, ordinary volume
        conn.execute("INSERT INTO candles VALUES ('A',?,1440,1,1,1,?,?, 'official')",
                     (ist_midnight + k * 86400, 100.0, 1000))
    conn.execute("INSERT INTO candles VALUES ('A',?,1440,1,1,1,?,?, 'official')",
                 (ist_midnight + 3 * 86400, 100.0, 999_999_999))    # 2020-01-04, the as_of day
    conn.commit(); conn.close()
    ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    # Prior bars are 100 x 1000 = 100,000 each. Let the as_of bar in and the sample becomes
    # [99,999,999,900, 100,000] and the median ~5e10 -- six orders of magnitude, four tiers.
    assert median_traded_value(ro, "A", date(2020, 1, 4), window=2) == pytest.approx(100_000.0)
