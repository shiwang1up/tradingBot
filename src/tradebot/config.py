"""Typed config loaded from config.yaml plus secrets from .env.

Conventions:
- Every ``*_pct`` field is a human percent: ``1.0`` means 1%. Divide by 100 at the point of use.
- Values are type-checked and coerced at load time so a bad config fails here, not mid-session.
- Process environment wins over ``.env`` so an operator can override credentials per run.
"""
from __future__ import annotations

import copy
import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class RiskConfig:
    """``*_pct`` fields are human percents (1.0 == 1%)."""
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
    """``*_pct`` fields are human percents (0.05 == 0.05% == 5 bps)."""
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
    holidays: tuple  # of ISO date strings


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
    strategy: dict  # strategy name -> params; each strategy validates its own keys
    ai: AIConfig
    execution: ExecutionConfig
    session: SessionConfig
    data: DataConfig
    paths: PathsConfig
    secrets: Secrets
    raw: dict


_COERCE = {"float": float, "int": int, "bool": bool, "str": str, "tuple": tuple}


def _coerce(section: str, name: str, type_name: str, value: Any) -> Any:
    """Coerce a YAML scalar to the dataclass field type, or raise a config error naming the key."""
    where = f"config.yaml {section}.{name}"
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{where}: expected true/false, got {value!r}")
    if type_name == "int":
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(f"{where}: expected an integer, got {value!r}")
    try:
        return _COERCE[type_name](value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{where}: expected {type_name}, got {value!r}") from e


def _section(raw: dict, name: str, cls):
    if name not in raw or not isinstance(raw[name], dict):
        raise ValueError(f"config.yaml missing section: {name}")
    given = dict(raw[name])
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(given) - set(fields)
    if unknown:
        raise ValueError(f"config.yaml section '{name}' has unknown keys: {sorted(unknown)}")
    missing = set(fields) - set(given)
    if missing:
        raise ValueError(f"config.yaml section '{name}' missing keys: {sorted(missing)}")
    return cls(**{k: _coerce(name, k, fields[k].type, v) for k, v in given.items()})


def _validate_risk(r: RiskConfig) -> None:
    checks = [
        (r.per_trade_pct > 0, "risk.per_trade_pct must be > 0"),
        (r.daily_loss_cap_pct > 0, "risk.daily_loss_cap_pct must be > 0"),
        (r.mis_leverage >= 1, "risk.mis_leverage must be >= 1"),
        (r.max_open_positions >= 1, "risk.max_open_positions must be >= 1"),
        (r.max_entries_per_day >= 1, "risk.max_entries_per_day must be >= 1"),
        (r.cooldown_bars >= 0, "risk.cooldown_bars must be >= 0"),
    ]
    for ok, msg in checks:
        if not ok:
            raise ValueError(f"config.yaml {msg}")


def load_config(path: Union[str, Path] = "config.yaml", env_path: Union[str, Path] = ".env") -> Config:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if Path(env_path).exists():
        load_dotenv(env_path)  # process env wins; .env only fills gaps
    if "capital" not in raw:
        raise ValueError("config.yaml missing 'capital'")
    capital = _coerce("(root)", "capital", "float", raw["capital"])
    if capital <= 0:
        raise ValueError("config.yaml capital must be > 0")
    session_raw = dict(raw.get("session") or {})
    if "holidays" in session_raw:
        session_raw["holidays"] = tuple(str(h) for h in (session_raw["holidays"] or ()))
    risk = _section(raw, "risk", RiskConfig)
    _validate_risk(risk)
    return Config(
        capital=capital,
        risk=risk,
        strategy=copy.deepcopy(raw.get("strategy") or {}),
        ai=_section(raw, "ai", AIConfig),
        execution=_section(raw, "execution", ExecutionConfig),
        session=_section({**raw, "session": session_raw}, "session", SessionConfig),
        data=_section(raw, "data", DataConfig),
        paths=_section(raw, "paths", PathsConfig),
        secrets=Secrets(
            groww_api_key=os.environ.get("GROWW_API_KEY", ""),
            groww_totp_secret=os.environ.get("GROWW_TOTP_SECRET", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
        raw=raw,
    )
