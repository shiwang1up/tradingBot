from datetime import date

from click.testing import CliRunner

from tests.helpers import make_config, synth_candles
from tradebot import cli
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.types import Candle


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
            return [Candle(symbol, end_ts - (end_ts % 300), 1, 2, 0.5, 1.5, 10)]

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
            return [Candle(symbol, end_ts - (end_ts % 300), 1, 2, 0.5, 1.5, 10)]

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
            return [Candle(symbol, end_ts - (end_ts % 300), 1, 2, 0.5, 1.5, 10)]

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


def test_estimate_ai_zero_approved_and_exclusive_report_flags(tmp_path):
    _setup(tmp_path)
    cfg_text = (tmp_path / "config.yaml").read_text().replace("max_open_positions: 5", "max_open_positions: 1")
    (tmp_path / "config.yaml").write_text(cfg_text)
    res = _invoke(tmp_path, "estimate-ai", "--run", "nope")
    assert res.exit_code == 1 and "unknown run" in res.output
    both = _invoke(tmp_path, "report", "--run", "a", "--compare", "a", "b")
    assert both.exit_code == 1 and "mutually exclusive" in both.output
