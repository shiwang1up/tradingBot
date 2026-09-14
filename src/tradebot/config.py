"""Typed config loaded from config.yaml plus secrets from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class RiskConfig:
    per_trade_pct: float
    daily_loss_cap_pct: float
    flatten_on_daily_cap: bool
    max_entries_per_day: int
    max_open_positions: int
    cooldown_bars: int
    mis_leverage: float
    adopted_stop_pct: float


@dataclass(frozen=True)
class AIConfig:
    filter: str
    model: str
    candles_in_context: int
    on_failure: str


@dataclass(frozen=True)
class ExecutionConfig:
    slippage_pct: float
    entry_buffer_pct: float
    bar_deadline_sec: int
    interval_minutes: int


@dataclass(frozen=True)
class SessionConfig:
    open: str
    close: str
    square_off: str
    no_new_entries_after: str
    holidays: tuple[str, ...]


@dataclass(frozen=True)
class DataConfig:
    official_fetch_concurrency: int


@dataclass(frozen=True)
class PathsConfig:
    db: str
    logs: str
    instruments: str
    kill_switch: str
    universe: str


@dataclass(frozen=True)
class Secrets:
    groww_api_key: str
    groww_totp_secret: str
    anthropic_api_key: str


@dataclass(frozen=True)
class Config:
    capital: float
    risk: RiskConfig
    strategy: dict[str, dict[str, Any]]
    ai: AIConfig
    execution: ExecutionConfig
    session: SessionConfig
    data: DataConfig
    paths: PathsConfig
    secrets: Secrets
    raw: dict[str, Any]


def _section(raw: dict, name: str, cls):
    try:
        return cls(**raw[name])
    except KeyError as e:
        raise ValueError(f"config.yaml missing section or key: {name} {e}") from e
    except TypeError as e:
        raise ValueError(f"config.yaml section '{name}' has wrong keys: {e}") from e


def load_config(path: str | Path = "config.yaml", env_path: str | Path = ".env") -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    if Path(env_path).exists():
        load_dotenv(env_path, override=True)
    session_raw = dict(raw["session"])
    session_raw["holidays"] = tuple(session_raw.get("holidays") or ())
    return Config(
        capital=float(raw["capital"]),
        risk=_section(raw, "risk", RiskConfig),
        strategy=dict(raw.get("strategy", {})),
        ai=_section(raw, "ai", AIConfig),
        execution=_section(raw, "execution", ExecutionConfig),
        session=SessionConfig(**session_raw),
        data=_section(raw, "data", DataConfig),
        paths=_section(raw, "paths", PathsConfig),
        secrets=Secrets(
            groww_api_key=os.environ.get("GROWW_API_KEY", ""),
            groww_totp_secret=os.environ.get("GROWW_TOTP_SECRET", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
        raw=raw,
    )
