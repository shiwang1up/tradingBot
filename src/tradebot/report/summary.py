"""Run summary: trade statistics, drawdown, rejection counts, daily PnL."""
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
    max_drawdown: float
    exit_reasons: dict[str, int]
    risk_rejections: dict[str, int]
    ai_rejections: int
    open_positions: int
    adopted_pnl: float
    days: list[dict] = field(default_factory=list)


def build_summary(repo: Repo, run_id: str) -> Summary:
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    rows = repo.list_positions(run_id)
    closed = sorted((r for r in rows if r["closed_at"] is not None and not r["adopted"]), key=lambda r: r["closed_at"])
    pnls = [r["pnl"] for r in closed]
    rs = []
    for r in closed:
        risk = abs(r["avg_price"] - r["stop"]) * r["qty"]
        if risk > 0:
            rs.append(r["pnl"] / risk)
    cum = peak = mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
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
        max_drawdown=mdd,
        exit_reasons=dict(Counter(r["exit_reason"] for r in closed)),
        risk_rejections=repo.rejection_counts(run_id),
        ai_rejections=repo.ai_rejection_count(run_id),
        open_positions=sum(1 for r in rows if r["closed_at"] is None),
        adopted_pnl=sum(r["pnl"] or 0.0 for r in rows if r["adopted"] and r["closed_at"] is not None),
        days=[dict(d) for d in repo.daily_pnl(run_id)],
    )


def format_summary(s: Summary) -> str:
    lines = [
        f"Run {s.run_id} ({s.mode})",
        "Survivorship note: universe.yaml is today's constituent list; past-period results are overstated.",
        "",
        f"Trades        {s.trades}   (wins {s.wins}, losses {s.losses})",
        f"Win rate      {s.win_rate * 100:.1f}%",
        f"Total PnL     {s.total_pnl:,.2f}",
        f"Avg R         {s.avg_r:.2f}",
        f"Max drawdown  {s.max_drawdown:,.2f}",
        f"Exit reasons  {s.exit_reasons}",
        f"Risk rejects  {s.risk_rejections}",
        f"AI rejects    {s.ai_rejections}",
        f"Open now      {s.open_positions}",
    ]
    if s.adopted_pnl:
        lines.append(f"Adopted PnL   {s.adopted_pnl:,.2f} (excluded from stats above)")
    if s.days:
        lines += ["", "Date        Realised    Unrealised  Fills/Entries  Fill rate"]
        for d in s.days:
            lines.append(f"{d['date']}  {d['realised']:>10,.2f}  {d['unrealised']:>10,.2f}  "
                         f"{d['fills']:>5}/{d['entries_placed']:<7}  {d['fill_rate'] * 100:.0f}%")
    return "\n".join(lines)
