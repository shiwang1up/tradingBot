# tradebot

NSE/BSE trading bot on the Groww Trade API. Spec: `docs/superpowers/specs/2026-09-14-nse-bse-trading-bot-design.md`.
Plan 1 (this code) is the backtester. Plan 2 adds the Claude filter; Plan 3 adds paper and live trading.

## Setup

    python3 -m venv .venv && .venv/bin/pip install -U pip setuptools && .venv/bin/pip install -e ".[dev]"
    cp .env.example .env   # fill in GROWW_API_KEY, GROWW_TOTP_SECRET, ANTHROPIC_API_KEY

Python 3.9 or newer. Secrets are read from `.env` next to `config.yaml` (or `--env <path>`);
a variable already exported in the shell wins over the file.

## Backtest

    .venv/bin/python scripts/update_universe.py        # optional: NIFTY 200 from NSE
    .venv/bin/tradebot fetch-data --days 90            # run weekly to grow the cache
    .venv/bin/tradebot backtest --start 2026-06-15 --end 2026-09-12
    .venv/bin/tradebot report --run <run id printed above>

`fetch-data` is incremental and idempotent. Groww serves about three months of 5-minute
history, so a weekly run grows the local cache past that window. `--full` refetches and
repairs the whole window.

Survivorship bias: `universe.yaml` is today's constituent list, so backtests overstate results.

## Tests

    .venv/bin/pytest -q

## Kill switch

Create a file named `KILL` in the project root to stop new entries. Put the word `flatten`
in it to also square off everything on the next bar. An unreadable file also blocks entries.
