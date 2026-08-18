"""Market operations service (Phase 4).

Manages manual market overrides and aggregates provider / cache / scheduler /
websocket / sync / log health for the Market Operations Center.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.providers.router import (
    SOURCE_DOCUMENTS, SOURCE_INTERNAL, SOURCE_NONE, cache, get_router,
)
from app.models.company import Company
from app.models.market_ops import MarketOverride
from app.schemas.company import LiveMarket
from app.schemas.market_ops import (
    MarketOverrideIn, MarketOverrideOut, ProviderHealthOut, ProviderInfoOut,
)


class MarketOpsError(Exception):
    """Raised when a market-operations action cannot be honoured."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: All supported providers for the operations center. `name` matches the
#: provider class; `configured()` is derived from the config key. Providers
#: without an implementation here (Infoway, AlphaVantage, Polygon, Custom) are
#: registered as available-but-unconfigured slots the operator can enable once
#: a key and an adapter exist.
PROVIDER_REGISTRY: list[dict[str, Any]] = [
    {"name": "Finnhub", "env_key": "FINNHUB_API_KEY", "implemented": True},
    {"name": "Financial Modeling Prep", "env_key": "FMP_API_KEY", "implemented": True},
    {"name": "Yahoo Finance (Fallback)", "env_key": "YAHOO", "implemented": True},
    {"name": "Infoway", "env_key": "INFOWAY_API_KEY", "implemented": False},
    {"name": "AlphaVantage", "env_key": "ALPHAVANTAGE_API_KEY", "implemented": False},
    {"name": "Polygon", "env_key": "POLYGON_API_KEY", "implemented": False},
    {"name": "Custom Provider", "env_key": "CUSTOM_MARKET_PROVIDER_KEY", "implemented": False},
]


class MarketOpsService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self._router = get_router()

    # ==================================================================
    # Manual overrides
    # ==================================================================
    def create_override(
        self, company_id: str, payload: MarketOverrideIn, *, actor_id=None, actor_email=None,
    ) -> MarketOverride:
        company = self.db.get(Company, company_id)
        if company is None or company.deleted_at is not None:
            raise MarketOpsError("company not found")

        expires_at = None
        if payload.expires_in_minutes is not None and payload.expires_in_minutes > 0:
            expires_at = _utcnow() + timedelta(minutes=payload.expires_in_minutes)

        # Deactivate any existing active override for this company.
        for existing in self.active_for(company_id):
            existing.expires_at = _utcnow()  # force-expire

        ov = MarketOverride(
            company_id=company.id, ticker=company.ticker,
            manual_price=payload.manual_price, manual_volume=payload.manual_volume,
            manual_market_cap=payload.manual_market_cap,
            manual_pe=payload.manual_pe, manual_pb=payload.manual_pb,
            reason=payload.reason, expires_at=expires_at,
            auto_revert=payload.auto_revert,
            created_by=actor_id, created_by_email=actor_email,
        )
        self.db.add(ov)
        self.db.flush()
        return ov

    def active_for(self, company_id: str) -> list[MarketOverride]:
        return list(self.db.execute(
            select(MarketOverride).where(
                MarketOverride.company_id == company_id,
                (MarketOverride.expires_at.is_(None))
                | (MarketOverride.expires_at > _utcnow()),
            )
        ).scalars())

    def list_overrides(self, *, active_only: bool = False) -> list[MarketOverride]:
        stmt = select(MarketOverride)
        if active_only:
            stmt = stmt.where(
                (MarketOverride.expires_at.is_(None))
                | (MarketOverride.expires_at > _utcnow())
            )
        return list(self.db.execute(
            stmt.order_by(MarketOverride.created_at.desc())
        ).scalars())

    def get_override(self, override_id: int) -> MarketOverride:
        ov = self.db.get(MarketOverride, override_id)
        if ov is None:
            raise MarketOpsError("override not found")
        return ov

    def clear_override(self, override_id: int) -> None:
        ov = self.get_override(override_id)
        ov.expires_at = _utcnow()  # immediate expiry = auto revert

    def clear_all(self) -> int:
        rows = self.list_overrides(active_only=True)
        now = _utcnow()
        for ov in rows:
            ov.expires_at = now
        return len(rows)

    def resolve_override(self, company: Company) -> MarketOverride | None:
        """The single active override for a company, or None."""
        active = self.active_for(company.id)
        return active[0] if active else None

    def apply_override(self, company: Company, market: LiveMarket) -> LiveMarket | None:
        """If an active override exists, return a market view reflecting it."""
        ov = self.resolve_override(company)
        if ov is None:
            return None
        return LiveMarket(
            live_price=ov.manual_price if ov.manual_price is not None else market.live_price,
            current_price=market.current_price,
            price_source="Manual Override",
            last_updated=_utcnow().isoformat(),
            market_status="manual",
            change=market.change,
            change_percent=market.change_percent,
            volume=ov.manual_volume if ov.manual_volume is not None else market.volume,
        )

    # ==================================================================
    # Provider health / registry
    # ==================================================================
    def provider_registry(self) -> list[ProviderInfoOut]:
        engine = self._router
        by_name = {p.name: p for p in engine.providers}
        out: list[ProviderInfoOut] = []
        for i, spec in enumerate(PROVIDER_REGISTRY):
            provider = by_name.get(spec["name"])
            configured = provider.configured() if provider else _env_key_present(spec["env_key"])
            health = provider.health() if provider else None
            out.append(ProviderInfoOut(
                name=spec["name"],
                priority=i + 1,
                configured=configured,
                implemented=spec["implemented"],
                available=(provider.available if provider else configured),
                latency_ms=(health or {}).get("average_response_ms"),
                last_success=(health or {}).get("last_successful_request"),
                calls=(health or {}).get("calls", 0),
                rate_limit_remaining=(health or {}).get("rate_limit_remaining"),
                status=_provider_status(provider, configured),
            ))
        return out

    def provider_health(self, *, probe: bool = False) -> list[ProviderHealthOut]:
        registry = self.provider_registry()
        return [ProviderHealthOut(
            name=r.name, status=r.status, configured=r.configured,
            available=r.available, latency_ms=r.latency_ms,
            last_success=r.last_success, calls=r.calls,
            rate_limit_remaining=r.rate_limit_remaining,
            priority=r.priority,
        ) for r in registry]

    # ==================================================================
    # Realtime dashboard
    # ==================================================================
    def dashboard(self) -> dict[str, Any]:
        from app.core.config import settings

        engine = self._router
        cache_stats = cache().stats()
        overrides = self.list_overrides(active_only=True)
        provider_count = sum(1 for p in self.provider_registry() if p.available)
        market = market_status() if (market_status := _market_status_fn()) else "unknown"

        return {
            "connected_symbols": cache_stats.get("entries", 0),
            "cache_size": cache_stats.get("entries", 0),
            "cache_hit_rate": cache_stats.get("hit_rate", 0),
            "ttl_seconds": cache_stats.get("ttl_seconds", 300),
            "memory_bytes": _memory_bytes(),
            "redis": {
                "configured": bool(settings.REDIS_URL),
                "backend": "redis" if settings.REDIS_URL else "memory",
            },
            "last_refresh": _utcnow().isoformat(),
            "active_overrides": len(overrides),
            "providers_available": provider_count,
            "market_status": market,
            "errors": _error_count(),
            "api_calls": _api_calls(),
        }

    # ==================================================================
    # Cache manager
    # ==================================================================
    def clear_cache(self) -> int:
        cache().clear()
        # Company pages use the unified cache so Redis can share background
        # Yahoo snapshots across workers. Clear both market cache backends.
        from app.services.platform.cache import Namespace, cache as shared_cache
        return shared_cache.invalidate(Namespace.MARKET_DATA)

    def refresh_cache(self) -> int:
        # A following company read returns its stored fallback immediately and
        # queues a bounded background refresh; it never fetches in the request.
        return self.clear_cache()

    # ==================================================================
    # Scheduler / sync / websocket / logs — lightweight state
    # ==================================================================
    def scheduler_status(self) -> dict[str, Any]:
        return {
            "priority_queue": {
                "visible": 0, "portfolio": 0, "watchlist": 0,
                "trending": 0, "market": 0,
            },
            "running": False,
            "paused": False,
            "last_run": None,
        }

    def sync_status(self) -> dict[str, Any]:
        return {
            "last_sync": None,
            "pending": 0,
            "companies_synced": 0,
        }

    def websocket_status(self) -> dict[str, Any]:
        return {
            "connected": 0,
            "disconnected": 0,
            "reconnects": 0,
            "subscriptions": 0,
            "messages_per_sec": 0.0,
            "dropped_messages": 0,
        }

    def logs(self, *, level: str | None = None, limit: int = 100) -> dict[str, Any]:
        return {
            "items": [],
            "errors": _error_count(),
            "api_calls": _api_calls(),
            "avg_latency_ms": 0.0,
        }


def _env_key_present(key: str) -> bool:
    import os
    return bool((os.environ.get(key) or "").strip())


def _provider_status(provider, configured: bool) -> str:
    if provider is None:
        return "unconfigured" if not configured else "configured"
    if not provider.available:
        return "rate_limited"
    if configured:
        return "live"
    return "offline"


def _market_status_fn():
    try:
        from app.services.live_market import market_status
        return market_status
    except Exception:  # noqa: BLE001
        return None


def _memory_bytes() -> int:
    try:
        import os
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    except Exception:  # noqa: BLE001
        return 0


def _error_count() -> int:
    return 0


def _api_calls() -> int:
    return 0
