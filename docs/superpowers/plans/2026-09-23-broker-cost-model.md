# Broker cost model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cost of a trade a verified, broker-selectable input instead of a typed assumption, and print the hurdle it implies, so that go/no-go decisions stop resting on rates nobody ever checked.

**Architecture:** A new `brokers.yaml` holds named schedules with mandatory provenance. A new `src/tradebot/brokers.py` loads them into the existing `ChargesConfig`. `charges.broker` in config selects one; left unset, every current default applies unchanged. A new `tradebot hurdle` command turns the cost arithmetic — currently done by hand inside spec documents — into a command. `scripts/daily_screen.py` stops keeping its own copy of the rates.

**Tech Stack:** Python 3.9, pytest, click, PyYAML. `src/` modules carry `from __future__ import annotations` already; keep it where it is and do not add it to files under `scripts/`.

**Spec:** `docs/superpowers/specs/2026-09-22-broker-cost-model-design.md`

**Branch:** `dev-daily-engine` (current). Task 1 merges `dev-swing-screen` into it.

---

## The constraint that governs every task

**No intraday behaviour may change, and `tests/fixtures/golden_trades.json` must stay byte-identical.** It is the only guard against the intraday path moving, and it pins backtest output bar by bar. If it moves, STOP and report — that is a regression, not a fixture to update.

Check it after every task:

```bash
git diff --stat main..HEAD -- tests/fixtures/golden_trades.json    # must print nothing
```

## Standing constraints

- **Never write to anything under `data/`.** Read it only via
  `sqlite3.connect("file:data/tradebot.db?mode=ro", uri=True)`.
- **Do not run** `tradebot paper`, `tradebot fetch-data`, `scripts/orb_experiment.py`,
  `scripts/momentum_screen.py`, or `scripts/swing_screen.py holdout`.
  `tradebot backtest`, `tradebot report`, `scripts/daily_screen.py insample` and
  `scripts/swing_screen.py insample` ARE allowed.
- **The holdout, 2024-01-01 onward, is RESERVED AND UNSPENT.** Nothing in this plan touches it.
  Task 6's backtest runs in 2021-2022 only. If you find yourself passing a `--end` later than
  2023-12-31, stop.
- **This system places no orders.** Nothing in this plan moves it closer to doing so.
- Python 3.9. `.venv/bin/pytest`, `.venv/bin/python`, `.venv/bin/tradebot`.
- **Stage explicit paths by name. Never `git add -u` or `git add -A`.**
- Every commit ends with, on its own last line:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Report the REAL test count.** The baseline changes in Task 1 and this plan does not predict it.
  Record what the suite actually prints and state deltas against that.

## Baseline

`.venv/bin/pytest -q` on `dev-daily-engine` before Task 1 gives **731 passed**. Task 1 merges a
branch carrying `tests/test_swing_screen.py` and `tests/test_mfe_mae.py`, which raises it by an
amount this plan deliberately does not guess. Task 1 Step 3 records the real number; every later
task states its delta against that recorded figure.

## File structure

| File | Change |
|---|---|
| `brokers.yaml` | New: named schedules, each with `verified_on` and `source`. |
| `src/tradebot/brokers.py` | New: load and validate schedules, build a `ChargesConfig`. |
| `src/tradebot/config.py` | `ChargesConfig.broker`, `PathsConfig.brokers`, resolution in `load_config`. |
| `src/tradebot/report/hurdle.py` | New: pure cost-fraction and hurdle arithmetic. |
| `src/tradebot/cli.py` | New `hurdle` command. |
| `scripts/daily_screen.py` | Rate constants replaced by a read of the `legacy` schedule. |
| `config-daily.yaml` | Selects `broker: groww`. |
| `tests/test_brokers.py` | New. |
| `tests/test_hurdle.py` | New. |
| `tests/test_config.py` | Selection and defaults. |
| `tests/test_daily_screen.py` | `legacy` reproduces the committed constants. |

---

### Task 1: merge the swing screen branch and record the real baseline

**Files:** none edited; this is a merge.

`scripts/daily_screen.py` is imported by `scripts/swing_screen.py`, which lives on
`dev-swing-screen`. Task 5 changes that function's signature. Unifying the rates without the
consumer present would mean shipping a change whose only caller is on another branch and cannot be
tested. The branches touch disjoint files (docs, `scripts/`, `tests/` — no `src/` overlap) and the
merge was verified conflict-free with `git merge-tree` on 2026-09-22.

- [ ] **Step 1: Confirm the tree is clean and the merge is still clean**

```bash
git status --short                      # must print nothing
git merge-tree --write-tree HEAD dev-swing-screen >/dev/null && echo CLEAN
```

Expected: `CLEAN`. **If it reports a conflict, STOP and report** — do not resolve conflicts as part
of this task.

- [ ] **Step 2: Merge**

```bash
git merge --no-ff dev-swing-screen -m "Merge dev-swing-screen: the setup screen and its MFE/MAE diagnostic

scripts/daily_screen.py is about to become the single definition of a round trip, and
swing_screen.py is its only other caller. Merging first so the change has its consumer
present and testable rather than landing against a branch.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Run the full suite and RECORD the number**

```bash
.venv/bin/pytest -q 2>&1 | tail -2
```

Write the real figure down; every later task's expected count is this number plus that task's
delta. **If any test fails, STOP and report.** A merge that was conflict-free can still break a
test, and that is a finding, not something to fix by editing the test.

- [ ] **Step 4: Confirm the fixture did not move**

```bash
git diff --stat main..HEAD -- tests/fixtures/golden_trades.json    # must print nothing
```

---

### Task 2: `brokers.yaml` and its loader

**Files:**
- Create: `brokers.yaml`, `src/tradebot/brokers.py`
- Test: `tests/test_brokers.py`

Three schedules. `groww` and `zerodha` are the real ones, verified 2026-09-22 against their pricing
pages. `legacy` is the schedule exactly as `ChargesConfig`'s defaults stand today — Groww's
brokerage with Zerodha's DP fee — and exists only so every figure already committed to
`docs/superpowers/notes/` stays reproducible.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_brokers.py`:

```python
import textwrap

import pytest

from tradebot.brokers import BrokerScheduleError, load_brokers
from tradebot.execution.charges import round_trip_charges


def test_the_three_shipped_schedules_load():
    brokers = load_brokers("brokers.yaml")
    assert set(brokers) == {"groww", "zerodha", "legacy"}


def test_groww_round_trip_matches_the_pricing_page():
    """Worked by hand from groww.in/pricing, verified 2026-09-22, on a 12,500 position:
    brokerage min(12.50, 20) x 2 = 25.00; STT 25,000 x 0.1% = 25.00; txn 25,000 x 0.00297% =
    0.7425; SEBI 25,000 x 0.0001% = 0.025; stamp 12,500 x 0.015% = 1.875;
    GST 18% x (25 + 0.7425 + 0.025) = 4.63815; DP 23.60. Total 80.88065."""
    cfg = load_brokers("brokers.yaml")["groww"].charges
    assert round_trip_charges(12_500.0, 12_500.0, cfg, product="CNC") == pytest.approx(80.88)


def test_zerodha_round_trip_matches_the_pricing_page():
    """Same position at zerodha.com/charges, verified 2026-09-22: delivery brokerage is zero, so
    GST falls to 18% x (0.7425 + 0.025) = 0.13815, and the DP fee is 15.34. Total 43.12065."""
    cfg = load_brokers("brokers.yaml")["zerodha"].charges
    assert round_trip_charges(12_500.0, 12_500.0, cfg, product="CNC") == pytest.approx(43.12)


def test_legacy_is_the_schedule_the_committed_notes_were_computed_from():
    """Groww's brokerage with Zerodha's DP fee: 80.88065 - 23.60 + 15.34 = 72.62065. This
    corresponds to no real broker and exists only to reproduce figures already published."""
    cfg = load_brokers("brokers.yaml")["legacy"].charges
    assert round_trip_charges(12_500.0, 12_500.0, cfg, product="CNC") == pytest.approx(72.62)


def test_legacy_matches_the_dataclass_defaults_exactly():
    """If these ever diverge, every note in docs/superpowers/notes/ silently stops reproducing."""
    from tradebot.config import ChargesConfig
    legacy = load_brokers("brokers.yaml")["legacy"].charges
    default = ChargesConfig()
    for field in ("brokerage_pct", "brokerage_max", "brokerage_min", "stt_sell_pct",
                  "exchange_txn_pct", "sebi_pct", "stamp_buy_pct", "delivery_stt_pct",
                  "delivery_stamp_buy_pct", "dp_charge", "gst_pct"):
        assert getattr(legacy, field) == getattr(default, field), field


def test_intraday_charges_are_identical_across_the_three_schedules_except_brokerage():
    """Only brokerage and the DP fee differ by broker, and DP is delivery-only. An intraday round
    trip at groww and at legacy must therefore be the same number."""
    brokers = load_brokers("brokers.yaml")
    groww = round_trip_charges(12_500.0, 12_500.0, brokers["groww"].charges)
    legacy = round_trip_charges(12_500.0, 12_500.0, brokers["legacy"].charges)
    assert groww == pytest.approx(legacy)


def test_provenance_is_mandatory(tmp_path):
    """A rate whose source nobody recorded is the defect this whole file exists to fix."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          brokerage_pct: 0.1
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    assert "verified_on" in str(e.value) and "acme" in str(e.value)


def test_an_unknown_rate_key_is_an_error(tmp_path):
    """A typo must not silently leave a rate at its default."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          verified_on: 2026-09-22
          source: https://example.com
          brokerage_percent: 0.1
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    assert "brokerage_percent" in str(e.value)


def test_a_missing_file_names_the_path(tmp_path):
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(e.value)


def test_verified_on_is_exposed_so_a_stale_rate_can_be_found():
    from datetime import date
    assert load_brokers("brokers.yaml")["groww"].verified_on == date(2026, 9, 22)
    assert "groww.in" in load_brokers("brokers.yaml")["groww"].source
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_brokers.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.brokers'`

- [ ] **Step 3: Create `brokers.yaml`**

```yaml
# Charge schedules, one per broker. Selected by `charges.broker` in a config file.
#
# Every entry MUST carry verified_on and source: the defect this file fixes is that the shipped
# rates were typed from a spec and never checked against a pricing page. See
# docs/superpowers/specs/2026-09-22-broker-cost-model-design.md.
#
# The DP charge is stated GST-INCLUSIVE. charges.py computes GST on brokerage + exchange + SEBI and
# then adds dp_charge untaxed. Adding dp_charge to the GST base would double-tax it.
#
# The statutory rates (STT, stamp duty, SEBI, exchange transaction charge, GST) are set by the
# government and the exchange and are identical for every broker. Only brokerage and the DP fee
# differ. groww.in/pricing gives the NSE transaction charge as 0.00297% and zerodha.com/charges
# gives 0.00307%; it is an exchange-set rate and cannot differ by broker, so one page is stale. The
# gap is 0.0001% of turnover, about 2.5 paise on a 25,000 round trip, and is immaterial to every
# decision this repository makes. Both use 0.00297%.

groww:
  verified_on: 2026-09-22
  source: https://groww.in/pricing
  brokerage_pct: 0.1          # "20 or 0.1% per executed order, whichever is lower, minimum 5"
  brokerage_max: 20.0
  brokerage_min: 5.0
  stt_sell_pct: 0.025         # intraday, sell side
  exchange_txn_pct: 0.00297
  sebi_pct: 0.0001
  stamp_buy_pct: 0.003        # intraday, buy side
  delivery_stt_pct: 0.1       # delivery, both sides
  delivery_stamp_buy_pct: 0.015
  dp_charge: 23.60            # depository 3.5 + Groww 16.5 = 20.00, +18% GST
  gst_pct: 18.0

zerodha:
  verified_on: 2026-09-22
  source: https://zerodha.com/charges
  brokerage_pct: 0.0          # "Zero Brokerage" on equity delivery
  brokerage_max: 0.0
  brokerage_min: 0.0
  stt_sell_pct: 0.025
  exchange_txn_pct: 0.00297
  sebi_pct: 0.0001
  stamp_buy_pct: 0.003
  delivery_stt_pct: 0.1
  delivery_stamp_buy_pct: 0.015
  dp_charge: 15.34            # CDSL 3.5 + Zerodha 9.5 + GST 2.34
  gst_pct: 18.0

legacy:
  verified_on: 2026-09-22     # the date the chimera was IDENTIFIED, not a rate anyone verified
  source: docs/superpowers/specs/2026-09-22-broker-cost-model-design.md
  # NOT A REAL BROKER. Groww's brokerage with Zerodha's DP fee -- the schedule this repository
  # shipped before the rates were checked. It exists so every figure already committed to
  # docs/superpowers/notes/ reproduces exactly. Do not use it for a new decision.
  brokerage_pct: 0.1
  brokerage_max: 20.0
  brokerage_min: 5.0
  stt_sell_pct: 0.025
  exchange_txn_pct: 0.00297
  sebi_pct: 0.0001
  stamp_buy_pct: 0.003
  delivery_stt_pct: 0.1
  delivery_stamp_buy_pct: 0.015
  dp_charge: 15.34
  gst_pct: 18.0
```

- [ ] **Step 4: Create `src/tradebot/brokers.py`**

```python
"""Named charge schedules, loaded from brokers.yaml.

The rates in this repository were typed from a spec and went unverified for the whole life of the
project, while every go/no-go decision rested on them. A schedule here must say where its numbers
came from and when someone looked: `verified_on` and `source` are mandatory, and a schedule without
them fails to load rather than quietly becoming another unsourced assumption.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Union

import yaml

from tradebot.config import ChargesConfig


class BrokerScheduleError(ValueError):
    """A brokers.yaml that is missing, malformed, or missing provenance."""


@dataclass(frozen=True)
class Broker:
    name: str
    verified_on: date
    source: str
    charges: ChargesConfig


_RATE_FIELDS = tuple(f.name for f in dataclasses.fields(ChargesConfig)
                     if f.name not in ("enabled", "broker"))


def load_brokers(path: Union[str, Path] = "brokers.yaml") -> Dict[str, Broker]:
    """{name: Broker} from a brokers.yaml. Raises BrokerScheduleError with the offending name."""
    p = Path(path)
    if not p.exists():
        raise BrokerScheduleError(f"broker schedules not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise BrokerScheduleError(f"{p}: expected a mapping of broker name to schedule")
    out: Dict[str, Broker] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise BrokerScheduleError(f"{p}: schedule '{name}' must be a mapping, got {entry!r}")
        for required in ("verified_on", "source"):
            if not entry.get(required):
                raise BrokerScheduleError(
                    f"{p}: schedule '{name}' is missing '{required}'. Every rate must be traceable "
                    f"to a page someone read on a date; that is the point of this file.")
        rates = {k: v for k, v in entry.items() if k not in ("verified_on", "source")}
        unknown = sorted(set(rates) - set(_RATE_FIELDS))
        if unknown:
            raise BrokerScheduleError(f"{p}: schedule '{name}' has unknown keys: {unknown}")
        verified = entry["verified_on"]
        if not isinstance(verified, date):
            raise BrokerScheduleError(
                f"{p}: schedule '{name}' verified_on must be an unquoted ISO date, got {verified!r}")
        out[name] = Broker(name=name, verified_on=verified, source=str(entry["source"]),
                           charges=ChargesConfig(**{k: float(v) for k, v in rates.items()}))
    return out
```

- [ ] **Step 5: Run**

`.venv/bin/pytest tests/test_brokers.py -q` → all pass.
Then `.venv/bin/pytest -q` → the Task 1 baseline **+10**.

**If any pre-existing test fails, STOP.** Nothing in this task touches an existing code path.

- [ ] **Step 6: Commit**

```bash
git add brokers.yaml src/tradebot/brokers.py tests/test_brokers.py
git commit -m "brokers: charge schedules with mandatory provenance

groww and zerodha, both verified 2026-09-22 against their pricing pages, and
legacy -- Groww's brokerage with Zerodha's DP fee, the chimera this repository
shipped -- kept so every figure in docs/superpowers/notes/ still reproduces.

verified_on and source are mandatory. A schedule without them fails to load,
because an unsourced rate is the defect this file exists to fix.

Nothing selects a schedule yet; ChargesConfig's defaults are untouched.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: select a schedule from config

**Files:**
- Modify: `src/tradebot/config.py`
- Test: `tests/test_config.py`

`charges.broker: groww` replaces the rate fields from `brokers.yaml`. Left unset — which is every
config in the repository as of this task — nothing changes at all.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_an_unset_broker_leaves_every_rate_at_its_default(tmp_path):
    """Every config in the repository omits it today, and must be charged exactly as before."""
    from tradebot.config import ChargesConfig
    cfg = load_config(_write_config(tmp_path))
    assert cfg.charges.broker == ""
    assert cfg.charges.dp_charge == ChargesConfig().dp_charge


def test_selecting_a_broker_replaces_the_rates(tmp_path):
    cfg = load_config(_write_config(tmp_path, charges={"broker": "groww"}))
    assert cfg.charges.dp_charge == pytest.approx(23.60)
    assert cfg.charges.brokerage_max == pytest.approx(20.0)


def test_selecting_zerodha_zeroes_the_brokerage(tmp_path):
    cfg = load_config(_write_config(tmp_path, charges={"broker": "zerodha"}))
    assert cfg.charges.brokerage_pct == 0.0
    assert cfg.charges.dp_charge == pytest.approx(15.34)


def test_enabled_is_kept_from_the_config_not_the_schedule(tmp_path):
    """The schedule holds rates; whether charges apply at all is the run's decision."""
    cfg = load_config(_write_config(tmp_path, charges={"broker": "groww", "enabled": False}))
    assert cfg.charges.enabled is False
    assert cfg.charges.dp_charge == pytest.approx(23.60)


def test_an_unknown_broker_names_the_ones_that_exist(tmp_path):
    with pytest.raises(ValueError) as e:
        load_config(_write_config(tmp_path, charges={"broker": "hdfcsec"}))
    assert "hdfcsec" in str(e.value) and "groww" in str(e.value)


def test_a_broker_and_an_explicit_rate_together_is_an_error(tmp_path):
    """Silently letting one win would make the effective rate unreadable from the config."""
    with pytest.raises(ValueError) as e:
        load_config(_write_config(tmp_path, charges={"broker": "groww", "dp_charge": 1.0}))
    assert "dp_charge" in str(e.value)
```

`tests/test_config.py` has one helper, `_write(tmp_path, text=YAML)` at line 63: it writes the
module-level `YAML` string to `tmp_path` and returns the path. It takes raw YAML text, not
sections, so add `_write_config` beside it rather than reshaping it — the existing tests depend on
`_write` as it is:

```python
def _write_config(tmp_path, charges=None):
    """The module's YAML with a charges section appended, for the broker-selection tests."""
    text = YAML
    if charges is not None:
        body = "\n".join(f"  {k}: {v!r}" if isinstance(v, str) else f"  {k}: {v}"
                          for k, v in charges.items())
        text = f"{YAML}\ncharges:\n{body}\n"
    return _write(tmp_path, text)
```

If `YAML` already contains a `charges:` section, strip it before appending rather than emitting two.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: `TypeError: __init__() got an unexpected keyword argument 'broker'` (or a config error
naming `broker` as an unknown key in section `charges`).

- [ ] **Step 3: Write the implementation**

In `src/tradebot/config.py`, add to `PathsConfig` after `universe`:

```python
    brokers: str = "brokers.yaml"
```

Add to `ChargesConfig`, immediately after `enabled`:

```python
    # Empty means "use the rate fields on this dataclass", which is what every config in this
    # repository did before brokers.yaml existed. A name selects a schedule from paths.brokers and
    # replaces every rate below. See docs/superpowers/specs/2026-09-22-broker-cost-model-design.md.
    broker: str = ""
```

In `load_config`, immediately after the `cfg = Config(...)` assignment and **before**
`_validate(cfg)`:

```python
    if cfg.charges.broker:
        cfg = dataclasses.replace(cfg, charges=_apply_broker(cfg.charges, cfg.paths.brokers, raw))
    _validate(cfg)
```

and add this module-level function just above `load_config`:

```python
def _apply_broker(charges: "ChargesConfig", brokers_path: str, raw: dict) -> "ChargesConfig":
    """Replace every rate on `charges` with the named schedule's, keeping `enabled` and `broker`.

    Imported here rather than at module scope: tradebot.brokers imports ChargesConfig from this
    module, and a top-level import would be circular.
    """
    from tradebot.brokers import BrokerScheduleError, load_brokers
    given = set((raw.get("charges") or {})) - {"broker", "enabled"}
    if given:
        raise ValueError(
            f"config.yaml charges: {sorted(given)} cannot be set alongside 'broker': the schedule "
            f"supplies every rate. Remove them, or drop 'broker' and set them all yourself.")
    try:
        brokers = load_brokers(brokers_path)
    except BrokerScheduleError as e:
        raise ValueError(f"config.yaml charges.broker: {e}") from e
    if charges.broker not in brokers:
        raise ValueError(f"config.yaml charges.broker: unknown broker {charges.broker!r}; "
                         f"brokers.yaml has {sorted(brokers)}")
    return dataclasses.replace(brokers[charges.broker].charges,
                               enabled=charges.enabled, broker=charges.broker)
```

`paths.brokers` is resolved relative to the config file's directory by the existing `PathsConfig`
comprehension in `load_config`; it needs no change.

- [ ] **Step 4: Run**

`.venv/bin/pytest tests/test_config.py -q`, then `.venv/bin/pytest -q` → the Task 1 baseline
**+16**.

**If any pre-existing config or charges test fails, STOP.** With `broker` unset the loader must
behave exactly as it did.

- [ ] **Step 5: Confirm the fixture did not move**

```bash
git diff --stat main..HEAD -- tests/fixtures/golden_trades.json    # must print nothing
```

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/config.py tests/test_config.py
git commit -m "config: charges.broker selects a schedule, unset keeps today's rates

paths.brokers points at brokers.yaml, resolved against the config file's
directory like every other path. Setting a broker alongside an explicit rate is
refused rather than resolved by precedence: with both present the effective rate
is not readable from the config, which is how the shipped chimera survived.

Every config in the repository omits charges.broker and is unaffected.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `tradebot hurdle`

**Files:**
- Create: `src/tradebot/report/hurdle.py`
- Modify: `src/tradebot/cli.py`
- Test: `tests/test_hurdle.py`

The excess-per-month a strategy must beat is the number that decided every go/no-go in this
project, and it exists only as hand-arithmetic inside spec documents. That is how the swing screen's
hurdle table came to be computed against an unverified DP fee.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hurdle.py`:

```python
import pytest

from tradebot.brokers import load_brokers
from tradebot.report.hurdle import hurdle_per_month, round_trip_fraction

GROWW = load_brokers("brokers.yaml")["groww"].charges
ZERODHA = load_brokers("brokers.yaml")["zerodha"].charges


def test_round_trip_fraction_is_charges_plus_slippage_on_both_sides():
    """80.88 of charges on a 12,500 position is 0.647%, plus 0.05% of slippage each side."""
    assert round_trip_fraction(12_500.0, GROWW, slippage_pct=0.05) == pytest.approx(
        0.0064704 + 0.001, rel=1e-3)


def test_a_bigger_position_costs_proportionally_less():
    """Brokerage caps at 20 a leg and the DP fee is flat, so the fraction falls as size rises.
    This is the entire reason concentration changes whether a strategy pays for itself."""
    small = round_trip_fraction(12_500.0, GROWW, slippage_pct=0.05)
    large = round_trip_fraction(50_000.0, GROWW, slippage_pct=0.05)
    assert large < small


def test_zerodha_costs_less_than_groww_at_every_size():
    for v in (12_500.0, 25_000.0, 100_000.0):
        assert (round_trip_fraction(v, ZERODHA, slippage_pct=0.05)
                < round_trip_fraction(v, GROWW, slippage_pct=0.05))


def test_hurdle_per_month_divides_the_round_trip_over_the_hold():
    """A 60-trading-day hold is 60/21 months, so a 0.681% round trip is 0.238%/mo -- the figure
    the swing screen set its bar against."""
    assert hurdle_per_month(0.00681, hold_days=60) == pytest.approx(0.002384, rel=1e-3)


def test_a_longer_hold_lowers_the_hurdle_proportionally():
    assert hurdle_per_month(0.00681, hold_days=120) == pytest.approx(
        hurdle_per_month(0.00681, hold_days=60) / 2, rel=1e-6)


def test_the_groww_eight_slot_hurdle_is_above_the_measured_edge():
    """The finding this whole plan rests on: trend_dip's 0.216%/mo does not clear Groww at eight
    positions on a lakh, and does clear zerodha there."""
    groww = hurdle_per_month(round_trip_fraction(12_500.0, GROWW, 0.05), 60)
    zerodha = hurdle_per_month(round_trip_fraction(12_500.0, ZERODHA, 0.05), 60)
    assert groww > 0.00216 > zerodha


def test_a_zero_or_negative_hold_raises():
    with pytest.raises(ValueError):
        hurdle_per_month(0.00681, hold_days=0)


def test_a_zero_or_negative_position_raises():
    with pytest.raises(ValueError):
        round_trip_fraction(0.0, GROWW, slippage_pct=0.05)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_hurdle.py -q`
Expected: `ModuleNotFoundError: No module named 'tradebot.report.hurdle'`

- [ ] **Step 3: Create `src/tradebot/report/hurdle.py`**

```python
"""What a round trip costs, and the excess return per month it demands.

Cost scales with turnover, so the holding period sets the bar before any signal is considered: a
10-day hold on a lakh across eight positions demands roughly 1.43% a month, about double the best
documented anomalies, and no signal can clear it. That arithmetic decided the horizons of every
screen in this repository, and until now it lived as hand-worked tables inside spec documents --
which is how one of them came to be computed against a DP fee that belonged to a different broker.
"""
from __future__ import annotations

from typing import Optional

from tradebot.config import ChargesConfig
from tradebot.execution.charges import round_trip_charges

TRADING_DAYS_PER_MONTH = 21.0


def round_trip_fraction(position_value: float, charges: Optional[ChargesConfig],
                        slippage_pct: float = 0.05) -> float:
    """Cost of buying and selling `position_value` of one name, as a FRACTION of that value.

    Charges are taken at the delivery schedule: this is a swing-holding figure. Slippage is charged
    on both sides, against the trade, as a human percent per side.
    """
    if position_value <= 0:
        raise ValueError(f"position_value: expected a value greater than 0, got {position_value!r}")
    if slippage_pct < 0:
        raise ValueError(f"slippage_pct: expected a non-negative percent, got {slippage_pct!r}")
    charged = round_trip_charges(position_value, position_value, charges, product="CNC")
    return charged / position_value + 2 * slippage_pct / 100.0


def hurdle_per_month(fraction: float, hold_days: int) -> float:
    """The excess return per month a strategy must beat, as a fraction, for a hold of `hold_days`
    trading days. One round trip's cost spread over the months it is held."""
    if hold_days <= 0:
        raise ValueError(f"hold_days: expected a positive number of trading days, got {hold_days!r}")
    return fraction / (hold_days / TRADING_DAYS_PER_MONTH)
```

- [ ] **Step 4: Add the command to `src/tradebot/cli.py`**

Add these imports beside the existing `tradebot.report` imports:

```python
from tradebot.brokers import load_brokers
from tradebot.report.hurdle import hurdle_per_month, round_trip_fraction
```

and add this command after the `report` command:

```python
@main.command()
@click.option("--capital", type=float, default=None, help="Defaults to the config's capital.")
@click.option("--slots", default="2,3,4,5,8", help="Comma-separated concurrent position counts.")
@click.option("--hold", type=int, default=60, show_default=True, help="Hold in trading days.")
@click.option("--slippage", type=float, default=0.05, show_default=True, help="Percent per side.")
@click.option("--broker", "broker_names", default=None,
              help="Comma-separated schedules from brokers.yaml. Defaults to all of them.")
@click.pass_obj
def hurdle(cfg: Config, capital: Optional[float], slots: str, hold: int, slippage: float,
           broker_names: Optional[str]) -> None:
    """What a round trip costs, and the excess per month a strategy must beat to pay for it."""
    capital = capital if capital is not None else cfg.capital
    if capital <= 0:
        raise click.ClickException("--capital must be greater than 0")
    try:
        counts = [int(s) for s in slots.split(",") if s.strip()]
    except ValueError:
        raise click.ClickException(f"--slots: expected comma-separated integers, got {slots!r}")
    if not counts or any(c <= 0 for c in counts):
        raise click.ClickException("--slots must be positive integers")
    brokers = load_brokers(cfg.paths.brokers)
    names = [n.strip() for n in broker_names.split(",")] if broker_names else sorted(brokers)
    unknown = [n for n in names if n not in brokers]
    if unknown:
        raise click.ClickException(f"unknown broker(s) {unknown}; brokers.yaml has {sorted(brokers)}")
    click.echo(f"{capital:,.0f} fully deployed, {hold}-day hold, {slippage}% slippage per side")
    for n in names:
        b = brokers[n]
        click.echo(f"  {n} (verified {b.verified_on}, {b.source})")
    click.echo()
    click.echo(f"{'slots':>6} {'position':>10} " + " ".join(f"{n:>12}" for n in names))
    for c in counts:
        v = capital / c
        cells = []
        for n in names:
            frac = round_trip_fraction(v, brokers[n].charges, slippage)
            cells.append(f"{100 * hurdle_per_month(frac, hold):>11.3f}%")
        click.echo(f"{c:>6} {v:>10,.0f} " + " ".join(cells))
    click.echo()
    click.echo("Excess per month a strategy must beat to pay for itself. For reference, the only "
               "setup in this repository to clear a pre-registered statistical bar (trend_dip, "
               "h=60) measured 0.216%/mo in-sample, and its holdout is unspent.")
```

- [ ] **Step 5: Run**

`.venv/bin/pytest tests/test_hurdle.py -q` → all pass.
`.venv/bin/pytest -q` → the Task 1 baseline **+24**.

Then run the command itself and read it:

```bash
.venv/bin/tradebot --config config-daily.yaml hurdle --capital 100000 --hold 60
```

Expected: `groww` at 8 slots prints `0.261%` and `zerodha` at 8 slots prints `0.156%`. If either
differs, the schedules or the arithmetic are wrong — STOP and report rather than adjusting the
expected numbers.

- [ ] **Step 6: Commit**

```bash
git add src/tradebot/report/hurdle.py src/tradebot/cli.py tests/test_hurdle.py
git commit -m "hurdle: the number every go/no-go rested on becomes a command

Cost scales with turnover, so the holding period sets the bar before any signal
is considered. That arithmetic chose the horizons of every screen here and lived
only as hand-worked tables inside spec documents -- which is how the swing
screen's hurdle came to be computed against another broker's DP fee.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: one definition of a round trip

**Files:**
- Modify: `scripts/daily_screen.py:45-73`
- Test: `tests/test_daily_screen.py`

`scripts/daily_screen.py` restates the rates as module constants and `scripts/swing_screen.py`
imports `round_trip_cost` from it. The screens and the engine currently agree only by coincidence of
typing. Pinning to `legacy` keeps every committed screen figure reproducing to the digit.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daily_screen.py`:

```python
def test_round_trip_cost_still_matches_the_constants_the_notes_were_computed_from():
    """The committed screen results in docs/superpowers/notes/ were computed at these exact
    numbers. 0.572% on a 25,000 position is quoted verbatim in the 2026-09-20 daily screen note."""
    assert ds.round_trip_cost(25_000.0) == pytest.approx(0.00572, abs=5e-6)


def test_the_default_position_value_is_unchanged():
    assert ds.round_trip_cost() == pytest.approx(ds.round_trip_cost(25_000.0))


def test_a_named_broker_can_be_asked_for_and_groww_costs_more_than_legacy():
    """legacy carries Zerodha's DP fee; Groww's is 8.26 dearer, so the real Groww cost is higher."""
    assert ds.round_trip_cost(25_000.0, broker="groww") > ds.round_trip_cost(25_000.0)


def test_the_screen_and_the_engine_cannot_disagree():
    """The whole point of the task: one definition. The screen's fraction times the position value
    must equal what the engine would charge for the same round trip, slippage aside."""
    from tradebot.brokers import load_brokers
    from tradebot.execution.charges import round_trip_charges
    v = 25_000.0
    engine = round_trip_charges(v, v, load_brokers("brokers.yaml")["legacy"].charges, product="CNC")
    screen = (ds.round_trip_cost(v) - 2 * ds.SLIPPAGE_PCT / 100.0) * v
    assert screen == pytest.approx(engine, abs=0.01)
```

`tests/test_daily_screen.py` already loads the script by path with `importlib` and binds it as
`ds` (see its module header, amendment D7) — the tests above use that name. Do not add a second
import mechanism beside it.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_daily_screen.py -q`
Expected: `TypeError: round_trip_cost() got an unexpected keyword argument 'broker'` on the third
test; the first two pass already and are there to prove the refactor does not move them.

- [ ] **Step 3: Write the implementation**

In `scripts/daily_screen.py`, delete the rate constants on lines 48-54 (`BROKERAGE_PCT`,
`BROKERAGE_MIN`, `BROKERAGE_MAX`, `STT_PCT`, `EXCHANGE_PCT`, `SEBI_PCT`, `STAMP_BUY_PCT`,
`GST_PCT`, `DP_CHARGE`). **Keep `POSITION_VALUE` and `SLIPPAGE_PCT`** — they are the screen's own
assumptions, not broker rates. Replace the comment block on lines 45-46 with:

```python
# Rates come from brokers.yaml, which records where each was verified and when. The screens pin to
# `legacy` -- Groww's brokerage with Zerodha's DP fee, the schedule this repository shipped before
# anyone checked -- so every figure already committed to docs/superpowers/notes/ reproduces to the
# digit. It corresponds to no real broker. Pass broker="groww" for what a trade actually costs.
POSITION_VALUE = 25_000.0
SLIPPAGE_PCT = 0.05                     # each side, against the trade
SCREEN_BROKER = "legacy"
```

Replace `round_trip_cost` entirely:

```python
def round_trip_cost(position_value=POSITION_VALUE, broker=SCREEN_BROKER):
    """Round-trip cost as a FRACTION of position value: charges plus slippage on both opens."""
    from tradebot.report.hurdle import round_trip_fraction
    from tradebot.brokers import load_brokers
    root = Path(__file__).resolve().parent.parent
    schedule = load_brokers(root / "brokers.yaml")[broker].charges
    return round_trip_fraction(position_value, schedule, SLIPPAGE_PCT)
```

Add `from pathlib import Path` to the imports if it is not already there.

- [ ] **Step 4: Run**

`.venv/bin/pytest tests/test_daily_screen.py -q`, then `.venv/bin/pytest -q` → the Task 1 baseline
**+28**.

Then prove the screens still reproduce:

```bash
.venv/bin/python scripts/daily_screen.py insample
.venv/bin/python scripts/swing_screen.py insample
```

The daily screen must print `mr` -0.203%/date, `tf` -1.348%, `bo` -2.532%, matching
`docs/superpowers/notes/2026-09-20-expectancy-risk-daily-screen-results.md`. The swing screen must
print `trend_dip h=60 +0.618%/date t 2.96` and a hurdle line reading
`20d 0.715%/mo, 60d 0.238%/mo, 120d 0.119%/mo`.

**If any figure moves, STOP and report.** `legacy` exists precisely so they do not.

- [ ] **Step 5: Commit**

```bash
git add scripts/daily_screen.py tests/test_daily_screen.py
git commit -m "screens: one definition of a round trip, pinned to legacy

daily_screen.py kept its own copy of the rates and swing_screen.py imported the
result, so a screen and the engine agreed only by coincidence of typing. Both
now read brokers.yaml. Pinned to legacy, so every figure in the committed notes
reproduces to the digit; daily_screen insample and swing_screen insample were
re-run and match.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: charge the daily config what Groww actually charges

**Files:**
- Modify: `config-daily.yaml`
- Test: `tests/test_config.py`

This is the behaviour change. Every CNC figure this repository has produced was charged Zerodha's
DP fee. It lands alone, and its commit message restates the numbers it moves.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_the_daily_config_is_charged_a_real_broker():
    """config-daily.yaml is the only CNC config, so it is the only one the DP fee reaches. Charged
    at the shipped default it paid Zerodha's 15.34 while assuming Groww's brokerage."""
    cfg = load_config("config-daily.yaml")
    assert cfg.charges.broker == "groww"
    assert cfg.charges.dp_charge == pytest.approx(23.60)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py::test_the_daily_config_is_charged_a_real_broker -q`
Expected: `AssertionError` — `cfg.charges.broker` is `''`.

- [ ] **Step 3: Write the implementation**

In `config-daily.yaml`, find the `charges:` section. Replace its entire body with:

```yaml
charges:
  enabled: true
  # Rates come from brokers.yaml, verified 2026-09-22 against groww.in/pricing. Setting any rate
  # here alongside `broker` is refused: with both present the effective rate is not readable from
  # this file, which is how the shipped schedule came to pair Groww's brokerage with Zerodha's DP
  # fee for the whole life of the project.
  broker: groww
```

- [ ] **Step 4: Run**

`.venv/bin/pytest -q` → the Task 1 baseline **+29**.

- [ ] **Step 5: Measure what the correction moved**

Re-run the daily smoke backtest and compare it against `daily-smoke-guard`, which was run at the
old DP fee:

```bash
.venv/bin/tradebot --config config-daily.yaml backtest --strategy trend_dip \
  --start 2021-01-01 --end 2022-12-31 --run-id daily-dp-corrected
.venv/bin/tradebot --config config-daily.yaml report --run daily-dp-corrected 2>&1 | head -20
```

**2022 at the latest. The holdout is 2024-01-01 onward and must not be touched.**

Report, as facts: the charges total before and after, the number of closed trades, and the
per-round-trip difference against the expected ₹8.26 per closed position. They should agree to
within rounding; if they do not, something other than the DP fee moved and that is a finding.

- [ ] **Step 6: Commit**

```bash
git add config-daily.yaml tests/test_config.py
git commit -m "charges: the daily config pays Groww's real DP fee, not Zerodha's

groww.in/pricing: depository 3.5 plus Groww 16.5, plus 18% GST, is 23.60 a sell.
The shipped default was 15.34 -- Zerodha's -- so every CNC figure this repository
has produced understated charges by 8.26 a round trip.

<restate here: the daily-smoke-guard totals against daily-dp-corrected, and the
per-trade difference measured in Step 5>

This is the hurdle moving, not a strategy result: at a lakh across eight
positions the 60-day hurdle goes from 0.238%/mo to 0.261%/mo, against trend_dip's
measured 0.216%/mo. It does not change which side of the bar that setup falls on;
it moves it further from it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.** §2a/§2b chimera and DP fee → Tasks 2 and 6. §2c duplication → Task 5. §3 verified
rates → Task 2's `brokers.yaml` and its two pricing-page tests. §4 `brokers.yaml` with mandatory
provenance → Task 2. §5 selection and unchanged defaults → Task 3, with the correction split into
Task 6 exactly as the spec requires. §6 `tradebot hurdle` → Task 4. §7 unifying the screens, and the
`dev-swing-screen` merge it needs → Tasks 1 and 5. §8 validation-only scope → the standing
constraints; no task touches `engine/paper.py` or `cli.py`'s paper refusal. §9 testing → every
task's tests, plus the fixture check after Tasks 1 and 3.

**Knowingly deferred.** The point-in-time universe and the holdout run are Part A and get their own
spec, as §8 says. No `trend_dip` parameter is touched. Nothing here is intraday.

**Placeholders.** One deliberate blank: Task 6's commit message has a line to fill with figures that
do not exist until Step 5 runs. It names exactly what goes there. Task 3 Step 1 and Task 5 Step 1
say to build on the existing helper in each test file rather than naming one I have not read; both
say which file to read and what the test must prove.

**Type consistency.** `load_brokers(path) -> Dict[str, Broker]` in Task 2 is called with one
positional argument in Tasks 3, 4 and 5. `Broker.charges` is a `ChargesConfig` everywhere.
`round_trip_fraction(position_value, charges, slippage_pct)` in Task 4 is called positionally in
Task 5. `ChargesConfig.broker` is a `str` defaulting to `""` in Task 3 and read as such in Tasks 4
and 6. `PathsConfig.brokers` is a `str` set in Task 3 and read in Task 4 and via `load_brokers` in
Task 5.
