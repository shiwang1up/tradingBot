# tests/test_ai_prompt.py
import hashlib
import json

from tradebot.ai.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, prompt_hash, render_candidates
from tradebot.types import Candidate, Candle, Signal

BAR = 1789357500  # 2026-09-14 09:15 IST


def _cand(sym="RELIANCE", bar_ts=BAR, direction="LONG", n=3):
    sig = Signal("ema_rsi", sym, direction, 1280.0, 1275.5, 1289.0, "MIS", bar_ts)
    candles = tuple(Candle(sym, bar_ts - 300 * (n - i), 1279.0 + i, 1281.0 + i, 1278.0 + i, 1280.0 + i, 1000 * (i + 1))
                    for i in range(n))
    return Candidate(sig, 222, {"rsi": 61.234567, "ema_fast": 1280.123456, "ema_slow": 1279.5, "atr": 3.0}, candles)


SESSION = {"square_off": "15:10", "entry_cutoff": "14:45", "close": "15:30", "bars_left": 70}


def test_render_is_deterministic_and_compact():
    a = render_candidates([_cand(), _cand("TCS")], SESSION)
    b = render_candidates([_cand(), _cand("TCS")], SESSION)
    assert a == b and ": " not in a and ", " not in a
    data = json.loads(a)
    assert data["bar_time"] == "2026-09-14T09:15:00+05:30" and data["session"] == SESSION
    c0 = data["candidates"][0]
    assert list(c0.keys())[:4] == ["index", "symbol", "direction", "product"] and list(c0.keys())[-1] == "candles"
    assert c0["index"] == 0 and c0["symbol"] == "RELIANCE" and c0["direction"] == "LONG"
    assert c0["entry"] == 1280.0 and c0["stop"] == 1275.5 and c0["target"] == 1289.0
    assert c0["stop_pct"] == 0.352 and c0["reward_risk"] == 2.0 and c0["quantity"] == 222 and c0["notional"] == 284160
    assert list(c0["indicators"]) == ["atr", "ema_fast", "ema_slow", "rsi"]  # sorted regardless of insertion order
    assert c0["indicators"]["rsi"] == 61.2346 and c0["indicators"]["ema_fast"] == 1280.1235
    assert c0["candles"][0] == ["09-14 09:00", 1279.0, 1281.0, 1278.0, 1280.0, 1000]
    assert len(c0["candles"]) == 3


def test_candle_rows_carry_the_date_so_overnight_gaps_are_visible():
    c = _cand(n=6)  # 09:15 with six prior 5-minute bars, but pretend the window spans a day boundary
    prev_day = tuple(Candle("RELIANCE", ts - 86400 * 3, 1, 2, 0.5, 1.5, 10) for ts in (BAR - 600, BAR - 300))
    c = Candidate(c.signal, c.quantity, c.indicators, prev_day + c.candles[-2:])
    rows = json.loads(render_candidates([c], SESSION))["candidates"][0]["candles"]
    assert rows[0][0].startswith("09-11") and rows[-1][0].startswith("09-14")


def test_render_order_follows_input_order():
    data = json.loads(render_candidates([_cand("TCS"), _cand("RELIANCE")], SESSION))
    assert [c["symbol"] for c in data["candidates"]] == ["TCS", "RELIANCE"]
    assert [c["index"] for c in data["candidates"]] == [0, 1]


def test_render_without_session_and_empty_batch():
    assert json.loads(render_candidates([_cand()]))["session"] is None
    assert render_candidates([]) == '{"bar_time":null,"session":null,"candidates":[]}'


def test_prompt_hash_is_sha256_of_system_and_user():
    user = render_candidates([_cand()], SESSION)
    h = prompt_hash(SYSTEM_PROMPT, user)
    assert h == hashlib.sha256((SYSTEM_PROMPT + "\n\n" + user).encode()).hexdigest()
    assert h != prompt_hash(SYSTEM_PROMPT, render_candidates([_cand("TCS")], SESSION))
    assert h != prompt_hash(SYSTEM_PROMPT + " ", user)  # a prompt edit misses the cache on purpose


def test_response_schema_shape():
    assert RESPONSE_SCHEMA["type"] == "object" and RESPONSE_SCHEMA["additionalProperties"] is False
    item = RESPONSE_SCHEMA["properties"]["decisions"]["items"]
    assert set(item["required"]) == {"index", "symbol", "approve", "confidence", "reason"}
    assert item["additionalProperties"] is False
    assert item["properties"]["confidence"] == {"type": "number"}  # no min/max: unsupported by structured outputs


def test_system_prompt_states_the_things_the_model_needs():
    for needle in ("[time, open, high, low, close, volume]", "MM-DD HH:MM", "overnight gap", "square-off",
                   "1 / (1 + R)", "between 0 and 1", "Approve a candidate unless"):
        assert needle in SYSTEM_PROMPT, needle
    assert len(SYSTEM_PROMPT.split()) >= 480, "must stay well above Opus 5's 512-token cacheable minimum"
