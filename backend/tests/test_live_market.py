"""Live-market service: status derivation and fallback semantics."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.schemas.company import LiveMarket
from app.services.live_market import LiveMarketService, market_status


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


class TestMarketStatus:
    def test_weekend(self):
        # 2026-08-08 is a Saturday, any time of day.
        assert market_status(_utc(2026, 8, 8, 10, 0)) == "weekend"

    def test_open_during_session(self):
        # 2026-08-06 is a Thursday, 10:00 IST (04:30 UTC).
        assert market_status(_utc(2026, 8, 6, 4, 30)) == "open"

    def test_closed_before_open(self):
        # 08:00 IST = 02:30 UTC.
        assert market_status(_utc(2026, 8, 6, 2, 30)) == "closed"

    def test_closed_after_close(self):
        # 16:00 IST = 10:30 UTC.
        assert market_status(_utc(2026, 8, 6, 10, 30)) == "closed"


class TestSnapshotFallback:
    def test_missing_company_is_a_labelled_null(self):
        market = LiveMarketService(None).snapshot(None)
        assert market.live_price is None
        assert market.current_price is None
        assert market.market_status == "closed"
        assert market.price_source == "Internal Financial Database"


class TestNonBlockingQuotePath:
    @staticmethod
    def _company(ticker="360ONE", price=987.0):
        return SimpleNamespace(
            ticker=ticker, exchange="NSE", current_price=price, id="company-id",
        )

    def test_cache_miss_returns_stored_price_and_queues_nse_symbol(
        self, monkeypatch,
    ):
        import app.services.live_market as module

        scheduled = []
        monkeypatch.setattr(module.cache, "get", lambda *args: None)
        monkeypatch.setattr(module._REFRESHER, "schedule", scheduled.append)

        result = LiveMarketService(None).bulk_quotes([self._company()])["360ONE"]

        assert result.live_price == 987.0
        assert result.price_source == "Internal Financial Database"
        assert scheduled == ["360ONE.NS"]

    def test_cached_live_quote_is_shared_and_does_not_schedule(self, monkeypatch):
        import app.services.live_market as module

        live = LiveMarket(
            live_price=1012.5, price_source="Yahoo Finance (Fallback)",
            last_updated="2026-08-18T10:00:00+00:00", market_status="open",
        )
        scheduled = []
        monkeypatch.setattr(module.cache, "get", lambda *args: live)
        monkeypatch.setattr(module._REFRESHER, "schedule", scheduled.append)
        company = self._company()
        service = LiveMarketService(None)

        first = service.snapshot(company)
        second = service.bulk_quotes([company])["360ONE"]

        assert first.live_price == second.live_price == 1012.5
        assert first.last_updated == second.last_updated
        assert first.current_price == 987.0
        assert scheduled == []
