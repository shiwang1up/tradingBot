"""Command-line entry point: fetch-data, backtest, report, estimate-ai, paper. Live trading arrives in the live plan."""
from __future__ import annotations

import logging
import os
import signal as os_signal
import sqlite3
import time
from dataclasses import replace as dc_replace
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import click
import requests

from tradebot.ai.claude_client import ClaudeClient, ClaudeReviewError
from tradebot.ai.filter import AIFilterAborted, build_filter
from tradebot.ai.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, render_candidates
from tradebot.config import Config, load_config
from tradebot.data.historical import CHUNK_DAYS, HistoricalSource, fetch_incremental
from tradebot.data.instruments import download_instruments, load_instruments, resolve_universe
from tradebot.data.live import LiveBarSource
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, date_of, ist_epoch
from tradebot.engine.logsetup import setup_logging
from tradebot.engine.loop import BacktestEngine
from tradebot.engine.paper import PaperEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.execution.groww_adapter import GrowwAdapter, is_non_retryable
from tradebot.report.compare import Prices, build_compare, format_compare
from tradebot.report.summary import build_summary, format_summary
from tradebot.store.db import SchemaVersionError, connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy, strategy_params
from tradebot.strategy.ta import IndicatorSet
from tradebot.types import Candidate, Signal

# Operational failures that deserve a one-line message. sqlite3 programming errors (bad SQL) still traceback.
_FRIENDLY = (ValueError, SchemaVersionError, sqlite3.OperationalError, sqlite3.DatabaseError,
             requests.RequestException, NotImplementedError, AIFilterAborted, ClaudeReviewError)
_FATAL_AUTH_CODES = {"401", "403"}
log = logging.getLogger("tradebot.cli")


def _is_fatal_auth(e: BaseException) -> bool:
    """Auth/authorisation failures abort the whole run (spec 11). The SDK raises the generic
    GrowwAPIException with a code for Groww failure bodies, so match on the code as well as the name."""
    name = type(e).__name__
    if any(m in name for m in ("Authentication", "Authorisation", "Authorization")):
        return True
    return str(getattr(e, "code", "")) in _FATAL_AUTH_CODES


class _FriendlyGroup(click.Group):
    """Turn expected operational failures into one-line `Error: ...` messages with exit code 1.
    Programming errors still traceback. Groww SDK exceptions are matched by name so this module
    never imports growwapi."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except click.ClickException:
            raise
        except _FRIENDLY as e:
            raise click.ClickException(str(e)) from e
        except Exception as e:  # noqa: BLE001
            if type(e).__name__.startswith("Groww"):
                raise click.ClickException(f"{type(e).__name__}: {e}{_groww_hint(e)}") from e
            raise


def _groww_hint(e: BaseException) -> str:
    """Groww returns a bare 403 for market-data calls on accounts without the Trade API data plan."""
    if str(getattr(e, "code", "")) == "403":
        return ("\nHint: login succeeded but market data (LTP, quotes, historical candles) needs the Groww "
                "Trade API subscription. Subscribe at https://groww.in/trade-api, then retry. "
                "Approval-flow keys also need daily approval on the API keys page.")
    return ""


@click.group(cls=_FriendlyGroup)
@click.option("--config", "config_path", default="config.yaml", show_default=True,
              help="Path to config.yaml. Secrets are read from .env in the same directory unless --env is given.")
@click.option("--env", "env_path", default=None, help="Path to the .env file with GROWW_* and ANTHROPIC_* keys.")
@click.pass_context
def main(ctx: click.Context, config_path: str, env_path: Optional[str]) -> None:
    ctx.obj = load_config(config_path, env_path or Path(config_path).parent / ".env")
    setup_logging(None, run_id="cli")  # commands that create a run swap in the JSONL file handler


def _instruments_fresh(path: Path) -> bool:
    return path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() >= date.today()


def _symbols_and_lots(cfg: Config, require_instruments: bool) -> tuple:
    uni = load_universe(cfg.paths.universe)
    path = Path(cfg.paths.instruments)
    if not path.exists():
        if require_instruments:
            raise click.ClickException(f"instrument master missing at {path}")
        click.echo(f"WARNING: instrument master missing at {path}; assuming lot size 1 and skipping "
                   f"tradeability checks for {len(uni.symbols)} symbols. Run fetch-data to download it.", err=True)
        return list(uni.symbols), {s: 1 for s in uni.symbols}, uni.exchange
    resolved, dropped = resolve_universe(uni, load_instruments(path))
    for sym, why in dropped:
        click.echo(f"dropping {sym}: {why}")
    click.echo(f"resolved {len(resolved)} of {len(uni.symbols)} universe symbols")
    if not resolved:
        raise click.ClickException("no universe symbol resolved against the instrument master")
    return list(resolved), {s: i.lot_size for s, i in resolved.items()}, uni.exchange


def _resolve_and_login(cfg: Config, adapter, note: str = "") -> tuple:
    """Instrument master (refreshed when stale), universe resolution, then one Groww login so a
    credential problem aborts before any symbol loop. Returns (symbols, lots, exchange)."""
    path = Path(cfg.paths.instruments)
    if not _instruments_fresh(path):
        click.echo("downloading instrument master")
        download_instruments(path)
    symbols, lots, exchange = _symbols_and_lots(cfg, require_instruments=True)
    adapter.client
    click.echo(f"logged in to Groww ({adapter.flow} flow){note}")
    return symbols, lots, exchange


@main.command("fetch-data")
@click.option("--days", default=90, show_default=True, help="Lookback for symbols with no stored candles")
@click.option("--full", is_flag=True, help="Ignore stored candles and refetch the whole window")
@click.option("--sleep", "pause", default=0.5, show_default=True, help="Seconds to pause between API requests")
@click.pass_obj
def fetch_data(cfg: Config, days: int, full: bool, pause: float) -> None:
    """Incrementally download candles for the universe into SQLite. Run weekly to grow the cache.

    One bad symbol does not stop the others: failures are listed at the end and the exit code is 1."""
    if cfg.execution.interval_minutes not in CHUNK_DAYS:
        raise click.ClickException(f"unsupported candle interval: {cfg.execution.interval_minutes} minutes")
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret,
                           cfg.secrets.groww_api_secret)  # fails fast on malformed credentials
    setup_logging(cfg.paths.logs, run_id=f"fetch-{datetime.now():%Y%m%d-%H%M%S}")
    symbols, _, exchange = _resolve_and_login(cfg, adapter)
    repo = Repo(connect(cfg.paths.db))
    had_data = any(repo.latest_candle_ts(s, cfg.execution.interval_minutes) is not None for s in symbols)
    failures: list = []

    def throttled(symbol, exch, start_ts, end_ts, interval):
        try:
            return adapter.fetch_candles(symbol, exch, start_ts, end_ts, interval)
        finally:
            if pause:
                time.sleep(pause)

    clock = SessionClock(cfg.session, cfg.execution.interval_minutes)
    total = 0
    for sym in symbols:
        try:
            total += fetch_incremental(repo, throttled, [sym], exchange, cfg.execution.interval_minutes,
                                       days, int(time.time()), full=full, log=click.echo,
                                       keep=lambda cd: clock.in_session(cd.ts))[sym]
        except Exception as e:  # noqa: BLE001 - isolate per symbol; auth errors are fatal
            if _is_fatal_auth(e):
                raise
            if is_non_retryable(e) or isinstance(e, _FRIENDLY):
                failures.append((sym, f"{type(e).__name__}: {e}"))
                click.echo(f"failed: {sym} ({type(e).__name__}: {e})")
                continue
            raise
    click.echo(f"inserted {total} candles across {len(symbols) - len(failures)} symbols")
    if failures:
        raise click.ClickException(f"{len(failures)} symbol(s) failed: " + ", ".join(s for s, _ in failures))
    if total == 0 and not had_data:
        raise click.ClickException("no candles were returned for any symbol on a first fetch; "
                                   "check the date range, the universe, and the Groww response format")


@main.command()
@click.option("--start", required=True, type=click.DateTime(["%Y-%m-%d"]))
@click.option("--end", required=True, type=click.DateTime(["%Y-%m-%d"]))
@click.option("--strategy", "strategy_name", default="ema_rsi", show_default=True)
@click.option("--run-id", default=None, help="Defaults to bt-<timestamp>")
@click.option("--ai", "ai_filter", default=None, type=click.Choice(["stub", "claude", "claude_cached"]),
              help="Override ai.filter from config for this run")
@click.pass_obj
def backtest(cfg: Config, start: datetime, end: datetime, strategy_name: str, run_id: Optional[str],
             ai_filter: Optional[str]) -> None:
    """Replay stored candles through the strategy, risk engine, AI filter, and simulated broker."""
    if start > end:
        raise click.ClickException("--start must not be after --end")
    symbols, lots, _ = _symbols_and_lots(cfg, require_instruments=False)
    repo = Repo(connect(cfg.paths.db))
    interval = cfg.execution.interval_minutes
    source = HistoricalSource.from_repo(repo, symbols, interval,
                                        ist_epoch(start.date(), "00:00"), ist_epoch(end.date(), "23:59"))
    if not source.bar_timestamps():
        raise click.ClickException("no candles in range; run `tradebot fetch-data` first")
    if strategy_name not in cfg.strategy:
        raise click.ClickException(f"no config for strategy '{strategy_name}'")
    run_id = run_id or f"bt-{datetime.now():%Y%m%d-%H%M%S}"
    if repo.get_run(run_id) is not None:
        raise click.ClickException(f"run '{run_id}' already exists; pick another --run-id")
    log_path = setup_logging(cfg.paths.logs, run_id=run_id)
    strategy = build_strategy(strategy_name, strategy_params(cfg.strategy, strategy_name))
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    ai_cfg = dc_replace(cfg.ai, filter=ai_filter) if ai_filter else cfg.ai
    clock = SessionClock(cfg.session, interval)
    ai = build_filter(ai_cfg, cfg.secrets.anthropic_api_key, repo=repo, clock=clock)
    # The engine stores the resolved config with the run, so record the effective filter there too.
    engine = BacktestEngine(dc_replace(cfg, ai=ai_cfg), repo, source, [strategy], broker, ai, clock, lots, run_id)
    rid = engine.run()
    click.echo(format_summary(build_summary(repo, rid)))
    if log_path:
        click.echo(f"log: {log_path}")


@main.command()
@click.option("--run", "run_id", default=None)
@click.option("--compare", "compare", nargs=2, default=None, metavar="RUN_A RUN_B",
              help="Side-by-side of an unfiltered run A and a Claude-filtered run B")
@click.pass_obj
def report(cfg: Config, run_id: Optional[str], compare) -> None:
    """Print the summary for a stored run, or compare two runs."""
    if not Path(cfg.paths.db).exists():
        raise click.ClickException(f"no database at {cfg.paths.db}")
    if not run_id and not compare:
        raise click.ClickException("give --run RUN or --compare RUN_A RUN_B")
    if run_id and compare:
        raise click.ClickException("--run and --compare are mutually exclusive")
    repo = Repo(connect(cfg.paths.db))
    if compare:
        click.echo(format_compare(build_compare(repo, compare[0], compare[1], _prices(cfg))))
        return
    click.echo(format_summary(build_summary(repo, run_id)))


def _estimate_candidate(cfg: Config, r, window) -> Candidate:
    """A candidate shaped like the engine's, from a stored signal row and its candle window: the
    shared indicators are computed on the window so the token count matches real prompts."""
    ind = IndicatorSet(cfg.strategy.get("indicators"))
    for cd in window:
        ind.update(cd)
    snap = {**ind.snapshot(), "ema_fast": r["entry"], "ema_slow": r["entry"], "rsi": 55.0, "atr": abs(r["entry"] - r["stop"])}
    return Candidate(Signal(r["strategy"], r["symbol"], r["direction"], r["entry"], r["stop"], r["target"],
                            r["product"], r["bar_ts"]), 100, snap, tuple(window[-cfg.ai.candles_in_context:]))


def _prices(cfg: Config) -> Prices:
    return Prices(cfg.ai.price_in_per_mtok, cfg.ai.price_out_per_mtok,
                  cfg.ai.price_cache_read_per_mtok, cfg.ai.price_cache_write_per_mtok)


def _warm_fetch(cfg: Config, adapter, repo: Repo, symbols: list, exchange: str, clock: SessionClock,
                now_ts: int, pause: float = 0.5) -> list:
    """Bring the candle cache up to the last completed bar before starting paper. One symbol's failure
    does not stop the others; auth failures are fatal. `pause` spaces the requests like fetch-data's
    --sleep so a cold cache does not trip Groww's rate limit. Returns the symbols that failed."""
    failed = []
    interval = cfg.execution.interval_minutes
    click.echo(f"warming up: fetching recent candles for {len(symbols)} symbols")
    inserted = 0
    for sym in symbols:
        try:
            inserted += fetch_incremental(repo, adapter.fetch_candles, [sym], exchange, interval, 10, now_ts,
                                          keep=lambda cd: clock.in_session(cd.ts))[sym]
        except Exception as e:  # noqa: BLE001 - isolate per symbol; auth errors are fatal
            if _is_fatal_auth(e):
                raise
            failed.append(sym)
            log.warning("warm-up fetch failed for %s: %s: %s", sym, type(e).__name__, e, extra={"symbol": sym})
            click.echo(f"warm-up fetch failed for {sym}: {type(e).__name__}: {e}", err=True)
        finally:
            if pause:
                time.sleep(pause)
    click.echo(f"warm-up: {inserted} new candles stored, {len(failed)} symbol(s) failed")
    return failed


def _warm_candles(repo: Repo, symbols: list, interval: int, now_ts: int, n: int) -> list:
    """The last `n` stored bars per symbol up to now, today's included: PaperEngine treats warmed
    bars from today as processed on a new run and trims them to the resume point on a restart."""
    out = []
    for sym in symbols:
        out.extend(repo.load_candles([sym], interval, now_ts - 30 * 86400, now_ts)[-n:])
    return out


@main.command()
@click.option("--strategy", "strategy_name", default="ema_rsi", show_default=True)
@click.option("--run-id", default=None, help="Defaults to paper-<today>; a run resumes if it exists and has not ended")
@click.option("--ai", "ai_filter", default=None, type=click.Choice(["stub", "claude", "claude_cached"]),
              help="Override ai.filter from config for this session")
@click.pass_obj
def paper(cfg: Config, strategy_name: str, run_id: Optional[str], ai_filter: Optional[str]) -> None:
    """Paper-trade today's session on live 5-minute REST bars. Places no real orders: fills and exits
    are simulated exactly as in backtest. Ctrl-C finishes the current bar and leaves the run resumable."""
    if strategy_name not in cfg.strategy:
        raise click.ClickException(f"no config for strategy '{strategy_name}'")
    params = strategy_params(cfg.strategy, strategy_name)
    if params.get("product", "MIS") != "MIS":
        raise click.ClickException("paper mode supports MIS (intraday) strategies only; set product: MIS")
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret, cfg.secrets.groww_api_secret)
    interval = cfg.execution.interval_minutes
    clock = SessionClock(cfg.session, interval)
    now = int(time.time())
    today = date_of(now)
    run_id = run_id or f"paper-{today.isoformat()}"
    repo = Repo(connect(cfg.paths.db))
    existing = repo.get_run(run_id)
    if existing is not None and existing["mode"] != "paper":
        raise click.ClickException(f"run '{run_id}' exists and is not a paper run; pick another --run-id")
    if existing is not None and existing["ended_at"] is not None and now < clock.close_ts(today):
        raise click.ClickException(f"run '{run_id}' already ended today; pass a different --run-id to start another session")
    if existing is not None and date_of(existing["started_at"]) != today:
        raise click.ClickException(f"run '{run_id}' was started on {date_of(existing['started_at'])} and is still open; "
                                   f"paper runs are one per day, pass a different --run-id")
    resumable = existing is not None and existing["ended_at"] is None
    if not clock.is_trading_day(today) or (now >= clock.close_ts(today) and not resumable):
        click.echo(f"nothing to trade: {today} is not a trading day or the session has closed")
        return
    log_path = setup_logging(cfg.paths.logs, run_id=run_id)
    symbols, lots, exchange = _resolve_and_login(cfg, adapter, note="; paper mode places no orders")
    failed = _warm_fetch(cfg, adapter, repo, symbols, exchange, clock, now)
    if failed:
        click.echo(f"warm-up fetch failed for {len(failed)} symbol(s); they warm up live", err=True)
    warm = _warm_candles(repo, symbols, interval, now, cfg.data.warmup_bars)
    source = LiveBarSource(adapter.fetch_candles, repo, symbols, exchange, interval,
                           cfg.data.official_fetch_concurrency, clock,
                           # a fetch that outlives the deadline yields a stale bar anyway, so stop it earlier
                           budget_sec=max(1.0, cfg.execution.bar_deadline_sec - cfg.data.bar_grace_sec))
    strategy = build_strategy(strategy_name, params)
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage,
                            cfg.execution.entry_buffer_pct, charges=cfg.charges)
    ai_cfg = dc_replace(cfg.ai, filter=ai_filter) if ai_filter else cfg.ai
    ai = build_filter(ai_cfg, cfg.secrets.anthropic_api_key, repo=repo, clock=clock)
    engine = PaperEngine(dc_replace(cfg, ai=ai_cfg), repo, source, [strategy], broker, ai, clock, lots, run_id,
                         now=time.time, sleep=time.sleep)

    def _stop(signum, frame):
        # Only the flag and a raw write: a signal can land while stderr's buffered writer is locked.
        os_signal.signal(signum, os_signal.SIG_DFL)  # a second Ctrl-C (or kill) aborts at once
        os.write(2, b"stop requested; finishing the current bar, positions stay on the books (again to abort)\n")
        engine.request_stop()

    previous = {sig: os_signal.signal(sig, _stop) for sig in (os_signal.SIGINT, os_signal.SIGTERM)}
    try:
        rid = engine.run(warm)
    finally:
        for sig, handler in previous.items():
            os_signal.signal(sig, handler)
    if rid is None:
        click.echo("nothing to trade: the session closed while warming up")
        return
    click.echo(format_summary(build_summary(repo, rid)))
    if repo.get_run(rid)["ended_at"] is None:
        click.echo(f"run {rid} is still open: start the command again today to resume it")
    if log_path:
        click.echo(f"log: {log_path}")


@main.command("estimate-ai")
@click.option("--run", "run_id", required=True, help="A completed stub run whose risk-approved signals define the workload")
@click.option("--sample", default=0, show_default=True,
              help="Make this many real review calls on the largest bars to measure output tokens (costs cents)")
@click.pass_obj
def estimate_ai(cfg: Config, run_id: str, sample: int) -> None:
    """Estimate calls, tokens and cost of replaying a run through the Claude filter, as a range.

    Input tokens come from two free count_tokens calls on the run's real largest bar (one candidate and
    all of them), which separates the fixed per-call cost from the per-candidate cost. The system prompt is
    priced as one cache write plus cache reads. Output tokens are the unknown: --sample measures them with
    real calls; without it the low end assumes a compact answer and the high end assumes ai.max_tokens."""
    if not Path(cfg.paths.db).exists():
        raise click.ClickException(f"no database at {cfg.paths.db}")
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is None:
        raise click.ClickException(f"unknown run: {run_id}")
    by_bar = repo.approved_signals_by_bar(run_id)
    if not by_bar:
        raise click.ClickException("that run has no risk-approved signals")
    bars = len(by_bar)
    candidates = sum(len(v) for v in by_bar.values())
    biggest_ts, biggest_rows = max(by_bar.items(), key=lambda kv: len(kv[1]))
    biggest = len(biggest_rows)
    interval = cfg.execution.interval_minutes
    clock = SessionClock(cfg.session, interval)
    session = {"square_off": cfg.session.square_off, "entry_cutoff": cfg.session.no_new_entries_after,
               "close": cfg.session.close,
               "bars_left": max(0, (clock.square_off_bar_ts(date_of(biggest_ts)) - biggest_ts) // (interval * 60))}
    window = repo.load_candles([r["symbol"] for r in biggest_rows], interval,
                               biggest_ts - cfg.ai.candles_in_context * interval * 60, biggest_ts)
    by_symbol: dict = {}
    for cd in window:
        by_symbol.setdefault(cd.symbol, []).append(cd)
    cands = [_estimate_candidate(cfg, r, by_symbol.get(r["symbol"], ())) for r in biggest_rows]
    client = ClaudeClient(cfg.secrets.anthropic_api_key, cfg.ai.model, cfg.ai.effort, cfg.ai.max_tokens, cfg.ai.timeout_sec)
    system_tokens = client.count_tokens(SYSTEM_PROMPT, "x", RESPONSE_SCHEMA)
    one = client.count_tokens(SYSTEM_PROMPT, render_candidates(cands[:1], session), RESPONSE_SCHEMA)
    per_cand = (client.count_tokens(SYSTEM_PROMPT, render_candidates(cands, session), RESPONSE_SCHEMA) - one) / (biggest - 1) \
        if biggest > 1 else one - system_tokens
    fixed_user = max(0.0, one - system_tokens - per_cand)  # envelope + schema + session facts, per call
    uncached_input = int(bars * fixed_user + candidates * per_cand)
    cache_read = system_tokens * max(0, bars - 1)
    cache_write = system_tokens
    measured = None
    if sample > 0:
        top = sorted(by_bar.items(), key=lambda kv: -len(kv[1]))[:sample]
        flt = build_filter(dc_replace(cfg.ai, filter="claude"), cfg.secrets.anthropic_api_key, clock=clock)
        for ts, rows in top:
            w = repo.load_candles([r["symbol"] for r in rows], interval, ts - cfg.ai.candles_in_context * interval * 60, ts)
            bs: dict = {}
            for cd in w:
                bs.setdefault(cd.symbol, []).append(cd)
            flt.review([_estimate_candidate(cfg, r, bs.get(r["symbol"], ())) for r in rows])
        if flt.successful_calls:
            measured = flt.tokens["output"] / flt.successful_calls
            click.echo(f"sampled {flt.successful_calls} real calls on the largest bars ({flt.calls - flt.successful_calls} failed): "
                       f"mean output {measured:.0f} tokens, mean latency {flt.latency_total_ms // flt.successful_calls} ms, "
                       f"spent ~${_prices(cfg).cost(flt.tokens['input'], flt.tokens['output'], flt.tokens['cache_read'], flt.tokens['cache_write']):.2f}")
        else:
            click.echo(f"all {flt.calls} sampled calls failed; output tokens stay unmeasured", err=True)
    # A measured mean comes from the largest bars, so it leans high for a typical bar: the low bound
    # uses it as-is and the high bound adds 50% headroom for variance.
    low_out = int(bars * (measured if measured is not None else 60 * (candidates / bars) + 150))
    high_out = int(bars * (measured * 1.5 if measured is not None else cfg.ai.max_tokens))
    prices = _prices(cfg)
    low = prices.cost(uncached_input, low_out, cache_read, cache_write)
    high = prices.cost(uncached_input, high_out, cache_read, cache_write)
    click.echo(f"API calls: {bars} bars with candidates   candidates: {candidates}   largest bar: {biggest}")
    click.echo(f"tokens: system {system_tokens} (cached after the first call), per call {fixed_user:.0f} + {per_cand:.0f} per candidate")
    click.echo(f"input: {uncached_input:,} uncached + {cache_read:,} cache reads;  output: {low_out:,} to {high_out:,}"
               + ("" if measured is not None else "  (unmeasured: run with --sample 10 to measure)"))
    click.echo(f"estimated cost ({cfg.ai.model}, {cfg.ai.effort} effort): ${low:,.2f} to ${high:,.2f}; a cached replay costs $0")
    click.echo("note: a stub run under-counts candidates slightly, since Claude's rejections free position slots later on")
    if bars > cfg.ai.max_calls_per_run:
        click.echo(f"WARNING: {bars} calls exceed ai.max_calls_per_run={cfg.ai.max_calls_per_run}; the tail of the replay "
                   f"would silently become on_failure decisions. Raise the cap first.", err=True)
