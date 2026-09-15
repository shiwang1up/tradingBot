"""Stub-versus-Claude comparison (spec 14): the same signals, one run with every signal taken and
one with Claude filtering, joined by the deterministic client id so the report can say what
the rejected signals earned in the unfiltered run."""
from __future__ import annotations

from dataclasses import dataclass, field

from tradebot.engine.clock import iso_ist
from tradebot.report.summary import Summary, build_summary
from tradebot.store.repo import Repo
from tradebot.types import make_client_id


@dataclass(frozen=True)
class Prices:
    """USD per million tokens by kind, from ai.price_* config."""
    input: float
    output: float
    cache_read: float
    cache_write: float

    def cost(self, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output
                + cache_read * self.cache_read + cache_write * self.cache_write) / 1e6


@dataclass
class Compare:
    a: Summary
    b: Summary
    rejected: int
    rejected_with_position_in_a: int
    rejected_pnl_in_a: float        # net PnL of rejected signals in run A (what the filter gave up or avoided)
    rejected_losses_avoided: float  # sum of losses among rejected signals (positive number)
    rejected_wins_forgone: float    # sum of wins among rejected signals
    total_pnl_delta: float          # b.total_pnl - a.total_pnl
    calls: int
    failures: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    avg_latency_ms: float
    est_cost_usd: float
    rows: list = field(default_factory=list)


def build_compare(repo: Repo, run_a: str, run_b: str, prices: Prices) -> Compare:
    a, b = build_summary(repo, run_a), build_summary(repo, run_b)  # raises ValueError for unknown runs
    positions_a = repo.positions_by_client_id(run_a)
    rows, with_pos, net, losses, wins = [], 0, 0.0, 0.0, 0.0
    for r in repo.ai_rejected_signals(run_b):
        cid = make_client_id(r["strategy"], r["symbol"], r["bar_ts"])
        pos = positions_a.get(cid)
        pnl = None if pos is None or pos["pnl"] is None else float(pos["pnl"])
        if pnl is not None:
            with_pos += 1
            net += pnl
            if pnl < 0:
                losses += -pnl
            else:
                wins += pnl
        rows.append({"symbol": r["symbol"], "bar": iso_ist(r["bar_ts"]), "direction": r["direction"],
                     "reason": r["reason"], "confidence": r["confidence"], "pnl_in_a": pnl,
                     "exit_in_a": pos["exit_reason"] if pos is not None else None})
    u = repo.ai_usage(run_b)
    cost = prices.cost(int(u["input_tokens"]), int(u["output_tokens"]), int(u["cache_read_tokens"]),
                       int(u["cache_write_tokens"]))
    return Compare(
        a=a, b=b, rejected=len(rows), rejected_with_position_in_a=with_pos, rejected_pnl_in_a=net,
        rejected_losses_avoided=losses, rejected_wins_forgone=wins, total_pnl_delta=b.total_pnl - a.total_pnl,
        calls=int(u["calls"]), failures=int(u["failures"]), input_tokens=int(u["input_tokens"]),
        output_tokens=int(u["output_tokens"]), cache_read_tokens=int(u["cache_read_tokens"]),
        cache_write_tokens=int(u["cache_write_tokens"]),
        avg_latency_ms=float(u["avg_latency_ms"]), est_cost_usd=cost, rows=rows,
    )


def _side_by_side(a: Summary, b: Summary) -> list:
    metrics = [
        ("Trades", f"{a.trades}", f"{b.trades}"),
        ("Win rate", f"{a.win_rate * 100:.1f}%", f"{b.win_rate * 100:.1f}%"),
        ("Total PnL", f"{a.total_pnl:,.2f}", f"{b.total_pnl:,.2f}"),
        ("Avg R", f"{a.avg_r:.2f}", f"{b.avg_r:.2f}"),
        ("Max DD (closed)", f"{a.max_drawdown:,.2f}", f"{b.max_drawdown:,.2f}"),
        ("Max DD (equity)", f"{a.max_drawdown_equity:,.2f}", f"{b.max_drawdown_equity:,.2f}"),
        ("AI rejects", f"{a.ai_rejections}", f"{b.ai_rejections}"),
    ]
    out = [f"{'Metric':<16} {'A: ' + a.run_id:>18} {'B: ' + b.run_id:>18}"]
    out += [f"{m:<16} {va:>18} {vb:>18}" for m, va, vb in metrics]
    return out


def format_compare(c: Compare) -> str:
    lines = [f"Compare  A={c.a.run_id} (unfiltered)  vs  B={c.b.run_id} (Claude filter)", ""]
    lines += _side_by_side(c.a, c.b)
    lines += ["",
              f"Rejected by Claude      {c.rejected} signals, {c.rejected_with_position_in_a} of which A actually traded",
              f"  losses avoided        {c.rejected_losses_avoided:,.2f}",
              f"  wins forgone          {c.rejected_wins_forgone:,.2f}",
              f"  net PnL given up      {c.rejected_pnl_in_a:,.2f}   (negative means the filter removed net losers)",
              f"Total PnL delta (B-A)   {c.total_pnl_delta:,.2f}",
              f"Calls / failures        {c.calls} / {c.failures}   avg latency {c.avg_latency_ms:.0f} ms",
              f"Tokens in/out/cached    {c.input_tokens:,} / {c.output_tokens:,} / {c.cache_read_tokens:,} (cache writes {c.cache_write_tokens:,})",
              f"Estimated cost          ${c.est_cost_usd:,.2f}"]
    verdict = ("filter helped" if c.total_pnl_delta > c.est_cost_usd * 0 and c.total_pnl_delta > 0
               else "filter did not help on this window")
    lines += ["", f"Verdict: {verdict} (PnL delta {c.total_pnl_delta:,.2f}, cost ${c.est_cost_usd:,.2f})"]
    if c.rows:
        lines += ["", f"{'Bar (IST)':<25} {'Symbol':<12} {'Dir':<5} {'PnL in A':>10} {'Exit A':<11} Reason"]
        for r in c.rows:
            pnl = "unfilled" if r["pnl_in_a"] is None else f"{r['pnl_in_a']:,.2f}"
            lines.append(f"{r['bar']:<25} {r['symbol']:<12} {r['direction']:<5} {pnl:>10} {str(r['exit_in_a'] or '-'):<11} "
                         f"{r['reason']}")
    return "\n".join(lines)
