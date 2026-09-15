# tests/test_config.py
import textwrap

import pytest

from tradebot.config import load_config
from tests.helpers import make_config

YAML = textwrap.dedent("""
capital: 100000
risk:
  per_trade_pct: 1.0
  daily_loss_cap_pct: 3.0
  flatten_on_daily_cap: false
  max_entries_per_day: 20
  max_open_positions: 5
  cooldown_bars: 3
  mis_leverage: 5.0
  adopted_stop_pct: 1.5
strategy:
  ema_rsi:
    fast: 9
    slow: 21
    rsi_period: 14
    rsi_long_min: 55
    rsi_short_max: 45
    atr_period: 14
    atr_stop_mult: 1.5
    reward_risk: 2.0
    product: MIS
ai:
  filter: stub
  model: claude-sonnet-5
  candles_in_context: 30
  on_failure: reject
execution:
  slippage_pct: 0.05
  entry_buffer_pct: 0.1
  bar_deadline_sec: 60
  interval_minutes: 5
session:
  open: "09:15"
  close: "15:30"
  square_off: "15:10"
  no_new_entries_after: "14:45"
  holidays: ["2026-10-02"]
data:
  official_fetch_concurrency: 5
paths:
  db: data/tradebot.db
  logs: data/logs
  instruments: data/instruments.csv
  kill_switch: KILL
  universe: universe.yaml
""")


def _write(tmp_path, text=YAML):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_load_config_reads_all_sections(tmp_path):
    cfg_path = _write(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\nANTHROPIC_API_KEY=a\n")

    cfg = load_config(cfg_path, env_path)

    assert cfg.capital == 100000
    assert cfg.risk.per_trade_pct == 1.0
    assert cfg.risk.cooldown_bars == 3
    assert cfg.strategy["ema_rsi"]["fast"] == 9
    assert cfg.ai.filter == "stub"
    assert cfg.execution.interval_minutes == 5
    assert cfg.session.holidays == ("2026-10-02",)
    assert cfg.paths.kill_switch == str(tmp_path / "KILL")  # relative paths resolve against the config dir
    assert cfg.data.official_fetch_concurrency == 5
    assert cfg.secrets.groww_api_key == "k"
    assert cfg.raw["capital"] == 100000


def test_missing_env_yields_empty_secrets(tmp_path):
    cfg = load_config(_write(tmp_path), tmp_path / "missing.env")
    assert cfg.secrets.groww_api_key == ""


def test_process_env_beats_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("GROWW_API_KEY", "from-process")
    env_path = tmp_path / ".env"
    env_path.write_text("GROWW_API_KEY=from-file\n")
    cfg = load_config(_write(tmp_path), env_path)
    assert cfg.secrets.groww_api_key == "from-process"


def test_unquoted_holiday_dates_are_normalised_to_iso_strings(tmp_path):
    cfg = load_config(_write(tmp_path, YAML.replace('["2026-10-02"]', "[2026-10-02]")), tmp_path / "x.env")
    assert cfg.session.holidays == ("2026-10-02",)


@pytest.mark.parametrize("broken, fragment", [
    (YAML.replace("cooldown_bars: 3", "cooldown_bar: 3"), "risk"),
    (YAML.replace("  square_off: \"15:10\"\n", ""), "session"),
    (YAML.replace("capital: 100000\n", ""), "capital"),
    (YAML.replace("per_trade_pct: 1.0", "per_trade_pct: one"), "risk.per_trade_pct"),
    (YAML.replace("max_open_positions: 5", "max_open_positions: 2.5"), "risk.max_open_positions"),
    (YAML.replace("flatten_on_daily_cap: false", "flatten_on_daily_cap: nope"), "risk.flatten_on_daily_cap"),
    (YAML.replace("mis_leverage: 5.0", "mis_leverage: 0.5"), "mis_leverage"),
    (YAML.replace("capital: 100000", "capital: true"), "capital"),
    (YAML.replace('close: "15:30"', "close: 15:30"), "session.close"),        # PyYAML sexagesimal -> 930
    (YAML.replace('square_off: "15:10"', 'square_off: "3:10pm"'), "session.square_off"),
    (YAML.replace('holidays: ["2026-10-02"]', 'holidays: "2026-10-02"'), "session.holidays"),
    (YAML.replace("interval_minutes: 5", "interval_minutes: 0"), "interval_minutes"),
    (YAML.replace('holidays: ["2026-10-02"]', "holidays: [null]"), "session.holidays"),
    (YAML[:YAML.index("session:")] + YAML[YAML.index("data:"):], "missing section: session"),
    (YAML.replace("on_failure: reject", "on_failure: maybe"), "ai.on_failure"),
    (YAML.replace("filter: stub", "filter: gpt"), "ai.filter"),
    ("", "capital"),
])
def test_bad_config_fails_at_load_with_key_named(tmp_path, broken, fragment):
    with pytest.raises(ValueError) as e:
        load_config(_write(tmp_path, broken), tmp_path / "x.env")
    assert fragment in str(e.value)


def test_strategy_params_are_not_aliased_to_raw(tmp_path):
    cfg = load_config(_write(tmp_path), tmp_path / "x.env")
    cfg.strategy["ema_rsi"]["fast"] = 999
    assert cfg.raw["strategy"]["ema_rsi"]["fast"] == 9


def test_relative_paths_resolve_against_config_directory(tmp_path):
    sub = tmp_path / "cfg"
    sub.mkdir()
    cfg = load_config(_write(sub), sub / "x.env")
    assert cfg.paths.db == str(sub / "data" / "tradebot.db")
    assert cfg.paths.universe == str(sub / "universe.yaml")
    absolute = YAML.replace("db: data/tradebot.db", "db: /abs/elsewhere.db")
    assert load_config(_write(sub, absolute), sub / "x.env").paths.db == "/abs/elsewhere.db"


def test_ai_section_defaults_and_validation(tmp_path):
    cfg = load_config(_write(tmp_path), tmp_path / "x.env")
    assert (cfg.ai.effort, cfg.ai.max_tokens, cfg.ai.timeout_sec, cfg.ai.max_calls_per_run) == ("low", 4000, 60, 10000)
    assert (cfg.ai.price_in_per_mtok, cfg.ai.price_out_per_mtok) == (5.0, 25.0)
    assert (cfg.ai.price_cache_read_per_mtok, cfg.ai.price_cache_write_per_mtok) == (0.5, 6.25)
    over = load_config(_write(tmp_path, YAML.replace("on_failure: reject", 'on_failure: reject\n  effort: max\n  max_tokens: "2500"')),
                       tmp_path / "x.env")
    assert (over.ai.effort, over.ai.max_tokens) == ("max", 2500)  # YAML overrides a default and is coerced
    for broken, frag in [
        (YAML.replace("on_failure: reject", "on_failure: reject\n  effort: turbo"), "ai.effort"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  max_calls_per_run: 0"), "ai.max_calls_per_run"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  max_tokens: 0"), "ai.max_tokens"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  timeout_sec: 0"), "ai.timeout_sec"),
        (YAML.replace("on_failure: reject", "on_failure: reject\n  price_out_per_mtok: -1"), "ai.price_out_per_mtok"),
    ]:
        with pytest.raises(ValueError) as e:
            load_config(_write(tmp_path, broken), tmp_path / "x.env")
        assert frag in str(e.value)


def test_data_section_defaults_and_validation(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.data.bar_grace_sec == 5
    assert cfg.data.warmup_bars == 300
    cfg2 = make_config(tmp_path, data={"official_fetch_concurrency": 5, "bar_grace_sec": 0, "warmup_bars": 50})
    assert cfg2.data.bar_grace_sec == 0 and cfg2.data.warmup_bars == 50
    with pytest.raises(ValueError, match="data.warmup_bars must be >= 1"):
        make_config(tmp_path, data={"official_fetch_concurrency": 5, "warmup_bars": 0})
    with pytest.raises(ValueError, match="data.bar_grace_sec must be >= 0"):
        make_config(tmp_path, data={"official_fetch_concurrency": 5, "bar_grace_sec": -1})


def test_bar_grace_must_fit_under_the_deadline_and_inside_a_bar(tmp_path):
    with pytest.raises(ValueError, match="below execution.bar_deadline_sec"):
        make_config(tmp_path, data={"official_fetch_concurrency": 5, "bar_grace_sec": 60})
    with pytest.raises(ValueError, match="shorter than a bar"):
        make_config(tmp_path, execution={"slippage_pct": 0.05, "entry_buffer_pct": 0.1, "bar_deadline_sec": 900,
                                         "interval_minutes": 5}, data={"official_fetch_concurrency": 5, "bar_grace_sec": 300})
