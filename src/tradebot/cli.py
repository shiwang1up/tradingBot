"""Command-line entry point: fetch-data, backtest, report. Paper/live/flatten arrive in Plan 3."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import replace as dc_replace
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import click
import requests

from tradebot.ai.claude_client import ClaudeClient
from tradebot.ai.filter import AIFilterAborted, build_filter
from tradebot.ai.prompt import SYSTEM_PROMPT, render_candidates
from tradebot.config import Config, load_config
from tradebot.data.historical import CHUNK_DAYS, HistoricalSource, fetch_incremental
from tradebot.data.instruments import download_instruments, load_instruments, resolve_universe
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.engine.logsetup import setup_logging
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.execution.groww_adapter import GrowwAdapter, is_non_retryable
from tradebot.report.compare import Prices, build_compare, format_compare
from tradebot.report.summary import build_summary, format_summary
from tradebot.store.db import SchemaVersionError, connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy
from tradebot.types import Candidate, Candle, Signal

# Operational failures that deserve a one-line message. sqlite3 programming errors (bad SQL) still traceback.
_FRIENDLY = (ValueError, SchemaVersionError, sqlite3.OperationalError, sqlite3.DatabaseError,
             requests.RequestException, NotImplementedError, AIFilterAborted)
_FATAL_AUTH_CODES = {"401", "403"}


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
    path = Path(cfg.paths.instruments)
    if not _instruments_fresh(path):
        click.echo("downloading instrument master")
        download_instruments(path)
    symbols, _, exchange = _symbols_and_lots(cfg, require_instruments=True)
    adapter.client  # log in once, here, so a credential problem aborts before the symbol loop
    click.echo(f"logged in to Groww ({adapter.flow} flow)")
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
    strategy = build_strategy(strategy_name, cfg.strategy[strategy_name])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
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
    repo = Repo(connect(cfg.paths.db))
    if compare:
        click.echo(format_compare(build_compare(repo, compare[0], compare[1], _prices(cfg))))
        return
    click.echo(format_summary(build_summary(repo, run_id)))


def _prices(cfg: Config) -> Prices:
    return Prices(cfg.ai.price_in_per_mtok, cfg.ai.price_out_per_mtok,
                  cfg.ai.price_cache_read_per_mtok, cfg.ai.price_cache_write_per_mtok)


@main.command("estimate-ai")
@click.option("--run", "run_id", required=True, help="A completed stub run whose risk-approved signals define the workload")
@click.pass_obj
def estimate_ai(cfg: Config, run_id: str) -> None:
    """Estimate calls, tokens and cost of replaying a run through the Claude filter.

    Counts tokens once on the largest bar's prompt via the API, then scales by the number of
    bars that had at least one risk-approved candidate."""
    repo = Repo(connect(cfg.paths.db))
    if repo.get_run(run_id) is None:
        raise click.ClickException(f"unknown run: {run_id}")
    rows = repo.conn.execute(
        "SELECT s.bar_ts, COUNT(*) AS n FROM risk_decisions r JOIN signals s ON s.id = r.signal_id "
        "WHERE r.run_id=? AND r.approved=1 GROUP BY s.bar_ts", (run_id,)).fetchall()
    if not rows:
        raise click.ClickException("that run has no risk-approved signals")
    bars = len(rows)
    candidates = sum(r["n"] for r in rows)
    biggest = max(r["n"] for r in rows)
    client = ClaudeClient(cfg.secrets.anthropic_api_key, cfg.ai.model, cfg.ai.effort, cfg.ai.max_tokens, cfg.ai.timeout_sec)
    sample = [Candidate(Signal("ema_rsi", f"SYM{i}", "LONG", 1280.0, 1275.5, 1289.0, "MIS", 1789357500), 200,
                        {"ema_fast": 1280.1, "ema_slow": 1279.9, "rsi": 61.2, "atr": 3.1},
                        tuple(Candle(f"SYM{i}", 1789357500 - 300 * k, 1279.0, 1281.0, 1278.0, 1280.0, 12345)
                              for k in range(cfg.ai.candles_in_context, 0, -1)))
              for i in range(biggest)]
    session = {"square_off": cfg.session.square_off, "entry_cutoff": cfg.session.no_new_entries_after,
               "close": cfg.session.close, "bars_left": 40}
    per_biggest = client.count_tokens(SYSTEM_PROMPT, render_candidates(sample, session))
    per_candidate = per_biggest / biggest
    input_tokens = int(candidates * per_candidate)
    output_tokens = candidates * 60
    cost = _prices(cfg).cost(input_tokens, output_tokens, 0, 0)
    click.echo(f"bars with candidates: {bars}   candidates: {candidates}   largest bar: {biggest}")
    click.echo(f"tokens per candidate: {per_candidate:.0f}   input total: {input_tokens:,}   output total ~{output_tokens:,}")
    click.echo(f"estimated cost ({cfg.ai.model}): ${cost:,.2f} as an upper bound; the shared system prompt caches, "
               f"and a cached replay costs $0")
