import textwrap

from tradebot.config import load_config

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


def test_load_config_reads_all_sections(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(YAML)
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
    assert cfg.paths.kill_switch == "KILL"
    assert cfg.data.official_fetch_concurrency == 5
    assert cfg.secrets.groww_api_key == "k"
    assert cfg.raw["capital"] == 100000


def test_missing_env_yields_empty_secrets(tmp_path, monkeypatch):
    for k in ("GROWW_API_KEY", "GROWW_TOTP_SECRET", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(YAML)
    cfg = load_config(cfg_path, tmp_path / "missing.env")
    assert cfg.secrets.groww_api_key == ""
