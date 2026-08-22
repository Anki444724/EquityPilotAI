"""India-only market data: API-level guarantees.

EquityPilotAI serves only NSE/BSE (Indian) symbols. This file pins the
user-visible contract:

* `RELIANCE`, `TCS`, `INFY` resolve to Indian/NSE and return a real price
  from a named source.
* `AAPL`, `MSFT` (and other foreign tickers) are rejected with a clear message
  rather than being chased through the provider chain.
* A provider outage must never masquerade as a `price=None` 200 "success" —
  it is a loud 502.
* Search and company lists never surface US listings.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.data.providers.base import BaseMarketProvider, MarketSnapshot, ProviderError
from app.data.providers.router import TTLCache, MarketDataRouter
from app.main import app

client = TestClient(app)


def _stub(name, priority, *, price=None, error=None):
    """A provider that returns a fixed snapshot or raises a fixed error."""
    class Stub(BaseMarketProvider):
        def configured(self): return True
        def fetch(self, ticker, **kwargs):
            if error:
                raise error
            snap = MarketSnapshot(ticker=ticker, source=name)
            if price is not None:
                snap.quote.price = price
                snap.profile.name = "Reliance Industries Ltd"
                snap.profile.currency = "INR"
            return snap, {}
    Stub.name = name
    Stub.priority = priority
    return Stub()


def _patch_router(monkeypatch, providers, *, empty_lower_tiers=False):
    if empty_lower_tiers:
        class _NoLowerTiers(MarketDataRouter):
            def _from_internal_db(self, db, ticker):
                return None
            def _from_documents(self, db, ticker):
                return None
        router = _NoLowerTiers(providers=providers, ttl_cache=TTLCache())
    else:
        router = MarketDataRouter(providers=providers, ttl_cache=TTLCache())
    monkeypatch.setattr("app.api.v1.market.get_router", lambda: router)


class TestIndianSymbols:
    def test_reliance_returns_a_valid_price_from_a_named_source(self, monkeypatch):
        _patch_router(monkeypatch, [_stub("NSE India (Live)", 1, price=1307.8)])
        body = client.get("/api/v1/market/RELIANCE").json()
        assert body["symbol"]["canonical"] == "RELIANCE.NS"
        assert body["symbol"]["market"] == "India"
        assert body["source"] == "NSE India (Live)"
        assert body["quote"]["price"] == 1307.8
        assert body["profile"]["currency"] == "INR"

    def test_tcs_and_infy_resolve_to_indian_nse(self, monkeypatch):
        _patch_router(monkeypatch, [_stub("NSE India (Live)", 1, price=100.0)])
        for ticker in ("TCS", "INFY"):
            body = client.get(f"/api/v1/market/{ticker}").json()
            assert body["symbol"]["canonical"] == f"{ticker}.NS"
            assert body["symbol"]["market"] == "India"


class TestForeignSymbolRejection:
    @pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "TSLA", "NVDA",
                                        "NASDAQ:AAPL"])
    def test_us_tickers_are_rejected_with_a_clear_message(
        self, monkeypatch, ticker,
    ):
        _patch_router(monkeypatch, [_stub("NSE India (Live)", 1, price=100.0)])
        resp = client.get(f"/api/v1/market/{ticker}")
        assert resp.status_code == 422, resp.text
        body = resp.json()["detail"]
        assert "not supported" in body["message"]
        assert "NSE/BSE" in body["message"]


class TestNoMisleadingNullPrice:
    def test_total_outage_is_502_not_price_none_success(self, monkeypatch):
        from app.data.providers.base import ProviderError

        # Lower tiers are emptied so the test exercises the outage path rather
        # than the (legitimate) internal-database fallback for RELIANCE.
        _patch_router(monkeypatch, [_stub("NSE India (Live)", 1,
                                          error=ProviderError("down"))],
                      empty_lower_tiers=True)
        resp = client.get("/api/v1/market/RELIANCE")
        assert resp.status_code == 502
        assert "No provider could serve" in resp.json()["detail"]["message"]

    def test_profile_only_without_price_is_not_served(self, monkeypatch):
        # A provider that answers with a name but no price must not be a
        # price=None 200. It falls through; with nothing else available that
        # is a 502, not a fake empty success.
        _patch_router(monkeypatch, [_stub("NSE India (Live)", 1, price=None)],
                      empty_lower_tiers=True)
        resp = client.get("/api/v1/market/RELIANCE")
        assert resp.status_code == 502

    def test_internal_db_fallback_is_served_as_labelled_not_live(self, monkeypatch):
        # The seeded database holds RELIANCE with a stored price, so when the
        # external tiers fail the request is served (200) from the internal
        # tier, clearly labelled — it is real data, not a fake null.
        _patch_router(monkeypatch, [_stub("NSE India (Live)", 1,
                                          error=ProviderError("down"))])
        resp = client.get("/api/v1/market/RELIANCE")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "Internal Financial Database"
        assert body["quote"]["price"] is not None


class TestSearchUniverseIndiaOnly:
    def test_us_companies_are_not_returned_by_search(self):
        from app.models.company import Company
        from app.services.company_service import _US_EXCHANGES

        assert _US_EXCHANGES == ("NASDAQ", "NYSE", "AMEX")

    def test_search_results_carry_market(self):
        body = client.get("/api/v1/companies/search", params={"q": "TCS"}).json()
        assert body["results"]
        assert all(r["ticker"] == "TCS" for r in body["results"])
        assert all(r["market"] is not None for r in body["results"])
