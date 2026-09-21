# Point-in-time universe design

**Status:** approved in outline 2026-09-21, awaiting spec review
**Branch:** `dev-nonprice-signals`
**Scope:** this spec covers the DATASET only. Using it in the screens is a second spec.

## 1. Why

Every cross-sectional result this project has produced is measured against
`universe.yaml`, which is today's constituent list. Stocks added during the
window because they rose are present with the full history of the rise that
earned them a place. Stocks dropped after falling are absent. The bias runs in
favour of any momentum-like signal.

Cross-sectional momentum returned a spread of +0.187%/mo (t 0.49) on 2021-2025.
Survivorship could account for the whole of it. Until the universe is
point-in-time, no positive result from this data can be taken at face value,
so this blocks every screen that follows rather than only the one that found it.

A second problem is resolved at the same time. The spread's standard error is
0.382%/mo, so 56 monthly observations cannot resolve an edge below +0.764%/mo
(9.6%/yr) at t >= 2. That ceiling is a property of the dataset, not of momentum:
50 names over 56 months is 2,600 name-months. NIFTY 200 is roughly 11,200.

The original design already intended this. `docs/superpowers/specs/2026-09-14-nse-bse-trading-bot-design.md`
section 4.1 says "universe.yaml is today's NIFTY 200" and describes `as_of` as
"the hook for point-in-time constituents later". The file holds 50 names. This
spec finishes what was specified then.

## 2. What this delivers

After this spec:

- `load_universe(path, as_of=date)` returns the NIFTY 200 constituents on any
  date from 2020-01-01 onward, instead of raising `NotImplementedError`.
- `data/tradebot.db` holds daily candles for every symbol that was in the index
  at any point in the window, not only those in it today.
- A slippage estimate that varies by the name's own liquidity, replacing the
  flat 5 bps that was calibrated for large caps.

Explicitly NOT in this spec: changing any screen, rerunning any experiment,
touching the live or paper engines, or intraday (5m/15m) data for the new
symbols. Daily candles only.

## 3. The membership timeline

### 3.1 Source

NSE Indices publishes a press release for each index review. The Index
Maintenance Sub-Committee (Equity) reviews semi-annually with cut-off dates of
31 January and 31 July, effective at the end of March and September. Releases
live under `nsearchives.nseindia.com` and carry a table with columns
`Sr. No. | Index Name | Security Name | Symbol | Remarks`, where Remarks reads
`Inclusion` or `Exclusion`. The text layer extracts cleanly; the tables are
machine-parseable.

Semi-annual reviews over 2020-01-01 to 2026-09-21 give roughly fourteen releases, plus occasional
off-cycle changes (a merger, a delisting, a revoked inclusion). Off-cycle
releases must be included; the count invariant in 3.4 is what detects a missed
one.

`niftyhistory.in` offers the same data as a CSV download. It is NOT the source
of record: it discloses no operator, no data source, no methodology and nothing
about how it handles renames or mergers. Using an unattributed third party as
the authority for the dataset whose purpose is trustworthiness would defeat the
exercise. It MAY be used as an independent cross-check, and any disagreement
with an NSE release is resolved in favour of the release and recorded.

### 3.2 Anchor

Reconstruction runs BACKWARD from a known-good snapshot, because NSE publishes
changes rather than snapshots. The anchor is today's NIFTY 200 constituent list
as published by NSE Indices, stored with the exact URL it came from and the date
it was fetched.

**Verified 2026-09-21.** Both of these return the same 13,081-byte file:

    https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv
    https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv

Columns: `Company Name, Industry, Symbol, Series, ISIN Code`. 200 data rows,
200 unique symbols, 200 unique ISINs. Fetch it with `curl` and a browser
user-agent; the WebFetch tool times out against NSE hosts, curl does not.

If a later fetch returns anything other than exactly 200 rows, stop and report
rather than falling back to a third party. The anchor is the foundation every
reconstructed date rests on, and an unattributed anchor makes the whole
timeline unattributable.

### 3.2.1 ISIN is the stable key

The constituent file carries an ISIN per name, which the spec did not
anticipate. ISIN survives a ticker rename where the symbol does not, so the
timeline stores BOTH and matches on ISIN wherever one is available. This
demotes the `renames` table from load-bearing to a fallback for the cases
where a press release gives only a symbol.

A merger is still not a rename: the ISIN disappears rather than changing, so
3.5 stands unaltered.

### 3.2.2 Scale, measured rather than estimated

49 of the current 50 names are in today's NIFTY 200, so 151 names must be
fetched before any historical entrant is considered. Total distinct symbols
across the window is therefore likely 200-250, not the 300-400 estimated in
section 4.

The one name of our 50 absent from today's NIFTY 200 is TATAMOTORS, which
demerged on 2025-10-23. It is already in the database and already ends early.
That is this entire spec's problem in miniature: a name that was in the index
for most of the window, is not in it now, and would be invisible to any screen
built from today's list.

### 3.3 File format

`index_membership.yaml` at the repository root, beside `universe.yaml`,
committed to git (it is reference data, not generated output, and must be
reviewable in a diff).

NOT under `data/`: that directory is gitignored in its entirety and nothing in
it is tracked, so a timeline placed there would be invisible to review and lost
on a fresh clone. `universe.yaml`, the file this one supersedes, already lives
at the root for the same reason.

```yaml
index: NIFTY 200
anchor:
  as_of: 2026-09-21
  source: <the NSE Indices constituent-list URL actually used, recorded at fetch>
  fetched: 2026-09-21
  symbols: [ADANIENT, ADANIPORTS, ...]      # exactly 200

events:                                      # newest first
  - effective: 2025-09-30                    # first date the new list applies
    source: https://nsearchives.nseindia.com/.../ind_prs23082025.pdf
    include: [INDIGO, MAXHEALTH]
    exclude: [HEROMOTOCO, INDUSINDBK]

renames:
  - from: HDFC
    to: HDFCBANK
    effective: 2023-07-13
    kind: merger
    source: https://...
```

`effective` is the first trading date on which the new list applies. NSE
announces "effective from 28 March 2024 (close of 27 March 2024)"; the stored
date is 2024-03-28.

### 3.4 Replay and the count invariant

`load_universe(path, as_of=d)`:

1. If `d >= anchor.as_of`, return the anchor.
2. Otherwise start from the anchor and, for every event with
   `effective > d`, taken newest first, UNDO it: remove each `include`, restore
   each `exclude`.
3. Apply renames in reverse across the same span, so a pre-merger date returns
   `HDFC` rather than `HDFCBANK`.

**The membership count must be exactly 200 after every undo step.** If it is
not, a release was missed, misparsed, or double-applied. The loader raises with
the offending event's effective date and source URL, and the count it produced.

This invariant is the main correctness control in this spec, and it is cheap:
it runs on every load, names the date that broke, and cannot be satisfied by a
wrong-but-plausible timeline. Nothing comparable existed for the hold-window
defect found in the momentum screen, which is why that bug survived a full
review cycle and two rounds of controls.

Two failure modes the invariant does NOT catch, which therefore need their own
handling:

- **A symbol swapped for itself under a new name.** Caught by the rename table;
  a rename not recorded there shows up as a symbol with no candles, which the
  fetch step reports.
- **Two errors that cancel** (a missed inclusion and a missed exclusion in the
  same release). Mitigated by the cross-check in 3.1 and by asserting that each
  parsed release has equal include and exclude counts for NIFTY 200, which
  periodic reviews always do.

### 3.5 Mergers and delistings

A symbol that ceases to exist is not a rename in the backtest sense: its price
series ends. The `renames` table records it so that a pre-event date returns the
correct symbol, and the fetch step is expected to find candles ending at the
event date. A screen holding such a name must exit at its last close; that is
the second spec's problem, not this one's, but the data must make it detectable
rather than silently truncating.

## 4. Historical data

Every symbol appearing in the anchor, in any event, or in any rename needs
daily candles from 2020-01-01. Expect 200-250 distinct symbols against today's
50 (see 3.2.2; 151 are needed for the anchor alone).

- Reuse the existing `tradebot fetch-data` path at `interval=1440`. No new
  fetch machinery.
- **Never write to `data/` without a backup first.** Take
  `data/tradebot.pre-universe.bak.db` before the first insert.
- Groww's approval-flow key must be approved that morning. At 200-250 symbols
  this is likely hours, not minutes; it must be resumable and must not restart
  from the beginning after a failure.
- A symbol Groww has no history for is recorded in a
  `data/missing_symbols.txt` with the reason, not silently skipped. The second
  spec decides how to treat them; this spec only guarantees they are visible.
- The existing `gap_mask` corporate-action detectors apply to the new symbols
  unchanged, including the intrabar detector added for Groww's synthetic 2025
  `open`. Expect more unadjusted splits than the current 50 show, because
  midcaps split more often.

## 5. Liquidity-tiered slippage

### 5.1 What is being modelled

At ₹10,000 a position, a trade is about 0.00065% of daily turnover even in the
thinnest current name (₹10,000 against a ₹154 cr median). This is therefore NOT market impact. It is the bid-ask
spread, half of which is paid on each side. Turnover is a proxy for spread, not
a measure of it.

### 5.2 The rule

`slippage_for(symbol, date)` returns a percent per side, from the median daily
traded value (close x volume) over the 60 trading days ending the day before
`date`. Tiers, fixed here BEFORE any result is computed:

| Median daily traded value | Slippage per side |
|---|---|
| >= ₹500 cr | 0.05% |
| ₹100 cr to ₹500 cr | 0.08% |
| ₹25 cr to ₹100 cr | 0.15% |
| < ₹25 cr | 0.30% |

Today's 50 names span ₹133 cr to ₹2,668 cr median daily turnover, so the
existing universe lands in the top two tiers and the 5 bps figure is preserved
for the largest names.

**"Insufficient history" means fewer than a full 60-bar window, and takes the
most conservative tier.** The original wording did not define the threshold,
which let an implementation price a 5-bar history as though it were 60 and land
a barely-traded name in the CHEAPEST tier. The rule is deliberately strict
rather than a fraction of the window, because the two errors are not
symmetrical: charging a liquid name too much is bounded and merely pessimistic,
while charging a thin name 5 bps is unbounded flattery of whichever strategy
selected it. This matters most for exactly the names this spec adds -- a stock
entering the index part-way through the window has a short history at precisely
the dates it first becomes rankable.

### 5.3 Honesty about the tiers

These are estimates. The project has no quote data, so no tier is calibrated
against an observed spread. The direction of the error is the thing to state:
if the tiers are too generous, thin names look better than they are, and thin
names are exactly where momentum premia are largest in the published
literature. Every report using this must print the tier boundaries alongside
the result, the way the round-trip cost is printed today.

Changing a tier after seeing a result is forbidden. If the tiers turn out to be
wrong, they are changed once, with the reason recorded, and every affected
result is rerun and restated.

## 6. Error handling

- A parse that yields no NIFTY 200 rows for a release fails loudly with the URL.
  A release genuinely containing no NIFTY 200 change is recorded explicitly with
  empty include/exclude lists, so "no change" is distinguishable from "not
  parsed".
- `as_of` earlier than the timeline's declared `covers_from` raises, rather than
  returning the oldest reconstructed list as if it extended indefinitely
  backwards.

  `covers_from` is a required top-level key meaning "every index change effective
  on or after this date is recorded here". It is NOT derivable from the events.
  Undoing the earliest event does yield the list in force the day before it, and
  because NSE reviews semi-annually that list usually stays valid for months
  further back: the first recorded review is around 2020-03-31 while the real
  file must answer for 2020-01-01. What bounds the answer is whether the record
  is COMPLETE over the span, which is a fact about how the file was compiled and
  so must be declared rather than inferred. A timeline whose `covers_from` is
  later than one of its own events is internally inconsistent and must not load.
- `as_of` in the future raises.
- The count invariant raises, as in 3.4.
- Symbols are upper-cased and de-duplicated on load, as `load_universe` does now.

## 7. Testing

Unit, against a small synthetic timeline, not the real file:

- Replay to a date between two events returns the correct set.
- Replay to the anchor date returns the anchor unchanged.
- An event that would break the 200 count raises, naming the date and source.
- A rename resolves correctly either side of its effective date.
- `as_of` before the earliest event, and in the future, both raise.
- Insufficient history assigns the most conservative slippage tier.
- Each slippage tier boundary maps as specified, tested at the boundary value
  itself, not only in the middle of a band.

Integration, against the real committed timeline:

- Every date from 2020-01-01 to the anchor yields exactly 200 symbols. This is a
  loop over ~1,700 dates and is the single most valuable test in this spec.
- Every symbol the timeline ever mentions has candles, or appears in
  `missing_symbols.txt` with a reason.
- Recomputing today's list from the timeline equals `universe.yaml`'s 50 names
  as a subset, since NIFTY 50 is contained in NIFTY 200.

## 8. Risks

- **Sourcing is the main risk, not code.** If a release cannot be found for a
  given review, the timeline has a hole and the invariant will fail at that
  date. That is the correct outcome: a loud hole beats a quiet wrong answer.
- Groww may lack history for names dropped early in the window. Recorded, not
  worked around.
- The fetch is long and depends on a key that is approved daily.
- The tiers are unvalidated, as stated in 5.3.
- Roughly 200-250 symbols of daily history is 300-360k rows, which SQLite
  handles comfortably; no schema change is expected beyond more rows.

## 9. Out of scope, and what comes next

Not here: rerunning momentum, the delivery screen, any change to the screens'
eligibility logic, intraday data for new symbols, point-in-time data for any
index other than NIFTY 200, and the live or paper engines.

The second spec reruns the screens on this dataset and tests at the name-month
level (roughly 11,200 observations) rather than as a 10-name portfolio (56),
because the power analysis in section 1 shows the portfolio construction throws
away most of the resolution the data contains.
