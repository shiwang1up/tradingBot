# tests/helpers.py
"""Shared test builders: a config on disk and deterministic synthetic candles."""
import math
from datetime import date

import yaml

from tradebot.ai.filter import StubFilter
from tradebot.config import Config, load_config
from tradebot.data.historical import HistoricalSource
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.strategy.base import Strategy
from tradebot.types import Candle, Signal, round_tick_down, round_tick_up

MON = date(2026, 9, 14)  # a Monday; shared by test_engine.py and test_engine_regime.py

BASE_CONFIG = {
    "capital": 100000,
    "risk": {"per_trade_pct": 1.0, "daily_loss_cap_pct": 3.0, "flatten_on_daily_cap": False,
             "max_entries_per_day": 20, "max_open_positions": 5, "cooldown_bars": 3,
             "mis_leverage": 5.0, "adopted_stop_pct": 1.5},
    "strategy": {"ema_rsi": {"fast": 9, "slow": 21, "rsi_period": 14, "rsi_long_min": 55,
                             "rsi_short_max": 45, "atr_period": 14, "atr_stop_mult": 1.5,
                             "reward_risk": 2.0, "min_stop_pct": 0.1, "product": "MIS"},
                 "confluence": {"threshold": 3.0, "adx_min": 20, "product": "MIS"}},
    "ai": {"filter": "stub", "model": "claude-opus-5", "candles_in_context": 30, "on_failure": "reject"},
    "execution": {"slippage_pct": 0.05, "entry_buffer_pct": 0.1, "bar_deadline_sec": 60, "interval_minutes": 5},
    "session": {"open": "09:15", "close": "15:30", "square_off": "15:10",
                "no_new_entries_after": "14:45", "holidays": []},
    "data": {"official_fetch_concurrency": 5},
    "paths": {"db": "data/tradebot.db", "logs": "data/logs", "instruments": "data/instruments.csv",
              "kill_switch": "KILL", "universe": "universe.yaml"},
    "charges": {"enabled": False},  # tests pin pre-charges numbers (golden-trades fixture); opt in with charges={"enabled": True}
}


def make_config(tmp_path, **overrides) -> Config:
    """Write a config.yaml under tmp_path with db/kill/universe paths pointing into tmp_path."""
    raw = yaml.safe_load(yaml.safe_dump(BASE_CONFIG))  # deep copy
    raw["paths"] = {
        "db": str(tmp_path / "tradebot.db"), "logs": str(tmp_path / "logs"),
        "instruments": str(tmp_path / "instruments.csv"), "kill_switch": str(tmp_path / "KILL"),
        "universe": str(tmp_path / "universe.yaml"),
    }
    for key, val in overrides.items():          # e.g. risk={"max_open_positions": 1}
        raw[key] = {**(raw.get(key) or {}), **val} if isinstance(val, dict) else val
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p, tmp_path / "nonexistent.env")


def synth_candles(symbol: str, days: list[date], phase: float = 0.0, seed: int = 7,
                  bars_per_day: int = 75, noise: float = 1.0, gap: float = 0.08) -> list[Candle]:
    """Deterministic wavy price path with LCG noise so some trades stop out.
    5-minute bars from 09:15, `bars_per_day` per day. Each bar opens within +/- `gap` of the
    previous close, like real intraday bars, so the entry buffer does not reject every fill.
    Same inputs always give the same candles."""
    out, i, x, prev_close = [], 0, seed, None

    def rnd() -> float:  # linear congruential generator, uniform in [0, 1)
        nonlocal x
        x = (1103515245 * x + 12345) % (2 ** 31)
        return x / 2 ** 31

    for d in days:
        open_ts = ist_epoch(d, "09:15")
        for k in range(bars_per_day):
            base = 100.0 + 6.0 * math.sin((i + phase) / 7.0) + 0.02 * i
            c = round(base + (rnd() - 0.5) * 2 * noise, 2)
            o = round(c if prev_close is None else prev_close + (rnd() - 0.5) * 2 * gap, 2)
            h = round(max(o, c) + 0.2 + rnd() * noise, 2)
            l = round(min(o, c) - 0.2 - rnd() * noise, 2)
            out.append(Candle(symbol, open_ts + k * 300, o, h, l, c, 1000))
            prev_close = c
            i += 1
    return out


class FixedStrategy(Strategy):
    """Fires exactly the signals it is told to and records every symbol it was shown.
    `fires` maps (symbol, bar_ts) to (direction, priority)."""
    name = "fixed"
    product = "MIS"

    def __init__(self, fires: dict):
        self.fires = dict(fires)
        self.seen: set = set()
        self.seen_bars: set = set()  # (symbol, bar_ts) pairs actually shown to on_candle

    def on_candle(self, candle: Candle):
        self.seen.add(candle.symbol)
        self.seen_bars.add((candle.symbol, candle.ts))
        hit = self.fires.get((candle.symbol, candle.ts))
        if hit is None:
            return None
        direction, priority = hit
        stop = round_tick_down(candle.close * 0.99) if direction == "LONG" else round_tick_up(candle.close * 1.01)
        return Signal(self.name, candle.symbol, direction, candle.close, stop, None, "MIS", candle.ts, priority=priority)

    def is_ready(self, symbol: str) -> bool:
        return True

    def snapshot(self, symbol: str) -> dict:
        return {}

    def reset(self, symbol: str) -> None:
        pass


def run_fixed(repo, cfg, candles, fires, symbols, run_id="t1"):
    """Run a FixedStrategy over `candles` through a real BacktestEngine and return the strategy
    (so a caller can inspect what it was shown). Shared by test_engine.py and test_engine_regime.py."""
    repo.insert_candles(candles, interval=5)
    src = HistoricalSource.from_repo(repo, symbols, 5, 0, 2_000_000_000)
    strat = FixedStrategy(fires)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    BacktestEngine(cfg, repo, src, [strat], broker, StubFilter(), SessionClock(cfg.session, 5),
                   {s: 1 for s in symbols}, run_id).run()
    return strat


def decisions(repo, run_id="t1"):
    rows = repo.conn.execute(
        "SELECT s.symbol, s.direction, d.approved, d.reason FROM signals s JOIN risk_decisions d ON d.signal_id = s.id "
        "WHERE s.run_id=? ORDER BY s.id", (run_id,)).fetchall()
    return [(r["symbol"], r["direction"], r["approved"], r["reason"]) for r in rows]


class FakeTime:
    """Deterministic wall clock for the paper engine: `sleep` advances `now`."""

    def __init__(self, start_ts: int):
        self.t = float(start_ts)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
