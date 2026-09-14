"""Run summary: trade statistics, drawdown, rejection counts, daily PnL.

Conventions:
- Strategy statistics exclude adopted positions (spec 8.5); their PnL is reported separately.
- A trade with pnl == 0 counts as a loss so wins + losses == trades.
- "Max drawdown (closed)" is the peak-to-trough of cumulative realised PnL over closed trades.
  "Max drawdown (equity)" is over the daily rows' realised + unrealised, so open-position
  excursions count. Both are 2-dp rupee figures, not percentages.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from tradebot.store.repo import Repo


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
    days: list = field(default_factory=list)


def _max_drawdown(increments) -> float:
    cum = peak = mdd = 0.0
    for p in increments:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def build_summary(repo: Repo, run_id: str) -> Summary:
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    rows = repo.list_positions(run_id)
    closed = sorted((r for r in rows if r["closed_at"] is not None and not r["adopted"]), key=lambda r: r["closed_at"])
    adopted_closed = [r for r in rows if r["closed_at"] is not None and r["adopted"]]
    pnls = [r["pnl"] or 0.0 for r in closed]
    rs = []
    for r in closed:
        risk = abs(r["avg_price"] - r["stop"]) * r["qty"]
        if risk > 0:
            rs.append((r["pnl"] or 0.0) / risk)
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
        days=days,
    )


def _counts(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def format_summary(s: Summary) -> str:
    win_rate = f"{s.win_rate * 100:.1f}%" if s.trades else "n/a"
    lines = [
        f"Run {s.run_id} ({s.mode})",
        "Survivorship note: universe.yaml is today's constituent list; past-period results are overstated.",
        "",
        f"Trades                {s.trades}   (wins {s.wins}, losses {s.losses})",
        f"Win rate              {win_rate}",
        f"Total PnL             {s.total_pnl:,.2f}",
        f"Avg R                 {s.avg_r:.2f}   (over {s.r_trades} of {s.trades} trades with non-zero risk)",
        f"Max drawdown (closed) {s.max_drawdown:,.2f}   realised, closed trades only",
        f"Max drawdown (equity) {s.max_drawdown_equity:,.2f}   daily realised + unrealised",
        f"Exit reasons          {_counts(s.exit_reasons)}",
        f"Risk rejects          {_counts(s.risk_rejections)}",
        f"AI rejects            {s.ai_rejections}",
        f"Open now              {s.open_positions}",
    ]
    if s.adopted_trades:
        lines.append(f"Adopted               {s.adopted_trades} trades, PnL {s.adopted_pnl:,.2f} (excluded from stats above)")
    if s.days:
        lines += ["", f"{'Date':<10}  {'Realised':>14}  {'Unrealised':>14}  {'Fills/Entries':>14}  {'Fill rate':>9}"]
        for d in s.days:
            fe = f"{d['fills']}/{d['entries_placed']}"
            lines.append(f"{d['date']:<10}  {d['realised']:>14,.2f}  {d['unrealised']:>14,.2f}  {fe:>14}  "
                         f"{d['fill_rate'] * 100:>8.0f}%")
    return "\n".join(lines)
