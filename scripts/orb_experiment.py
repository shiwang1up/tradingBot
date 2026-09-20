"""ORB experiments against the project database (spec 2026-09-19-charges-orb-regime-design.md).

    .venv/bin/python scripts/orb_experiment.py tune
    .venv/bin/python scripts/orb_experiment.py holdout --range 30 --rr 2.0     # --rr none for no target
    .venv/bin/python scripts/orb_experiment.py regime --range 30 --rr 2.0 [--source composite]

Tuning runs see 2026-06-17..2026-08-15 only. The holdout (2026-08-16..2026-09-15) has a fixed run id,
so it can be run once: choose the parameters from the tuning table first. The AI filter is the stub.

The fixed run id is a discipline aid, not a lock: `tradebot backtest` over the same dates writes its
own run id and would bypass it without complaint. A holdout run that crashes part-way still creates
the run row and burns the id -- there is no clean do-over under the same name. And at roughly 21
trading days, only a clearly negative holdout says anything: a small positive R on risk is consistent
with both a real, small edge and with noise, and this sample size cannot tell those apart.

The regime phase runs ORB (the range/rr chosen here, not by this script) and 5-minute confluence,
filter off and on, on the tuning window: four runs `rg-<strategy>-<off|on>-tune`. `--with-holdout`
adds the holdout window too (`rg-<strategy>-<off|on>-hold`), but only once `orb-holdout` already
exists -- the run-once discipline applies to this phase as much as to `holdout` itself. The two
filter-off tuning runs repeat `tune`'s own run with the same parameters -- the index is loaded and
stripped either way (see `run`'s `with_index`), so their rows must match those exactly.

Filter-on is a DIFFERENT PORTFOLIO from filter-off, not a subset of it: a blocked signal takes no
slot (engine/loop.py's `_place` rejects it before it ever joins `state.pending_symbols` or
`entries_today`), so a lower-ranked signal on the very same bar moves up to fill the slot it left
free -- with ORB (at most a couple of names a day) a blocked LONG can be replaced by a SHORT in a
different stock, not left empty. The `regime` and `regime_not_ready` rejection counts a report shows
are signals gated at that point, not trades removed after the fact: the gate runs before the risk
check, so a `regime` rejection also masks whatever reason the risk check would otherwise have given
(kill switch, daily loss cap, max entries for the day).

A 20-bar EMA spans about 100 minutes on 5-minute bars and about 5 hours on 15-minute bars. ORB fires
its first signal at 09:45 or 10:15 (a 30- or 60-minute opening range) -- at that point in the day a
15-minute EMA(20) is still mostly carrying the previous session's closes, so on ORB's own bars the
filter is closer to an overnight-gap check than a same-day trend filter.

Decision rule (tuning window only -- a plain `regime` run never touches the holdout): the filter is
worth validating for a strategy only if R on risk is higher WITH it than without it AND is itself
positive. "Loses less with the filter on" is recorded as exactly that -- the filter trading less of a
losing strategy -- never as a result worth pursuing on its own.
"""
import argparse
import logging
import sys
from dataclasses import replace as dc_replace
from datetime import date, timedelta
from pathlib import Path

import click

from tradebot.ai.filter import build_filter
from tradebot.cli import _with_index
from tradebot.config import load_config
from tradebot.data.historical import HistoricalSource
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, date_of, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.report.summary import build_summary
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy, strategy_params

TUNE = (date(2026, 6, 17), date(2026, 8, 15))
HOLDOUT = (date(2026, 8, 16), date(2026, 9, 15))
GRID = [(r, rr) for r in (30, 60) for rr in (1.5, 2.0, None)]

logging.basicConfig(level=logging.WARNING)


def label(range_minutes, rr) -> str:
    return f"{range_minutes}-{'none' if rr is None else rr}"


def run(cfg, strategy_name: str, run_id: str, window, with_index: bool = False) -> object:
    """`with_index=True` (the regime phase) folds the regime index into the loaded source via the
    CLI's own `_with_index`, so the run sees exactly what a real `tradebot backtest` would; lot
    sizes stay built from the plain universe list regardless, since the index is never traded."""
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is not None:
        sys.exit(f"run '{run_id}' already exists: this experiment has been run; report it with "
                 f"`tradebot report --run {run_id}`")
    symbols = list(load_universe(cfg.paths.universe).symbols)
    interval = cfg.execution.interval_minutes
    load_symbols = symbols
    if with_index:
        try:
            load_symbols = _with_index(cfg, symbols)
        except click.ClickException as e:
            sys.exit(e.message)
    start, end = ist_epoch(window[0], "00:00"), ist_epoch(window[1], "23:59")  # no open upper bound anywhere
    source = HistoricalSource.from_repo(repo, load_symbols, interval, start, end)
    if not source.bar_timestamps():
        sys.exit(f"no {interval}-minute candles in {window[0]}..{window[1]}; run fetch-data with this config")
    clock = SessionClock(cfg.session, interval)
    ai_cfg = dc_replace(cfg.ai, filter="stub")
    cfg = dc_replace(cfg, ai=ai_cfg)  # the resolved config stored with the run records this substitution
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    engine = BacktestEngine(cfg, repo, source, [build_strategy(strategy_name, strategy_params(cfg.strategy, strategy_name))],
                            broker, build_filter(ai_cfg, "", repo=repo, clock=clock), clock,
                            {s: 1 for s in symbols}, run_id)   # lots: universe only, never the index
    engine.run()
    return build_summary(repo, run_id, cfg.charges)


def with_orb(cfg, range_minutes, rr):
    strat = dict(cfg.strategy)
    strat["orb"] = dict(strat["orb"], range_minutes=range_minutes, reward_risk=rr)
    return dc_replace(cfg, strategy=strat)


def with_regime(cfg, enabled: bool, source: str):
    return dc_replace(cfg, regime=dc_replace(cfg.regime, enabled=enabled, source=source))


def row(s) -> str:
    return (f"{s.run_id:<22} {s.trades:>6} {s.win_rate * 100:>6.1f}% {s.gross_pnl:>12,.0f} {s.charges:>10,.0f} "
            f"{s.total_pnl:>12,.0f} {s.r_on_risk:>10.3f}")


# "R on risk" is net PnL over rupees at risk, all trades pooled. It is the decision metric: unlike the
# unweighted per-trade Avg R, scrap-sized trades cannot dominate it, and its sign is the sign of net PnL.
HEADER = f"{'run':<22} {'trades':>6} {'win':>7} {'gross':>12} {'charges':>10} {'net':>12} {'R on risk':>10}"


def _hhmm_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def check_holdout_data(conn, symbols: list, interval: int, window, session) -> int:
    """Count-only completeness check of a window: only `symbol` and `ts` are read, never a price
    column. Closes `conn` -- the caller passes a connection just for this.

    The grid is (symbol, EXPECTED trading day): the expected days come from the calendar
    (`SessionClock.is_trading_day` over every date of the window: weekdays that are not configured
    holidays), never from the rows seen. A pair is incomplete unless it holds exactly the expected
    number of bars, so a symbol with no rows on a day counts (amendment D7), and so does a trading
    day with no rows for ANY symbol -- a day the rows themselves could never reveal. This guards a
    run-once window, so a hole must not be able to hide. An exchange holiday missing from
    `session.holidays` shows up here as a whole day of incomplete symbol-days: add it to the config
    rather than passing --accept-gaps. Rows on a day that is not an expected trading day are
    reported and left out of the count. Returns the incomplete count so the caller can decide
    whether to refuse to run on gappy data."""
    try:
        marks = ",".join("?" * len(symbols))
        start, end = ist_epoch(window[0], "00:00"), ist_epoch(window[1], "23:59")
        rows = conn.execute(
            f"SELECT symbol, ts FROM candles WHERE symbol IN ({marks}) AND interval = ? AND ts BETWEEN ? AND ?",
            (*symbols, interval, start, end),
        ).fetchall()
    finally:
        conn.close()
    bars_per_symbol_day: dict = {}
    for r in rows:
        key = (r["symbol"], date_of(r["ts"]))
        bars_per_symbol_day[key] = bars_per_symbol_day.get(key, 0) + 1
    expected = (_hhmm_minutes(session.close) - _hhmm_minutes(session.open)) // interval
    clock = SessionClock(session, interval)
    span = (window[1] - window[0]).days + 1
    days = [d for d in (window[0] + timedelta(days=i) for i in range(span)) if clock.is_trading_day(d)]
    total = len(days) * len(symbols)
    incomplete = sum(1 for d in days for s in symbols if bars_per_symbol_day.get((s, d), 0) != expected)
    print(f"holdout data: {len(days)} expected trading days, {len(symbols)} symbols, "
          f"{incomplete} incomplete symbol-days of {total}")
    stray = sorted({d for _, d in bars_per_symbol_day} - set(days))
    if stray:
        print(f"holdout data: rows on {len(stray)} day(s) that are not expected trading days, not counted: "
              f"{', '.join(d.isoformat() for d in stray)}")
    return incomplete


def regime_run_ids(strategy_name: str, tags: list) -> list:
    """Every run id the regime phase creates for one strategy, in the order it creates them."""
    return [f"rg-{strategy_name}-{'on' if enabled else 'off'}-{tag}" for tag in tags for enabled in (False, True)]


def existing_run_ids(db_path: str, run_ids: list) -> list:
    """Those of `run_ids` already stored in the database at `db_path`. Opens and closes its own
    connection."""
    conn = connect(db_path)
    try:
        repo = Repo(conn)
        return [r for r in run_ids if repo.get_run(r) is not None]
    finally:
        conn.close()


def _since_after_last_not_ready(repo: Repo, run_id: str, window) -> int:
    """The IST-midnight epoch of the first trading day after the last day on which `run_id` recorded
    any `regime_not_ready` rejection -- the window's own start if it never recorded one. Used to
    score a filter-on run and its filter-off twin from a common point, so the filter-on run's warm-up
    days (rejected for lack of EMA history, not on the strategy's merit) don't also cost the
    filter-off comparison its own early days (amendment D3)."""
    rows = repo.conn.execute(
        "SELECT s.bar_ts FROM signals s JOIN risk_decisions d ON d.signal_id = s.id "
        "WHERE s.run_id=? AND d.reason='regime_not_ready'", (run_id,)).fetchall()
    if not rows:
        return ist_epoch(window[0], "00:00")
    last_bad_day = max(date_of(r["bar_ts"]) for r in rows)
    return ist_epoch(last_bad_day + timedelta(days=1), "00:00")


def regime_phase(a) -> None:
    rr = None if a.rr.lower() == "none" else float(a.rr)
    variants = [("orb", with_orb(load_config("config-orb.yaml"), a.range_minutes, rr)),
                ("confluence", load_config("config.yaml"))]
    windows = [("tune", TUNE)]
    if a.with_holdout:
        # The run-once discipline applies to this phase too: `--with-holdout` cannot be used to burn
        # the holdout run id under a different name before the tuning window has earned a look at it.
        if not existing_run_ids(variants[0][1].paths.db, ["orb-holdout"]):
            sys.exit("--with-holdout needs an existing 'orb-holdout' run first (run-once discipline)")
        windows.append(("hold", HOLDOUT))
    # Every pre-check below runs before any run is created: a failure must leave no partial set of
    # run rows behind. Run ids first -- `run` refuses an existing id too, but only when its turn
    # comes, so after a crash part-way a second invocation would otherwise add some rows and then
    # stop, or (had the first ids been cleaned up by hand) mix rows from two invocations.
    taken = [r for strategy_name, base in variants
             for r in existing_run_ids(base.paths.db, regime_run_ids(strategy_name, [tag for tag, _ in windows]))]
    if taken:
        sys.exit(f"run(s) already exist: {', '.join(taken)}. The regime phase creates all of its runs or "
                 f"none: report them with `tradebot report --run <id>`; to repeat the phase, every "
                 f"rg-* run of this set has to be removed from the database first")
    if a.source == "index":
        # Checked once for every (strategy, window) this invocation will touch.
        for strategy_name, base in variants:
            idx = base.data.index_symbol
            if not idx:
                sys.exit(f"--source index needs {strategy_name}'s data.index_symbol set; run "
                         f"`tradebot fetch-data`, or pass --source composite")
            conn = connect(base.paths.db)
            try:
                repo = Repo(conn)
                for tag, window in windows:
                    start, end = ist_epoch(window[0], "00:00"), ist_epoch(window[1], "23:59")
                    if not repo.load_candles([idx], base.execution.interval_minutes, start, end):
                        sys.exit(f"no {idx} candles for {strategy_name} in {window[0]}..{window[1]}; run "
                                 f"`tradebot fetch-data` for this config, or pass --source composite")
            finally:
                conn.close()
    summaries: dict = {}
    for strategy_name, base in variants:
        for tag, window in windows:
            for enabled, run_id in zip((False, True), regime_run_ids(strategy_name, [tag])):
                s = run(with_regime(base, enabled, a.source), strategy_name, run_id, window, with_index=True)
                summaries[(strategy_name, tag, enabled)] = s
                print(row(s), flush=True)   # unscored, whole window: the raw run as stored

    # D3: score each (off, on) pair from a common start, so the filter-on run's own warm-up days
    # don't skew the comparison against it.
    for strategy_name, base in variants:
        repo = Repo(connect(base.paths.db))
        for tag, window in windows:
            on_id = f"rg-{strategy_name}-on-{tag}"
            off_id = f"rg-{strategy_name}-off-{tag}"
            on_summary = summaries[(strategy_name, tag, True)]
            since_ts = _since_after_last_not_ready(repo, on_id, window)
            print(f"{strategy_name} ({tag}): scoring from {date_of(since_ts)} (the day after the last "
                 f"regime_not_ready day of {on_id}, or the window start if there was none); {on_id} rejections: "
                 f"regime {on_summary.risk_rejections.get('regime', 0)}, "
                 f"regime_not_ready {on_summary.risk_rejections.get('regime_not_ready', 0)}")
            print(row(build_summary(repo, off_id, base.charges, since_ts=since_ts)))
            print(row(build_summary(repo, on_id, base.charges, since_ts=since_ts)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["tune", "holdout", "regime"])
    ap.add_argument("--config", default="config-orb.yaml",
                    help="used by tune and holdout; regime always uses config-orb.yaml and config.yaml")
    ap.add_argument("--range", dest="range_minutes", type=int, choices=[30, 60])
    ap.add_argument("--rr", choices=["1.5", "2.0", "none"], help="1.5, 2.0 or none")
    ap.add_argument("--source", default="index", choices=["index", "composite"],
                    help="regime phase only: where the filter reads market direction from")
    ap.add_argument("--with-holdout", action="store_true",
                    help="regime phase only: also run the holdout window; refused unless 'orb-holdout' exists")
    ap.add_argument("--accept-gaps", action="store_true",
                    help="holdout phase only: run even when check_holdout_data finds incomplete symbol-days")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if Path(cfg.paths.kill_switch).exists():
        # D2: a live kill switch blocks every entry any phase's runs try to place, burning run ids
        # for nothing -- the same discipline the holdout phase always had, now for every phase.
        sys.exit(f"KILL file present at {cfg.paths.kill_switch}: it would block every entry these runs "
                 f"try to place and burn run ids for nothing; remove it before starting")
    print(HEADER)
    if a.phase == "tune":
        for range_minutes, rr in GRID:
            print(row(run(with_orb(cfg, range_minutes, rr), "orb", f"orb-t-{label(range_minutes, rr)}", TUNE)), flush=True)
        return
    if a.range_minutes is None or a.rr is None:
        sys.exit(f"{a.phase} needs --range and --rr, chosen from the tuning table")
    if a.phase == "regime":
        regime_phase(a)
        return
    symbols = list(load_universe(cfg.paths.universe).symbols)
    incomplete = check_holdout_data(connect(cfg.paths.db), symbols, cfg.execution.interval_minutes, HOLDOUT, cfg.session)
    if incomplete > 0 and not a.accept_gaps:
        sys.exit(f"{incomplete} incomplete symbol-day(s) in the holdout window; pass --accept-gaps to run anyway")
    rr = None if a.rr.lower() == "none" else float(a.rr)
    s = run(with_orb(cfg, a.range_minutes, rr), "orb", "orb-holdout", HOLDOUT)
    print(row(s))
    verdict = ("inconclusive: fewer than 30 holdout trades" if s.trades < 30
               else "positive out of sample, but ~21 days cannot separate a small edge from noise: "
                    "inconclusive without more data" if s.r_on_risk > 0 else "no edge out of sample")
    print(f"holdout verdict: {verdict}")


if __name__ == "__main__":
    main()
