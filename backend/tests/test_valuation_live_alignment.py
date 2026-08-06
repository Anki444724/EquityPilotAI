"""Valuation/Forecast alignment: their 'Current Price' must equal the live market.

The valuation and forecast modules render a "current price" that is now sourced
from the shared LiveMarketService. This test pins that the value they expose is
exactly the live `market.live_price` from the company view — never the stale
DB column presented independently.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _market_price_for(ticker: str) -> float:
    rows = client.get("/api/v1/companies", params={"page_size": 100}).json()["results"]
    return next(c["market"]["live_price"] for c in rows if c["ticker"] == ticker)


class TestValuationForecastAlignment:
    def test_valuation_current_price_matches_live_market(self):
        live = _market_price_for("RELIANCE")
        body = client.get("/api/v1/company/RELIANCE/valuation").json()
        assert body["summary"]["current_price"] == live
        assert body["dcf_fcff"]["current_price"] == live
        assert body["relative"]["current_price"] == live
        assert body["summary"]["maximum_buy_price"] is not None

    def test_forecast_scenario_current_price_matches_live_market(self):
        live = _market_price_for("RELIANCE")
        body = client.get("/api/v1/company/RELIANCE/forecast/scenarios").json()
        assert body["current_price"] == live
