# tradebot

NSE/BSE trading bot on the Groww Trade API. Spec: `docs/superpowers/specs/2026-09-14-nse-bse-trading-bot-design.md`.
Plan 1 (this code) is the backtester. Plan 2 adds the Claude filter; Plan 3 adds paper and live trading.

## Setup

    python3 -m venv .venv && .venv/bin/pip install -U pip setuptools && .venv/bin/pip install -e ".[dev]"
    cp .env.example .env   # fill in GROWW_API_KEY, one of GROWW_TOTP_SECRET / GROWW_API_SECRET, ANTHROPIC_API_KEY

Python 3.9 or newer. Secrets are read from `.env` next to `config.yaml` (or `--env <path>`);
a variable already exported in the shell wins over the file.

Groww offers two login flows. The TOTP flow pairs a TOTP api key with a base32 secret and never
expires. The approval flow pairs a JWT api key with an API secret and must be approved daily on
the Groww API keys page. Set `GROWW_TOTP_SECRET` for the first or `GROWW_API_SECRET` for the second.

## Backtest

    .venv/bin/python scripts/update_universe.py        # optional: NIFTY 200 from NSE
    .venv/bin/tradebot fetch-data --days 90            # run weekly to grow the cache
    .venv/bin/tradebot backtest --start 2026-06-15 --end 2026-09-12
    .venv/bin/tradebot report --run <run id printed above>

`fetch-data` is incremental and idempotent. Groww serves about three months of 5-minute
history, so a weekly run grows the local cache past that window. `--full` refetches and
repairs the whole window.

Survivorship bias: `universe.yaml` is today's constituent list, so backtests overstate results.

## Paper trading

    .venv/bin/tradebot paper --strategy ema_rsi --ai stub

Runs today's session on live 5-minute candles fetched over REST at each bar boundary, through the
same strategy, risk, AI filter and simulated broker as the backtester. It places no real orders.
Start it before 09:15 IST (it waits) or any time during the session (it warms up from the cache and
catches up; signals on bars older than `execution.bar_deadline_sec` are dropped as `stale`). One run
per day, id `paper-YYYY-MM-DD`; `report --run paper-YYYY-MM-DD` prints the summary and the daily
fill rate. Ctrl-C finishes the current bar and leaves the run resumable: start the command again the
same day and it reloads open positions and pending entries from SQLite. A run left open by a crash
can be started after 15:30 to replay the missed bars, square off and close the books. The
approval-flow Groww key must be approved on the API keys page before starting each day.

`--ai stub` keeps Claude out of the loop; drop it (or pass `--ai claude_cached`) to pay for the
filter on live signals. Intraday (MIS) strategies only. `data.bar_grace_sec` and `data.warmup_bars`
in `config.yaml` tune the wait after each boundary and the warm-up depth.

## Tests

    .venv/bin/pytest -q

## Kill switch

Create a file named `KILL` in the project root to stop new entries. Put the word `flatten`
in it to also square off everything on the next bar. An unreadable file also blocks entries.
