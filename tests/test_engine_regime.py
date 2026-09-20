"""The regime filter: the engine strips the index candle, feeds it (or the composite) to the
filter, and gates entries by direction. Split out of test_engine.py once that file passed 700
lines; see tests/helpers.py for the shared run_fixed/decisions/MON this file and test_engine.py
both use."""
import logging
from datetime import date

from tests.helpers import MON, FixedStrategy, decisions, make_config, run_fixed, synth_candles
from tradebot.ai.filter import StubFilter
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, ist_epoch, iso_ist
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.types import Candle

REGIME = dict(regime={"enabled": True, "ema_period": 3}, data={"index_symbol": "NIFTY"})


def _index(day, closes):
    t0 = ist_epoch(day, "09:15")
    return [Candle("NIFTY", t0 + i * 300, c, c, c, c, 0) for i, c in enumerate(closes)]   # an index has no volume


# -- regime filter: the engine splits off the index and gates entries ---------------------------

def test_regime_blocks_longs_in_a_falling_index_and_lets_shorts_through(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    t_long, t_short = ist_epoch(MON, "10:00"), ist_epoch(MON, "10:30")
    strat = run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 - 10 * i for i in range(75)]),
                       {("A", t_long): ("LONG", 0.0), ("A", t_short): ("SHORT", 0.0)}, ["A", "NIFTY"])
    assert decisions(repo) == [("A", "LONG", 0, "regime"), ("A", "SHORT", 1, "ok")]
    assert strat.seen == {"A"}                     # the index never reaches a strategy
    assert repo.rejection_counts("t1") == {"regime": 1}
    assert [r["symbol"] for r in repo.list_positions("t1")] == ["A"]


def test_regime_blocks_shorts_in_a_rising_index(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    t_short, t_long = ist_epoch(MON, "10:00"), ist_epoch(MON, "10:30")
    run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 + 10 * i for i in range(75)]),
               {("A", t_short): ("SHORT", 0.0), ("A", t_long): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert decisions(repo) == [("A", "SHORT", 0, "regime"), ("A", "LONG", 1, "ok")]


def test_regime_holds_everything_back_until_the_ema_is_warm(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 + 10 * i for i in range(75)]),
               {("A", ist_epoch(MON, "09:20")): ("LONG", 0.0)}, ["A", "NIFTY"])    # second bar; EMA3 needs three
    assert decisions(repo) == [("A", "LONG", 0, "regime_not_ready")]


def test_a_missing_index_bar_keeps_the_last_state(repo, tmp_path):
    cfg = make_config(tmp_path, **REGIME)
    ts = ist_epoch(MON, "10:00")
    index = [c for c in _index(MON, [25000.0 - 10 * i for i in range(75)]) if c.ts != ts]
    run_fixed(repo, cfg, synth_candles("A", [MON]) + index, {("A", ts): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert decisions(repo) == [("A", "LONG", 0, "regime")]


def test_index_candles_never_reach_strategies_with_the_filter_off(repo, tmp_path):
    cfg = make_config(tmp_path, data={"index_symbol": "NIFTY"})
    strat = run_fixed(repo, cfg, synth_candles("A", [MON]) + _index(MON, [25000.0 - 10 * i for i in range(75)]),
                       {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert decisions(repo) == [("A", "LONG", 1, "ok")] and strat.seen == {"A"}


def test_usable_runs_before_the_index_split_so_a_bad_index_candle_is_dropped_not_crashed_on(repo, tmp_path):
    """A NaN index candle on the same bar as a signal must be dropped by _usable before it ever
    reaches the regime filter (amendment A1): the filter keeps its last (DOWN) state and the bar
    still processes normally, instead of RegimeFilter.update raising on the NaN close.

    Driven straight through process_bar, not the repo round trip run_fixed uses: SQLite's REAL
    column binds float('nan') as NULL, and the `c REAL NOT NULL` column then makes INSERT OR IGNORE
    silently drop the row, so a NaN candle sent through the database never reaches the engine at
    all - not what this test needs to exercise."""
    cfg = make_config(tmp_path, **REGIME)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    warmup_ts = [ist_epoch(MON, t) for t in ("09:15", "09:20", "09:25")]
    ts = ist_epoch(MON, "10:00")
    strat = FixedStrategy({("A", ts): ("LONG", 0.0)})
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [strat], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1}, "t1")
    repo.create_run("t1", "backtest", 0, "{}")
    eng._start_day()
    for t, close in zip(warmup_ts, (25000.0, 24990.0, 24980.0)):     # EMA3 seeds at 24990.0; state -> DOWN
        eng.process_bar(t, {"NIFTY": Candle("NIFTY", t, close, close, close, close, 0)})
    candle_a = Candle("A", ts, 100.0, 100.5, 99.8, 100.2, 1000)
    bad_index = Candle("NIFTY", ts, 24000.0, 24000.0, 24000.0, float("nan"), 0)
    eng.process_bar(ts, {"A": candle_a, "NIFTY": bad_index})
    assert decisions(repo, "t1") == [("A", "LONG", 0, "regime")]


def test_an_index_only_bar_is_skipped_and_a_pending_entry_still_fills_next_bar(repo, tmp_path):
    """Amendment A4: a timestamp whose only candle is the index must never reach the broker (which
    would otherwise mark every pending entry unfilled as no_candle). The entry placed on the signal
    bar must still fill at the next *tradable* bar's open."""
    cfg = make_config(tmp_path, data={"index_symbol": "NIFTY"})
    t_sig = ist_epoch(MON, "10:00")
    t_next = t_sig + 300
    extra_ts = t_sig + 150   # between t_sig and t_next; NIFTY only, no candle for A
    a_candles = synth_candles("A", [MON])
    index = _index(MON, [25000.0] * 75) + [Candle("NIFTY", extra_ts, 25000.0, 25000.0, 25000.0, 25000.0, 0)]
    run_fixed(repo, cfg, a_candles + index, {("A", t_sig): ("LONG", 0.0)}, ["A", "NIFTY"])
    rows = repo.list_positions("t1")
    assert len(rows) == 1 and rows[0]["opened_at"] == t_next
    statuses = [r["status"] for r in repo.conn.execute("SELECT status FROM orders WHERE run_id='t1'").fetchall()]
    assert statuses == ["FILLED"]


def test_a_blocked_signal_does_not_hold_its_slot_for_the_next_one(repo, tmp_path):
    """Job A review item 1: the regime gate must `continue` before the signal ever touches
    `state` (pending_symbols / entries_today), so a slot it did not use stays free for the very
    next signal on the same bar."""
    cfg = make_config(tmp_path, risk={"max_open_positions": 1}, **REGIME)
    ts = ist_epoch(MON, "10:00")
    candles = (synth_candles("A", [MON]) + synth_candles("B", [MON], phase=4.0, seed=99)
              + _index(MON, [25000.0 - 10 * i for i in range(75)]))
    run_fixed(repo, cfg, candles, {("B", ts): ("LONG", 2.5), ("A", ts): ("SHORT", 1.0)}, ["A", "B", "NIFTY"])
    assert decisions(repo) == [("B", "LONG", 0, "regime"), ("A", "SHORT", 1, "ok")]


def test_regime_state_carries_over_a_day_boundary_without_resetting(repo, tmp_path):
    """Job A review item 2: nothing re-creates the filter (or otherwise resets its state) at the
    start of a day, so a LONG on day 2's very first bar is rejected `regime`, never
    `regime_not_ready` - the EMA warmed on day 1 and stays warm."""
    cfg = make_config(tmp_path, **REGIME)
    day2 = date(2026, 9, 15)
    closes = [25000.0 - 10 * i for i in range(150)]     # falls straight through both days
    index = _index(MON, closes[:75]) + _index(day2, closes[75:])
    ts2 = ist_epoch(day2, "09:15")
    run_fixed(repo, cfg, synth_candles("A", [MON, day2]) + index, {("A", ts2): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert decisions(repo) == [("A", "LONG", 0, "regime")]


def test_an_index_only_tail_does_not_open_a_second_trading_day(repo, tmp_path):
    """Job A review item 3: a day whose only candle is the index (here day 2, once the stocks stop
    trading after day 1) must never reach day bookkeeping: one daily_pnl row, the run ends cleanly,
    and nothing closes after day 1's last tradable bar."""
    cfg = make_config(tmp_path, data={"index_symbol": "NIFTY"})
    day2 = date(2026, 9, 15)
    ts_sig = ist_epoch(MON, "10:00")
    a_candles = synth_candles("A", [MON])
    last_mon_bar = a_candles[-1].ts
    candles = a_candles + _index(MON, [25000.0] * 75) + _index(day2, [25000.0] * 75)
    run_fixed(repo, cfg, candles, {("A", ts_sig): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert repo.get_run("t1")["ended_at"] is not None
    assert [d["date"] for d in repo.daily_pnl("t1")] == [MON.isoformat()]
    for r in repo.list_positions("t1"):
        assert r["closed_at"] is None or r["closed_at"] <= last_mon_bar


# -- Task 14: the composite regime source, equal-weight universe returns chained from 100 -------

def _drift(sym, day, base, step):
    t0 = ist_epoch(day, "09:15")
    out, prev = [], base
    for i in range(75):
        p = round(base + step * (i + 1), 2)
        out.append(Candle(sym, t0 + i * 300, prev, max(prev, p) + 0.05, min(prev, p) - 0.05, p, 1000))
        prev = p
    return out


def test_composite_regime_needs_no_index_candles(repo, tmp_path):
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3})
    candles = _drift("A", MON, 100.0, -0.1) + _drift("B", MON, 200.0, -0.2)        # every symbol falls every bar
    run_fixed(repo, cfg, candles, {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0),
                                    ("A", ist_epoch(MON, "10:30")): ("SHORT", 0.0)}, ["A", "B"])
    assert decisions(repo) == [("A", "LONG", 0, "regime"), ("A", "SHORT", 1, "ok")]


def test_composite_regime_rising_universe_blocks_shorts(repo, tmp_path):
    """Job A review item 1: the feed-before-observe order is the one invariant the docstrings call
    a must, and it was unpinned - a reviewer swapped it in both call sites and the whole suite still
    passed, because with the order swapped every composite return is close/close - 1 == 0, the level
    never moves, and both existing composite tests use a FALLING universe expecting DOWN, which a
    permanently-flat, never-warmed filter also produces. A RISING universe is the only way to tell
    the two apart: a correctly-fed composite goes UP and blocks the SHORT; a flat one stays
    NOT_READY (never > or <= its own EMA in a way that resolves) and blocks nothing the same way
    DOWN would have looked the same on a falling series."""
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3})
    candles = _drift("A", MON, 100.0, 0.1) + _drift("B", MON, 200.0, 0.2)
    run_fixed(repo, cfg, candles, {("A", ist_epoch(MON, "10:00")): ("SHORT", 0.0),
                                   ("A", ist_epoch(MON, "10:30")): ("LONG", 0.0)}, ["A", "B"])
    assert decisions(repo) == [("A", "SHORT", 0, "regime"), ("A", "LONG", 1, "ok")]


def test_composite_source_ignores_a_loaded_rising_index_when_the_universe_falls(repo, tmp_path):
    """The engine must honour cfg.regime.source: with 'composite' a present index candle is still
    stripped (no strategy sees it) but never fed to the filter - only the universe's own falling
    returns are, so the LONG is blocked even though the loaded index is rising strongly."""
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3},
                      data={"index_symbol": "NIFTY"})
    candles = (_drift("A", MON, 100.0, -0.1) + _drift("B", MON, 200.0, -0.2)
              + _index(MON, [25000.0 + 10 * i for i in range(75)]))
    run_fixed(repo, cfg, candles, {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0)}, ["A", "B", "NIFTY"])
    assert decisions(repo) == [("A", "LONG", 0, "regime")]


def test_index_source_ignores_a_falling_universe_when_the_index_rises(repo, tmp_path):
    """The mirror of the test above: with source 'index' the composite is never computed, so a
    falling universe does not block a LONG the rising index would allow."""
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "index", "ema_period": 3},
                      data={"index_symbol": "NIFTY"})
    candles = (_drift("A", MON, 100.0, -0.1) + _drift("B", MON, 200.0, -0.2)
              + _index(MON, [25000.0 + 10 * i for i in range(75)]))
    run_fixed(repo, cfg, candles, {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0)}, ["A", "B", "NIFTY"])
    assert decisions(repo) == [("A", "LONG", 1, "ok")]


def test_composite_skips_a_bar_with_too_few_returns_for_minimum_breadth(repo, tmp_path):
    """Amendment B2: a bar carrying returns for only a few of the symbols `_last_close` knows
    about (fetch failures in paper mode) must not move the composite level or the regime state -
    not even a hair - regardless of how extreme that one return is. With 4 known symbols, a bar
    carrying only 1 of them is below the minimum breadth of max(1, 4 // 2) = 2, so it is skipped."""
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3})
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [], BacktestBroker(cfg.capital, 0.0, cfg.risk.mis_leverage),
                         StubFilter(), SessionClock(cfg.session, 5), {}, "t1")
    eng._last_close = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
    for _ in range(3):        # warm the EMA to a known, non-NOT_READY state before the probe bar
        eng._regime.update(100.0)
    level_before, state_before = eng._composite_level, eng._regime.state
    only_one = {"A": Candle("A", 1000, 200.0, 200.0, 200.0, 200.0, 1)}  # +100% on A alone
    eng._feed_regime(None, only_one)
    assert eng._composite_level == level_before
    assert eng._regime.state == state_before


def test_composite_level_guards_against_a_non_finite_result(repo, tmp_path):
    """Amendment B1: a bar whose return would send the composite level non-finite must leave the
    level and the filter state exactly as they were. Not reachable through the normal candle path
    - _usable already filters non-finite/non-positive OHLC before _feed_regime ever runs, and a
    real previous close can never be poisoned to something this extreme - so this drives
    _feed_regime directly with a poisoned `_last_close` as a second line of defence."""
    cfg = make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3})
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [], BacktestBroker(cfg.capital, 0.0, cfg.risk.mis_leverage),
                         StubFilter(), SessionClock(cfg.session, 5), {}, "t1")
    eng._last_close = {"A": 1e-300}     # poisoned: no real candle ever leaves a last close this tiny
    level_before = eng._composite_level
    huge = {"A": Candle("A", 1000, 1e308, 1e308, 1e308, 1e308, 1)}   # ratio overflows to inf
    eng._feed_regime(None, huge)
    assert eng._composite_level == level_before
    # No state assertion here: the filter is NOT_READY before and after either way (never warmed
    # in this test), so it would pass whether or not the guard worked. The level assertion above
    # is what this test actually pins.


# -- Task 15 amendments: the stale-filter log and the never-ready safety net --------------------

def test_a_missing_index_bar_with_tradable_candles_logs_once_per_trading_day(repo, tmp_path, caplog):
    """With the filter on and source 'index', a bar with tradable candles but no index candle used
    to keep the last state silently - in paper mode that could last all day with nothing in the log
    to say so. Two consecutive such bars on the same day must log exactly one warning; a later bar
    that does carry an index candle must log none."""
    cfg = make_config(tmp_path, **REGIME)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    eng = BacktestEngine(cfg, repo, HistoricalSource([]), [FixedStrategy({})], broker, StubFilter(),
                         SessionClock(cfg.session, 5), {"A": 1}, "t1")
    repo.create_run("t1", "backtest", 0, "{}")
    eng._start_day()
    for t, close in zip((ist_epoch(MON, tt) for tt in ("09:15", "09:20", "09:25")), (25000.0, 24990.0, 24980.0)):
        eng.process_bar(t, {"NIFTY": Candle("NIFTY", t, close, close, close, close, 0)})   # warm the EMA

    def a(ts, close=100.0):
        return Candle("A", ts, close, close + 0.5, close - 0.2, close, 1000)

    t1, t2 = ist_epoch(MON, "09:30"), ist_epoch(MON, "09:35")
    with caplog.at_level(logging.WARNING, logger="tradebot.engine"):
        eng.process_bar(t1, {"A": a(t1)})      # no NIFTY candle this bar
        eng.process_bar(t2, {"A": a(t2)})      # still none, same trading day: rate-limited
    stale = [r for r in caplog.records if "no index candle" in r.getMessage()]
    assert len(stale) == 1 and iso_ist(t1) in stale[0].getMessage()

    caplog.clear()
    t3 = ist_epoch(MON, "09:40")
    with caplog.at_level(logging.WARNING, logger="tradebot.engine"):
        eng.process_bar(t3, {"A": a(t3), "NIFTY": Candle("NIFTY", t3, 25000.0, 25000.0, 25000.0, 25000.0, 0)})
    assert not any("no index candle" in r.getMessage() for r in caplog.records)


def test_no_stale_regime_warning_with_the_filter_off_or_on_composite(repo, tmp_path, caplog):
    """The stale-filter warning is specific to an enabled filter on source 'index': neither a
    disabled filter nor the composite source (which never looks at an index candle at all) may
    ever log it, no matter how many bars in a row carry no index candle."""
    candle = Candle("A", 1000, 100.0, 100.5, 99.8, 100.2, 1000)
    for cfg in (make_config(tmp_path, data={"index_symbol": "NIFTY"}),
               make_config(tmp_path, regime={"enabled": True, "source": "composite", "ema_period": 3})):
        eng = BacktestEngine(cfg, repo, HistoricalSource([]), [], BacktestBroker(cfg.capital, 0.0, cfg.risk.mis_leverage),
                             StubFilter(), SessionClock(cfg.session, 5), {}, "t1")
        with caplog.at_level(logging.WARNING, logger="tradebot.engine"):
            eng._feed_regime(None, {"A": candle})
        assert not any("no index candle" in r.getMessage() for r in caplog.records)
        caplog.clear()


def test_a_run_that_ends_still_not_ready_warns_that_every_entry_was_rejected(repo, tmp_path, caplog):
    """Amendment: BacktestEngine.run's own safety net for callers that bypass the CLI's start-up
    check (scripts/orb_experiment.py does) - an index that never sends a single usable candle leaves
    the filter NOT_READY for the whole run, so every entry was silently rejected regime_not_ready;
    the run must say so once, at the end."""
    cfg = make_config(tmp_path, **REGIME)
    with caplog.at_level(logging.WARNING, logger="tradebot.engine"):
        run_fixed(repo, cfg, synth_candles("A", [MON]), {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0)}, ["A"])
    assert decisions(repo) == [("A", "LONG", 0, "regime_not_ready")]
    # Search on the run-end phrasing specifically: with no NIFTY candle ever loaded, every bar also
    # trips the once-a-day stale-index warning, whose own message happens to quote the state
    # (NOT_READY) too - not what this assertion is pinning.
    warnings = [r for r in caplog.records if "ended with the regime filter still NOT_READY" in r.getMessage()]
    assert len(warnings) == 1 and "t1" in warnings[0].getMessage() and "no usable index candles" in warnings[0].getMessage()


def test_a_run_that_warms_up_the_regime_does_not_get_the_not_ready_warning(repo, tmp_path, caplog):
    cfg = make_config(tmp_path, **REGIME)
    candles = synth_candles("A", [MON]) + _index(MON, [25000.0 + 10 * i for i in range(75)])
    with caplog.at_level(logging.WARNING, logger="tradebot.engine"):
        run_fixed(repo, cfg, candles, {("A", ist_epoch(MON, "10:00")): ("LONG", 0.0)}, ["A", "NIFTY"])
    assert not any("ended with the regime filter still NOT_READY" in r.getMessage() for r in caplog.records)
