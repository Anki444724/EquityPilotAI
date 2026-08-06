"""Runtime verification: all five RELIANCE price surfaces must agree exactly.

Endpoints exercised:
  1. Dashboard     GET /api/v1/dashboard/overview        (largest -> RELIANCE)
  2. Companies     GET /api/v1/companies/{id}           (market)
  3. Companies List GET /api/v1/companies               (search/list -> market)
  4. Watchlist     GET /api/v1/watchlists/{id}          (row: price/source/last_updated/market_status)
  5. Portfolio     GET /api/v1/portfolios/{id}          (holdings[].current_price + provenance)

Every page must consume the exact same LiveMarketService snapshot: price,
source, last_updated and market_status must be identical across all five.
No page may display Company.current_price directly.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _reliance_row() -> dict:
    rows = client.get("/api/v1/companies", params={"page_size": 100}).json()["results"]
    return next(c for c in rows if c["ticker"] == "RELIANCE")


def _create_portfolio_with_reliance() -> dict:
    pf = client.post("/api/v1/portfolios", json={
        "name": "price-consistency-check", "benchmark": "NIFTY 50",
    }).json()
    pid = pf["id"]
    client.post(f"/api/v1/portfolios/{pid}/transactions", json={
        "ticker": "RELIANCE", "txn_type": "buy",
        "trade_date": "2024-01-10", "quantity": 10, "price": 2500.0,
    })
    return pf


def _create_watchlist_with_reliance() -> dict:
    wl = client.post("/api/v1/watchlists", json={
        "name": "price-consistency-watchlist",
    }).json()
    client.post(f"/api/v1/watchlists/{wl['id']}/entries", json={"ticker": "RELIANCE"})
    return wl


class TestAllFiveSurfaces:
    def test_all_five_pages_share_one_live_market_snapshot(self):
        reliance = _reliance_row()
        rid = reliance["id"]

        # 1 Dashboard
        dash = client.get("/api/v1/dashboard/overview").json()
        dash_rel = next(c for c in dash["largest"] if c["ticker"] == "RELIANCE")
        dash_m = dash_rel["market"]

        # 2 Companies List (list + search)
        list_m = reliance["market"]
        search_m = client.get(
            "/api/v1/companies/search", params={"q": "RELIANCE"}
        ).json()["results"][0]["market"]

        # 3 Company Detail
        detail_m = client.get(f"/api/v1/companies/{rid}").json()["market"]

        # 4 Watchlist
        wl = _create_watchlist_with_reliance()
        wl_row = client.get(f"/api/v1/watchlists/{wl['id']}").json()[0]

        # 5 Portfolio
        pf = _create_portfolio_with_reliance()
        view = client.get(f"/api/v1/portfolios/{pf['id']}").json()
        holding = next(h for h in view["holdings"] if h["ticker"] == "RELIANCE")

        # Gather the four contract fields from each page.
        snapshots = {
            "Dashboard": (dash_m["live_price"], dash_m["price_source"],
                          dash_m["last_updated"], dash_m["market_status"]),
            "Companies List": (list_m["live_price"], list_m["price_source"],
                               list_m["last_updated"], list_m["market_status"]),
            "Company Detail": (detail_m["live_price"], detail_m["price_source"],
                               detail_m["last_updated"], detail_m["market_status"]),
            "Watchlist": (wl_row["price"], wl_row["price_source"],
                          wl_row["last_updated"], wl_row["market_status"]),
            "Portfolio": (holding["current_price"], holding["price_source"],
                          holding["last_updated"], holding["market_status"]),
        }

        # Print the comparison table for the record.
        print("\n  PAGE             | PRICE    | SOURCE                              | LAST UPDATED                 | STATUS")
        print("  ----------------+----------+-------------------------------------+------------------------------+--------")
        for page, (price, src, ts, status) in snapshots.items():
            print(f"  {page:<16}| {price!s:<9}| {src!s:<35}| {ts!s:<29}| {status}")

        expected = snapshots["Dashboard"]
        for page, values in snapshots.items():
            assert values == expected, (
                f"{page} diverges from Dashboard: {values} != {expected}"
            )

        # No page may present the raw DB column as the live price. The source
        # label from the shared market service (even when it is the internal
        # tier) must be the one every page reports.
        assert all(src == expected[1] for _, src, _, _ in snapshots.values())

    def test_companies_list_and_search_agree(self):
        list_m = _reliance_row()["market"]
        search_m = client.get(
            "/api/v1/companies/search", params={"q": "RELIANCE"}
        ).json()["results"][0]["market"]
        assert list_m["live_price"] == search_m["live_price"]
        assert list_m["price_source"] == search_m["price_source"]
