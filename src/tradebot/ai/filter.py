"""AI filter implementations behind one interface (spec 7).

StubFilter approves everything (backtest default). ClaudeFilter makes one structured-output
request per bar for all candidates. CachedClaudeFilter serves decisions from the ai_cache table
when the exact prompt was seen before, so AI-replay backtests are free after the first run."""
from __future__ import annotations

import logging
import time
from typing import Optional, Protocol

from tradebot.ai.cache import AICache
from tradebot.ai.claude_client import ClaudeClient, ClaudeReviewError, ReviewResponse
from tradebot.ai.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, prompt_hash, render_candidates
from tradebot.config import AIConfig
from tradebot.engine.clock import SessionClock, date_of
from tradebot.types import Candidate, Decision

log = logging.getLogger("tradebot.ai")
REASON_MAX = 200
PROGRESS_EVERY = 50


class AIFilterAborted(RuntimeError):
    """The circuit breaker tripped: the run must stop rather than degrade into on_failure decisions."""


class AIFilter(Protocol):
    kind: str

    def review(self, candidates: list[Candidate]) -> list[Decision]:
        """Return exactly one Decision per candidate, in the same order, each carrying the
        candidate's own Signal. A length or order mismatch is a bug in the filter, never a
        way to express rejection: reject with approved=False instead. The engine checks this."""


class StubFilter:
    kind = "stub"

    def review(self, candidates: list[Candidate]) -> list[Decision]:
        return [Decision(c.signal, True, "stub", 1.0, self.kind) for c in candidates]


def _clamp(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, v))


class ClaudeFilter:
    kind = "claude"

    def __init__(self, cfg: AIConfig, client: ClaudeClient, cache: Optional[AICache] = None,
                 clock: Optional[SessionClock] = None):
        self.cfg = cfg
        self.client = client
        self.cache = cache
        self.clock = clock  # supplies the session facts the prompt's "bars left" rule needs
        self.calls = 0
        self.consecutive_failures = 0
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.latency_total_ms = 0

    def _session(self, bar_ts: int) -> Optional[dict]:
        if self.clock is None:
            return None
        sq = self.clock.square_off_bar_ts(date_of(bar_ts))
        return {"square_off": self.clock.session.square_off, "entry_cutoff": self.clock.session.no_new_entries_after,
                "close": self.clock.session.close, "bars_left": max(0, (sq - bar_ts) // self.clock.interval_sec)}

    # -- interface --------------------------------------------------------------------------
    def review(self, candidates: list[Candidate]) -> list[Decision]:
        if not candidates:
            return []
        user = render_candidates(candidates, self._session(candidates[0].signal.bar_ts))
        # The key covers model and effort too: a replay with a different model must miss the cache.
        h = prompt_hash(f"{self.cfg.model}|{self.cfg.effort}|{SYSTEM_PROMPT}", user)
        if self.cache is not None:
            cached = [self.cache.get(c.signal.symbol, c.signal.bar_ts, h) for c in candidates]
            if all(d is not None for d in cached):
                return [self._from_dict(c, d, None, i) for i, (c, d) in enumerate(zip(candidates, cached))]
        if self.calls >= self.cfg.max_calls_per_run:
            return self._failed(candidates, f"max_calls_per_run ({self.cfg.max_calls_per_run}) reached")
        self.calls += 1
        t0 = time.monotonic()
        try:
            resp = self.client.review(SYSTEM_PROMPT, user, RESPONSE_SCHEMA)
        except ClaudeReviewError as e:
            self._note_failure(str(e))
            return self._failed(candidates, str(e), latency_ms=int((time.monotonic() - t0) * 1000))
        self._account(resp, len(candidates))
        by_index = {}
        for d in resp.data.get("decisions", []) if isinstance(resp.data, dict) else []:
            if isinstance(d, dict) and isinstance(d.get("index"), int) and not isinstance(d.get("index"), bool):
                by_index[d["index"]] = d
        out = []
        matched = 0
        for i, c in enumerate(candidates):
            d = by_index.get(i)
            if d is None or d.get("symbol") != c.signal.symbol or not isinstance(d.get("approve"), bool):
                out.append(self._failed([c], f"no usable decision for index {i} {c.signal.symbol}", resp, i)[0])
                continue
            matched += 1
            out.append(self._from_dict(c, d, resp, i))
            if self.cache is not None:
                # One entry per (symbol, bar) is safe: the risk engine rejects a second signal for a symbol
                # that already has a pending entry, so a batch never holds the same symbol twice.
                self.cache.put(c.signal.symbol, c.signal.bar_ts, h, d)
        if matched == 0:
            self._note_failure("response matched no candidate")  # a live API returning garbage counts too
        else:
            self.consecutive_failures = 0
        return out

    def _account(self, resp: ReviewResponse, n: int) -> None:
        """Running totals plus a per-call DEBUG line (request id for support) and a progress line
        every 50 calls so a multi-hour paid replay shows it is alive and what it has spent."""
        self.tokens["input"] += resp.input_tokens
        self.tokens["output"] += resp.output_tokens
        self.tokens["cache_read"] += resp.cache_read_tokens
        self.tokens["cache_write"] += resp.cache_write_tokens
        self.latency_total_ms += resp.latency_ms
        log.debug("claude call %d: %d candidates, %d ms, in=%d out=%d cached=%d req=%s", self.calls, n,
                  resp.latency_ms, resp.input_tokens, resp.output_tokens, resp.cache_read_tokens, resp.request_id)
        if self.calls % PROGRESS_EVERY == 0:
            log.info("claude filter progress: %d calls, tokens in=%d out=%d cached=%d, avg %d ms/call",
                     self.calls, self.tokens["input"], self.tokens["output"], self.tokens["cache_read"],
                     self.latency_total_ms // self.calls)

    def _note_failure(self, why: str) -> None:
        self.consecutive_failures += 1
        log.warning("claude review failed (%d in a row): %s", self.consecutive_failures, why)
        if self.consecutive_failures >= self.cfg.max_consecutive_failures:
            # Circuit breaker: a dead API must not quietly turn a whole replay into on_failure decisions.
            raise AIFilterAborted(f"Claude filter: {self.consecutive_failures} consecutive failures, last: {why}")

    # -- helpers ----------------------------------------------------------------------------
    def _from_dict(self, c: Candidate, d: dict, resp: Optional[ReviewResponse], i: int) -> Decision:
        usage = resp if (resp is not None and i == 0) else None  # batch usage attributed to decision 0
        return Decision(
            c.signal, bool(d["approve"]), str(d.get("reason") or "")[:REASON_MAX], _clamp(d.get("confidence")),
            self.kind, latency_ms=resp.latency_ms if resp is not None else 0, failure=None,
            input_tokens=usage.input_tokens if usage else 0, output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
        )

    def _failed(self, candidates: list[Candidate], why: str, resp: Optional[ReviewResponse] = None,
                first_index: int = 0, latency_ms: int = 0) -> list[Decision]:
        """on_failure decisions. `latency_ms` carries the wall time of a failed call (a timeout is the
        slowest event and must enter the average); a spend-guard rejection has none."""
        approved = self.cfg.on_failure == "pass_through"
        out = []
        for i, c in enumerate(candidates):
            usage = resp if (resp is not None and first_index + i == 0) else None
            out.append(Decision(
                c.signal, approved, f"ai_failure: {why}"[:REASON_MAX], 0.0, self.kind,
                latency_ms=resp.latency_ms if resp is not None else latency_ms, failure=why[:REASON_MAX],
                input_tokens=usage.input_tokens if usage else 0, output_tokens=usage.output_tokens if usage else 0,
                cache_read_tokens=usage.cache_read_tokens if usage else 0,
                cache_write_tokens=usage.cache_write_tokens if usage else 0,
            ))
        return out


class CachedClaudeFilter(ClaudeFilter):
    kind = "claude_cached"

    def __init__(self, cfg: AIConfig, client: ClaudeClient, cache: AICache, clock: Optional[SessionClock] = None):
        super().__init__(cfg, client, cache, clock)


def build_filter(cfg: AIConfig, api_key: str, repo=None, clock: Optional[SessionClock] = None) -> AIFilter:
    if cfg.filter == "stub":
        return StubFilter()
    if cfg.filter not in ("claude", "claude_cached"):
        raise ValueError(f"unknown ai.filter '{cfg.filter}'")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY must be set in .env to use the Claude filter")
    client = ClaudeClient(api_key, cfg.model, cfg.effort, cfg.max_tokens, cfg.timeout_sec)
    if cfg.filter == "claude":
        return ClaudeFilter(cfg, client, clock=clock)
    if repo is None:
        raise ValueError("claude_cached needs a repo for the ai_cache table")
    return CachedClaudeFilter(cfg, client, AICache(repo), clock)
