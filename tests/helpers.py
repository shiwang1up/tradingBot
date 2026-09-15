# tests/helpers.py
"""Shared test builders: a config on disk and deterministic synthetic candles."""
import math
from datetime import date

import yaml

from tradebot.config import Config, load_config
from tradebot.engine.clock import ist_epoch
from tradebot.types import Candle

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
        raw[key] = {**raw[key], **val} if isinstance(val, dict) else val
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


class FakeTime:
    """Deterministic wall clock for the paper engine: `sleep` advances `now`."""

    def __init__(self, start_ts: int):
        self.t = float(start_ts)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
