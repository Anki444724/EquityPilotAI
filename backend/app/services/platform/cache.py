"""Unified cache for the expensive read paths.

Phase 2 caches four things the brief names — market data, financial
statements, news and RAG retrieval — behind one interface with two backends,
mirroring `rate_limit.py` so the platform has one caching idiom rather than
four ad-hoc ones.

* **memory** — a per-process TTL map. Correct for a single Railway instance
  and free of any dependency.
* **redis** — the same entries as Redis keys with TTLs, so N replicas share
  one cache and a redeploy does not start cold.

Redis failure falls back to memory rather than raising. A cache is an
optimisation, and an optimisation that can take the product down is a defect;
the worst a broken cache may do is make the platform as slow as it was before
the cache existed.

**What is deliberately not cached here.** LLM completions are cached by
`ProviderRouter` on the exact request, and duplicating that at this layer
would produce two caches with different keys and different TTLs disagreeing
about the same answer. Each thing is cached in exactly one place.
"""
from __future__ import annotations

import hashlib
import json
import pickle  # noqa: S403 - see _serialise
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, TypeVar

from app.core.config import settings
from app.services.platform.observability import get_logger

log = get_logger("ierp.cache")

T = TypeVar("T")


class Namespace(StrEnum):
    """The cacheable read paths, each with its own TTL.

    Separate namespaces rather than one flat keyspace so a single family can
    be invalidated — reindexing a company's documents must drop its retrieval
    entries without discarding every price in the process.
    """

    MARKET_DATA = "market"
    STATEMENTS = "statements"
    NEWS = "news"
    RAG = "rag"


#: Default lifetime per namespace, in seconds.
#:
#: These differ by an order of magnitude because the underlying data does.
#: A quote is stale in minutes; an annual filing is stale in a year. Using one
#: TTL for all four would either serve stale prices or re-query the statement
#: tables on every page load, and both were observable before this existed.
DEFAULT_TTLS: dict[Namespace, int] = {
    # Live prices. Also protects FMP's 250-call daily free budget from a page
    # that renders several tickers.
    Namespace.MARKET_DATA: 300,
    # Filed statements change when a company reports, four times a year at
    # most, and the ingestion path invalidates explicitly when they do.
    Namespace.STATEMENTS: 3_600,
    # Headlines move faster than fundamentals but not faster than quotes.
    Namespace.NEWS: 900,
    # Retrieval over an immutable corpus is deterministic, so the only reason
    # to expire at all is to bound memory and to pick up newly-ingested
    # documents that did not invalidate explicitly.
    Namespace.RAG: 1_800,
}


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


@dataclass
class CacheStats:
    """Per-namespace accounting, so a hit rate can be attributed."""

    hits: int = 0
    misses: int = 0
    sets: int = 0
    errors: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits, "misses": self.misses, "sets": self.sets,
            "errors": self.errors, "hit_rate": self.hit_rate,
        }


def make_key(namespace: Namespace, *parts: Any) -> str:
    """A stable key from arbitrary parts.

    Hashed because a RAG key contains a free-text question, which may be
    thousands of characters, may contain a newline, and would otherwise
    produce Redis keys that are unwieldy and occasionally invalid. The
    namespace stays in clear text so keys remain greppable in `redis-cli` and
    so a namespace can be scanned for invalidation.
    """
    payload = json.dumps(
        [str(p) for p in parts], sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"ierp:{namespace.value}:{digest}"


class MemoryCache:
    """In-process TTL map, bounded by eviction.

    Capacity matters: an unbounded cache keyed partly on user-supplied
    question text is a memory leak whose size the caller controls.
    """

    def __init__(self, capacity: int = 4_096) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self.capacity = capacity

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                del self._data[key]
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            if len(self._data) >= self.capacity and key not in self._data:
                self._evict()
            self._data[key] = _Entry(value, time.monotonic() + ttl)

    def _evict(self) -> None:
        """Drop the soonest-to-expire eighth. Called under the lock."""
        victims = sorted(self._data.items(), key=lambda kv: kv[1].expires_at)
        for key, _ in victims[: max(1, len(victims) // 8)]:
            self._data.pop(key, None)

    def delete_namespace(self, namespace: Namespace) -> int:
        prefix = f"ierp:{namespace.value}:"
        with self._lock:
            doomed = [k for k in self._data if k.startswith(prefix)]
            for key in doomed:
                del self._data[key]
        return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    @property
    def entries(self) -> int:
        return len(self._data)


class RedisCache:
    """Shared cache across replicas, degrading to memory on any failure."""

    def __init__(self, url: str, fallback: MemoryCache) -> None:
        import redis

        # A short timeout is essential. The cache sits in the request path, so
        # a hung Redis must cost milliseconds and fall through, not block the
        # response until the client gives up.
        self.client = redis.Redis.from_url(
            url, socket_timeout=0.25, socket_connect_timeout=0.25,
        )
        self.fallback = fallback

    def get(self, key: str) -> Any:
        try:
            raw = self.client.get(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis cache read failed", error=str(exc)[:160])
            return self.fallback.get(key)
        if raw is None:
            return None
        try:
            return _deserialise(raw)
        except Exception as exc:  # noqa: BLE001
            # A value written by an older build with an incompatible shape.
            # Dropping it is correct; failing the request over it is not.
            log.warning("cache value undecodable, discarding",
                        error=str(exc)[:160])
            try:
                self.client.delete(key)
            except Exception:  # noqa: BLE001
                pass
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        self.fallback.set(key, value, ttl)
        try:
            self.client.setex(key, ttl, _serialise(value))
        except Exception as exc:  # noqa: BLE001
            log.warning("redis cache write failed", error=str(exc)[:160])

    def delete_namespace(self, namespace: Namespace) -> int:
        count = self.fallback.delete_namespace(namespace)
        try:
            for found in self.client.scan_iter(f"ierp:{namespace.value}:*"):
                self.client.delete(found)
                count += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("redis namespace purge failed", error=str(exc)[:160])
        return count

    def clear(self) -> None:
        self.fallback.clear()
        try:
            for namespace in Namespace:
                for found in self.client.scan_iter(f"ierp:{namespace.value}:*"):
                    self.client.delete(found)
        except Exception:  # noqa: BLE001
            pass

    @property
    def entries(self) -> int:
        return self.fallback.entries


def _serialise(value: Any) -> bytes:
    """Encode for Redis.

    Pickle rather than JSON because the cached values are dataclasses —
    `MarketDataResult`, `SearchAnswer`, `CanonicalFinancials` — not plain
    dicts, and a JSON round trip would silently return a dict where the caller
    expects an object with properties.

    Pickle deserialisation is unsafe against untrusted input. It is acceptable
    here and only here: the Redis instance is private to the platform, on
    Railway's internal network, password-protected, and every value in it was
    written by this process. If that ever stops being true this must become a
    typed codec, and the docstring is the warning to whoever changes it.
    """
    return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)


def _deserialise(raw: bytes) -> Any:
    return pickle.loads(raw)  # noqa: S301 - see _serialise


class CacheService:
    """The façade every caller uses."""

    def __init__(self, backend: MemoryCache | RedisCache | None = None) -> None:
        self.backend = backend or MemoryCache()
        self.stats: dict[Namespace, CacheStats] = {
            n: CacheStats() for n in Namespace
        }
        self.enabled = True

    # ------------------------------------------------------------ core ops
    def get(self, namespace: Namespace, *parts: Any) -> Any:
        if not self.enabled:
            return None
        key = make_key(namespace, *parts)
        value = self.backend.get(key)
        if value is None:
            self.stats[namespace].misses += 1
        else:
            self.stats[namespace].hits += 1
        return value

    def set(self, namespace: Namespace, value: Any, *parts: Any,
            ttl: int | None = None) -> None:
        if not self.enabled or value is None:
            return
        key = make_key(namespace, *parts)
        # `ttl if ttl is not None`, not `ttl or …`: zero is a legitimate TTL
        # meaning "store but treat as immediately stale", and it is falsy, so
        # the obvious spelling silently substituted the namespace default.
        # Caught by a test that set ttl=0 and read the value back.
        self.backend.set(
            key, value, DEFAULT_TTLS[namespace] if ttl is None else ttl,
        )
        self.stats[namespace].sets += 1

    def get_or_set(
        self, namespace: Namespace, factory: Callable[[], T], *parts: Any,
        ttl: int | None = None,
    ) -> T:
        """Read through. The only method most callers need.

        Note that a miss computes `factory()` outside any lock, so two
        concurrent misses on the same key both compute. That is a deliberate
        trade: single-flight locking would serialise the parallel section
        generation this phase exists to enable, and the duplicated work is a
        database read, not a paid API call.
        """
        hit = self.get(namespace, *parts)
        if hit is not None:
            return hit
        value = factory()
        self.set(namespace, value, *parts, ttl=ttl)
        return value

    # --------------------------------------------------------- invalidation
    def invalidate(self, namespace: Namespace) -> int:
        """Drop one family. Called when its source of truth changes."""
        dropped = self.backend.delete_namespace(namespace)
        log.info("cache namespace invalidated",
                 namespace=namespace.value, dropped=dropped)
        return dropped

    def clear(self) -> None:
        self.backend.clear()
        for stat in self.stats.values():
            stat.hits = stat.misses = stat.sets = stat.errors = 0

    # -------------------------------------------------------------- report
    def snapshot(self) -> dict[str, Any]:
        totals = CacheStats()
        for stat in self.stats.values():
            totals.hits += stat.hits
            totals.misses += stat.misses
            totals.sets += stat.sets
        return {
            "backend": type(self.backend).__name__,
            "enabled": self.enabled,
            "entries": self.backend.entries,
            "overall": totals.as_dict(),
            "by_namespace": {
                n.value: {**s.as_dict(), "ttl_seconds": DEFAULT_TTLS[n]}
                for n, s in self.stats.items()
            },
        }


def _build() -> CacheService:
    memory = MemoryCache()
    if settings.REDIS_URL:
        try:
            return CacheService(RedisCache(settings.REDIS_URL, memory))
        except Exception as exc:  # noqa: BLE001
            log.warning("redis cache unavailable at startup, using memory",
                        error=str(exc)[:160])
    return CacheService(memory)


#: Process-wide cache.
cache = _build()
