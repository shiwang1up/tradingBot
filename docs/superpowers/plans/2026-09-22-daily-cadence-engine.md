# Daily-cadence engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the backtest engine run on daily bars and hold a CNC position across days, so a swing signal can be traded rather than only measured.

**Architecture:** Four narrow changes to existing modules — a delivery charge schedule in `execution/charges.py`, a daily mode in `engine/clock.py`, a square-off skip in `engine/loop.py`, and a close-based fill in `execution/backtest.py` — each gated so that intraday behaviour is bit-for-bit unchanged. Then one strategy class so there is something to run.

**Tech Stack:** Python 3.9 (`src/` uses `from __future__ import annotations` already and that stays), pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-daily-cadence-engine-design.md`

**Baseline:** `.venv/bin/pytest -q` from the repo root gives `682 passed`.

(Not 720. This branch is off `main`; the swing screen's 38 tests live on
`dev-swing-screen` and are not here. An earlier draft of this plan said 720 and the
spec still says it in prose — the spec's point stands, the number is from the other
branch.)

**Branch:** `dev-daily-engine`, to be created from `main`.

---

## The constraint that governs every task

**No intraday behaviour may change.** 682 tests pass on this branch and `tests/fixtures/golden_trades.json` pins backtest output bar by bar. Every change here is gated on daily mode or on an argument that defaults to today's behaviour.

**After every single task, run the full suite.** If any pre-existing test fails, or the golden fixture moves, STOP and report — that is a regression, not something to update the fixture for. The fixture is the control; changing it to match new output would destroy the only guard this plan has.

## Standing constraints

- **Never write to anything under `data/`.** Read it only via `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`.
- **Do not run** `tradebot paper`, `tradebot fetch-data`, `scripts/orb_experiment.py`, `scripts/daily_screen.py`, `scripts/momentum_screen.py`, or `scripts/swing_screen.py`. `tradebot backtest` IS allowed here and Task 5 uses it.
- **The swing screen's holdout, 2024-01-01 onward, is RESERVED.** Task 5's integration backtest runs in 2022 only.
- Python 3.9. `src/` modules already carry `from __future__ import annotations`; keep it where it is and do not add it to new files under `scripts/`.
- `.venv/bin/pytest`, `.venv/bin/python`.
- **Stage explicit paths by name. Never `git add -u` or `git add -A`.**
- Every commit ends with, on its own last line:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Report the REAL test count. The plan's arithmetic has been wrong before; that is the planner's error, not yours to hide.

## File structure

| File | Change |
|---|---|
| `src/tradebot/config.py` | Three delivery fields on `ChargesConfig`. |
| `src/tradebot/execution/charges.py` | `product` argument selecting the schedule. |
| `src/tradebot/engine/clock.py` | Daily mode at interval >= 1440. |
| `src/tradebot/engine/loop.py` | Skip the per-bar square-off in daily mode. |
| `src/tradebot/execution/backtest.py` | Fill at close when told to. |
| `src/tradebot/strategy/trend_dip.py` | New: the setup that cleared the screen. |
| `config-daily.yaml` | New: a daily CNC config. |

Test counts: Task 1 +6, Task 2 +7, Task 3 +3, Task 4 +4, Task 5 +5. From 682: 688, 695, 698, 702, 707.

---

### Task 1: a delivery charge schedule

**Files:**
- Modify: `src/tradebot/config.py`, `src/tradebot/execution/charges.py`
- Test: `tests/test_charges.py`

`charges.py` is intraday-only today — its docstring says so and it has no mention of CNC. Delivery differs in three ways: STT is 0.1% on BOTH sides rather than 0.025% on the sell, stamp duty is 0.015% rather than 0.003%, and a DP charge of Rs 15.34 applies per sell. On a 12,500 position that is about 0.31% a round trip. Getting it wrong would understate a 60-day-hold strategy by roughly 0.11%/mo against a measured edge of 0.216%/mo.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_charges.py`:

```python
def test_delivery_charges_more_than_intraday_on_the_same_trade():
    """The point of the whole task: CNC pays STT on both sides plus a DP charge, so a delivery
    round trip must cost materially more than the same trade intraday. If these ever come out
    equal, the product argument is not reaching the schedule."""
    mis = round_trip_charges(10_000.0, 10_200.0, CFG, product="MIS")
    cnc = round_trip_charges(10_000.0, 10_200.0, CFG, product="CNC")
    assert cnc > mis
    assert cnc - mis > 25.0          # STT alone is ~0.175% of 20,200 = ~35


def test_the_default_product_is_intraday_and_unchanged():
    """Every existing caller omits the argument and must get exactly what it got before."""
    assert round_trip_charges(10_000.0, 10_200.0, CFG) == round_trip_charges(
        10_000.0, 10_200.0, CFG, product="MIS")
    assert round_trip_charges(10_000.0, 10_200.0, CFG) == pytest.approx(27.42)


def test_delivery_stt_is_charged_on_both_sides():
    """Intraday STT is sell-side only. A delivery buy is taxed too, so raising the BUY value
    must raise the charge under CNC and not under MIS."""
    base_mis = round_trip_charges(10_000.0, 10_000.0, CFG, product="MIS")
    more_mis = round_trip_charges(20_000.0, 10_000.0, CFG, product="MIS")
    base_cnc = round_trip_charges(10_000.0, 10_000.0, CFG, product="CNC")
    more_cnc = round_trip_charges(20_000.0, 10_000.0, CFG, product="CNC")
    # both rise (brokerage, stamp, exchange, sebi), but CNC rises by more, by the extra STT
    assert (more_cnc - base_cnc) - (more_mis - base_mis) == pytest.approx(
        10_000.0 * CFG.delivery_stt_pct / 100.0, rel=1e-6)


def test_the_dp_charge_is_flat_and_applied_once():
    """A fixed rupee fee per sell, so it does not scale with value and appears exactly once in
    a round trip."""
    small = round_trip_charges(1_000.0, 1_000.0, CFG, product="CNC")
    small_mis = round_trip_charges(1_000.0, 1_000.0, CFG, product="MIS")
    stt_delta = 2_000.0 * CFG.delivery_stt_pct / 100.0 - 1_000.0 * CFG.stt_sell_pct / 100.0
    stamp_delta = 1_000.0 * (CFG.delivery_stamp_buy_pct - CFG.stamp_buy_pct) / 100.0
    assert small - small_mis == pytest.approx(stt_delta + stamp_delta + CFG.dp_charge, abs=0.02)


def test_a_disabled_schedule_is_free_for_delivery_too():
    disabled = replace(CFG, enabled=False)
    assert round_trip_charges(10_000.0, 10_200.0, disabled, product="CNC") == 0.0


def test_an_unknown_product_raises():
    """Silently falling back to the cheaper schedule is the failure mode that matters."""
    with pytest.raises(ValueError) as e:
        round_trip_charges(10_000.0, 10_200.0, CFG, product="NRML")
    assert "NRML" in str(e.value)
```

Add `from dataclasses import replace` to that file's imports if it is not already there.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_charges.py -q`
Expected: `TypeError: round_trip_charges() got an unexpected keyword argument 'product'`

- [ ] **Step 3: Write the implementation**

In `src/tradebot/config.py`, add three fields to `ChargesConfig` immediately after `stamp_buy_pct`:

```python
    # Delivery (CNC) differs from intraday in exactly three ways. Rates duplicated in
    # scripts/daily_screen.py as module constants; that script's committed results depend on
    # those exact numbers, so the two are deliberately not unified. Keep them in step.
    delivery_stt_pct: float = 0.1       # both sides, against 0.025% sell-side intraday
    delivery_stamp_buy_pct: float = 0.015   # buy side, against 0.003% intraday
    dp_charge: float = 15.34            # depository, flat, per sell
```

In `src/tradebot/execution/charges.py`, change the two signatures and the computation:

```python
def round_trip_charges(buy_value: float, sell_value: float, cfg: Optional[ChargesConfig],
                       product: str = "MIS") -> float:
```

Update its docstring's first line to say the charges are for one closed trade, intraday or
delivery, and add a paragraph: delivery pays STT on both sides rather than the sell alone, a
higher stamp duty, and a flat DP charge per sell; `product` defaults to MIS so every existing
caller is unchanged.

Validate the product BEFORE the disabled check, so a typo is caught even with charges off:

```python
    if product not in ("MIS", "CNC"):
        raise ValueError(f"product: expected MIS or CNC, got {product!r}")
    if cfg is None or not cfg.enabled:
        return 0.0
```

and replace the stt/stamp lines:

```python
    if product == "CNC":
        stt = turnover * cfg.delivery_stt_pct / 100.0
        stamp = buy_value * cfg.delivery_stamp_buy_pct / 100.0
        dp = cfg.dp_charge
    else:
        stt = sell_value * cfg.stt_sell_pct / 100.0
        stamp = buy_value * cfg.stamp_buy_pct / 100.0
        dp = 0.0
    txn = turnover * cfg.exchange_txn_pct / 100.0
    sebi = turnover * cfg.sebi_pct / 100.0
    gst = (brok + txn + sebi) * cfg.gst_pct / 100.0
    return round(brok + stt + txn + sebi + stamp + gst + dp, 2)
```

Give `position_charges` the same `product: str = "MIS"` parameter and pass it through.

- [ ] **Step 4: Run**

`.venv/bin/pytest tests/test_charges.py -q`, then `.venv/bin/pytest -q` → `688 passed`.

**If any pre-existing charge test fails, STOP.** The default path must be untouched.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/config.py src/tradebot/execution/charges.py tests/test_charges.py
git commit -m "charges: a delivery schedule, selected by product"
```

---

### Task 2: a daily mode in the clock

**Files:**
- Modify: `src/tradebot/engine/clock.py`
- Test: `tests/test_clock.py`

Daily bars are stamped 00:00 IST, which is BEFORE `session.open` at 09:15. Every window check in this class has the form `open_ts(d) <= ts < something`, so on a daily bar all of them are false. Relaxing only the constructor's validation would give an engine that runs, consumes every bar, and silently never trades.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_clock.py`:

```python
DAILY_TS = 1577817000       # 2020-01-01 00:00 IST, how daily candles are actually stamped


def test_a_daily_interval_constructs():
    """1440 raised before: one bar does not fit between 09:15 and 15:10."""
    c = SessionClock(SESSION, 1440)
    assert c.daily is True


def test_intraday_intervals_are_not_daily():
    assert SessionClock(SESSION, 5).daily is False
    assert SessionClock(SESSION, 15).daily is False


def test_a_daily_bar_is_in_session_despite_being_stamped_before_the_open():
    """The bar is stamped 00:00 IST and the session opens at 09:15. Reusing the intraday window
    check would reject every daily bar, and the engine would consume them all and trade nothing."""
    c = SessionClock(SESSION, 1440)
    assert c.in_session(DAILY_TS)


def test_entries_are_allowed_on_a_daily_bar():
    """Same trap as in_session: the intraday cutoff check would forbid every entry."""
    c = SessionClock(SESSION, 1440)
    assert c.entries_allowed(DAILY_TS)


def test_a_daily_bar_is_never_a_square_off_bar():
    c = SessionClock(SESSION, 1440)
    assert not c.is_square_off_bar(DAILY_TS)
    assert not c.square_off_due(DAILY_TS)


def test_a_daily_bar_on_a_weekend_is_not_in_session():
    """Trading-day filtering still applies; only the intra-day window is dropped."""
    c = SessionClock(SESSION, 1440)
    saturday = DAILY_TS + 4 * 86400     # 2020-01-01 was a Wednesday
    assert not c.in_session(saturday)
    assert not c.entries_allowed(saturday)


def test_sixty_and_two_forty_minute_intervals_still_raise():
    """These are genuine misconfigurations for an intraday session and must keep failing.
    Widening the daily gate to cover them would silently change what an intraday run does."""
    for bad in (60, 240):
        with pytest.raises(ValueError):
            SessionClock(SESSION, bad)
```

If `tests/test_clock.py` has no `SESSION` fixture at module level, build one from the same config the existing tests use and name it `SESSION`.

- [ ] **Step 2: Run to verify they fail** — `ValueError: interval 1440m does not fit ...`

- [ ] **Step 3: Write the implementation**

In `SessionClock.__init__`, set the flag and skip the intraday-only validation:

```python
        self.session = session
        self.interval_sec = interval_minutes * 60
        self._holidays = {date.fromisoformat(h) for h in session.holidays}
        o, cut, sq, c = (_minute_of_day(x) for x in
                         (session.open, session.no_new_entries_after, session.square_off, session.close))
        if not (o < cut <= sq <= c):
            raise ValueError("session times must satisfy open < no_new_entries_after <= square_off <= close")
        self.daily = interval_minutes >= 1440
        if self.daily:
            # One bar a day, stamped 00:00 IST. The intra-session window and the square-off do
            # not apply: a CNC position is meant to survive the close, which is the entire point.
            self._square_off_offset_sec = 0
            return
        n_bars = (sq - o) // interval_minutes
        ...
```

leaving the rest of the intraday validation exactly as it is.

Then gate the four window methods. Each keeps its intraday body unchanged:

```python
    def in_session(self, ts: int) -> bool:
        d = date_of(ts)
        if self.daily:
            return self.is_trading_day(d)
        return self.open_ts(d) <= ts < self.close_ts(d)

    def is_square_off_bar(self, ts: int) -> bool:
        if self.daily:
            return False
        return ts == self.square_off_bar_ts(date_of(ts))

    def square_off_due(self, ts: int) -> bool:
        if self.daily:
            return False
        return ts >= self.square_off_bar_ts(date_of(ts))

    def entries_allowed(self, ts: int) -> bool:
        d = date_of(ts)
        if self.daily:
            return self.is_trading_day(d)
        cutoff = ist_epoch(d, self.session.no_new_entries_after)
        return self.open_ts(d) <= ts < cutoff
```

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → `695 passed`. **Any pre-existing clock or engine test failing is a STOP.**

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/clock.py tests/test_clock.py
git commit -m "clock: a daily mode, where the session window does not apply"
```

---

### Task 3: the loop must not square off in daily mode

**Files:**
- Modify: `src/tradebot/engine/loop.py`
- Test: `tests/test_engine.py`

`square_off_due` now returns False in daily mode, so `loop.py:246` already stops firing. This task makes that explicit and proves it, rather than leaving it as an emergent consequence that a later refactor could undo.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine.py`:

```python
def test_a_cnc_position_survives_a_daily_bar_boundary():
    """The whole point of daily mode. A position opened on one daily bar must still be open
    several bars later, with no square-off having run."""
    # build a daily-interval engine over 5 consecutive daily bars, open a CNC position on bar 1
    # and assert it is still open after bar 5. Use the same harness the other engine tests use.
    raise NotImplementedError("fill in using this module's existing engine fixture")


def test_an_intraday_run_still_squares_off():
    """The control. If this ever fails, daily mode has leaked into intraday behaviour."""
    raise NotImplementedError("fill in using this module's existing engine fixture")


def test_end_of_run_still_flattens_an_open_daily_position():
    """An open position at the last bar is closed and recorded, so a run never reports an
    unrealised position as though it were cash."""
    raise NotImplementedError("fill in using this module's existing engine fixture")
```

**Read `tests/test_engine.py` first and build these three on whatever fixture it already uses** — do not invent a new harness. The three `NotImplementedError` bodies above are placeholders for exactly that reason: I do not know that file's fixtures and guessing them would waste your time. If its harness cannot express a daily interval, say so and report NEEDS_CONTEXT rather than building a parallel one.

- [ ] **Step 2: Run to verify they fail** — `NotImplementedError`, then real failures once written.

- [ ] **Step 3: Write the implementation**

At `loop.py:246`, make the daily skip explicit rather than implicit:

```python
        if not self.clock.daily and not self._day.squared_off and self.clock.square_off_due(ts):
```

Add a comment: in daily mode there is no intraday square-off, because a CNC position is meant to
survive the close; end-of-run flattening still applies and is handled elsewhere.

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → `698 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/engine/loop.py tests/test_engine.py
git commit -m "loop: no intraday square-off on a daily bar"
```

---

### Task 4: fill at the close in daily mode

**Files:**
- Modify: `src/tradebot/execution/backtest.py`
- Test: `tests/test_backtest_broker.py`

`BacktestBroker.on_bar` fills every pending entry at `c.open`. For daily bars that is wrong, and not as a matter of taste: Groww's daily `open` is synthetic for 2025 — 98.6% of bars carry the previous close forward — so a daily backtest filling at the open would be fictional over a large part of the window. `swing_screen.py` and `momentum_screen.py` both use closes throughout, and a portfolio backtest that disagreed with the screen motivating it would not be comparable to it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backtest_broker.py`:

```python
def test_a_daily_broker_fills_at_the_close_not_the_open():
    """Groww's daily open is synthetic for 2025, so filling there would be fiction. Build a bar
    whose open and close differ clearly and assert which one the fill used."""
    raise NotImplementedError("fill in using this module's existing broker fixture")


def test_the_default_broker_still_fills_at_the_open():
    """The control. Intraday fills must not move."""
    raise NotImplementedError("fill in using this module's existing broker fixture")


def test_the_entry_buffer_is_measured_against_the_fill_price():
    """_beyond_buffer rejects a signal whose price has run away. In daily mode the fill is the
    close, so the buffer must be judged against the close too -- judging it against the open
    while filling at the close would accept entries the buffer exists to reject."""
    raise NotImplementedError("fill in using this module's existing broker fixture")


def test_a_daily_stop_still_exits_at_the_stop_level():
    """Only the ENTRY convention changes. Exits on a stop or target are unaffected."""
    raise NotImplementedError("fill in using this module's existing broker fixture")
```

**Read `tests/test_backtest_broker.py` and build these on its existing fixtures.** As in Task 3, the placeholders are deliberate.

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Write the implementation**

Give `BacktestBroker.__init__` a `fill_on_close: bool = False` parameter, stored as `self._fill_on_close`. In `on_bar`, choose the reference price once and use it for BOTH the buffer check and the fill:

```python
            ref = c.close if self._fill_on_close else c.open
            if self._beyond_buffer(sig, ref):
                events.append(Unfilled(order, ts, "beyond_buffer"))
                continue
            price = self._entry_price(sig.direction, ref)
```

The buffer and the fill must use the same price. Judging the buffer against the open while filling
at the close would let through exactly the runaway entries the buffer exists to reject.

Then in `cli.py`, pass `fill_on_close=(cfg.execution.interval_minutes >= 1440)` where the
backtest broker is constructed (both construction sites, around lines 262 and 397).

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` → `702 passed`. **The golden fixture must be byte-identical.** Confirm with `git diff --stat tests/fixtures/golden_trades.json` showing no change.

- [ ] **Step 5: Commit**

```bash
git add src/tradebot/execution/backtest.py src/tradebot/cli.py tests/test_backtest_broker.py
git commit -m "backtest: fill at the close when the bar is daily"
```

---

### Task 5: a trend_dip strategy, and a backtest that holds for days

**Files:**
- Create: `src/tradebot/strategy/trend_dip.py`, `config-daily.yaml`
- Test: `tests/test_trend_dip.py`

The setup that cleared the swing screen: SMA(50) > SMA(200), close < SMA(20), close > SMA(50). Long only, CNC. Exit after a fixed 60 trading days, which is the horizon the screen measured and the one whose cost hurdle the edge was compared against.

**The parameters are 20/50/200 and 60 days, and they are not to be tuned here.** The screen's parameter grid showed neighbours scoring higher; choosing one of those after the fact is curve-fitting, and the whole measurement rests on them being fixed in advance.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trend_dip.py`. Model it on `tests/test_pullback.py`, which tests a strategy of the same shape. Cover:

- fires when SMA50 > SMA200, close < SMA20 and close > SMA50
- does not fire when close is below SMA50 (a breakdown, not a dip)
- does not fire when SMA50 < SMA200 (no uptrend)
- returns no signal before 200 bars of warm-up
- the signal's product is CNC and its direction LONG
- `PRODUCTS` contains CNC

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Write the implementation**

Create `src/tradebot/strategy/trend_dip.py` following the structure of
`src/tradebot/strategy/pullback.py`: the same `Strategy` base, `PRODUCTS`, `on_candle`,
`is_ready`, `snapshot`, `reset` and `recompute`. Module docstring should record that this is the
only setup of six that cleared the swing screen's pre-registered bar (+0.618%/date at h=60,
t 2.96, permutation p 0.0005 over 2,000 draws), that its measured edge of 0.216%/mo is BELOW the
0.238%/mo cost hurdle at eight positions, and that the holdout is unspent — so it is a candidate,
not a validated strategy.

Create `config-daily.yaml` from `config.yaml` with `execution.interval_minutes: 1440`, the
`trend_dip` strategy, `product: CNC`, and the session block unchanged.

- [ ] **Step 4: Run the tests** — `.venv/bin/pytest -q` → `707 passed`.

- [ ] **Step 5: Run a real daily backtest, in 2022 only**

```bash
.venv/bin/tradebot --config config-daily.yaml backtest --strategy trend_dip \
  --from 2022-01-01 --to 2022-12-31 --run-id daily-smoke
```

(check `tradebot backtest --help` for the exact flag names and adjust.)

**2022 only. The swing screen's holdout is 2024-01-01 onward and must not be touched.**

Then report, as facts:
- did any position stay open across more than one day, and what was the longest hold?
- what does `tradebot report --run daily-smoke` show for trades, net PnL and charges?
- do the charges look like delivery rather than intraday — roughly 0.68% of a round trip on a
  12,500 position, not roughly 0.37%?

This is a smoke test of the machinery, NOT a result. A positive or negative PnL over one year of
one strategy says nothing, and must not be reported as though it did.

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/strategy/trend_dip.py config-daily.yaml tests/test_trend_dip.py
git commit -m "strategy(trend_dip): the setup that cleared the swing screen, as a CNC strategy"
```

---

## Self-review

**Spec coverage.** §3 clock → Task 2. §4 square-off → Task 3. §5 fills at close → Task 4. §6 delivery charges → Task 1. §7 risk, unchanged → nothing to build, verified by the suite. §9 testing → every task, plus Task 5's integration run.

**Knowingly deferred.** Spec §2 puts paper and live out of scope; nothing here touches
`engine/paper.py` or the `cli.py` paper refusal, which stays. Per-name liquidity slippage stays
out for the reason §6 gives.

**Placeholders.** Tasks 3, 4 and 5 deliberately leave test bodies as `NotImplementedError` with an
instruction to build them on the existing fixtures in those files. This is not a plan failure by
omission: I have not read those three test modules, and inventing fixture names would send the
implementer chasing code that does not exist. Every such placeholder names the file to model on
and says what the test must prove.

**Type consistency.** `product` is the same `str` in `round_trip_charges`, `position_charges` and
`Position.product`. `daily` is a bool attribute on `SessionClock` read by `loop.py`.
`fill_on_close` is a bool on `BacktestBroker` set from `interval_minutes >= 1440`, the same
threshold `SessionClock.daily` uses — if one ever changes, both must.
