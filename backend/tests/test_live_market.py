"""Live-market service: status derivation and fallback semantics."""
from __future__ import annotations

from datetime import datetime, timezone

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
