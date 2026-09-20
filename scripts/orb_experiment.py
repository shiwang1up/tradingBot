"""ORB experiments against the project database (spec 2026-09-19-charges-orb-regime-design.md).

    .venv/bin/python scripts/orb_experiment.py tune
    .venv/bin/python scripts/orb_experiment.py holdout --range 30 --rr 2.0     # --rr none for no target

Tuning runs see 2026-06-17..2026-08-15 only. The holdout (2026-08-16..2026-09-15) has a fixed run id,
so it can be run once: choose the parameters from the tuning table first. The AI filter is the stub."""
import argparse
import logging
import sys
from dataclasses import replace as dc_replace
from datetime import date

from tradebot.ai.filter import build_filter
from tradebot.config import load_config
from tradebot.data.historical import HistoricalSource
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, ist_epoch
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


def run(cfg, strategy_name: str, run_id: str, window) -> object:
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is not None:
        sys.exit(f"run '{run_id}' already exists: this experiment has been run; report it with "
                 f"`tradebot report --run {run_id}`")
    symbols = list(load_universe(cfg.paths.universe).symbols)
    interval = cfg.execution.interval_minutes
    source = HistoricalSource.from_repo(repo, symbols, interval,
                                        ist_epoch(window[0], "00:00"), ist_epoch(window[1], "23:59"))
    if not source.bar_timestamps():
        sys.exit(f"no {interval}-minute candles in {window[0]}..{window[1]}; run fetch-data with this config")
    clock = SessionClock(cfg.session, interval)
    ai_cfg = dc_replace(cfg.ai, filter="stub")
    cfg = dc_replace(cfg, ai=ai_cfg)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    engine = BacktestEngine(cfg, repo, source, [build_strategy(strategy_name, strategy_params(cfg.strategy, strategy_name))],
                            broker, build_filter(ai_cfg, "", repo=repo, clock=clock), clock,
                            {s: 1 for s in symbols}, run_id)
    engine.run()
    return build_summary(repo, run_id, cfg.charges)


def with_orb(cfg, range_minutes, rr):
    strat = dict(cfg.strategy)
    strat["orb"] = dict(strat["orb"], range_minutes=range_minutes, reward_risk=rr)
    return dc_replace(cfg, strategy=strat)


def row(s) -> str:
    return (f"{s.run_id:<22} {s.trades:>6} {s.win_rate * 100:>6.1f}% {s.gross_pnl:>12,.0f} {s.charges:>10,.0f} "
            f"{s.total_pnl:>12,.0f} {s.r_on_risk:>10.3f}")


# "R on risk" is net PnL over rupees at risk, all trades pooled. It is the decision metric: unlike the
# unweighted per-trade Avg R, scrap-sized trades cannot dominate it, and its sign is the sign of net PnL.
HEADER = f"{'run':<22} {'trades':>6} {'win':>7} {'gross':>12} {'charges':>10} {'net':>12} {'R on risk':>10}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["tune", "holdout"])
    ap.add_argument("--config", default="config-orb.yaml")
    ap.add_argument("--range", dest="range_minutes", type=int, choices=[30, 60])
    ap.add_argument("--rr", help="1.5, 2.0 or none")
    a = ap.parse_args()
    cfg = load_config(a.config)
    print(HEADER)
    if a.phase == "tune":
        for range_minutes, rr in GRID:
            print(row(run(with_orb(cfg, range_minutes, rr), "orb", f"orb-t-{label(range_minutes, rr)}", TUNE)), flush=True)
        return
    if a.range_minutes is None or a.rr is None:
        sys.exit("holdout needs --range and --rr, chosen from the tuning table")
    rr = None if a.rr.lower() == "none" else float(a.rr)
    s = run(with_orb(cfg, a.range_minutes, rr), "orb", "orb-holdout", HOLDOUT)
    print(row(s))
    verdict = ("inconclusive: fewer than 30 holdout trades" if s.trades < 30
               else "holds up out of sample" if s.r_on_risk > 0 else "no edge out of sample")
    print(f"holdout verdict: {verdict}")


if __name__ == "__main__":
    main()
