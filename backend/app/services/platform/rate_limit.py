"""Rate-limit storage.

`domain/platform/limits.sliding_window` decides; this supplies the counters.
Two backends behind one interface:

* **memory** — a dict of two-window counters. Correct for a single process,
  which is what a Railway deployment of one instance is. Not shared across
  replicas, and honest about that.
* **redis** — the same two counters as Redis keys with TTLs, so N replicas
  enforce one limit rather than N.

The window arithmetic lives in the domain module and is identical for both, so
the two backends cannot disagree about what the limit means — only about who
can see the count.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.core.config import settings
from app.domain.platform.limits import (
    DEFAULT_RULES, RateDecision, RateRule, RateScope, sliding_window,
)
from app.services.platform.observability import get_logger

log = get_logger("ierp.ratelimit")


@dataclass
class _Windows:
    """The previous and current window counts for one key."""

    window_index: int = 0
    current: int = 0
    previous: int = 0


class MemoryRateLimiter:
    """In-process sliding window.

    Bounded by eviction rather than by a TTL sweep: when the map exceeds
    capacity the oldest quarter is dropped. An unbounded rate-limiter map is a
    memory leak that an attacker controls the size of, simply by varying their
    source address.
    """

    def __init__(self, capacity: int = 20_000) -> None:
        self._data: dict[str, _Windows] = {}
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self.capacity = capacity

    def check(self, key: str, rule: RateRule, *, consume: bool = True) -> RateDecision:
        now = time.time()
        window = rule.window_seconds
        index = int(now // window)
        elapsed = now - (index * window)

        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                entry = _Windows(window_index=index)
                self._data[key] = entry
                if len(self._data) > self.capacity:
                    self._evict()

            if index != entry.window_index:
                # Rolled. One window on keeps the previous count; more than
                # that and both are stale, so start clean.
                entry.previous = entry.current if index == entry.window_index + 1 else 0
                entry.current = 0
                entry.window_index = index

            decision = sliding_window(
                rule=rule,
                previous_count=entry.previous,
                current_count=entry.current,
                elapsed_in_window=elapsed,
            )
            if decision.allowed and consume:
                entry.current += 1
            self._seen[key] = now

        return decision

    def _evict(self) -> None:
        """Drop the least recently seen quarter. Called under the lock."""
        victims = sorted(self._seen.items(), key=lambda kv: kv[1])
        for key, _ in victims[: max(1, len(victims) // 4)]:
            self._data.pop(key, None)
            self._seen.pop(key, None)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
                self._seen.clear()
            else:
                self._data.pop(key, None)
                self._seen.pop(key, None)

    @property
    def tracked_keys(self) -> int:
        return len(self._data)


class RedisRateLimiter:
    """Shared window across replicas.

    Falls back to the in-memory limiter if Redis is unreachable. Failing open
    is the deliberate choice: an unavailable rate limiter should not take the
    product down. It is a control, not the product.
    """

    def __init__(self, url: str, fallback: MemoryRateLimiter) -> None:
        import redis

        self.client = redis.Redis.from_url(url, socket_timeout=0.25, decode_responses=True)
        self.fallback = fallback

    def check(self, key: str, rule: RateRule, *, consume: bool = True) -> RateDecision:
        now = time.time()
        window = rule.window_seconds
        index = int(now // window)
        elapsed = now - (index * window)

        current_key = f"rl:{key}:{index}"
        previous_key = f"rl:{key}:{index - 1}"

        try:
            pipe = self.client.pipeline()
            pipe.get(previous_key)
            pipe.get(current_key)
            previous_raw, current_raw = pipe.execute()

            decision = sliding_window(
                rule=rule,
                previous_count=int(previous_raw or 0),
                current_count=int(current_raw or 0),
                elapsed_in_window=elapsed,
            )
            if decision.allowed and consume:
                pipe = self.client.pipeline()
                pipe.incr(current_key)
                # Two windows of TTL so the previous count survives long
                # enough to be read at the start of the next window.
                pipe.expire(current_key, window * 2)
                pipe.execute()
            return decision
        except Exception as exc:  # noqa: BLE001
            log.warning("redis rate limiter unavailable, using memory", error=str(exc))
            return self.fallback.check(key, rule, consume=consume)

    def reset(self, key: str | None = None) -> None:
        self.fallback.reset(key)
        if key is None:
            return
        try:
            for found in self.client.scan_iter(f"rl:{key}:*"):
                self.client.delete(found)
        except Exception:  # noqa: BLE001
            pass


def _build():
    memory = MemoryRateLimiter()
    if settings.RATE_LIMIT_BACKEND == "redis" and settings.REDIS_URL:
        try:
            return RedisRateLimiter(settings.REDIS_URL, memory)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis unavailable at startup, using memory", error=str(exc))
    return memory


#: Process-wide limiter.
limiter = _build()


def rule_for(name: str) -> RateRule:
    return DEFAULT_RULES.get(name, DEFAULT_RULES["default"])


def key_for(
    rule_name: str,
    *,
    scope: RateScope,
    identifier: str,
) -> str:
    """Compose the counter key.

    The rule name is part of the key, so the login limit and the general
    per-user limit count separately. Without it, ten failed logins would eat
    into the same budget as ten dashboard loads.
    """
    return f"{rule_name}:{scope.value}:{identifier}"


def check(
    rule_name: str,
    identifier: str,
    *,
    scope: RateScope | None = None,
    override: RateRule | None = None,
    consume: bool = True,
) -> RateDecision:
    """The one call site the middleware and the dependencies use."""
    rule = override or rule_for(rule_name)
    key = key_for(rule_name, scope=scope or rule.scope, identifier=identifier)
    return limiter.check(key, rule, consume=consume)


def plan_rule(requests_per_minute: int) -> RateRule:
    """Turn a plan's `rate_limit_per_minute` limit into a rule.

    A burst of a fifth of the minute allowance absorbs a dashboard's
    simultaneous fan-out without letting a script sustain a higher rate.
    """
    return RateRule(
        scope=RateScope.TENANT,
        limit=max(1, requests_per_minute),
        window_seconds=60,
        burst=max(5, requests_per_minute // 5),
    )
