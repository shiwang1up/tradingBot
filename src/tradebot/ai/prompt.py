"""Prompt text for the Claude filter. Byte-stable: the system prompt never changes between
calls (so it caches), and the user message is deterministic JSON so (symbol, bar_ts,
sha256(prompt)) is a reliable cache key (spec 7). Session facts (square-off time, bars left)
travel in the user message, never in the system prompt, so the cached prefix stays fixed."""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from tradebot.engine.clock import iso_ist, to_ist
from tradebot.types import Candidate

SYSTEM_PROMPT = """You review intraday equity trade candidates for an automated NSE strategy and decide, for each one, whether to approve or reject it. Your review is the last check before an order goes to the exchange, so both kinds of mistake cost money: a rejected winner is a real loss, and an approved loser is a real loss. Neither approving everything nor rejecting everything is useful.

How the candidates arise. A fast EMA crossing a slow EMA, confirmed by RSI, produces a candidate. Its stop is an ATR multiple from the entry and its target is a reward-to-risk multiple of that stop distance. Entries fill at the next bar's open. Intraday (MIS) positions that reach neither level are squared off at the session's square-off time, which the message states.

What you receive. For each candidate: the symbol, direction (LONG or SHORT), entry, stop and target prices, the stop distance as a percent of price, the reward-to-risk ratio, the planned quantity and notional, indicator values, and recent 5-minute candles. The indicators are the strategy's own (ema_fast, ema_slow, rsi, atr) plus a shared technical set: trend is +1 when EMA20 is above EMA50 and -1 below; macd_hist is the 12/26/9 histogram and macd_turned is +1 or -1 on the bar it changed direction; mom_1, mom_5 and mom_60 are percent moves over those bar counts, with the 60-bar sign the anchor; adx with di_plus and di_minus, below 20 meaning no trend and above 25 trending; pct_b is the Bollinger %B with bb_upper and bb_lower, above 0.8 near the top band and below 0.2 near the bottom; volume_spike_pct is this bar's volume as a percent of the previous 20 bars' average, and a break without volume is suspect; bar_position is where the close sits in the bar's range from 0 to 1; support and resistance are the previous 100 bars' low and high, with breakout or breakdown set to 1 on the bar the close crossed them; engulfing is +1 bullish or -1 bearish; double_top and double_bottom are 1 when that pattern completed on this bar. Any of these can be null while its window is still filling. A high RSI together with a high pct_b is exhaustion, not strength. Each candle row is [time, open, high, low, close, volume] with the time as MM-DD HH:MM in IST, oldest first; a change of date between rows is an overnight gap, not an intraday move. The message also gives the session facts: the current bar time, the square-off time, the entry cutoff, and how many 5-minute bars remain before square-off. You do not receive news or any data outside these candles.

Approve a candidate unless one of these clearly applies:
- the recent candles are flat or choppy, so the crossover is a wiggle rather than a change of direction
- the stop is inside the ordinary bar-to-bar range of the recent candles, so noise alone will hit it
- the candidate trades against the clear direction of the recent candles
- RSI reads as extreme after a stretch of near-identical closes, which means the indicator is stale rather than strong
- too few bars remain before square-off for the target to be reached at the pace the candles show

Two worked examples of the judgment. A LONG at 1280 with a stop at 1275.5 and a target at 1289, after eight candles whose closes drifted between 1278 and 1282 with ranges of 4 to 6 points each: the stop sits inside the ordinary bar range and the crossover is a wiggle, so reject with a confidence around 0.25. A SHORT at 842 with a stop at 847 and a target at 832, after a run of falling closes with widening ranges, a bounce that failed below the prior high, and 40 bars left: the direction is clear, the stop is beyond the failed bounce, and the target fits the pace, so approve with a confidence around 0.45.

Confidence is your probability, between 0 and 1, that the trade reaches its target before its stop or the square-off. A trade with reward-to-risk R is worth taking when that probability exceeds 1 / (1 + R): about 0.33 at R = 2. Let the approve decision follow from that comparison rather than from a 0.5 threshold. Give a one-sentence reason in plain words. Return one decision per candidate, keeping the candidate's index and symbol."""

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
                    "confidence": {"type": "number"},  # the API rejects minimum/maximum here; the prompt and _clamp bound it
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

_JSON = {"sort_keys": False, "separators": (",", ":")}


def _stamp(ts: int) -> str:
    """MM-DD HH:MM in IST: the date is what lets the model see an overnight gap in the window."""
    return to_ist(ts).strftime("%m-%d %H:%M")


def render_candidates(candidates: list[Candidate], session: Optional[dict] = None) -> str:
    """Deterministic JSON for one bar's batch. Key order is fixed by construction (trade parameters
    before the candle rows); indicators are sorted; floats are rounded so equal inputs give equal bytes.
    `session` is the dict of session facts (square_off, entry_cutoff, close, bars_left) or None."""
    if not candidates:
        return json.dumps({"bar_time": None, "session": session, "candidates": []}, **_JSON)
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
            "stop_pct": round(risk / s.entry_price * 100, 3) if s.entry_price else None,
            "reward_risk": None if reward is None or risk == 0 else round(reward / risk, 2),
            "quantity": c.quantity,
            "notional": int(round(c.quantity * s.entry_price)),
            "indicators": {k: (None if v is None else round(v, 4)) for k, v in sorted(c.indicators.items())},
            "candles": [[_stamp(cd.ts), round(cd.open, 2), round(cd.high, 2), round(cd.low, 2), round(cd.close, 2), cd.volume]
                        for cd in c.candles],
        })
    return json.dumps({"bar_time": iso_ist(bar_ts), "session": session, "candidates": items}, **_JSON)


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n\n" + user).encode()).hexdigest()
