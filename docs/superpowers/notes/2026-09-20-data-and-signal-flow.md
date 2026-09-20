# How the data connects, and how a signal becomes an order

A map of the code as it stands on 2026-09-20. Every box names the file that does the work, so the
picture and the code can be checked against each other.

---

## 1. Where the data comes from, and where it lands

Two files describe *what* to trade; Groww supplies *the prices*. Everything meets in one SQLite file.

```
  universe.yaml                    Groww instrument master              Groww Trade API
  ─────────────                    ───────────────────────              ───────────────
  exchange: NSE                    instrument.csv (19 MB)               get_historical_candles
  symbols:                           trading_symbol                    (~3 months of 5m history)
    - RELIANCE                       lot_size, tick_size                       │
    - TCS        ...                 segment, instrument_type                  │
        │                            buy_allowed, sell_allowed                 │
        │                                    │                                 │
        │  data/universe.py                  │  data/instruments.py            │  execution/
        └──────────────┬─────────────────────┘                                 │  groww_adapter.py
                       │                                                       │
                       ▼                                                       │
        resolve_universe(universe, instruments)                                 │
        ┌────────────────────────────────────┐                                  │
        │ kept:    symbol -> Instrument      │   dropped: not_found /           │
        │          (CASH + EQ + tradeable)   │            not_cash_equity /     │
        │ lots:    symbol -> lot_size        │            not_tradeable         │
        └──────────────┬─────────────────────┘                                  │
                       │                                                        │
                       │  the symbol list to fetch  ────────────────────────────┤
                       │  (+ data.index_symbol: NIFTY, fetched LAST, never traded)
                       │                                                        │
                       │                        data/historical.py              │
                       │                        fetch_incremental()             │
                       │                          from last stored ts + 1       │
                       │                          to the last COMPLETED bar     │
                       │                          INSERT OR IGNORE, so a re-run │
                       │                          repairs nothing and breaks    │
                       │                          nothing                       │
                       │                                   │                    │
                       ▼                                   ▼                    ▼
        ╔═══════════════════════════════════════════════════════════════════════════╗
        ║                      data/tradebot.db   (store/schema.sql)                ║
        ║                                                                           ║
        ║   candles (symbol, ts, interval) -> o h l c v source   <-- THE ONE INPUT  ║
        ║                                                                           ║
        ║   runs · signals · risk_decisions · ai_decisions · ai_cache               ║
        ║   orders · fills · positions · daily_pnl               <-- THE OUTPUTS    ║
        ╚═══════════════════════════════════════════════════════════════════════════╝
```

`interval` separates the worlds that share the table: **5** = the 5-minute intraday systems,
**15** = the pullback/ORB configs, **1440** = the daily bars the offline screen reads.

---

## 2. Two ways the same candles reach the engine

The engine cannot tell the difference. That is the whole point of the design: a backtest and a paper
session run *identical* code per bar.

```
   BACKTEST                                    PAPER (live bars, no orders placed)
   ────────                                    ──────────────────────────────────
   repo.load_candles(...)                      at each bar boundary + bar_grace_sec:
        │                                      data/live.py LiveBarSource.fetch_range()
        ▼                                        - one REST window per symbol, 5 at a time
   data/historical.py                             - a symbol that fails or runs past the
   HistoricalSource                                 budget is simply absent this bar
     _by_ts[ts][symbol] = Candle                  - every completed bar is also STORED,
        │                                            so the cache heals its own gaps
        │  bar_timestamps()  (sorted)                    │
        │  candles_at(ts)                                │  + warm-up: data.warmup_bars
        │                                                │    stored bars replayed first
        └────────────────┬───────────────────────────────┘
                         ▼
              Engine.process_bar(ts, {symbol: Candle}, now_ts)
                         engine/loop.py
```

---

## 3. The per-bar cycle — the spine of the bot

One timestamp in, at most a few orders out. Read top to bottom; the order is load-bearing.

```
process_bar(ts, candles)                                            engine/loop.py
│
├─ 0a  _usable()          drop any candle with a non-finite or <= 0 OHLC
│                         (a bad row must never reach an indicator)
│
├─ 0b  _strip_index()     pull NIFTY out of the bar
│                         ┌────────────────────────────────────────────────┐
│                         │  the index goes ONE place only:                 │
│                         │  _feed_regime() -> risk/regime.py RegimeFilter  │
│                         │     EMA(regime.ema_period) of the index close   │
│                         │     close > EMA  ->  UP    (longs allowed)      │
│                         │     close <= EMA ->  DOWN  (shorts allowed)     │
│                         │     not enough bars -> NOT_READY (all blocked)  │
│                         │  source: composite builds this from the mean    │
│                         │  bar return of the universe when no index data  │
│                         └────────────────────────────────────────────────┘
│                         No strategy, indicator, broker or AI prompt ever sees NIFTY.
│
├─ 0c  _observe()         for every tradable symbol, three things get updated:
│                           _last_close[sym]    -> marks open positions, feeds composite
│                           _history[sym]       -> deque(maxlen=ai.candles_in_context)
│                                                  = the candle window the AI will read
│                           _indicators[sym]    -> IndicatorSet.update(candle)   strategy/ta.py
│                                                  = the SHARED technical context
│
├─ 1   broker.on_bar()    execution/backtest.py
│                           pending entry  -> fills at THIS bar's open (+ slippage,
│                                             unfilled if beyond entry_buffer_pct)
│                           open position  -> bar low/high vs stop/target
│                                             both touched in one bar => STOP wins
│                           on close       -> execution/charges.py position_charges()
│
├─ 2   square-off         from session.square_off onward, MIS positions are closed
├─ 3   daily loss cap     realised (net) + change in unrealised vs risk.daily_loss_cap_pct
├─ 4   kill switch        a KILL file on disk flattens everything
│
├─ 5   _run_strategies()  EVERY candle goes to EVERY strategy, every bar, always —
│                         even outside entry hours — so indicators never go cold.
│                         A strategy that raises is disabled for the day and reset.
│                              │
│                              ▼  list of (strategy, Signal)
│                         sort by (-priority, symbol)   <- deterministic in both modes
│
└─ 6   _place()           the gauntlet. Each signal is written down FIRST, then filtered:

          Signal ──> repo.insert_signal()  ────────────────────────► signals table
             │
             ▼  (a)  REGIME          regime / regime_not_ready ──┐
             │                                                   │
             ▼  (b)  RISK  risk/engine.py evaluate()             │
             │        kill_switch · daily_loss_cap                │
             │        max_entries_per_day · max_open_positions    │
             │        symbol_already_open · cooldown              ├─► risk_decisions
             │        invalid_price · invalid_stop                │   (approved 0/1,
             │        short_requires_mis                          │    reason, qty)
             │        ── sizing: risk/sizing.py ──                │
             │        qty = min( capital*per_trade_pct / |entry-stop| ,
             │                   available_margin / entry )  floored to lot size
             │        insufficient_size · risk_too_small ─────────┘
             │        (risk_too_small = margin shrank the position below
             │         min_risk_fraction of the planned risk: a scrap trade
             │         cannot pay its own brokerage, so it is skipped)
             │
             ▼  (c)  build the Candidate that the AI will judge
             │        ┌──────────────────────────────────────────────┐
             │        │ Candidate = Signal                            │
             │        │           + quantity                          │
             │        │           + indicators:                       │
             │        │               IndicatorSet.snapshot()  (0c)   │
             │        │             + strategy.snapshot(symbol)       │
             │        │           + candles: the _history deque       │
             │        └──────────────────────────────────────────────┘
             │
             ▼  (d)  AI FILTER   ai/filter.py — ONE request per bar for the whole batch
             │        stub          approves everything (backtest default)
             │        claude        ai/prompt.py: fixed system prompt (so it caches)
             │                     + deterministic JSON user message
             │                     + session facts (square-off, bars_left)
             │                     -> structured decisions {approve, confidence, reason}
             │        claude_cached same, but keyed by (symbol, bar_ts, sha256(prompt))
             │                     in ai_cache, so a replay is free ────► ai_cache
             │        failure       on_failure: reject | pass_through;
             │                     20 failures in a row aborts the run
             │                                                     └───► ai_decisions
             │
             └─ approved ──► repo.insert_order() ──► orders
                             broker.place_entry()  (fills on the NEXT bar's open)
                                      │
                                      └─ fill ──► fills, positions
                                         close ─► positions.closed_at/pnl/charges
                                                  + daily_pnl (realised, unrealised, fill_rate)
```

**The audit trail is the point.** Every signal gets a `signals` row and a `risk_decisions` row even
when it is thrown away, so `tradebot report` can say *why* nothing traded.

---

## 4. Signal generation: how candles become a direction, a stop and a target

Four strategies, one interface (`strategy/base.py`): `on_candle(candle) -> Signal | None`. Each keeps
its own state per symbol, and nothing looks ahead — an indicator sees one bar at a time.

```
                          ┌─────────────────────────┐
     Candle ─────────────►│  incremental indicators │──────► Signal(direction,
     (o,h,l,c,v,ts)       │  strategy/indicators.py │        entry=this bar's close,
                          │  strategy/ta.py         │        stop, target, product, priority)
                          └─────────────────────────┘
```

### (a) ema_rsi — 5-minute crossover

```
  close ──► EMA(9) ─┐
  close ──► EMA(21)─┴─► diff = fast - slow      LONG  when diff crosses 0 UPWARD  and RSI >= 55
  close ──► RSI(14) ──► confirmation            SHORT when diff crosses 0 DOWNWARD and RSI <= 45
  bar   ──► ATR(14) ──► stop distance
                                                stop   = close -/+ ATR * 1.5
                                                target = close +/- (stop distance) * 2.0
                                                skipped if the stop is < 0.1% of price (noise)
```

### (b) confluence — six weighted votes crossing a threshold

The only strategy that uses the whole `IndicatorSet`. Each layer votes −1, 0 or +1, times its weight:

```
  ┌───────────────────────────────────────────────────────────────────────────────┐
  │ layer       reads                                what makes it +1       weight│
  ├───────────────────────────────────────────────────────────────────────────────┤
  │ trend       EMA20 vs EMA50                       EMA20 above             1.0  │
  │ macd        MACD(12,26,9) hist + its turn        turned up, or hist      1.0  │
  │                                                  agreeing with trend          │
  │ momentum    Momentum(1), (5), (60) bars          all three positive      1.0  │
  │ breakout    RollingLevels(100) + VolumeSpike(20) close above the 100-bar 1.5  │
  │                                                  high WITH volume >=150%      │
  │ pattern     Patterns (engulfing, double top/bot)  bullish engulfing or   1.0  │
  │                                                   double bottom               │
  │ exhaustion  RSI + Bollinger %B                   RSI>=70 AND %B>=0.8   −1.5  │
  │                                                  (counts AGAINST a long)      │
  └───────────────────────────────────────────────────────────────────────────────┘
                                    │
                       score = sum of the weighted votes
                                    │
          ADX(14) < adx_min (20)? ───┴──► the bar counts as score 0 (no trend, no trade)
                                    │
          LONG  when score crosses +3.0 from below        stop   = close -/+ ATR*1.5
          SHORT when score crosses −3.0 from above        target = risk * 2.0
          (a crossing, not a level: a score that STAYS beyond +3 does not re-fire)
```

### (c) pullback — 15-minute pullback to the fast EMA

A three-phase state machine per symbol, not a formula:

```
  IDLE ──(EMA fast above slow = uptrend, a bar's LOW touches the fast EMA)──► PULLBACK
  PULLBACK ──(a bar closes back ABOVE the fast EMA with a HIGHER low)──► fire LONG, go DONE
           ──(more than max_pullback_bars elapse)──► spent, back to IDLE
  DONE ──(a bar exceeds the swing high)──► IDLE, armed for the next pullback

  stop = one tick under the pullback's lowest low        target = risk * reward_risk
  A trend flip or the first bar of a new session resets the cycle; shorts mirror.
```

### (d) orb — opening-range breakout (one shot a day)

```
  09:15 ─────────── the RANGE ───────────► 09:45              rest of the session
  │ bars before session_open + range_minutes │
  │   high = max high, low = min low         │  first bar whose CLOSE is beyond the range fires:
  │   mean range-bar volume recorded         │
  └──────────────────────────────────────────┘     close > range high ──► LONG, stop = range low
                                                   close < range low  ──► SHORT, stop = range high
  the day trades NOTHING if:                       target = risk * reward_risk (or none: ride to
    - fewer bars than expected arrived (data hole)           the square-off)
    - width < min_range_pct (costs eat it)
    - width > max_range_pct (the position becomes  priority = this bar's volume / mean range volume
      scrap-sized)                                 └─► when several names break out on one bar, the
  One signal per symbol per day, traded or not.         most-participated one gets the scarce slot
```

---

## 5. The offline daily path — a separate circuit

`scripts/daily_screen.py` does **not** use the engine, the broker, the risk layer or the AI. It reads
`interval = 1440` candles straight out of SQLite and measures trade-level expectancy against a
random-entry baseline of the same holding length.

```
  candles (interval 1440) ──► SMA(200), SMA(50), RSI(2), 20-day channel
        │                            │
        │  gap > 20% overnight       │  three classic long-only systems:
        │  = unadjusted corporate    │    mean reversion · trend following · breakout
        │  action -> mask 200 days   │
        │                            ▼
        └──────────────────► signal on day t's CLOSE -> enter at day t+1's OPEN
                             exit condition on day u -> exit at day u+1's OPEN
                                        │
                                        ▼
                             expectancy vs a same-holding-period baseline,
                             net of the DELIVERY cost schedule (STT both sides + DP charge)
                             -> docs/superpowers/notes/*-daily-screen-results.md
```

`scripts/orb_experiment.py`, `scripts/sweep_confluence.py` and `scripts/signal_forward_returns.py`
sit in the same place: they drive the real engine or read the real tables, but they never place
anything.

---

## 6. The whole thing in one frame

```
   universe.yaml + instrument.csv          Groww API
              │                                │
              └────────────► symbols ──────────┤
                                               ▼
                                    ┌──────────────────────┐
                                    │  candles in SQLite   │◄── paper mode also writes back
                                    └──────────┬───────────┘
                                               │  one timestamp at a time
                                               ▼
                    ┌──────────────────────────────────────────────┐
     NIFTY ────────►│ regime │  indicators  │  strategies          │
   (index only)     └────┬───────────┬──────────────┬──────────────┘
                         │           │              │
                         │           │              ▼  Signal
                         │           │        ┌───────────┐
                         └──────────►│        │   RISK    │  size it, or reject it
                                     │        └─────┬─────┘
                                     └────────────► ▼  Candidate (indicators + candles)
                                              ┌───────────┐
                                              │ AI FILTER │  approve / reject + reason
                                              └─────┬─────┘
                                                    ▼
                                              ┌───────────┐
                                              │  BROKER   │  next bar's open, stop/target, charges
                                              └─────┬─────┘
                                                    ▼
                              positions · orders · fills · daily_pnl
                                                    │
                                                    ▼
                                    tradebot report  (report/summary.py)
                                    gross · charges · net · R on risk · expectancy
```

One sentence: **candles are the only input; indicators turn them into numbers, strategies turn those
numbers into a direction with a stop and a target, the regime and risk layers decide whether it is
allowed and how big, the AI filter gets the last word, and the broker turns whatever survives into a
fill at the next bar's open — with every rejection written down along the way.**
