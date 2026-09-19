"""Parameter sweep for the confluence strategy against the stored candles, on a scratch copy of the
database so the project DB keeps only deliberate runs.

    .venv/bin/python scripts/sweep_confluence.py grid.json [--db data/sweep.db] [--start D] [--end D]

grid.json maps a label to an override of the `strategy.confluence` section (nested dicts merge, so
{"weights": {"trend": 2.0}} changes one weight). Two reserved keys override other sections:
"_exec" (execution: slippage_pct, ...) and "_risk" (risk: per_trade_pct, ...). The AI filter is
always the stub. One backtest per variant over the window; the IS/OOS columns split the daily PnL at
--oos-from. Results (including "pnl") are net of charges, same as `tradebot backtest`. Results are
appended to <grid>.results.jsonl next to the grid file."""
import json, logging, sys, time
from dataclasses import replace as dc_replace
from datetime import datetime
from pathlib import Path

from tradebot.ai.filter import build_filter
from tradebot.config import load_config
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.report.summary import build_summary
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy, strategy_params
from tradebot.data.universe import load_universe

import argparse, shutil

START, END = datetime(2026, 6, 15), datetime(2026, 9, 12)
OOS_FROM = "2026-08-17"   # last ~19 trading days held out

logging.basicConfig(level=logging.WARNING)
logging.getLogger("tradebot").setLevel(logging.WARNING)


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def run_variant(cfg, label: str, over: dict):
    over = dict(over)
    exec_over = over.pop("_exec", {})
    risk_over = over.pop("_risk", {})
    strat_cfg = dict(cfg.strategy)
    strat_cfg["confluence"] = deep_merge(strat_cfg["confluence"], over)
    cfg = dc_replace(cfg, strategy=strat_cfg, execution=dc_replace(cfg.execution, **exec_over),
                     risk=dc_replace(cfg.risk, **risk_over))
    repo = Repo(connect(cfg.paths.db))
    uni = load_universe(cfg.paths.universe)
    symbols = list(uni.symbols)
    lots = {s: 1 for s in symbols}
    interval = cfg.execution.interval_minutes
    source = HistoricalSource.from_repo(repo, symbols, interval,
                                        ist_epoch(START.date(), "00:00"), ist_epoch(END.date(), "23:59"))
    run_id = f"sw-{label}-{int(time.time()*1000) % 10**8}"
    strategy = build_strategy("confluence", strategy_params(cfg.strategy, "confluence"))
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct,
                            charges=cfg.charges)
    ai_cfg = dc_replace(cfg.ai, filter="stub")
    clock = SessionClock(cfg.session, interval)
    ai = build_filter(ai_cfg, None, repo=repo, clock=clock)
    engine = BacktestEngine(dc_replace(cfg, ai=ai_cfg), repo, source, [strategy], broker, ai, clock, lots, run_id)
    rid = engine.run()
    s = build_summary(repo, rid, cfg.charges)
    is_pnl = sum(d["realised"] for d in s.days if d["date"] < OOS_FROM)
    oos_pnl = sum(d["realised"] for d in s.days if d["date"] >= OOS_FROM)
    ex = s.exit_reasons
    return {"label": label, "over": over, "trades": s.trades, "win": round(s.win_rate * 100, 1),
            "pnl": round(s.total_pnl), "is": round(is_pnl), "oos": round(oos_pnl), "avg_r": round(s.avg_r, 2),
            "mdd": round(s.max_drawdown_equity), "stop": ex.get("STOP", 0), "target": ex.get("TARGET", 0),
            "sq": ex.get("SQUARE_OFF", 0), "run": rid}


def main():
    global START, END, OOS_FROM
    ap = argparse.ArgumentParser()
    ap.add_argument("grid")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="data/sweep.db", help="scratch copy of the DB (created from paths.db if missing)")
    ap.add_argument("--start", default=START.strftime("%Y-%m-%d"))
    ap.add_argument("--end", default=END.strftime("%Y-%m-%d"))
    ap.add_argument("--oos-from", default=OOS_FROM)
    a = ap.parse_args()
    START, END = datetime.strptime(a.start, "%Y-%m-%d"), datetime.strptime(a.end, "%Y-%m-%d")
    OOS_FROM = a.oos_from
    cfg = load_config(a.config)
    if not Path(a.db).exists():
        shutil.copy(cfg.paths.db, a.db)
    cfg = dc_replace(cfg, paths=dc_replace(cfg.paths, db=a.db, logs=None))
    grid_file = Path(a.grid)
    grid = json.loads(grid_file.read_text())
    out = grid_file.with_name(grid_file.stem + ".results.jsonl")
    print(f"{'label':<28}{'trades':>7}{'win%':>7}{'pnl':>9}{'IS':>9}{'OOS':>9}{'avgR':>7}{'mdd':>8}{'stop':>6}{'tgt':>5}{'sq':>4}")
    with out.open("a") as f:
        for label, over in grid.items():
            t0 = time.time()
            r = run_variant(cfg, label, over)
            f.write(json.dumps(r) + "\n"); f.flush()
            print(f"{r['label']:<28}{r['trades']:>7}{r['win']:>7}{r['pnl']:>9}{r['is']:>9}{r['oos']:>9}{r['avg_r']:>7}{r['mdd']:>8}{r['stop']:>6}{r['target']:>5}{r['sq']:>4}  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
