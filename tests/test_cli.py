import logging
from datetime import date, time, timedelta
from pathlib import Path

import yaml
from click.testing import CliRunner

from tests.helpers import FakeTime, make_config, synth_candles
from tradebot import cli
from tradebot.engine.clock import ist_epoch, iso_ist, to_ist
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.types import Candle, Position, Signal


def _bar_ts(end_ts):
    """An in-session bar (10:00 IST the day before `end_ts`): fetch-data keeps only session bars, so a
    bar stamped 'now' would vanish whenever the tests run outside market hours."""
    return ist_epoch(to_ist(end_ts).date() - timedelta(days=1), "10:00")


def _setup(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B]\n")
    repo = Repo(connect(cfg.paths.db))
    days = [date(2026, 9, 14), date(2026, 9, 15)]
    repo.insert_candles(synth_candles("A", days) + synth_candles("B", days, phase=4.0, seed=99), interval=5)
    repo.conn.close()
    return cfg


def test_backtest_then_report(tmp_path):
    _setup(tmp_path)
    r = CliRunner()
    res = r.invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                              "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "cli1"])
    assert res.exit_code == 0, res.output
    assert "Trades" in res.output
    rep = r.invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "report", "--run", "cli1"])
    assert rep.exit_code == 0, rep.output
    assert "Run cli1" in rep.output


def test_report_estimates_charges_for_a_row_that_predates_them(tmp_path):
    """The CLI must pass the config's charges schedule into build_summary: a closed position with
    NULL charges (a run recorded before charges existed) should come back estimated, not free."""
    cfg = make_config(tmp_path, charges={"enabled": True})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    repo = Repo(connect(cfg.paths.db))
    repo.create_run("old-run", "backtest", 0, "{}")
    p = Position("A", "MIS", "LONG", 10, 100.0, 99.0, None, 1, "c1", "ema_rsi")
    repo.close_position(repo.insert_position("old-run", p), 9, 102.0, "TARGET", 20.0, charges=None)
    repo.conn.close()
    res = _invoke(tmp_path, "report", "--run", "old-run")
    assert res.exit_code == 0, res.output
    assert "est." in res.output
    charges_line = next(ln for ln in res.output.splitlines() if ln.startswith("Charges"))
    assert charges_line.split()[1] != "0.00"


def test_hurdle_prints_a_row_per_slot_count_and_a_column_per_broker(tmp_path):
    make_config(tmp_path)
    res = _invoke(tmp_path, "hurdle", "--capital", "100000", "--slots", "4,8")
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    header = next(ln for ln in lines if ln.strip().startswith("slots"))
    assert "groww" in header and "zerodha" in header
    assert any(ln.strip().startswith("4 ") for ln in lines)
    assert any(ln.strip().startswith("8 ") for ln in lines)


def test_hurdle_capital_defaults_to_the_configs_capital(tmp_path):
    make_config(tmp_path)  # BASE_CONFIG capital is 100,000
    res = _invoke(tmp_path, "hurdle")
    assert res.exit_code == 0, res.output
    assert "100,000 fully deployed" in res.output


def test_hurdle_unknown_broker_names_the_bad_name_and_the_available_schedules(tmp_path):
    make_config(tmp_path)
    res = _invoke(tmp_path, "hurdle", "--broker", "nope")
    assert res.exit_code == 1
    assert "nope" in res.output
    assert "groww" in res.output and "zerodha" in res.output


def test_hurdle_bad_slots_names_the_option(tmp_path):
    make_config(tmp_path)
    res = _invoke(tmp_path, "hurdle", "--slots", "abc")
    assert res.exit_code == 1
    assert "--slots" in res.output


def test_backtest_without_data_fails_clearly(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                                        "--start", "2026-09-14", "--end", "2026-09-15"])
    assert res.exit_code != 0
    assert "fetch-data" in res.output


def _invoke(tmp_path, *args):
    return CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), *args])


def test_missing_credentials_is_a_clean_error(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,2885,RELIANCE,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = _invoke(tmp_path, "fetch-data")
    assert res.exit_code == 1 and "Error: GROWW_API_KEY must be set" in res.output
    assert "Traceback" not in res.output


def test_groww_auth_failure_is_a_clean_error(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,2885,RELIANCE,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Auth:
        flow = "fake"
        client = object()

        def __init__(self, k, s, a=""):
            pass

        def fetch_candles(self, *a):
            raise type("GrowwAPIAuthenticationException", (Exception,), {})("bad totp")

    monkeypatch.setattr(cli, "GrowwAdapter", Auth)
    res = _invoke(tmp_path, "fetch-data", "--sleep", "0")
    assert res.exit_code == 1 and "Error: GrowwAPIAuthenticationException: bad totp" in res.output


def test_generic_groww_exception_with_auth_code_aborts_on_first_symbol(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B, C]\n")
    hdr = "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
    (tmp_path / "instruments.csv").write_text(hdr + "".join(f"NSE,{i},{s},CASH,EQ,1,0.05,1,1\n" for i, s in enumerate("ABC")))
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    calls = []

    class Expired:
        flow = "fake"
        client = object()

        def __init__(self, k, s, a=""):
            pass

        def fetch_candles(self, symbol, *a):
            calls.append(symbol)
            raise type("GrowwAPIException", (Exception,), {"code": "401"})("Invalid session")

    monkeypatch.setattr(cli, "GrowwAdapter", Expired)
    res = _invoke(tmp_path, "fetch-data", "--sleep", "0")
    assert res.exit_code == 1 and "Error: GrowwAPIException: Invalid session" in res.output
    assert calls == ["A"], "auth failure must abort before touching the next symbol"
    assert "symbol(s) failed" not in res.output


def test_fetch_data_isolates_symbol_failures(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B, C]\n")
    hdr = "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
    (tmp_path / "instruments.csv").write_text(hdr + "".join(f"NSE,{i},{s},CASH,EQ,1,0.05,1,1\n" for i, s in enumerate("ABC")))
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Flaky:
        flow = "fake"
        client = object()

        def __init__(self, k, s, a=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            if symbol == "B":
                raise type("GrowwAPINotFoundException", (Exception,), {})("no such symbol")
            return [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", Flaky)
    res = _invoke(tmp_path, "fetch-data", "--days", "5", "--sleep", "0")
    assert res.exit_code == 1
    assert "failed: B" in res.output and "1 symbol(s) failed: B" in res.output
    repo = Repo(connect(make_config(tmp_path).paths.db))
    assert repo.latest_candle_ts("A", 5) is not None and repo.latest_candle_ts("C", 5) is not None


def test_run_id_reuse_and_reversed_range_are_clean_errors(tmp_path):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "dup")
    assert ok.exit_code == 0, ok.output
    again = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "dup")
    assert again.exit_code == 1 and "already exists" in again.output
    rev = _invoke(tmp_path, "backtest", "--start", "2026-09-15", "--end", "2026-09-14")
    assert rev.exit_code == 1 and "--start must not be after --end" in rev.output


def test_bad_config_and_unknown_run_are_clean_errors(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "config.yaml").write_text((tmp_path / "config.yaml").read_text().replace("per_trade_pct: 1.0", "per_trade_pct: -1"))
    res = _invoke(tmp_path, "report", "--run", "x")
    assert res.exit_code == 1 and res.output.startswith("Error: config.yaml")
    make_config(tmp_path)
    res = _invoke(tmp_path, "report", "--run", "nope")
    assert res.exit_code == 1 and "no database" in res.output


def test_env_is_read_next_to_config_or_from_override(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfgs"
    cfg_dir.mkdir()
    make_config(cfg_dir)
    (cfg_dir / ".env").write_text("GROWW_API_KEY=beside-config\nGROWW_TOTP_SECRET=s\n")
    seen = {}

    class Spy:
        flow = "fake"
        client = object()

        def __init__(self, k, s, a=""):
            seen["key"] = k

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            return [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 10)]

    (cfg_dir / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    (cfg_dir / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,1,A,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    monkeypatch.setattr(cli, "GrowwAdapter", Spy)
    monkeypatch.chdir(tmp_path)  # CWD has no .env
    res = CliRunner().invoke(cli.main, ["--config", str(cfg_dir / "config.yaml"), "fetch-data", "--sleep", "0"])
    assert res.exit_code == 0, res.output
    assert seen["key"] == "beside-config"
    # A real CLI call is a fresh process; here the first invocation populated os.environ and
    # process env deliberately beats .env, so clear it before testing the override.
    for name in ("GROWW_API_KEY", "GROWW_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "other.env").write_text("GROWW_API_KEY=override\nGROWW_TOTP_SECRET=s\n")
    res = CliRunner().invoke(cli.main, ["--config", str(cfg_dir / "config.yaml"), "--env", str(tmp_path / "other.env"),
                                        "fetch-data", "--sleep", "0"])
    assert res.exit_code == 0 and seen["key"] == "override"


def test_fetch_data_uses_adapter_and_instruments(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE, NOSUCH]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
        "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
        "freeze_quantity,is_reserved,buy_allowed,sell_allowed,feed_key\n"
        "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,,,,,1,0.05,,0,1,1,NSE_CASH_2885\n")
    calls = []

    class FakeAdapter:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            calls.append((symbol, exchange, interval))
            return [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "fetch-data", "--days", "20", "--sleep", "0"])
    assert res.exit_code == 0, res.output
    assert calls and all(c[0] == "RELIANCE" and c[1] == "NSE" and c[2] == 5 for c in calls)
    assert "dropping NOSUCH: not_found" in res.output


def test_first_fetch_with_no_candles_at_all_is_an_error(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,1,A,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Empty:
        flow = "fake"
        client = object()

        def __init__(self, k, s, a=""):
            pass

        def fetch_candles(self, *a):
            return []

    monkeypatch.setattr(cli, "GrowwAdapter", Empty)
    res = _invoke(tmp_path, "fetch-data", "--days", "5", "--sleep", "0")
    assert res.exit_code == 1 and "no candles were returned" in res.output


def test_missing_credentials_fail_before_any_download(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: False)
    monkeypatch.setattr(cli, "download_instruments", lambda p: (_ for _ in ()).throw(AssertionError("downloaded")))
    res = _invoke(tmp_path, "fetch-data")
    assert res.exit_code == 1 and "GROWW_API_KEY" in res.output and "downloaded" not in res.output


def test_backtest_writes_a_jsonl_log_and_warns_without_instruments(tmp_path):
    _setup(tmp_path)
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "logged")
    assert res.exit_code == 0, res.output
    assert "WARNING: instrument master missing" in res.output
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert logs and any('"run_id": "logged"' in ln for ln in logs[0].read_text().splitlines())


def test_bad_totp_secret_aborts_before_any_symbol(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=jwt\nGROWW_TOTP_SECRET=TY#Fnotbase32\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    monkeypatch.setattr(cli, "download_instruments", lambda p: (_ for _ in ()).throw(AssertionError("downloaded")))
    res = _invoke(tmp_path, "fetch-data")
    assert res.exit_code == 1 and "not a base32 TOTP secret" in res.output and "failed:" not in res.output


def test_403_on_market_data_explains_the_subscription(tmp_path, monkeypatch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=jwt\nGROWW_API_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A]\n")
    (tmp_path / "instruments.csv").write_text(
        "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
        "NSE,1,A,CASH,EQ,1,0.05,1,1\n")
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Forbidden:
        flow = "approval"
        client = object()

        def __init__(self, k, s, a=""):
            pass

        def fetch_candles(self, *a):
            raise type("GrowwAPIException", (Exception,), {"code": "403"})("Access forbidden for this request.")

    monkeypatch.setattr(cli, "GrowwAdapter", Forbidden)
    res = _invoke(tmp_path, "fetch-data", "--sleep", "0")
    assert res.exit_code == 1 and "Trade API subscription" in res.output and "failed:" not in res.output


def test_backtest_ai_override_and_compare_report(tmp_path, monkeypatch):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "stub-run")
    assert ok.exit_code == 0, ok.output

    from tradebot.ai import filter as filter_mod
    from tradebot.ai.claude_client import ReviewResponse

    class FakeClaude:
        def __init__(self, *a, **k):
            pass

        def review(self, system, user, schema):
            import json
            cands = json.loads(user)["candidates"]
            return ReviewResponse({"decisions": [{"index": c["index"], "symbol": c["symbol"], "approve": c["index"] % 2 == 0,
                                                  "confidence": 0.5, "reason": "test"} for c in cands]}, 10, 500, 20, 0, 0, None)

    monkeypatch.setattr(filter_mod, "ClaudeClient", FakeClaude)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "claude-run",
                  "--ai", "claude_cached")
    assert res.exit_code == 0, res.output
    assert "AI rejects" in res.output
    cmp_ = _invoke(tmp_path, "report", "--compare", "stub-run", "claude-run")
    assert cmp_.exit_code == 0, cmp_.output
    assert "Rejected by Claude" in cmp_.output and "Estimated cost" in cmp_.output
    # second claude_cached run makes zero calls: cost line shows 0 tokens
    res2 = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "claude-run-2",
                   "--ai", "claude_cached")
    assert res2.exit_code == 0, res2.output
    cmp2 = _invoke(tmp_path, "report", "--compare", "stub-run", "claude-run-2")
    assert "Tokens in/out/cached    0 / 0 / 0 (cache writes 0)" in cmp2.output


def test_backtest_claude_without_key_is_clean_error(tmp_path):
    _setup(tmp_path)
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--ai", "claude")
    assert res.exit_code == 1 and "ANTHROPIC_API_KEY" in res.output


def test_estimate_ai_reports_a_range_and_records_the_ai_override(tmp_path, monkeypatch):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "stub-run", "--ai", "stub")
    assert ok.exit_code == 0, ok.output
    import json
    from tradebot.store.db import connect as _connect
    from tradebot.store.repo import Repo as _Repo
    stored = json.loads(_Repo(_connect(make_config(tmp_path).paths.db)).get_run("stub-run")["config_json"])
    assert stored["ai"]["filter"] == "stub"
    from tradebot import cli as cli_mod

    class Counter:
        def __init__(self, *a, **k):
            pass

        def count_tokens(self, system, user, schema=None):
            return 700 if user == "x" else 700 + 150 + 900 * user.count('"index":')

    monkeypatch.setattr(cli_mod, "ClaudeClient", Counter)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "estimate-ai", "--run", "stub-run")
    assert res.exit_code == 0, res.output
    assert "API calls: 9 bars with candidates" in res.output and "largest bar: 1" in res.output
    assert "per call 0 + 1050 per candidate" in res.output  # one-candidate bar: no fixed/per-candidate split possible
    assert "estimated cost" in res.output and " to $" in res.output and "unmeasured" in res.output


def test_estimate_ai_separates_fixed_and_per_candidate_cost_and_samples(tmp_path, monkeypatch):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "stub-run")
    assert ok.exit_code == 0, ok.output
    from tradebot import cli as cli_mod
    from tradebot.ai import filter as filter_mod
    from tradebot.ai.claude_client import ClaudeReviewError, ReviewResponse
    from tradebot.store.db import connect as _connect
    from tradebot.store.repo import Repo as _Repo
    # Make the largest bar hold three candidates by approving two extra signals on one bar.
    repo = _Repo(_connect(make_config(tmp_path).paths.db))
    bar = repo.conn.execute("SELECT s.bar_ts FROM risk_decisions r JOIN signals s ON s.id=r.signal_id "
                            "WHERE r.run_id='stub-run' AND r.approved=1 LIMIT 1").fetchone()[0]
    for sym in ("P", "Q"):
        sid = repo.insert_signal("stub-run", Signal("ema_rsi", sym, "LONG", 100.0, 99.0, 102.0, "MIS", bar))
        repo.insert_risk_decision("stub-run", sid, True, "ok", 10)
    repo.conn.close()

    class Counter:
        def __init__(self, *a, **k):
            pass

        def count_tokens(self, system, user, schema=None):
            return 700 if user == "x" else 700 + 150 + 900 * user.count('"index":')

    calls = {"n": 0}

    class Sampled:
        def __init__(self, *a, **k):
            pass

        def review(self, system, user, schema):
            import json
            calls["n"] += 1
            if calls["n"] == 1:
                raise ClaudeReviewError("transient")  # a failed sample must not drag the mean down
            cands = json.loads(user)["candidates"]
            return ReviewResponse({"decisions": [{"index": c["index"], "symbol": c["symbol"], "approve": True,
                                                  "confidence": 0.5, "reason": "r"} for c in cands]}, 100, 500, 400, 0, 0, None)

    monkeypatch.setattr(cli_mod, "ClaudeClient", Counter)
    monkeypatch.setattr(filter_mod, "ClaudeClient", Sampled)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "estimate-ai", "--run", "stub-run", "--sample", "3")
    assert res.exit_code == 0, res.output
    assert "largest bar: 3" in res.output and "per call 150 + 900 per candidate" in res.output
    assert "sampled 2 real calls on the largest bars (1 failed): mean output 400 tokens" in res.output
    assert "unmeasured" not in res.output


def test_estimate_ai_zero_approved_and_exclusive_report_flags(tmp_path):
    _setup(tmp_path)
    Path(make_config(tmp_path).paths.kill_switch).write_text("")  # every signal is rejected: nothing to review
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "killed")
    assert ok.exit_code == 0, ok.output
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "estimate-ai", "--run", "killed")
    assert res.exit_code == 1 and "no risk-approved signals" in res.output
    res = _invoke(tmp_path, "estimate-ai", "--run", "nope")
    assert res.exit_code == 1 and "unknown run" in res.output
    both = _invoke(tmp_path, "report", "--run", "a", "--compare", "a", "b")
    assert both.exit_code == 1 and "mutually exclusive" in both.output


def test_estimate_ai_api_failure_is_a_clean_error(tmp_path, monkeypatch):
    _setup(tmp_path)
    ok = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "stub-run")
    assert ok.exit_code == 0, ok.output
    from tradebot import cli as cli_mod
    from tradebot.ai.claude_client import ClaudeReviewError

    class Broken:
        def __init__(self, *a, **k):
            pass

        def count_tokens(self, *a, **k):
            raise ClaudeReviewError("API error 400: bad schema")

    monkeypatch.setattr(cli_mod, "ClaudeClient", Broken)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n")
    res = _invoke(tmp_path, "estimate-ai", "--run", "stub-run")
    assert res.exit_code == 1 and res.output.strip().endswith("Error: API error 400: bad schema")


PRIOR = [date(2026, 9, 10), date(2026, 9, 11)]
TODAY = date(2026, 9, 14)


def _paper_setup(tmp_path, monkeypatch, fetch):
    make_config(tmp_path)
    (tmp_path / ".env").write_text("GROWW_API_KEY=k\nGROWW_TOTP_SECRET=s\n")
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [A, B]\n")
    hdr = "exchange,exchange_token,trading_symbol,segment,instrument_type,lot_size,tick_size,buy_allowed,sell_allowed\n"
    (tmp_path / "instruments.csv").write_text(hdr + "".join(f"NSE,{i},{s},CASH,EQ,1,0.05,1,1\n" for i, s in enumerate("AB")))
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)

    class Fake:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            return fetch(symbol, exchange, start_ts, end_ts, interval)

    monkeypatch.setattr(cli, "GrowwAdapter", Fake)


def test_paper_refuses_cnc_strategies(tmp_path, monkeypatch):
    _paper_setup(tmp_path, monkeypatch, lambda *a: [])
    p = tmp_path / "config.yaml"
    raw = yaml.safe_load(p.read_text())
    raw["strategy"]["ema_rsi"]["product"] = "CNC"
    p.write_text(yaml.safe_dump(raw))
    res = _invoke(tmp_path, "paper")
    assert res.exit_code != 0
    assert "MIS" in res.output


def test_paper_outside_session_exits_cleanly_without_a_run(tmp_path, monkeypatch):
    _paper_setup(tmp_path, monkeypatch, lambda *a: [])
    saturday = FakeTime(ist_epoch(date(2026, 9, 12), "10:00"))
    monkeypatch.setattr(cli.time, "time", saturday.now)
    res = _invoke(tmp_path, "paper", "--run-id", "p1")
    assert res.exit_code == 0, res.output
    assert "nothing to trade" in res.output
    assert Repo(connect(make_config(tmp_path).paths.db)).get_run("p1") is None


def test_paper_full_day_end_to_end(tmp_path, monkeypatch):
    market = {"A": synth_candles("A", PRIOR + [TODAY]), "B": synth_candles("B", PRIOR + [TODAY], phase=4.0, seed=99)}
    _paper_setup(tmp_path, monkeypatch, lambda sym, ex, s, e, i: [c for c in market[sym] if s <= c.ts <= e])
    t = FakeTime(ist_epoch(TODAY, "09:00"))
    monkeypatch.setattr(cli.time, "time", t.now)
    monkeypatch.setattr(cli.time, "sleep", t.sleep)
    import signal as os_signal
    before = os_signal.getsignal(os_signal.SIGINT)
    res = _invoke(tmp_path, "paper", "--run-id", "p2", "--ai", "stub")
    assert res.exit_code == 0, res.output
    assert "Run p2 (paper)" in res.output
    assert os_signal.getsignal(os_signal.SIGINT) is before, "the stop handler must not outlive the command"
    repo = Repo(connect(make_config(tmp_path).paths.db))
    run = repo.get_run("p2")
    assert run["mode"] == "paper" and run["ended_at"] is not None
    assert run["last_bar_ts"] == ist_epoch(TODAY, "15:25")
    assert repo.latest_candle_ts("A", 5) == ist_epoch(TODAY, "15:25")     # the day's bars grew the cache
    assert repo.list_positions("p2"), "the warm-up from the prior days must make trades possible today"


def test_paper_wires_the_fetch_budget_below_the_deadline(tmp_path, monkeypatch):
    market = {"A": synth_candles("A", PRIOR + [TODAY]), "B": synth_candles("B", PRIOR + [TODAY], phase=4.0, seed=99)}
    _paper_setup(tmp_path, monkeypatch, lambda sym, ex, s, e, i: [c for c in market[sym] if s <= c.ts <= e])
    seen = {}
    real = cli.LiveBarSource

    def spy(*args, **kwargs):
        src = real(*args, **kwargs)
        seen["budget"] = src.budget_sec
        return src

    monkeypatch.setattr(cli, "LiveBarSource", spy)
    t = FakeTime(ist_epoch(TODAY, "15:29"))
    monkeypatch.setattr(cli.time, "time", t.now)
    monkeypatch.setattr(cli.time, "sleep", t.sleep)
    res = _invoke(tmp_path, "paper", "--run-id", "p3", "--ai", "stub")
    assert res.exit_code == 0, res.output
    cfg = make_config(tmp_path)
    assert seen["budget"] == cfg.execution.bar_deadline_sec - cfg.data.bar_grace_sec


def test_warm_fetch_isolates_symbol_failures_and_reraises_auth(tmp_path, caplog):
    cfg = make_config(tmp_path)
    repo = Repo(connect(cfg.paths.db))
    clock = cli.SessionClock(cfg.session, 5)
    now = ist_epoch(TODAY, "09:00")

    class Flaky:
        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            if symbol == "B":
                raise RuntimeError("boom")
            return [Candle(symbol, ist_epoch(date(2026, 9, 11), "10:00"), 1, 2, 0.5, 1.5, 10)]

    with caplog.at_level(logging.WARNING, logger="tradebot.cli"):
        failed = cli._warm_fetch(cfg, Flaky(), repo, ["A", "B", "C"], "NSE", clock, now, pause=0)
    assert failed == ["B"] and "warm-up fetch failed for B" in caplog.text
    assert repo.latest_candle_ts("A", 5) is not None and repo.latest_candle_ts("C", 5) is not None

    class Expired:
        def fetch_candles(self, *a):
            raise type("GrowwAPIAuthenticationException", (Exception,), {})("401 token expired")

    import pytest
    with pytest.raises(Exception, match="401"):
        cli._warm_fetch(cfg, Expired(), repo, ["A"], "NSE", clock, now, pause=0)


def test_paper_refuses_foreign_and_already_ended_runs_before_logging_in(tmp_path, monkeypatch):
    calls = []
    _paper_setup(tmp_path, monkeypatch, lambda *a: calls.append(a) or [])
    cfg = make_config(tmp_path)
    repo = Repo(connect(cfg.paths.db))
    repo.create_run("bt", "backtest", 0, "{}")
    repo.create_run("done", "paper", ist_epoch(TODAY, "09:00"), "{}")
    repo.end_run("done", ist_epoch(TODAY, "15:30"))
    repo.conn.close()
    monkeypatch.setattr(cli.time, "time", FakeTime(ist_epoch(TODAY, "10:00")).now)
    res = _invoke(tmp_path, "paper", "--run-id", "bt")
    assert res.exit_code != 0 and "not a paper run" in res.output
    res = _invoke(tmp_path, "paper", "--run-id", "done")
    assert res.exit_code != 0 and "already ended today" in res.output
    assert calls == [], "a refusal must not cost a login or a warm-up fetch"


# -- Task 15: the CLI fetches, loads and checks the regime index --------------------------------

_INSTRUMENTS = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
    "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
    "freeze_quantity,is_reserved,buy_allowed,sell_allowed,feed_key\n"
    "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,,,,,1,0.05,,0,1,1,NSE_CASH_2885\n")


def _fetch_with_index(tmp_path, monkeypatch, fail_index=False, index_extra_bar=None):
    """`index_extra_bar(end_ts) -> Candle` lets a caller tack an extra row onto the index's response,
    same shape as the 15:30 bar Groww actually returns."""
    make_config(tmp_path, data={"index_symbol": "NIFTY"})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(_INSTRUMENTS)
    calls = []

    class FakeAdapter:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            calls.append(symbol)
            if fail_index and symbol == "NIFTY":
                raise ValueError("index history not served")
            rows = [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 0 if symbol == "NIFTY" else 10)]
            if index_extra_bar is not None and symbol == "NIFTY":
                rows.append(index_extra_bar(end_ts))
            return rows

    monkeypatch.setattr(cli, "GrowwAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    return _invoke(tmp_path, "fetch-data", "--days", "20", "--sleep", "0"), calls


def test_fetch_data_also_fetches_the_index(tmp_path, monkeypatch):
    res, calls = _fetch_with_index(tmp_path, monkeypatch)
    assert res.exit_code == 0, res.output
    assert set(calls) == {"RELIANCE", "NIFTY"}           # the index is not in the instrument master's EQ rows
    assert calls.index("NIFTY") > calls.index("RELIANCE")  # Job A item 1: the index is fetched last
    repo = Repo(connect(str(tmp_path / "tradebot.db")))
    assert repo.latest_candle_ts("NIFTY", 5) is not None


def test_fetch_data_drops_the_index_15_30_bar_outside_the_session(tmp_path, monkeypatch):
    """A live check against Groww found it serves the index through the same cash-segment candles
    call as any EQ symbol, with zero volume, and one extra bar at 15:30 IST every day. fetch-data
    already keeps only `clock.in_session(ts)` bars via its `keep=` argument (the session closes at
    15:30 and in_session is a strict '<'), so that bar must never reach storage."""
    def extra_at_close(end_ts):
        d = to_ist(_bar_ts(end_ts)).date()
        return Candle("NIFTY", ist_epoch(d, "15:30"), 1, 2, 0.5, 1.5, 0)

    res, _ = _fetch_with_index(tmp_path, monkeypatch, index_extra_bar=extra_at_close)
    assert res.exit_code == 0, res.output
    repo = Repo(connect(str(tmp_path / "tradebot.db")))
    stored = repo.load_candles(["NIFTY"], 5, 0, 2_000_000_000)
    assert stored and all(to_ist(c.ts).time() != time(15, 30) for c in stored)


def test_a_failed_index_fetch_points_at_the_composite_source(tmp_path, monkeypatch):
    res, _ = _fetch_with_index(tmp_path, monkeypatch, fail_index=True)
    assert res.exit_code == 1 and "NIFTY" in res.output and "composite" in res.output
    assert "1 symbol(s) failed: NIFTY (regime index; universe candles were stored)" in res.output


def test_fetch_data_isolates_an_unclassified_index_failure(tmp_path, monkeypatch):
    """Job A item 1: an exception the CLI does not otherwise classify (not in _FRIENDLY, not
    is_non_retryable) is fatal for a universe symbol but must still be non-fatal for the index -
    login has already succeeded by the time fetch-data reaches it. The universe's candle must be
    stored and the failure named cleanly, with no traceback."""
    make_config(tmp_path, data={"index_symbol": "NIFTY"})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(_INSTRUMENTS)

    class FakeAdapter:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            if symbol == "NIFTY":
                raise RuntimeError("boom")
            return [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 10)]

    monkeypatch.setattr(cli, "GrowwAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = _invoke(tmp_path, "fetch-data", "--days", "20", "--sleep", "0")
    assert res.exit_code == 1
    assert "failed: NIFTY (RuntimeError: boom)" in res.output
    assert "Traceback" not in res.output
    repo = Repo(connect(str(tmp_path / "tradebot.db")))
    assert repo.latest_candle_ts("RELIANCE", 5) is not None


def test_had_data_check_ignores_pre_existing_index_candles(tmp_path, monkeypatch):
    """Job A item 3: `had_data` (and the first-fetch check) is computed over the universe symbols
    only - an index that already has stored candles from an earlier fetch must not mask a universe
    symbol that has never returned any."""
    make_config(tmp_path, data={"index_symbol": "NIFTY"})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(_INSTRUMENTS)
    repo = Repo(connect(str(tmp_path / "tradebot.db")))
    repo.insert_candles([Candle("NIFTY", ist_epoch(date(2026, 9, 10), "10:00"), 1, 2, 0.5, 1.5, 0)], interval=5)
    repo.conn.close()

    class Empty:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, *a):
            return []

    monkeypatch.setattr(cli, "GrowwAdapter", Empty)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = _invoke(tmp_path, "fetch-data", "--days", "5", "--sleep", "0")
    assert res.exit_code == 1 and "no candles were returned" in res.output


def test_first_fetch_guard_counts_universe_candles_only(tmp_path, monkeypatch):
    """The other half of the check above: on a first-ever fetch the index returns candles and the
    only universe symbol returns none. The index's inserted rows must not satisfy the first-fetch
    guard - there is still nothing to trade on."""
    make_config(tmp_path, data={"index_symbol": "NIFTY"})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(_INSTRUMENTS)

    class IndexOnly:
        flow = "fake"
        client = object()

        def __init__(self, key, secret, api_secret=""):
            pass

        def fetch_candles(self, symbol, exchange, start_ts, end_ts, interval):
            if symbol == "NIFTY":
                return [Candle(symbol, _bar_ts(end_ts), 1, 2, 0.5, 1.5, 0)]
            return []

    monkeypatch.setattr(cli, "GrowwAdapter", IndexOnly)
    monkeypatch.setattr(cli, "download_instruments", lambda p: p)
    monkeypatch.setattr(cli, "_instruments_fresh", lambda p: True)
    res = _invoke(tmp_path, "fetch-data", "--days", "5", "--sleep", "0")
    assert res.exit_code == 1, res.output
    assert "no candles were returned for any symbol on a first fetch" in res.output
    assert "Traceback" not in res.output
    # the summary line says what it counts: nothing for the universe, the index's rows apart
    assert "inserted 0 candles across 1 universe symbols" in res.output
    assert "regime index NIFTY" in res.output
    repo = Repo(connect(str(tmp_path / "tradebot.db")))
    assert repo.latest_candle_ts("NIFTY", 5) is not None  # the index rows were stored all the same


def test_with_index_refuses_an_index_that_is_also_in_the_universe(tmp_path):
    """Amendment C2: an index that is also a universe symbol would be stripped from every bar by
    the engine, mistaking a tradable symbol for the index - refused early, as a clean error."""
    make_config(tmp_path, data={"index_symbol": "RELIANCE"})
    (tmp_path / "universe.yaml").write_text("exchange: NSE\nsymbols: [RELIANCE]\n")
    (tmp_path / "instruments.csv").write_text(_INSTRUMENTS)
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15")
    assert res.exit_code == 1 and "also in the universe" in res.output
    assert "Traceback" not in res.output


def test_backtest_with_the_regime_on_needs_index_candles(tmp_path):
    """The CLI's start-up check runs before the run is created at all: a failed start here must
    not burn a run id (`repo.create_run` lives inside `engine.run()`, called much later)."""
    _setup(tmp_path)
    make_config(tmp_path, regime={"enabled": True}, data={"index_symbol": "NIFTY"})
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "rg0")
    assert res.exit_code == 1 and "NIFTY" in res.output and "fetch-data" in res.output
    assert "Traceback" not in res.output
    repo = Repo(connect(make_config(tmp_path).paths.db))
    assert repo.get_run("rg0") is None


def test_backtest_with_the_regime_on_runs_when_the_index_is_stored(tmp_path):
    cfg = _setup(tmp_path)
    make_config(tmp_path, regime={"enabled": True, "ema_period": 3}, data={"index_symbol": "NIFTY"})
    repo = Repo(connect(cfg.paths.db))
    t0 = ist_epoch(date(2026, 9, 14), "09:15")
    repo.insert_candles([Candle("NIFTY", t0 + i * 300, 25000.0, 25000.0, 25000.0, 25000.0, 0) for i in range(75)], interval=5)
    repo.conn.close()
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "rg1")
    assert res.exit_code == 0, res.output
    # Job A item 8: the coverage line. NIFTY was only stored for day 1, half of the 150 bar
    # timestamps A and B load across both days, so it is below 95% and gets the WARNING prefix.
    assert f"WARNING: NIFTY covers 75 of 150 bar timestamps in the window (first index bar {iso_ist(t0)})" in res.output
    import json
    repo = Repo(connect(cfg.paths.db))
    assert json.loads(repo.get_run("rg1")["config_json"])["regime"]["enabled"] is True
    assert all(r["symbol"] != "NIFTY" for r in repo.list_positions("rg1"))     # gating itself is covered in test_engine


def test_backtest_regime_coverage_line_has_no_warning_when_the_index_is_complete(tmp_path):
    """Job A item 8: full coverage of the window gets the plain line, no WARNING prefix."""
    cfg = _setup(tmp_path)
    make_config(tmp_path, regime={"enabled": True, "ema_period": 3}, data={"index_symbol": "NIFTY"})
    repo = Repo(connect(cfg.paths.db))
    t0 = ist_epoch(date(2026, 9, 14), "09:15")
    index_candles = [Candle("NIFTY", ist_epoch(d, "09:15") + i * 300, 25000.0, 25000.0, 25000.0, 25000.0, 0)
                     for d in (date(2026, 9, 14), date(2026, 9, 15)) for i in range(75)]
    repo.insert_candles(index_candles, interval=5)
    repo.conn.close()
    res = _invoke(tmp_path, "backtest", "--start", "2026-09-14", "--end", "2026-09-15", "--run-id", "rg2")
    assert res.exit_code == 0, res.output
    assert f"NIFTY covers 150 of 150 bar timestamps in the window (first index bar {iso_ist(t0)})" in res.output
    assert "WARNING: NIFTY covers" not in res.output


def test_paper_feeds_the_index_to_warm_up_and_only_to_the_live_source_when_the_filter_needs_it(tmp_path, monkeypatch):
    """Task 15 + Job A item 4: the index always rides into the warm-up fetch, first (amendment C1) -
    its history cannot be backfilled later, and the warm-up heals a missed day - but the live bar
    source only carries it (also first) when the regime filter is enabled with source 'index'; with
    the filter off it would be a wasted fetch, so the live source gets the universe only. `lot_sizes`
    stays universe-only either way: the index has no lot size and is never traded."""
    market = {"A": synth_candles("A", PRIOR + [TODAY]), "B": synth_candles("B", PRIOR + [TODAY], phase=4.0, seed=99),
             "NIFTY": [Candle("NIFTY", c.ts, 25000.0, 25000.0, 25000.0, 25000.0, 0)
                      for c in synth_candles("A", PRIOR + [TODAY])]}
    _paper_setup(tmp_path, monkeypatch, lambda sym, ex, s, e, i: [c for c in market[sym] if s <= c.ts <= e])
    p = tmp_path / "config.yaml"
    raw = yaml.safe_load(p.read_text())
    raw["data"] = {**(raw.get("data") or {}), "index_symbol": "NIFTY"}
    real_source, real_engine = cli.LiveBarSource, cli.PaperEngine

    def _run(run_id, regime):
        raw["regime"] = regime
        p.write_text(yaml.safe_dump(raw))
        seen = {}

        def spy_source(fetcher, repo, symbols, *a, **k):
            seen["source_symbols"] = list(symbols)
            return real_source(fetcher, repo, symbols, *a, **k)

        def spy_engine(cfg, repo, source, strategies, broker, ai, clock, lot_sizes, rid, **k):
            seen["lot_sizes"] = dict(lot_sizes)
            return real_engine(cfg, repo, source, strategies, broker, ai, clock, lot_sizes, rid, **k)

        monkeypatch.setattr(cli, "LiveBarSource", spy_source)
        monkeypatch.setattr(cli, "PaperEngine", spy_engine)
        t = FakeTime(ist_epoch(TODAY, "09:00"))
        monkeypatch.setattr(cli.time, "time", t.now)
        monkeypatch.setattr(cli.time, "sleep", t.sleep)
        res = _invoke(tmp_path, "paper", "--run-id", run_id, "--ai", "stub")
        assert res.exit_code == 0, res.output
        return seen

    seen_off = _run("p4-off", {"enabled": False})
    assert set(seen_off["source_symbols"]) == {"A", "B"}, "filter off: the live source must not carry the index"
    assert seen_off["lot_sizes"] == {"A": 1, "B": 1}
    repo = Repo(connect(make_config(tmp_path).paths.db))
    assert repo.latest_candle_ts("NIFTY", 5) is not None, "the warm-up fetch must have pulled the index too"

    seen_on = _run("p4-on", {"enabled": True, "source": "index", "ema_period": 3})
    assert seen_on["source_symbols"][0] == "NIFTY" and set(seen_on["source_symbols"]) == {"NIFTY", "A", "B"}
    assert seen_on["lot_sizes"] == {"A": 1, "B": 1}          # never NIFTY: it has no lot size and is never traded


def test_report_prints_a_benchmark(tmp_path):
    """The whole point: a report must say what not selecting would have returned."""
    _setup(tmp_path)
    r = CliRunner()
    cfg = str(tmp_path / "config.yaml")
    assert r.invoke(cli.main, ["--config", cfg, "backtest", "--start", "2026-09-14",
                               "--end", "2026-09-15", "--run-id", "bm1"]).exit_code == 0
    rep = r.invoke(cli.main, ["--config", cfg, "report", "--run", "bm1"])
    assert rep.exit_code == 0, rep.output
    assert "Benchmark" in rep.output and "selecting" in rep.output
    assert "of 2 names" in rep.output, "the two-name universe from _setup"


def test_the_backtest_command_prints_it_too(tmp_path):
    """Both format_summary sites, not just report."""
    _setup(tmp_path)
    res = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"), "backtest",
                                        "--start", "2026-09-14", "--end", "2026-09-15",
                                        "--run-id", "bm2"])
    assert res.exit_code == 0, res.output
    assert "Benchmark" in res.output


def test_a_run_with_no_daily_rows_omits_the_block_instead_of_erroring(tmp_path):
    """There is no window to price a basket over. Omit it rather than print a zero that reads like
    a real result, and above all do not crash the report."""
    cfg = _setup(tmp_path)
    repo = Repo(connect(cfg.paths.db))
    repo.create_run("empty1", "backtest", 0, '{"execution": {"interval_minutes": 5}}')
    repo.conn.close()
    rep = CliRunner().invoke(cli.main, ["--config", str(tmp_path / "config.yaml"),
                                        "report", "--run", "empty1"])
    assert rep.exit_code == 0, rep.output
    assert "Benchmark" not in rep.output
