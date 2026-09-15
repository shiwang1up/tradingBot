"""Prompt text for the Claude filter. Byte-stable: the system prompt never changes between
calls (so it caches), and the user message is deterministic JSON so (symbol, bar_ts,
sha256(prompt)) is a reliable cache key (spec 7)."""
from __future__ import annotations

import hashlib
import json

from tradebot.engine.clock import iso_ist, to_ist
from tradebot.types import Candidate

SYSTEM_PROMPT = """You review intraday equity trade candidates for an automated NSE strategy and decide, for each one, whether to approve or reject it.

Each candidate was produced by an EMA crossover confirmed by RSI, with a stop at an ATR multiple and a target at a reward-to-risk multiple. You receive the candidate's prices, indicator values and the most recent 5-minute candles (oldest first, times in IST). You do not receive news or any data outside these candles.

Reject a candidate when the setup is more likely noise than a move worth the stop:
- the recent candles are flat or choppy and the crossover is a wiggle rather than a change of direction
- the stop is inside the ordinary bar-to-bar range, so it will be hit by noise
- the candidate trades against the clear direction of the recent candles
- RSI is at an extreme after a stretch of near-identical closes, which means the indicator is stale rather than strong
- it is late in the session and there is little room for the target before square-off

Approve when the crossover follows a visible shift in direction with expanding ranges, the stop sits beyond the recent swing, and the target is plausible within the remaining session.

Be selective: approving everything is the same as having no reviewer. Give a one-sentence reason in plain words. Confidence is your probability that the trade reaches its target before its stop. Respond only with the JSON object described by the schema, one decision per candidate, keeping the candidate's index and symbol."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "symbol": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "symbol", "approve", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


def _hhmm(ts: int) -> str:
    return to_ist(ts).strftime("%H:%M")


def render_candidates(candidates: list[Candidate]) -> str:
    """Deterministic JSON for one bar's batch. Floats are rounded so equal inputs give equal bytes."""
    if not candidates:
        return json.dumps({"bar_time": None, "candidates": []}, sort_keys=True)
    bar_ts = candidates[0].signal.bar_ts
    items = []
    for i, c in enumerate(candidates):
        s = c.signal
        risk = abs(s.entry_price - s.stop_price)
        reward = abs(s.target_price - s.entry_price) if s.target_price is not None else None
        items.append({
            "index": i,
            "symbol": s.symbol,
            "direction": s.direction,
            "product": s.product,
            "entry": round(s.entry_price, 2),
            "stop": round(s.stop_price, 2),
            "target": None if s.target_price is None else round(s.target_price, 2),
            "stop_pct": round(risk / s.entry_price * 100, 3),
            "reward_risk": None if reward is None or risk == 0 else round(reward / risk, 2),
            "quantity": c.quantity,
            "notional": round(c.quantity * s.entry_price, 0),
            "indicators": {k: round(v, 4) for k, v in sorted(c.indicators.items())},
            "candles": [[_hhmm(cd.ts), round(cd.open, 2), round(cd.high, 2), round(cd.low, 2), round(cd.close, 2), cd.volume]
                        for cd in c.candles],
        })
    return json.dumps({"bar_time": iso_ist(bar_ts), "candidates": items}, sort_keys=True, separators=(",", ":"))


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n\n" + user).encode()).hexdigest()
