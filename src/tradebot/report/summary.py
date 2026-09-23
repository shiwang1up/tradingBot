"""Run summary: trade statistics, drawdown, rejection counts, daily PnL.

Conventions:
- Strategy statistics exclude adopted positions (spec 8.5); their PnL is reported separately.
- A trade with pnl == 0 counts as a loss so wins + losses == trades.
- "Max drawdown (closed)" is the peak-to-trough of cumulative realised PnL over closed trades.
  "Max drawdown (equity)" is over the daily rows' realised + unrealised, so open-position
  excursions count. Both are 2-dp rupee figures, not percentages.
- PnL figures are net of charges. A row whose charges is NULL means charges were not recorded for
  it: with a schedule passed in, its charges are estimated from the fills and the summary is
  marked estimated.
- R is computed on the FILLED entry price, i.e. the rupees actually at risk, not the signal's
  planned entry. "R on risk" pools net PnL and rupees at risk over every trade before dividing, so
  a scrap-sized position with a razor-thin stop cannot dominate it the way it can dominate an
  unweighted mean of per-trade R; its sign is the sign of net PnL over those trades.
- Expectancy is the net PnL per trade; the printed line also shows
  `win_rate x avg_win` against `loss_rate x |avg_loss|`, the form the trade-off between win rate and
  payoff is usually argued in, but that is not asserted to equal expectancy: the two averages are
  themselves rounded to 2dp for display, so the arithmetic would not reconcile at that precision.
  Payoff (avg_win / |avg_loss|) and the breakeven win rate (1 / (1 + payoff)) are undefined - `None`,
  printed `n/a` - when there are no losing trades or no winning trades to divide by; treating that as
  0.0 would make a flawless run look the worst.
- Evidence (days, mean, t) is computed from the trades' NET PnL summed per IST close date, NOT from
  the daily_pnl rows: those are gross for runs whose charges were estimated after the fact, and one
  source keeps the block internally consistent. The day is the unit because same-day trades are
  correlated. Only days with at least one closed trade are counted - a flat day contributes no PnL
  observation either way, and folding zeros in would dilute the variance without changing the mean
  it is compared against, so it is labelled "days traded" rather than implied to cover the whole run.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Tuple

from tradebot.config import ChargesConfig
from tradebot.engine.clock import date_of
from tradebot.execution.charges import position_charges
from tradebot.report.benchmark import Benchmark
from tradebot.store.repo import Repo

log = logging.getLogger("tradebot.report")


@dataclass
class Summary:
    run_id: str
    mode: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_r: float
    r_trades: int              # trades with non-zero risk that entered avg_r
    max_drawdown: float        # closed-trade, realised only
    max_drawdown_equity: float  # daily realised + unrealised
    exit_reasons: dict
    risk_rejections: dict
    ai_rejections: int
    open_positions: int
    adopted_trades: int
    adopted_pnl: float
    gross_pnl: float = 0.0           # after slippage, before charges
    charges: float = 0.0
    charges_estimated: bool = False  # some rows' charges were not recorded and were estimated here
    charges_estimated_trades: int = 0  # how many of `trades` those were
    charges_unknown: int = 0         # closed rows whose charges could not be computed at all
    r_on_risk: float = 0.0           # net PnL / rupees at risk, pooled over the trades that entered avg_r
    avg_win: float = 0.0             # mean net PnL of winning trades
    avg_loss: float = 0.0            # mean net PnL of the rest, negative; a scratch counts as a loss
    payoff: Optional[float] = None   # avg_win / |avg_loss|; None with no losses or no wins to divide
    expectancy: float = 0.0          # net PnL per trade
    breakeven_win_rate: Optional[float] = None  # 1 / (1 + payoff); None when payoff is undefined
    evidence_days: int = 0           # days with at least one closed trade
    evidence_mean: float = 0.0       # mean net PnL per such day
    evidence_t: Optional[float] = None  # None when fewer than 2 days or no variance
    days: list = field(default_factory=list)
    benchmark: Optional["Benchmark"] = None   # equal-weight buy & hold over the same window


def _max_drawdown(increments) -> float:
    cum = peak = mdd = 0.0
    for p in increments:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def row_charges(r, schedule: Optional[ChargesConfig]) -> Tuple[float, bool, bool]:
    """(charges, estimated, unknown) for one closed position row, charged on the row's own product. A NULL charges means charges were
    not recorded for this row; with no schedule (or a disabled one) it is treated as 0.0,
    unestimated, same as before charges existed. A row closed without a price (exit_price is None)
    predates a price rather than having a bad one, so it is not "unknown" either. A row whose
    stored numbers cannot be charged (e.g. a bad exit price) must not crash the report:
    position_charges' ValueError is contained here, logged, and counted as unknown so it does not
    vanish from the charge total silently."""
    if r["charges"] is not None:
        return float(r["charges"]), False, False
    if schedule is None or not schedule.enabled or r["exit_price"] is None:
        return 0.0, False, False
    try:
        # product, as in BacktestBroker._close: an estimate at intraday rates for a delivery row
        # would make the report disagree with the run that recorded it.
        return (position_charges(r["direction"], r["avg_price"], r["exit_price"], r["qty"], schedule,
                                 product=r["product"]), True, False)
    except ValueError as e:
        log.warning("position %s: could not estimate charges: %s: %s", r["id"], type(e).__name__, e)
        return 0.0, False, True


def _day_t(day_pnls: list) -> Tuple[float, Optional[float]]:
    """(mean, t) of a per-day PnL series. t is None with fewer than two days or no variance: a
    single day, or a run of identical days, says nothing about whether the mean differs from zero."""
    n = len(day_pnls)
    if n == 0:
        return 0.0, None
    mean = sum(day_pnls) / n
    if n < 2:
        return mean, None
    var = sum((p - mean) ** 2 for p in day_pnls) / (n - 1)
    if var <= 0:
        return mean, None
    return mean, mean / math.sqrt(var / n)


def build_summary(repo: Repo, run_id: str, schedule: Optional[ChargesConfig] = None,
                  since_ts: Optional[int] = None, *, benchmark: Optional[Benchmark] = None) -> Summary:
    """`since_ts`, when given, scores only positions opened at or after it and only daily rows dated
    on or after its IST date - so a filter-on run's warm-up days (rejected regime_not_ready, not on
    the strategy's merit) do not cost a filter-off comparison run its own early days too (D3). Only
    the trade statistics and the daily rows (`days`, and the equity drawdown taken from them) are
    filtered. `risk_rejections` and `ai_rejections` remain whole-run figures; `open_positions` counts
    only the still-open positions opened at or after `since_ts`, like the trade statistics."""
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    rows = repo.list_positions(run_id)
    if since_ts is not None:
        rows = [r for r in rows if r["opened_at"] >= since_ts]
    closed = sorted((r for r in rows if r["closed_at"] is not None and not r["adopted"]), key=lambda r: r["closed_at"])
    adopted_closed = [r for r in rows if r["closed_at"] is not None and r["adopted"]]
    costs = [row_charges(r, schedule) for r in closed]
    gross = [r["pnl"] or 0.0 for r in closed]
    pnls = [g - c for g, (c, _, _) in zip(gross, costs)]
    rs = []
    total_risk = total_risked_net = 0.0
    for r, p in zip(closed, pnls):
        risk = abs(r["avg_price"] - r["stop"]) * r["qty"]
        if risk > 0:
            rs.append(p / risk)
            total_risk += risk
            total_risked_net += p
    days = [dict(d) for d in repo.daily_pnl(run_id)]
    if since_ts is not None:
        since_date = date_of(since_ts).isoformat()
        days = [d for d in days if d["date"] >= since_date]
    wins = sum(1 for p in pnls if p > 0)
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    payoff = avg_win / abs(avg_loss) if avg_loss < 0 and avg_win > 0 else None
    by_day: dict = {}
    for r, p in zip(closed, pnls):
        d = date_of(r["closed_at"])
        by_day[d] = by_day.get(d, 0.0) + p
    evidence_mean, evidence_t = _day_t([by_day[d] for d in sorted(by_day)])
    return Summary(
        run_id=run_id,
        mode=run["mode"],
        trades=len(closed),
        wins=wins,
        losses=len(closed) - wins,
        win_rate=wins / len(closed) if closed else 0.0,
        total_pnl=sum(pnls),
        avg_r=sum(rs) / len(rs) if rs else 0.0,
        r_trades=len(rs),
        max_drawdown=_max_drawdown(pnls),
        max_drawdown_equity=_max_drawdown(d["realised"] + d["unrealised"] for d in days),
        exit_reasons=dict(Counter(r["exit_reason"] for r in closed)),
        risk_rejections=repo.rejection_counts(run_id),
        ai_rejections=repo.ai_rejection_count(run_id),
        open_positions=sum(1 for r in rows if r["closed_at"] is None),
        adopted_trades=len(adopted_closed),
        adopted_pnl=sum(r["pnl"] or 0.0 for r in adopted_closed),
        gross_pnl=sum(gross),
        charges=sum(c for c, _, _ in costs),
        charges_estimated=any(e for _, e, _ in costs),
        charges_estimated_trades=sum(1 for _, e, _ in costs if e),
        charges_unknown=sum(1 for _, _, u in costs if u),
        r_on_risk=(total_risked_net / total_risk) if total_risk > 0 else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=payoff,
        expectancy=(sum(pnls) / len(pnls)) if pnls else 0.0,
        breakeven_win_rate=(1 / (1 + payoff)) if payoff is not None else None,
        evidence_days=len(by_day),
        evidence_mean=evidence_mean,
        evidence_t=evidence_t,
        days=days,
        benchmark=benchmark,
    )


def _counts(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def gross_kind(s: Summary) -> Optional[str]:
    """None when every trade's charges were recorded; "full" when every trade was estimated (the
    daily rows are plain gross); "partial" when only some were (the daily rows mix real and gross
    figures, so calling the whole run gross would be false)."""
    if not s.charges_estimated:
        return None
    return "full" if s.charges_estimated_trades == s.trades else "partial"


def format_summary(s: Summary) -> str:
    win_rate = f"{s.win_rate * 100:.1f}%" if s.trades else "n/a"
    charges_notes = []
    if s.charges_estimated:
        charges_notes.append(f"est. for {s.charges_estimated_trades} of {s.trades} trades "
                              f"(not recorded at the time)")
    if s.charges_unknown:
        charges_notes.append(f"{s.charges_unknown} trade(s) could not be charged (bad stored prices)")
    charges_note = ("   " + "; ".join(charges_notes)) if charges_notes else ""
    kind = gross_kind(s)
    if kind == "full":
        equity_dd_note = "; gross here: the daily rows have no recorded charges"
    elif kind == "partial":
        equity_dd_note = "; partly gross: some daily rows have no recorded charges"
    else:
        equity_dd_note = ""
    payoff_str = "n/a" if s.payoff is None else f"{s.payoff:.2f}"
    if s.trades == 0:
        avg_line = "n/a"
        expectancy_line = "n/a"
        breakeven_line = "n/a"
        evidence = "n/a"
    else:
        avg_line = f"{s.avg_win:+,.2f} / {s.avg_loss:+,.2f}   payoff {payoff_str}"
        expectancy_line = (f"{s.expectancy:,.2f} per trade   {s.win_rate * 100:.1f}% x {s.avg_win:,.2f} won against "
                            f"{(1 - s.win_rate) * 100:.1f}% x {abs(s.avg_loss):,.2f} lost")
        breakeven_str = "n/a" if s.breakeven_win_rate is None else f"{s.breakeven_win_rate * 100:.1f}%"
        breakeven_line = (f"{breakeven_str} at the payoff this run actually achieved ({payoff_str}); "
                           f"actual win rate {s.win_rate * 100:.1f}%")
        if s.trades < 30 or s.evidence_days < 20:
            evidence = (f"{s.evidence_days} days traded, mean {s.evidence_mean:,.2f} per day   "
                        f"too few to judge (needs 30+ trades and 20+ days)")
        elif s.evidence_t is None:
            evidence = f"{s.evidence_days} days traded, mean {s.evidence_mean:,.2f} per day, t n/a"
        else:
            # Choose the tier from the SAME value that is displayed: t is rounded to 1dp first, so a
            # value like -3.9797 (displayed "-4.0") is judged against the -4 boundary as "-4.0", not
            # as the unrounded -3.9797 - which would print "-4.0   some evidence of a loss", a tier
            # that reads as contradicting the number right next to it.
            t_rounded = round(s.evidence_t, 1)
            if t_rounded >= 4:
                note = "   consistently profitable"
            elif t_rounded >= 2:
                note = "   some evidence of an edge"
            elif t_rounded <= -4:
                note = "   consistently losing"
            elif t_rounded <= -2:
                note = "   some evidence of a loss"
            else:
                note = "   not distinguishable from zero"
            evidence = f"{s.evidence_days} days traded, mean {s.evidence_mean:,.2f} per day, t {t_rounded:.1f}{note}"
    lines = [
        f"Run {s.run_id} ({s.mode})",
        "Survivorship note: universe.yaml is today's constituent list; past-period results are overstated.",
        "",
        f"Trades                {s.trades}   (wins {s.wins}, losses {s.losses})",
        f"Win rate              {win_rate}",
        f"Gross PnL             {s.gross_pnl:,.2f}   after slippage, before charges",
        f"Charges               {s.charges:,.2f}{charges_note}",
        f"Total PnL             {s.total_pnl:,.2f}   net",
    ]
    if s.benchmark is not None:
        b = s.benchmark
        verdict = "beat" if s.total_pnl > b.net else "lost to"
        held = f"{b.names_held} of {b.names_held + b.names_skipped} names"
        lines.append(f"Benchmark             {b.net:,.2f}   equal-weight buy & hold, {held}, "
                     f"net of one round trip")
        if b.names_skipped:
            # Not a footnote: at a 2,000 slice this drops the highest-priced names, and a basket
            # presented as "the universe" while holding four fifths of it would be its own lie.
            lines.append(f"                      {b.names_skipped} name(s) skipped: one share cost "
                         f"more than the {b.slice_value:,.0f} slice")
        lines.append(f"Strategy vs benchmark {s.total_pnl - b.net:+,.2f}   "
                     f"selecting {verdict} not selecting")
    lines += [
        f"Avg R (per trade)     {s.avg_r:.2f}   (over {s.r_trades} of {s.trades} trades with non-zero risk)",
        f"R on risk             {s.r_on_risk:.2f}   net PnL / rupees at risk, pooled over the same {s.r_trades} trades",
        f"Avg win / avg loss    {avg_line}",
        f"Expectancy            {expectancy_line}",
        f"Breakeven win rate    {breakeven_line}",
        f"Evidence              {evidence}",
        f"Max drawdown (closed) {s.max_drawdown:,.2f}   realised, closed trades only",
        f"Max drawdown (equity) {s.max_drawdown_equity:,.2f}   daily realised + unrealised{equity_dd_note}",
        f"Exit reasons          {_counts(s.exit_reasons)}",
        f"Risk rejects          {_counts(s.risk_rejections)}",
        f"AI rejects            {s.ai_rejections}",
        f"Open now              {s.open_positions}",
    ]
    if s.adopted_trades:
        lines.append(f"Adopted               {s.adopted_trades} trades, gross PnL {s.adopted_pnl:,.2f} (excluded from stats above)")
    if s.days:
        if kind == "full":
            table_note = "   (gross: no recorded charges)"
        elif kind == "partial":
            table_note = "   (partly gross: some days have no recorded charges)"
        else:
            table_note = ""
        lines += ["", f"{'Date':<10}  {'Realised':>14}  {'Unrealised':>14}  {'Fills/Entries':>14}  "
                      f"{'Fill rate':>9}{table_note}"]
        for d in s.days:
            fe = f"{d['fills']}/{d['entries_placed']}"
            lines.append(f"{d['date']:<10}  {d['realised']:>14,.2f}  {d['unrealised']:>14,.2f}  {fe:>14}  "
                         f"{d['fill_rate'] * 100:>8.0f}%")
    return "\n".join(lines)
