"""Decision cache keyed by (symbol, bar_ts, sha256(prompt)) so replays cost nothing (spec 7)."""
from __future__ import annotations

import json
import time
from typing import Optional

from tradebot.store.repo import Repo


class AICache:
    def __init__(self, repo: Repo):
        self.repo = repo

    def get(self, symbol: str, bar_ts: int, prompt_hash: str) -> Optional[dict]:
        raw = self.repo.get_ai_cache(symbol, bar_ts, prompt_hash)
        if not raw:
            return None
        try:
            d = json.loads(raw)
        except ValueError:
            return None  # a corrupt row is a miss, not a crash
        return d if isinstance(d, dict) else None

    def put(self, symbol: str, bar_ts: int, prompt_hash: str, decision: dict) -> None:
        self.repo.put_ai_cache(symbol, bar_ts, prompt_hash, json.dumps(decision, sort_keys=True), int(time.time()))
