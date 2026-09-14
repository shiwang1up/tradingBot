"""Command-line entry point: fetch-data, backtest, report. Paper/live/flatten arrive in Plan 3."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path

import click

from tradebot.ai.filter import build_filter
from tradebot.config import Config, load_config
from tradebot.data.historical import HistoricalSource, fetch_incremental
from tradebot.data.instruments import download_instruments, load_instruments, resolve_universe
from tradebot.data.universe import load_universe
from tradebot.engine.clock import SessionClock, ist_epoch
from tradebot.engine.loop import BacktestEngine
from tradebot.execution.backtest import BacktestBroker
from tradebot.execution.groww_adapter import GrowwAdapter
from tradebot.report.summary import build_summary, format_summary
from tradebot.store.db import connect
from tradebot.store.repo import Repo
from tradebot.strategy.ema_rsi import build_strategy


@click.group()
@click.option("--config", "config_path", default="config.yaml", show_default=True, help="Path to config.yaml")
@click.pass_context
def main(ctx: click.Context, config_path: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx.obj = load_config(config_path, Path(config_path).parent / ".env")


def _instruments_fresh(path: Path) -> bool:
    return path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() >= date.today()


def _symbols_and_lots(cfg: Config, require_instruments: bool) -> tuple[list[str], dict[str, int], str]:
    uni = load_universe(cfg.paths.universe)
    path = Path(cfg.paths.instruments)
    if not path.exists():
        if require_instruments:
            raise click.ClickException(f"instrument master missing at {path}")
        return list(uni.symbols), {s: 1 for s in uni.symbols}, uni.exchange
    resolved, dropped = resolve_universe(uni, load_instruments(path))
    for sym, why in dropped:
        click.echo(f"dropping {sym}: {why}")
    return list(resolved), {s: i.lot_size for s, i in resolved.items()}, uni.exchange


@main.command("fetch-data")
@click.option("--days", default=90, show_default=True, help="Lookback for symbols with no stored candles")
@click.option("--full", is_flag=True, help="Ignore stored candles and refetch the whole window")
@click.pass_obj
def fetch_data(cfg: Config, days: int, full: bool) -> None:
    """Incrementally download candles for the universe into SQLite. Run weekly to grow the cache."""
    path = Path(cfg.paths.instruments)
    if not _instruments_fresh(path):
        click.echo("downloading instrument master")
        download_instruments(path)
    symbols, _, exchange = _symbols_and_lots(cfg, require_instruments=True)
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret)
    repo = Repo(connect(cfg.paths.db))
    result = fetch_incremental(repo, adapter.fetch_candles, symbols, exchange, cfg.execution.interval_minutes,
                               days, int(time.time()), full=full, log=click.echo)
    click.echo(f"inserted {sum(result.values())} candles across {len(result)} symbols")


@main.command()
@click.option("--start", required=True, type=click.DateTime(["%Y-%m-%d"]))
@click.option("--end", required=True, type=click.DateTime(["%Y-%m-%d"]))
@click.option("--strategy", "strategy_name", default="ema_rsi", show_default=True)
@click.option("--run-id", default=None, help="Defaults to bt-<timestamp>")
@click.pass_obj
def backtest(cfg: Config, start: datetime, end: datetime, strategy_name: str, run_id: str | None) -> None:
    """Replay stored candles through the strategy, risk engine, AI filter, and simulated broker."""
    symbols, lots, _ = _symbols_and_lots(cfg, require_instruments=False)
    repo = Repo(connect(cfg.paths.db))
    interval = cfg.execution.interval_minutes
    source = HistoricalSource.from_repo(repo, symbols, interval,
                                        ist_epoch(start.date(), "00:00"), ist_epoch(end.date(), "23:59"))
    if not source.bar_timestamps():
        raise click.ClickException("no candles in range; run `tradebot fetch-data` first")
    if strategy_name not in cfg.strategy:
        raise click.ClickException(f"no config for strategy '{strategy_name}'")
    strategy = build_strategy(strategy_name, cfg.strategy[strategy_name])
    broker = BacktestBroker(cfg.capital, cfg.execution.slippage_pct, cfg.risk.mis_leverage, cfg.execution.entry_buffer_pct)
    engine = BacktestEngine(cfg, repo, source, [strategy], broker,
                            build_filter(cfg.ai, cfg.secrets.anthropic_api_key),
                            SessionClock(cfg.session, interval), lots,
                            run_id or f"bt-{datetime.now():%Y%m%d-%H%M%S}")
    rid = engine.run()
    click.echo(format_summary(build_summary(repo, rid)))


@main.command()
@click.option("--run", "run_id", required=True)
@click.pass_obj
def report(cfg: Config, run_id: str) -> None:
    """Print the summary for a stored run."""
    repo = Repo(connect(cfg.paths.db))
    try:
        click.echo(format_summary(build_summary(repo, run_id)))
    except ValueError as e:
        raise click.ClickException(str(e)) from e
