"""Stub-versus-Claude comparison (spec 14): the same signals, one run with every signal taken and
one with Claude filtering, joined by the deterministic client id so the report can say what
the rejected signals earned in the unfiltered run.

Attribution caveat: run A's state diverges from run B's once their trades differ (capital, open
positions, cooldowns), so a signal A skipped for its own reasons shows here as "unfilled" even
though the filter had nothing to do with it. The most directly attributable figure is the PnL of
the rejected signals in A; the total PnL delta also includes everything B did with the freed capital.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from tradebot.config import ChargesConfig
from tradebot.engine.clock import iso_ist
from tradebot.report.summary import Summary, build_summary, gross_kind, row_charges
from tradebot.store.repo import Repo
from tradebot.types import make_client_id

COMPARABLE_KEYS = ("strategy", "session", "capital", "risk", "execution", "charges")


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
    rejected_closed_in_a: int       # rejected signals that A traded to completion (the accounted ones)
    rejected_open_in_a: int         # rejected signals whose A position is still open
    rejected_pnl_in_a: float        # net PnL of the closed ones: positive means the filter cost money
    rejected_losses_avoided: float  # sum of losses among them (positive number)
    rejected_wins_forgone: float    # sum of wins among them
    total_pnl_delta: float          # b.total_pnl - a.total_pnl
    calls: int
    failures: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    avg_latency_ms: float
    est_cost_usd: float
    warnings: list = field(default_factory=list)
    rows: list = field(default_factory=list)


def _effective_regime(cfg: dict):
    """The regime section as it actually behaves, not as stored: disabled reads as absent (an
    older run with no 'regime' key at all must not warn against a newer run that stores the
    (disabled, default) section explicitly), and a 'source: index' run also carries the index
    symbol it gates on - two runs can store the identical 'regime' section yet trade against
    different indices via data.index_symbol, which is not itself a COMPARABLE_KEYS entry."""
    regime = cfg.get("regime") or {}
    if not regime.get("enabled"):
        return None
    if regime.get("source", "index") == "index":
        return regime, (cfg.get("data") or {}).get("index_symbol")
    return regime, None


def _compat_warnings(repo: Repo, run_a: str, run_b: str, a: Summary, b: Summary) -> list:
    out = []
    if a.mode != "backtest" or b.mode != "backtest":
        raise ValueError("compare works on backtest runs only (live positions do not carry client ids)")
    try:
        ca = json.loads(repo.get_run(run_a)["config_json"])
        cb = json.loads(repo.get_run(run_b)["config_json"])
    except (TypeError, ValueError):
        return ["could not parse one run's stored config; comparability unknown"]
    for key in COMPARABLE_KEYS:
        if ca.get(key) != cb.get(key):
            out.append(f"runs differ in '{key}': the comparison may not be like-for-like")
    if _effective_regime(ca) != _effective_regime(cb):
        out.append("runs differ in 'regime': the comparison may not be like-for-like")
    return out


def build_compare(repo: Repo, run_a: str, run_b: str, prices: Prices, schedule: Optional[ChargesConfig] = None) -> Compare:
    a, b = build_summary(repo, run_a, schedule), build_summary(repo, run_b, schedule)  # raises ValueError for unknown runs
    warnings = _compat_warnings(repo, run_a, run_b, a, b)
    positions_a = repo.positions_by_client_id(run_a)
    rows, closed, open_, net, losses, wins = [], 0, 0, 0.0, 0.0, 0.0
    for r in repo.ai_rejected_signals(run_b):
        cid = make_client_id(r["strategy"], r["symbol"], r["bar_ts"])
        pos = positions_a.get(cid)
        if pos is None:
            status, pnl = "unfilled", None
        elif pos["closed_at"] is None:
            status, pnl = "open", None
            open_ += 1
        else:
            status, pnl = "closed", float(pos["pnl"] or 0.0) - row_charges(pos, schedule)[0]  # (charges, estimated, unknown)
            closed += 1
            net += pnl
            if pnl <= 0:  # same convention as summary: a scratch counts on the loss side
                losses += -pnl
            else:
                wins += pnl
        rows.append({"symbol": r["symbol"], "bar": iso_ist(r["bar_ts"]), "direction": r["direction"],
                     "reason": r["reason"], "confidence": r["confidence"], "status": status, "pnl_in_a": pnl,
                     "exit_in_a": pos["exit_reason"] if pos is not None else None})
    u = repo.ai_usage(run_b)
    cost = prices.cost(int(u["input_tokens"]), int(u["output_tokens"]), int(u["cache_read_tokens"]),
                       int(u["cache_write_tokens"]))
    return Compare(
        a=a, b=b, rejected=len(rows), rejected_closed_in_a=closed, rejected_open_in_a=open_,
        rejected_pnl_in_a=net, rejected_losses_avoided=losses, rejected_wins_forgone=wins,
        total_pnl_delta=b.total_pnl - a.total_pnl,
        calls=int(u["calls"]), failures=int(u["failures"]), input_tokens=int(u["input_tokens"]),
        output_tokens=int(u["output_tokens"]), cache_read_tokens=int(u["cache_read_tokens"]),
        cache_write_tokens=int(u["cache_write_tokens"]),
        avg_latency_ms=float(u["avg_latency_ms"]), est_cost_usd=cost, warnings=warnings, rows=rows,
    )


def _dd_suffix(kind: Optional[str]) -> str:
    if kind == "full":
        return " (gross)"
    if kind == "partial":
        return " (partly gross)"
    return ""


def _side_by_side(a: Summary, b: Summary) -> list:
    charges_a = f"{a.charges:,.2f}" + (" (est.)" if a.charges_estimated else "")
    charges_b = f"{b.charges:,.2f}" + (" (est.)" if b.charges_estimated else "")
    dd_equity_a = f"{a.max_drawdown_equity:,.2f}" + _dd_suffix(gross_kind(a))
    dd_equity_b = f"{b.max_drawdown_equity:,.2f}" + _dd_suffix(gross_kind(b))
    metrics = [
        ("Trades", f"{a.trades}", f"{b.trades}"),
        ("Win rate", f"{a.win_rate * 100:.1f}%", f"{b.win_rate * 100:.1f}%"),
        ("Total PnL", f"{a.total_pnl:,.2f}", f"{b.total_pnl:,.2f}"),
        ("Charges", charges_a, charges_b),
        ("Avg R (per trade)", f"{a.avg_r:.2f}", f"{b.avg_r:.2f}"),
        ("R on risk", f"{a.r_on_risk:.2f}", f"{b.r_on_risk:.2f}"),
        ("Payoff", f"{a.payoff:.2f}", f"{b.payoff:.2f}"),
        ("Expectancy", f"{a.expectancy:,.2f}", f"{b.expectancy:,.2f}"),
        ("t (days)", "n/a" if a.evidence_t is None else f"{a.evidence_t:.1f}",
                      "n/a" if b.evidence_t is None else f"{b.evidence_t:.1f}"),
        ("Max DD (closed)", f"{a.max_drawdown:,.2f}", f"{b.max_drawdown:,.2f}"),
        ("Max DD (equity)", dd_equity_a, dd_equity_b),
        ("AI rejects", f"{a.ai_rejections}", f"{b.ai_rejections}"),
    ]
    out = [f"{'Metric':<16} {'A: ' + a.run_id:>18} {'B: ' + b.run_id:>18}"]
    out += [f"{m:<16} {va:>18} {vb:>18}" for m, va, vb in metrics]
    if a.charges_estimated or b.charges_estimated:
        out.append("(est.) charges were not recorded for that run; its Total PnL, R figures and "
                    "closed drawdown use estimated charges")
    return out


def format_compare(c: Compare) -> str:
    lines = [f"Compare  A={c.a.run_id} (unfiltered)  vs  B={c.b.run_id} (Claude filter)"]
    lines += [f"WARNING: {w}" for w in c.warnings]
    lines += [""] + _side_by_side(c.a, c.b)
    n = c.rejected
    lines += ["",
              f"Rejected by Claude      {n} signal{'' if n == 1 else 's'}: {c.rejected_closed_in_a} closed in A, "
              f"{c.rejected_open_in_a} still open in A, {n - c.rejected_closed_in_a - c.rejected_open_in_a} unfilled in A",
              f"  losses avoided        {c.rejected_losses_avoided:,.2f}",
              f"  wins forgone          {c.rejected_wins_forgone:,.2f}",
              f"  net PnL of rejected   {c.rejected_pnl_in_a:,.2f}   (positive = the filter cost money in A's terms)",
              f"Total PnL delta (B-A)   {c.total_pnl_delta:,.2f}   (includes what B did with the freed capital)",
              f"Calls / failures        {c.calls} / {c.failures}   avg latency {c.avg_latency_ms:.0f} ms",
              f"Tokens in/out/cached    {c.input_tokens:,} / {c.output_tokens:,} / {c.cache_read_tokens:,} (cache writes {c.cache_write_tokens:,})",
              f"Estimated cost          ${c.est_cost_usd:,.2f}   (USD; PnL is in INR, judge the two separately)"]
    if c.total_pnl_delta > 0 and c.rejected_pnl_in_a <= 0:
        verdict = "filter helped: B earned more and the signals it removed were net losers in A"
    elif c.total_pnl_delta > 0:
        verdict = "mixed: B earned more overall, but the rejected signals were net winners in A"
    elif c.rejected_pnl_in_a < 0:
        verdict = "mixed: the rejected signals were net losers in A, yet B did not earn more overall"
    else:
        verdict = "filter did not help on this window"
    lines += ["", f"Verdict: {verdict}"]
    if c.rows:
        lines += ["", f"{'Bar (IST)':<25} {'Symbol':<12} {'Dir':<5} {'PnL in A':>10} {'Exit A':<11} Reason"]
        for r in c.rows:
            pnl = r["status"] if r["pnl_in_a"] is None else f"{r['pnl_in_a']:,.2f}"
            lines.append(f"{r['bar']:<25} {r['symbol']:<12} {r['direction']:<5} {pnl:>10} {str(r['exit_in_a'] or '-'):<11} "
                         f"{r['reason']}")
    return "\n".join(lines)
