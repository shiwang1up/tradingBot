"""AI filter interface. Plan 1 ships only the stub; ClaudeFilter and the cache arrive in Plan 2."""
from __future__ import annotations

from typing import Protocol

from tradebot.config import AIConfig
from tradebot.types import Candidate, Decision


class AIFilter(Protocol):
    kind: str

    def review(self, candidates: list[Candidate]) -> list[Decision]:
        """Return one Decision per candidate, in the same order."""


class StubFilter:
    kind = "stub"

    def review(self, candidates: list[Candidate]) -> list[Decision]:
        return [Decision(c.signal, True, "stub", 1.0, self.kind) for c in candidates]


def build_filter(cfg: AIConfig, api_key: str) -> AIFilter:
    if cfg.filter == "stub":
        return StubFilter()
    raise ValueError(f"ai.filter '{cfg.filter}' is not available yet (Plan 2 adds claude filters)")
