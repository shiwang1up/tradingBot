"""Typed config loaded from config.yaml plus secrets from .env.

Conventions:
- Every ``*_pct`` field is a human percent: ``1.0`` means 1%. Divide by 100 at the point of use.
- Values are type-checked and coerced at load time so a bad config fails here, not mid-session.
- Process environment wins over ``.env`` so an operator can override credentials per run.
"""
from __future__ import annotations

import copy
import dataclasses
import math
import os
import re
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
    min_risk_fraction: float = 0.5   # reject a trade margin has shrunk below this fraction of the planned risk; 0 disables


@dataclass(frozen=True)
class AIConfig:
    filter: str
    model: str
    candles_in_context: int
    on_failure: str
    effort: str = "low"              # low | medium | high | xhigh | max
    max_tokens: int = 4000           # a backstop, not a cost knob: unused output is not billed
    timeout_sec: int = 60            # adaptive thinking can take a while; the SDK retries twice on top
    max_calls_per_run: int = 10000   # hard stop on spend per backtest; later bars use on_failure
    max_consecutive_failures: int = 20  # circuit breaker: abort the run when the API is dead
    # USD per million tokens, used only for the cost line in reports (Opus 5 list prices).
    price_in_per_mtok: float = 5.0
    price_out_per_mtok: float = 25.0
    price_cache_read_per_mtok: float = 0.5     # prompt-cache hits bill ~0.1x input
    price_cache_write_per_mtok: float = 6.25   # prompt-cache writes bill ~1.25x input


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
    bar_grace_sec: int = 5      # paper: seconds to wait after a bar boundary before fetching the closed bar
    warmup_bars: int = 300      # paper: stored bars replayed per symbol before the first live bar (~4 days of 5m)
    index_symbol: str = ""      # index fetched and stored with the universe for the regime filter; never traded


@dataclass(frozen=True)
class PathsConfig:
    db: str
    logs: str
    instruments: str
    kill_switch: str
    universe: str
    brokers: str = "brokers.yaml"


@dataclass(frozen=True)
class ChargesConfig:
    """Groww intraday equity schedule. ``*_pct`` fields are human percents of order value.
    Every field has a default, so the section may be left out of config.yaml."""
    enabled: bool = True
    # Empty means "use the rate fields on this dataclass", which is what every config in this
    # repository did before brokers.yaml existed. A name selects a schedule from paths.brokers and
    # replaces every rate below. See docs/superpowers/specs/2026-09-22-broker-cost-model-design.md.
    broker: str = ""
    brokerage_pct: float = 0.1         # per order ...
    brokerage_max: float = 20.0        # ... capped at this many rupees
    brokerage_min: float = 5.0         # ... and floored at this
    stt_sell_pct: float = 0.025        # sell side only
    exchange_txn_pct: float = 0.00297  # NSE, both sides
    sebi_pct: float = 0.0001           # both sides
    stamp_buy_pct: float = 0.003       # buy side only
    # Delivery (CNC) differs from intraday in exactly three ways. Rates duplicated in
    # scripts/daily_screen.py as module constants; that script's committed results depend on
    # those exact numbers, so the two are deliberately not unified. Keep them in step.
    delivery_stt_pct: float = 0.1       # both sides, against 0.025% sell-side intraday
    delivery_stamp_buy_pct: float = 0.015   # buy side, against 0.003% intraday
    dp_charge: float = 15.34            # depository, flat, per sell
    gst_pct: float = 18.0              # on brokerage + exchange txn + SEBI


REGIME_SOURCES = ("index", "composite")


@dataclass(frozen=True)
class RegimeConfig:
    """Market-direction gate: longs only while the index is above its EMA, shorts only while at or below."""
    enabled: bool = False
    source: str = "index"    # index: data.index_symbol candles | composite: equal-weight mean of universe returns
    ema_period: int = 20     # bars of the run's interval


@dataclass(frozen=True)
class Secrets:
    groww_api_key: str
    groww_totp_secret: str  # TOTP flow: base32 secret from the API keys page (no daily approval)
    groww_api_secret: str   # approval flow: API secret paired with the JWT api key (needs daily approval)
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
    charges: ChargesConfig = ChargesConfig()
    regime: RegimeConfig = RegimeConfig()


_COERCE = {"float": float, "int": int, "bool": bool, "str": str, "tuple": tuple}


def _coerce(section: str, name: str, type_name: str, value: Any) -> Any:
    """Coerce a YAML scalar to the dataclass field type, or raise a config error naming the key."""
    where = f"config.yaml {section}.{name}"
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{where}: expected true/false, got {value!r}")
    if type_name == "str":
        # Strict: PyYAML turns an unquoted 15:30 into the integer 930 and an empty value into None.
        if not isinstance(value, str):
            raise ValueError(f"{where}: expected a quoted string, got {value!r}")
        return value
    if type_name == "tuple":
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{where}: expected a list, got {value!r}")
        return tuple(value)
    if isinstance(value, bool):  # int and float must not accept true/false
        raise ValueError(f"{where}: expected {type_name}, got {value!r}")
    if type_name == "int" and isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{where}: expected an integer, got {value!r}")
    try:
        return _COERCE[type_name](value)
    except (TypeError, ValueError, KeyError) as e:
        raise ValueError(f"{where}: expected {type_name}, got {value!r}") from e


def _section(raw: dict, name: str, cls):
    if name not in raw or raw[name] is None:
        raise ValueError(f"config.yaml missing section: {name}")
    value = raw[name]
    if not isinstance(value, dict):
        raise ValueError(f"config.yaml section '{name}' must be a mapping, got {value!r}")
    given = dict(value)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(given) - set(fields)
    if unknown:
        raise ValueError(f"config.yaml section '{name}' has unknown keys: {sorted(unknown)}")
    required = {n for n, f in fields.items()
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    missing = required - set(given)
    if missing:
        raise ValueError(f"config.yaml section '{name}' missing keys: {sorted(missing)}")
    return cls(**{k: _coerce(name, k, fields[k].type, v) for k, v in given.items()})


def _optional_section(raw: dict, name: str, cls):
    """For a section whose every field has a default: absent or empty in YAML means all defaults."""
    if raw.get(name) is None:
        return cls()
    return _section(raw, name, cls)


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
AI_FILTERS = ("stub", "claude", "claude_cached")
AI_ON_FAILURE = ("reject", "pass_through")
AI_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _validate(cfg: "Config") -> None:
    r, e, s, a, d = cfg.risk, cfg.execution, cfg.session, cfg.ai, cfg.data
    checks = [
        (r.per_trade_pct > 0, "risk.per_trade_pct must be > 0"),
        (r.daily_loss_cap_pct > 0, "risk.daily_loss_cap_pct must be > 0"),
        (r.adopted_stop_pct > 0, "risk.adopted_stop_pct must be > 0"),
        (r.mis_leverage >= 1, "risk.mis_leverage must be >= 1"),
        (r.max_open_positions >= 1, "risk.max_open_positions must be >= 1"),
        (r.max_entries_per_day >= 1, "risk.max_entries_per_day must be >= 1"),
        (r.cooldown_bars >= 0, "risk.cooldown_bars must be >= 0"),
        (0.0 <= r.min_risk_fraction <= 1.0, "risk.min_risk_fraction must be between 0 and 1"),
        (e.slippage_pct >= 0, "execution.slippage_pct must be >= 0"),
        (e.entry_buffer_pct >= 0, "execution.entry_buffer_pct must be >= 0"),
        (e.bar_deadline_sec > 0, "execution.bar_deadline_sec must be > 0"),
        (e.interval_minutes > 0, "execution.interval_minutes must be > 0"),
        (d.official_fetch_concurrency >= 1, "data.official_fetch_concurrency must be >= 1"),
        (d.bar_grace_sec >= 0, "data.bar_grace_sec must be >= 0"),
        (d.bar_grace_sec < e.bar_deadline_sec, "data.bar_grace_sec must be below execution.bar_deadline_sec"),
        (d.bar_grace_sec < e.interval_minutes * 60, "data.bar_grace_sec must be shorter than a bar"),
        (d.warmup_bars >= 1, "data.warmup_bars must be >= 1"),
        (a.candles_in_context >= 1, "ai.candles_in_context must be >= 1"),
        (a.filter in AI_FILTERS, f"ai.filter must be one of {AI_FILTERS}"),
        (a.on_failure in AI_ON_FAILURE, f"ai.on_failure must be one of {AI_ON_FAILURE}"),
        (a.effort in AI_EFFORTS, f"ai.effort must be one of {AI_EFFORTS}"),
        (a.max_tokens >= 1, "ai.max_tokens must be >= 1"),
        (a.timeout_sec >= 1, "ai.timeout_sec must be >= 1"),
        (a.max_calls_per_run >= 1, "ai.max_calls_per_run must be >= 1"),
        (a.max_consecutive_failures >= 1, "ai.max_consecutive_failures must be >= 1"),
        (a.price_in_per_mtok >= 0, "ai.price_in_per_mtok must be >= 0"),
        (a.price_out_per_mtok >= 0, "ai.price_out_per_mtok must be >= 0"),
        (a.price_cache_read_per_mtok >= 0, "ai.price_cache_read_per_mtok must be >= 0"),
        (a.price_cache_write_per_mtok >= 0, "ai.price_cache_write_per_mtok must be >= 0"),
    ]
    ch = cfg.charges
    for f in dataclasses.fields(ch):
        if f.type == "float":  # under `from __future__ import annotations`, f.type is this literal string
            v = getattr(ch, f.name)
            checks.append((math.isfinite(v) and v >= 0, f"charges.{f.name} must be a finite number >= 0"))
    checks.append((ch.brokerage_min <= ch.brokerage_max, "charges.brokerage_min must not exceed charges.brokerage_max"))
    for name in ("open", "close", "square_off", "no_new_entries_after"):
        checks.append((bool(_HHMM.match(getattr(s, name))), f'session.{name} must be a quoted "HH:MM" time'))
    checks.append((all(isinstance(h, str) for h in s.holidays), "session.holidays must be a list of ISO date strings"))
    orb = cfg.strategy.get("orb")
    if isinstance(orb, dict):  # the strategy counts range bars itself, so its copy of these must not drift
        checks.append((orb.get("interval_minutes") == e.interval_minutes,
                       f"strategy.orb.interval_minutes must equal execution.interval_minutes ({e.interval_minutes}), "
                       f"got {orb.get('interval_minutes')!r}"))
        checks.append((orb.get("session_open") == s.open,
                       f"strategy.orb.session_open must equal session.open ({s.open!r}), "
                       f"got {orb.get('session_open')!r}"))
    g = cfg.regime
    checks += [
        (g.source in REGIME_SOURCES, f"regime.source must be one of {REGIME_SOURCES}"),
        (g.ema_period >= 2, "regime.ema_period must be >= 2"),
        (not (g.enabled and g.source == "index") or bool(d.index_symbol),
         "data.index_symbol must be set when regime.enabled uses source: index"),
    ]
    for ok, msg in checks:
        if not ok:
            raise ValueError(f"config.yaml {msg}")


def _apply_broker(charges: "ChargesConfig", brokers_path: str, raw: dict) -> "ChargesConfig":
    """Replace every rate on `charges` with the named schedule's, keeping `enabled` and `broker`.

    Imported here rather than at module scope: tradebot.brokers imports ChargesConfig from this
    module, and a top-level import would be circular.
    """
    from tradebot.brokers import BrokerScheduleError, load_brokers
    given = set((raw.get("charges") or {})) - {"broker", "enabled"}
    if given:
        raise ValueError(
            f"config.yaml charges: {sorted(given)} cannot be set alongside 'broker': the schedule "
            f"supplies every rate. Remove them, or drop 'broker' and set them all yourself.")
    try:
        brokers = load_brokers(brokers_path)
    except BrokerScheduleError as e:
        raise ValueError(f"config.yaml charges.broker: {e}") from e
    if charges.broker not in brokers:
        raise ValueError(f"config.yaml charges.broker: unknown broker {charges.broker!r}; "
                         f"brokers.yaml has {sorted(brokers)}")
    return dataclasses.replace(brokers[charges.broker].charges,
                               enabled=charges.enabled, broker=charges.broker)


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
    raw_for_session = raw
    if isinstance(raw.get("session"), dict) and isinstance(raw["session"].get("holidays"), (list, tuple)):
        session_raw = dict(raw["session"])
        session_raw["holidays"] = tuple(h if h is None else str(h) for h in session_raw["holidays"])  # dates -> ISO
        raw_for_session = {**raw, "session": session_raw}
    paths = _section(raw, "paths", PathsConfig)
    base = p.parent
    paths = PathsConfig(**{f.name: str(base / getattr(paths, f.name)) if not Path(getattr(paths, f.name)).is_absolute()
                           else getattr(paths, f.name) for f in dataclasses.fields(PathsConfig)})
    cfg = Config(
        capital=capital,
        risk=_section(raw, "risk", RiskConfig),
        strategy=copy.deepcopy(raw.get("strategy") or {}),
        ai=_section(raw, "ai", AIConfig),
        execution=_section(raw, "execution", ExecutionConfig),
        session=_section(raw_for_session, "session", SessionConfig),
        data=_section(raw, "data", DataConfig),
        paths=paths,  # relative entries are resolved against the config file's directory, not the CWD
        secrets=Secrets(
            groww_api_key=os.environ.get("GROWW_API_KEY", ""),
            groww_totp_secret=os.environ.get("GROWW_TOTP_SECRET", ""),
            groww_api_secret=os.environ.get("GROWW_API_SECRET", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
        raw=raw,
        charges=_optional_section(raw, "charges", ChargesConfig),
        regime=_optional_section(raw, "regime", RegimeConfig),
    )
    if cfg.charges.broker:
        cfg = dataclasses.replace(cfg, charges=_apply_broker(cfg.charges, cfg.paths.brokers, raw))
    _validate(cfg)
    return cfg


def resolved_config(cfg: Config) -> dict:
    """The config as actually used (defaults applied, paths resolved), minus secrets and raw YAML.
    Stored with every run so a report is reproducible (spec 9)."""
    d = dataclasses.asdict(cfg)
    d.pop("secrets", None)
    d.pop("raw", None)
    return d
