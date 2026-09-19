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
  unweighted mean of per-trade R; its sign is the sign of net PnL.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Tuple

from tradebot.config import ChargesConfig
from tradebot.execution.charges import position_charges
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
    days: list = field(default_factory=list)


def _max_drawdown(increments) -> float:
    cum = peak = mdd = 0.0
    for p in increments:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def row_charges(r, schedule: Optional[ChargesConfig]) -> Tuple[float, bool, bool]:
    """(charges, estimated, unknown) for one closed position row. A NULL charges means charges were
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
        return position_charges(r["direction"], r["avg_price"], r["exit_price"], r["qty"], schedule), True, False
    except ValueError as e:
        log.warning("position %s: could not estimate charges: %s: %s", r["id"], type(e).__name__, e)
        return 0.0, False, True


def build_summary(repo: Repo, run_id: str, schedule: Optional[ChargesConfig] = None) -> Summary:
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    rows = repo.list_positions(run_id)
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
    wins = sum(1 for p in pnls if p > 0)
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
        days=days,
    )


def _counts(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def format_summary(s: Summary) -> str:
    win_rate = f"{s.win_rate * 100:.1f}%" if s.trades else "n/a"
    charges_notes = []
    if s.charges_estimated:
        charges_notes.append(f"estimated after the fact for {s.charges_estimated_trades} of {s.trades} trades "
                              f"(charges not recorded); their daily rows are gross")
    if s.charges_unknown:
        charges_notes.append(f"{s.charges_unknown} trade(s) could not be charged (bad stored prices)")
    charges_note = ("   " + "; ".join(charges_notes)) if charges_notes else ""
    equity_dd_note = "; gross here: the daily rows have no recorded charges" if s.charges_estimated else ""
    lines = [
        f"Run {s.run_id} ({s.mode})",
        "Survivorship note: universe.yaml is today's constituent list; past-period results are overstated.",
        "",
        f"Trades                {s.trades}   (wins {s.wins}, losses {s.losses})",
        f"Win rate              {win_rate}",
        f"Gross PnL             {s.gross_pnl:,.2f}   after slippage, before charges",
        f"Charges               {s.charges:,.2f}{charges_note}",
        f"Total PnL             {s.total_pnl:,.2f}   net",
        f"Avg R (per trade)     {s.avg_r:.2f}   (over {s.r_trades} of {s.trades} trades with non-zero risk)",
        f"R on risk             {s.r_on_risk:.2f}   net PnL / rupees at risk, all trades pooled",
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
        lines += ["", f"{'Date':<10}  {'Realised':>14}  {'Unrealised':>14}  {'Fills/Entries':>14}  {'Fill rate':>9}"]
        for d in s.days:
            fe = f"{d['fills']}/{d['entries_placed']}"
            lines.append(f"{d['date']:<10}  {d['realised']:>14,.2f}  {d['unrealised']:>14,.2f}  {fe:>14}  "
                         f"{d['fill_rate'] * 100:>8.0f}%")
    return "\n".join(lines)
