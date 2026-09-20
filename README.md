# tradebot

NSE/BSE trading bot on the Groww Trade API. Spec: `docs/superpowers/specs/2026-09-14-nse-bse-trading-bot-design.md`.
Plan 1 is the backtester, Plan 2 the Claude filter and compare report, Plan 3 paper trading on live
candles. A later plan (`docs/superpowers/specs/2026-09-19-charges-orb-regime-design.md`) added real
charges, the opening-range breakout and the regime filter; results are in `docs/superpowers/notes/`.
Live order placement is a separate, later plan.

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

## Charges

Backtest and paper results are net of brokerage and statutory charges (`charges:` in the config,
Groww intraday equity rates; check them against Groww's pricing page). `pnl` in the database stays
gross; `positions.charges` holds the cost. Reports print gross, charges, net and `R on risk`: net PnL
over rupees at risk, all trades pooled. That is the figure to judge a run by; the per-trade `Avg R`
is dominated by scrap-sized trades. Runs stored before the charges model are estimated after the fact
and marked so.

Upgrading: a config file with no `charges:` block gets charges ENABLED at the default Groww rates, so
results from an existing config change after upgrading. Set `charges.enabled: false` to reproduce the
old gross-only numbers.

## Opening-range breakout

    .venv/bin/tradebot --config config-orb.yaml backtest --strategy orb --start 2026-06-17 --end 2026-08-15
    .venv/bin/tradebot --config config-orb.yaml paper --strategy orb --ai stub
    .venv/bin/python scripts/orb_experiment.py tune

`config-orb.yaml` configures only the `orb` strategy, so `--strategy orb` is required. 15-minute bars,
one signal per symbol per day, at most two entries a day, no entries after 13:00, positions flattened
at 15:00. When several symbols break out on one bar the engine takes the ones with the highest volume
relative to their opening range (`Signal.priority`), then alphabetical order, in backtest and paper
alike. The ORB paper session must be running before the first bar after the range closes (09:45 for
the shipped 30-minute range, 10:15 for a 60-minute one): most breakouts fire on that bar, and one seen
during warm-up is spent, not traded.

`scripts/orb_experiment.py` has three phases: `tune` (six parameter sets on 2026-06-17..2026-08-15),
`holdout` (2026-08-16..2026-09-15, a fixed run id, so it runs once; guarded) and `regime` (filter off
and on for ORB and confluence, tuning window only). `holdout` and `regime` take `--range` and `--rr`.
Results so far, none of them profitable: `docs/superpowers/notes/2026-09-19-charges-orb-regime-results.md`.

## Regime filter

`data.index_symbol: NIFTY` is fetched and stored with the universe and never traded. `fetch-data`
fetches it last, and an index failure never blocks the universe. The shipped configs name NIFTY, so
`fetch-data` exits 1 when only the index fails (the universe candles are still stored); clear
`data.index_symbol` to stop fetching it. With `regime.enabled: true` longs
are taken only while the index is above its EMA and shorts only while at or below; blocked signals
appear under "Risk rejects" as `regime` or `regime_not_ready`. `regime.source: composite` builds the
index from the universe's own returns when Groww serves no index history. The live paper feed carries
the index only when the filter is on.

## Expectancy and risk consistency

Reports print the expectancy block: average win and loss, payoff, expectancy per trade, the win rate
that payoff would need to break even, and how many days of evidence the number rests on. `R on risk`
(net PnL over rupees at risk, pooled) stays the decision metric.

`risk.min_risk_fraction` (default 0.5) skips a trade whose size margin has cut below half its planned
risk, instead of taking it at a fraction of the intended stake, and counts it under "Risk rejects" as
`risk_too_small`. Set it to 0 to reproduce older runs.

## Daily systems screen

    .venv/bin/python scripts/daily_screen.py fetch      # daily candles from 2020 (Groww key must be approved)
    .venv/bin/python scripts/daily_screen.py insample   # 2020-01-01..2023-12-31
    .venv/bin/python scripts/daily_screen.py holdout    # 2024-01-01..2025-11-21, refuses a second run

Three classic daily systems (mean reversion, trend following, breakout), long only, entered at the
next day's open, net of delivery charges, scored on expectancy in excess of holding the same stock for
the same number of days (printed per entry date, with the per-trade figure beside it, since same-day
trades are correlated). Rules are fixed in
`docs/superpowers/specs/2026-09-20-expectancy-risk-daily-screen-design.md`. No system has passed the
spec's bar so far. Results: `docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`.

## Paper trading

    .venv/bin/tradebot paper --strategy ema_rsi --ai stub

Runs today's session on live 5-minute candles fetched over REST at each bar boundary, through the
same strategy, risk, AI filter and simulated broker as the backtester. It places no real orders.
Start it before 09:15 IST (it waits) or any time during the session: a fresh start mid-session warms
up on the bars already gone (no entries on them) and trades from the next bar; a restart replays the
missed bars with the simulated broker, dropping their signals as `stale` past
`execution.bar_deadline_sec`, so exits are settled but nothing is entered late. One run
per day, id `paper-YYYY-MM-DD`; `report --run paper-YYYY-MM-DD` prints the summary and the daily
fill rate. Ctrl-C finishes the current bar and leaves the run resumable: start the command again the
same day and it reloads open positions and pending entries from SQLite. A run left open by a crash
can be started again the same day, even after 15:30, to replay the missed bars, square off and
close the books; from the next day on it is refused and you start a new run id. The
approval-flow Groww key must be approved on the API keys page before starting each day.

`--ai stub` keeps Claude out of the loop; drop it (or pass `--ai claude_cached`) to pay for the
filter on live signals. Intraday (MIS) strategies only. `data.bar_grace_sec` and `data.warmup_bars`
in `config.yaml` tune the wait after each boundary and the warm-up depth.

## Tests

    .venv/bin/pytest -q

## Kill switch

Create a file named `KILL` in the project root to stop new entries. Put the word `flatten`
in it to also square off everything on the next bar. An unreadable file also blocks entries.
