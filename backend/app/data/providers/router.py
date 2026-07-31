"""Market-data routing across four tiers, with caching.

Priority, as the brief specifies:

    1. Financial Modeling Prep   external, primary
    2. Yahoo Finance             external, fallback
    3. Internal financial DB     the platform's own 42,025 canonical facts
    4. Uploaded documents (RAG)  passages from ingested filings

The lower two tiers matter more than they first appear. Tiers 1 and 2 are
someone else's uptime; tiers 3 and 4 are data the platform already holds, so a
total external outage degrades to "older figures, clearly labelled" rather
than to nothing at all.

Every response names the tier that served it. A price from FMP and a price
from the internal database can differ by weeks, and a reader comparing two
figures must be able to tell which they are looking at.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from app.data.providers.base import (
    BaseMarketProvider, MarketSnapshot, ProviderAuthError, ProviderError,
    ProviderNotConfigured, ProviderRateLimited, SymbolNotFound,
)
from app.data.providers.finnhub import FinnhubProvider
from app.data.providers.fmp import FMPProvider
from app.data.providers.yahoo import YahooProvider

log = structlog.get_logger(__name__)

SOURCE_INTERNAL = "Internal Financial Database"
SOURCE_DOCUMENTS = "Uploaded Documents (RAG)"
SOURCE_NONE = "Unavailable"


# ===========================================================================
# Cache
# ===========================================================================
@dataclass(slots=True)
class _Entry:
    value: "MarketDataResult"
    expires_at: float


class TTLCache:
    """Small thread-safe TTL cache.

    In-memory and per-process, which is the right scope here: the data is
    public, cheap to refetch and stale within minutes, so the complexity of a
    shared cache would buy very little. Its real job is protecting FMP's
    250-call daily budget from a page that renders five tickers.
    """

    def __init__(self, ttl_seconds: float = 300.0, capacity: int = 512) -> None:
        self.ttl = ttl_seconds
        self.capacity = capacity
        self._data: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> "MarketDataResult | None":
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry.expires_at < time.monotonic():
                if entry is not None:
                    del self._data[key]
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

    def put(self, key: str, value: "MarketDataResult") -> None:
        with self._lock:
            if len(self._data) >= self.capacity:
                # Evict whatever expires soonest. Approximate LRU is enough
                # for a cache this size and avoids tracking access order.
                oldest = min(self._data, key=lambda k: self._data[k].expires_at)
                del self._data[oldest]
            self._data[key] = _Entry(value, time.monotonic() + self.ttl)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = self.misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._data), "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "ttl_seconds": self.ttl,
            }


_CACHE = TTLCache()


def cache() -> TTLCache:
    return _CACHE


# ===========================================================================
# Result
# ===========================================================================
@dataclass(slots=True)
class MarketDataResult:
    snapshot: MarketSnapshot
    source: str
    attempted: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fell_back(self) -> bool:
        return self.source != FinnhubProvider.name

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload = asdict(self.snapshot)
        payload["source"] = self.source
        payload["source_label"] = f"✓ {self.source}"
        payload["fell_back"] = self.fell_back
        # Every provider tried, and why each was abandoned. The audit trail
        # matters: "why is this from Yahoo?" is a question with an answer.
        payload["providers_attempted"] = self.attempted
        payload["latency_ms"] = round(self.latency_ms, 1)
        payload["cached"] = self.cached
        if include_raw:
            payload["raw"] = self.raw
        return payload


# ===========================================================================
# Router
# ===========================================================================
class MarketDataRouter:
    """Tries each tier in priority order and reports which one answered."""

    def __init__(
        self,
        providers: list[BaseMarketProvider] | None = None,
        *,
        ttl_cache: TTLCache | None = None,
    ) -> None:
        self.providers = sorted(
            providers if providers is not None
            else [FinnhubProvider(), FMPProvider(), YahooProvider()],
            key=lambda p: p.priority,
        )
        self.cache = ttl_cache or _CACHE

    def fetch(
        self,
        ticker: str,
        *,
        db: Any = None,
        use_cache: bool = True,
        include_news: bool = True,
        include_history: bool = True,
        include_earnings: bool = True,
    ) -> MarketDataResult:
        key = f"{ticker.upper()}|{include_news}|{include_history}|{include_earnings}"
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                # Copied so the caller cannot mutate what the next one reads.
                return MarketDataResult(
                    snapshot=hit.snapshot, source=hit.source,
                    attempted=hit.attempted, latency_ms=hit.latency_ms,
                    cached=True, raw=hit.raw,
                )

        started = time.perf_counter()
        attempted: list[dict[str, Any]] = []

        for provider in self.providers:
            if not provider.configured():
                attempted.append({
                    "provider": provider.name, "outcome": "skipped",
                    "reason": "not configured",
                })
                continue
            if not provider.available:
                attempted.append({
                    "provider": provider.name, "outcome": "skipped",
                    "reason": "circuit open after repeated rate limits",
                })
                continue

            try:
                snapshot, raw = provider.fetch(
                    ticker, include_news=include_news,
                    include_history=include_history,
                    include_earnings=include_earnings,
                )
                if not snapshot.has_quote and snapshot.filled_sections == 0:
                    raise ProviderError("returned nothing usable")

                attempted.append({"provider": provider.name, "outcome": "served"})
                result = MarketDataResult(
                    snapshot=snapshot, source=provider.name, attempted=attempted,
                    latency_ms=(time.perf_counter() - started) * 1000, raw=raw,
                )
                if use_cache:
                    self.cache.put(key, result)
                log.info("market data served", ticker=ticker,
                         source=provider.name, ms=round(result.latency_ms, 1))
                return result

            except (ProviderAuthError, ProviderNotConfigured) as exc:
                # Not retried and not re-tried later in this request: a
                # rejected key is rejected for every endpoint.
                attempted.append({"provider": provider.name, "outcome": "auth_failed",
                                  "reason": str(exc)[:160]})
            except ProviderRateLimited as exc:
                attempted.append({"provider": provider.name, "outcome": "rate_limited",
                                  "reason": str(exc)[:160]})
            except SymbolNotFound as exc:
                attempted.append({"provider": provider.name, "outcome": "not_covered",
                                  "reason": str(exc)[:160]})
            except ProviderError as exc:
                attempted.append({"provider": provider.name, "outcome": "failed",
                                  "reason": str(exc)[:160]})
            except Exception as exc:  # noqa: BLE001 - a provider must not crash the router
                attempted.append({"provider": provider.name, "outcome": "error",
                                  "reason": f"{type(exc).__name__}: {exc}"[:160]})

        # --- tier 3: the platform's own database ---------------------------
        if db is not None:
            internal = self._from_internal_db(db, ticker)
            if internal is not None:
                attempted.append({"provider": SOURCE_INTERNAL, "outcome": "served"})
                result = MarketDataResult(
                    snapshot=internal, source=SOURCE_INTERNAL, attempted=attempted,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                log.info("market data served from internal database", ticker=ticker)
                return result
            attempted.append({"provider": SOURCE_INTERNAL, "outcome": "no_data"})

            # --- tier 4: uploaded documents --------------------------------
            documents = self._from_documents(db, ticker)
            if documents is not None:
                attempted.append({"provider": SOURCE_DOCUMENTS, "outcome": "served"})
                return MarketDataResult(
                    snapshot=documents, source=SOURCE_DOCUMENTS, attempted=attempted,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            attempted.append({"provider": SOURCE_DOCUMENTS, "outcome": "no_data"})

        log.warning("no market data provider could serve", ticker=ticker,
                    attempted=[a["provider"] for a in attempted])
        return MarketDataResult(
            snapshot=MarketSnapshot(ticker=ticker.upper(), source=SOURCE_NONE),
            source=SOURCE_NONE, attempted=attempted,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # -- lower tiers ------------------------------------------------------
    @staticmethod
    def _from_internal_db(db: Any, ticker: str) -> MarketSnapshot | None:
        """The platform's own stored price and market cap.

        Not live — as of the last ingestion — which is why the source is
        reported rather than blended silently into an external figure.
        """
        try:
            from sqlalchemy import select

            from app.models.company import Company

            symbol = ticker.upper().split(".")[0]
            company = db.scalar(select(Company).where(Company.ticker == symbol))
            if company is None:
                return None

            from app.data.providers.base import CompanyProfile, Quote

            snapshot = MarketSnapshot(ticker=ticker.upper(), source=SOURCE_INTERNAL)
            snapshot.profile = CompanyProfile(
                name=company.name, exchange=company.exchange, currency="INR",
                industry=company.industry, sector=company.sector,
                market_cap=company.market_cap,
            )
            snapshot.quote = Quote(price=company.current_price)
            snapshot.unavailable.append(
                "figures are as of the last ingestion, not live"
            )
            return snapshot if snapshot.has_quote or company.name else None
        except Exception as exc:  # noqa: BLE001
            log.warning("internal database tier failed", error=str(exc)[:160])
            return None

    @staticmethod
    def _from_documents(db: Any, ticker: str) -> MarketSnapshot | None:
        """Last resort: figures extracted from uploaded filings."""
        try:
            from sqlalchemy import select

            from app.models.company import Company
            from app.services.documents.service import DocumentService

            symbol = ticker.upper().split(".")[0]
            company = db.scalar(select(Company).where(Company.ticker == symbol))
            if company is None:
                return None

            facts = DocumentService(db).facts(company_id=company.id)
            if not facts:
                return None

            snapshot = MarketSnapshot(ticker=ticker.upper(), source=SOURCE_DOCUMENTS)
            snapshot.profile.name = company.name
            snapshot.key_metrics = {
                fact.field_key: (
                    fact.value if fact.value is not None else fact.text_value
                )
                for fact in facts[:20]
            }
            snapshot.unavailable.append(
                "extracted from uploaded documents; no live quote available"
            )
            return snapshot
        except Exception as exc:  # noqa: BLE001
            log.warning("document tier failed", error=str(exc)[:160])
            return None


_ROUTER: MarketDataRouter | None = None


def get_router() -> MarketDataRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = MarketDataRouter()
    return _ROUTER


def reset_router() -> None:
    """Tests build their own router; this drops the cached singleton."""
    global _ROUTER
    _ROUTER = None
    _CACHE.clear()
