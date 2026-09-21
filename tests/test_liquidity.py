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
