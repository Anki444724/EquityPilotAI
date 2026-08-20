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

from app.data.providers.symbols import ResolvedSymbol, resolve
from app.data.providers.base import (
    BaseMarketProvider, MarketSnapshot, ProviderAuthError, ProviderError,
    ProviderMetadata, ProviderNotConfigured, ProviderRateLimited,
    SymbolNotFound,
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


def _is_news_key(key: str) -> bool:
    """Did this fetch include news?

    The market cache key is `symbol|news|history|earnings`, so a snapshot that
    carried news is attributed to the NEWS namespace as well as MARKET_DATA.
    The brief lists them as separate cacheable things and they are fetched in
    one call, so the accounting reflects both rather than pretending news is
    uncached.
    """
    parts = key.split("|")
    return len(parts) > 1 and parts[1] == "True"


def _mirror_stat(*, hit: bool, news: bool) -> None:
    """Report market-cache activity through the unified stats.

    Wrapped in a try/except that swallows: this is telemetry, and telemetry
    must never be able to fail a market-data fetch.
    """
    try:
        from app.services.platform.cache import Namespace, cache

        namespaces = [Namespace.MARKET_DATA]
        if news:
            namespaces.append(Namespace.NEWS)
        for namespace in namespaces:
            stat = cache.stats[namespace]
            if hit:
                stat.hits += 1
            else:
                stat.misses += 1
    except Exception:  # noqa: BLE001 - telemetry is never load-bearing
        pass


class TTLCache:
    """Small thread-safe TTL cache for market snapshots.

    In-memory and per-process. Its real job is protecting FMP's 250-call daily
    budget from a page that renders five tickers.

    Phase 2 added a unified `CacheService`, and the obvious move was to
    replace this with it. That was not done, for a reason worth recording: a
    `MarketDataResult` holds the *provenance* of a fetch — which providers
    were attempted, which answered, the measured latency — and those fields
    are only meaningful for the request that produced them. Serving another
    caller's `attempted` list and calling it their own would corrupt the audit
    trail the market layer exists to provide, so the cached copy is rebuilt
    with `cached=True` and a fresh `resolved` rather than returned verbatim.

    What *is* shared is the accounting: hits and misses are mirrored into the
    unified cache stats, so `/health/cache` reports one hit rate across all
    four namespaces rather than making an operator find this class to learn
    how market caching is performing.
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
                _mirror_stat(hit=False, news=_is_news_key(key))
                return None
            self.hits += 1
            _mirror_stat(hit=True, news=_is_news_key(key))
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
    resolved: ResolvedSymbol | None = None

    @property
    def fell_back(self) -> bool:
        return self.source != FinnhubProvider.name

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        from app.data.providers.currency import format_money

        payload = asdict(self.snapshot)
        # Item 3: provenance on every response.
        payload["meta"] = self.snapshot.meta.as_dict()
        currency = self.snapshot.meta.currency
        cap = self.snapshot.profile.market_cap
        payload["profile"]["market_cap_display"] = format_money(cap, currency)
        payload["profile"]["currency"] = currency
        if self.resolved is not None:
            payload["symbol"] = self.resolved.as_dict()
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

    def _chain_for(self, resolved) -> list[str]:
        """Provider order for an explicit market-data request.

        A market endpoint is asking for current market data, so external
        providers must precede stored fundamentals for every market. The
        previous Indian-only order returned the internal row immediately and
        made the external branch unreachable whenever a company had a name or
        stored price. User-facing company pages do not use this blocking path;
        ``LiveMarketService`` serves cache/fallback and refreshes in the
        background.
        """
        return ["external", "internal", "documents"]

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
        resolved = resolve(ticker)
        key = (f"{resolved.canonical}|{include_news}|{include_history}"
               f"|{include_earnings}")
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                return MarketDataResult(
                    snapshot=hit.snapshot, source=hit.source,
                    attempted=hit.attempted, latency_ms=hit.latency_ms,
                    cached=True, raw=hit.raw, resolved=resolved,
                )

        started = time.perf_counter()
        attempted: list[dict[str, Any]] = []

        def finish(snapshot: MarketSnapshot, source: str,
                   raw: dict[str, Any] | None = None) -> MarketDataResult:
            elapsed = (time.perf_counter() - started) * 1000
            self._stamp(snapshot, resolved, source)
            result = MarketDataResult(
                snapshot=snapshot, source=source, attempted=attempted,
                latency_ms=elapsed, raw=raw or {}, resolved=resolved,
            )
            if use_cache:
                self.cache.put(key, result)
            log.info("market data served", ticker=resolved.canonical,
                     source=source, market=resolved.market,
                     ms=round(elapsed, 1))
            return result

        for tier in self._chain_for(resolved):
            if tier == "external":
                for provider in self.providers:
                    outcome = self._try(provider, resolved, attempted,
                                        include_news=include_news,
                                        include_history=include_history,
                                        include_earnings=include_earnings)
                    if outcome is not None:
                        snapshot, raw = outcome
                        return finish(snapshot, provider.name, raw)

            elif tier == "internal" and db is not None:
                internal = self._from_internal_db(db, resolved.base)
                if internal is not None:
                    attempted.append({"provider": SOURCE_INTERNAL,
                                      "outcome": "served"})
                    return finish(internal, SOURCE_INTERNAL)
                attempted.append({"provider": SOURCE_INTERNAL,
                                  "outcome": "no_data"})

            elif tier == "documents" and db is not None:
                documents = self._from_documents(db, resolved.base)
                if documents is not None:
                    attempted.append({"provider": SOURCE_DOCUMENTS,
                                      "outcome": "served"})
                    return finish(documents, SOURCE_DOCUMENTS)
                attempted.append({"provider": SOURCE_DOCUMENTS,
                                  "outcome": "no_data"})

        log.warning("no provider could serve", ticker=resolved.canonical,
                    attempted=[a["provider"] for a in attempted])
        empty = MarketSnapshot(ticker=resolved.canonical, source=SOURCE_NONE)
        self._stamp(empty, resolved, SOURCE_NONE)
        return MarketDataResult(
            snapshot=empty, source=SOURCE_NONE, attempted=attempted,
            latency_ms=(time.perf_counter() - started) * 1000,
            resolved=resolved,
        )

    def _try(self, provider, resolved, attempted: list, **kwargs):
        """One external provider. Returns None when the router should move on."""
        if not provider.configured():
            attempted.append({"provider": provider.name, "outcome": "skipped",
                              "reason": "not configured"})
            return None
        if not provider.available:
            attempted.append({"provider": provider.name, "outcome": "skipped",
                              "reason": "circuit open"})
            return None

        call_started = time.perf_counter()
        try:
            snapshot, raw = provider.fetch(resolved.canonical, **kwargs)
            if not snapshot.has_quote and snapshot.filled_sections == 0:
                raise ProviderError("returned nothing usable")
            provider.record(ok=True, ms=(time.perf_counter() - call_started) * 1000)
            attempted.append({"provider": provider.name, "outcome": "served"})
            return snapshot, raw
        except (ProviderAuthError, ProviderNotConfigured) as exc:
            outcome, reason = "auth_failed", str(exc)
        except ProviderRateLimited as exc:
            outcome, reason = "rate_limited", str(exc)
        except SymbolNotFound as exc:
            outcome, reason = "not_covered", str(exc)
        except ProviderError as exc:
            outcome, reason = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - never let one provider crash the router
            outcome, reason = "error", f"{type(exc).__name__}: {exc}"

        provider.record(ok=False, ms=(time.perf_counter() - call_started) * 1000)
        attempted.append({"provider": provider.name, "outcome": outcome,
                          "reason": reason[:160]})
        return None

    @staticmethod
    def _stamp(snapshot: MarketSnapshot, resolved, source: str) -> None:
        """Attach provenance, and score how complete the answer is."""
        from datetime import datetime, timezone

        currency = (snapshot.profile.currency or resolved.currency or "USD").upper()
        snapshot.profile.currency = currency

        # Confidence: how much of what was asked for arrived, discounted for
        # answering from a fallback rather than the primary. Reported rather
        # than hidden so a thin answer is visibly thin.
        completeness = min(snapshot.filled_sections / 6.0, 1.0)
        tier_weight = {
            SOURCE_INTERNAL: 0.75, SOURCE_DOCUMENTS: 0.5, SOURCE_NONE: 0.0,
        }.get(source, 1.0 if source == (
            "Finnhub" if resolved.is_us else "Financial Modeling Prep"
        ) else 0.85)

        snapshot.meta = ProviderMetadata(
            provider=source,
            currency=currency,
            exchange=snapshot.profile.exchange or resolved.exchange,
            market=resolved.market,
            timezone=resolved.timezone,
            last_updated=datetime.now(timezone.utc).isoformat(),
            confidence=round(completeness * tier_weight, 3),
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
            from app.services.universe.resolution import resolve_company

            symbol = ticker.upper().split(".")[0]
            company = resolve_company(db, symbol, exchange="NSE")
            if company is None:
                return None

            from app.data.providers.base import CompanyProfile, Quote

            snapshot = MarketSnapshot(ticker=ticker.upper(), source=SOURCE_INTERNAL)
            # The platform's own column is denominated in ₹ crore, whereas
            # `market_cap` on the snapshot is absolute units in the listing's
            # currency — the formatter divides. Passing the crore figure
            # straight through rendered "₹1,990,820.00" instead of
            # "₹19.91 lakh crore": right number, wrong scale, and the reader
            # cannot tell which they are looking at.
            crore = company.market_cap
            snapshot.profile = CompanyProfile(
                name=company.name, exchange=company.exchange, currency="INR",
                industry=company.industry, sector=company.sector,
                market_cap=(crore * 1e7) if crore else None,
                market_cap_crore=crore,
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
            company = resolve_company(db, symbol, exchange="NSE")
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
