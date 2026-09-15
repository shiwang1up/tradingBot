import hashlib
import json

from tradebot.ai.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, prompt_hash, render_candidates
from tradebot.types import Candidate, Candle, Signal


def _cand(sym="RELIANCE", bar_ts=1789357500, direction="LONG"):
    sig = Signal("ema_rsi", sym, direction, 1280.0, 1275.5, 1289.0, "MIS", bar_ts)
    candles = tuple(Candle(sym, bar_ts - 300 * (3 - i), 1279.0 + i, 1281.0 + i, 1278.0 + i, 1280.0 + i, 1000 * (i + 1))
                    for i in range(3))
    return Candidate(sig, 222, {"ema_fast": 1280.123456, "ema_slow": 1279.5, "rsi": 61.234567, "atr": 3.0}, candles)


def test_render_is_deterministic_and_compact():
    a = render_candidates([_cand(), _cand("TCS")])
    b = render_candidates([_cand(), _cand("TCS")])
    assert a == b
    data = json.loads(a)
    assert data["bar_time"] == "2026-09-14T09:15:00+05:30"
    c0 = data["candidates"][0]
    assert c0["index"] == 0 and c0["symbol"] == "RELIANCE" and c0["direction"] == "LONG"
    assert c0["entry"] == 1280.0 and c0["stop"] == 1275.5 and c0["target"] == 1289.0
    assert c0["stop_pct"] == 0.352 and c0["reward_risk"] == 2.0 and c0["quantity"] == 222
    assert c0["indicators"]["rsi"] == 61.2346 and c0["indicators"]["ema_fast"] == 1280.1235
    assert c0["candles"][0] == ["09:00", 1279.0, 1281.0, 1278.0, 1280.0, 1000]
    assert len(c0["candles"]) == 3


def test_render_order_follows_input_order():
    data = json.loads(render_candidates([_cand("TCS"), _cand("RELIANCE")]))
    assert [c["symbol"] for c in data["candidates"]] == ["TCS", "RELIANCE"]
    assert [c["index"] for c in data["candidates"]] == [0, 1]


def test_prompt_hash_is_sha256_of_system_and_user():
    user = render_candidates([_cand()])
    h = prompt_hash(SYSTEM_PROMPT, user)
    assert h == hashlib.sha256((SYSTEM_PROMPT + "\n\n" + user).encode()).hexdigest()
    assert h != prompt_hash(SYSTEM_PROMPT, render_candidates([_cand("TCS")]))


def test_response_schema_shape():
    assert RESPONSE_SCHEMA["type"] == "object" and RESPONSE_SCHEMA["additionalProperties"] is False
    item = RESPONSE_SCHEMA["properties"]["decisions"]["items"]
    assert set(item["required"]) == {"index", "symbol", "approve", "confidence", "reason"}
    assert item["additionalProperties"] is False


def test_system_prompt_is_stable_text():
    assert "approve" in SYSTEM_PROMPT and "reject" in SYSTEM_PROMPT
    assert "{" not in SYSTEM_PROMPT  # no formatting placeholders: it must be byte-stable for caching
