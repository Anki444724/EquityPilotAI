"""Module 1 API integration tests — run against an in-memory database."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.seed import REFERENCE_COMPANY, UNIVERSE
from app.main import app

#: Universe size = the synthetic set plus the reference company.
EXPECTED_COMPANIES = len(UNIVERSE) + 1

# Database and dependency override live in conftest.py so both API modules
# share one seeded instance (they mutate the same FastAPI app object).
client = TestClient(app)


def _titan_id() -> str:
    rows = client.get("/api/v1/companies", params={"page_size": 50}).json()["results"]
    return next(c["id"] for c in rows if c["ticker"] == "TITAN")


class TestSystem:
    def test_health(self):
        body = client.get("/health").json()
        assert body["status"] == "ok"

    def test_openapi_schema_is_served(self):
        schema = client.get("/openapi.json").json()
        assert "/api/v1/companies/search" in schema["paths"]


class TestAuth:
    def test_dev_identity_when_no_auth_configured(self):
        """The unconfigured deployment still resolves a labelled identity.

        Module 10 replaced the three-role Clerk shim with the seven-role RBAC
        model, so the development identity is now `super_admin` rather than
        `admin` — it must be able to reach the operator console, which `admin`
        deliberately cannot. The behaviour under test is unchanged: no
        credentials configured means a clearly-flagged development principal,
        never a silently anonymous one.
        """
        body = client.get("/api/v1/auth/me").json()
        assert body["is_dev_identity"] is True
        assert body["role"] == "super_admin"

    def test_auth_config_exposes_state(self):
        body = client.get("/api/v1/auth/config").json()
        assert body["auth_enabled"] is False


class TestCompanySearch:
    def test_exact_ticker_ranks_first(self):
        results = client.get("/api/v1/companies/search", params={"q": "TCS"}).json()["results"]
        assert results[0]["ticker"] == "TCS"

    def test_partial_name_match(self):
        results = client.get("/api/v1/companies/search", params={"q": "titan"}).json()["results"]
        assert any(c["ticker"] == "TITAN" for c in results)

    def test_sector_match(self):
        results = client.get(
            "/api/v1/companies/search", params={"q": "IT Services"}
        ).json()["results"]
        assert {"TCS", "INFY", "WIPRO"} <= {c["ticker"] for c in results}

    def test_empty_query_returns_nothing(self):
        assert client.get("/api/v1/companies/search", params={"q": ""}).json()["total"] == 0

    def test_no_match_is_empty_not_error(self):
        body = client.get("/api/v1/companies/search", params={"q": "zzzz"}).json()
        assert body["total"] == 0


class TestCompanyList:
    def test_pagination(self):
        body = client.get("/api/v1/companies", params={"page": 1, "page_size": 5}).json()
        assert body["total"] == EXPECTED_COMPANIES
        assert len(body["results"]) == 5

    def test_sorted_by_market_cap_desc(self):
        rows = client.get("/api/v1/companies", params={"page_size": 5}).json()["results"]
        caps = [r["market_cap"] for r in rows]
        assert caps == sorted(caps, reverse=True)

    def test_sector_filter(self):
        rows = client.get(
            "/api/v1/companies", params={"sector": "IT Services", "page_size": 50}
        ).json()["results"]
        assert rows and all(r["sector"] == "IT Services" for r in rows)

    def test_sectors_endpoint(self):
        sectors = client.get("/api/v1/companies/sectors").json()
        assert "IT Services" in sectors and sectors == sorted(sectors)


class TestCompanyProfile:
    def test_unknown_company_404(self):
        assert client.get("/api/v1/companies/missing/profile").status_code == 404

    def test_profile_matches_workbook_figures(self):
        """TITAN is seeded with the workbook engine-test fixture parameters."""
        p = client.get(f"/api/v1/companies/{_titan_id()}/profile").json()
        assert p["revenue"] == pytest.approx(14528.6, abs=0.05)
        assert p["ebitda"] == pytest.approx(961.9, abs=0.05)
        assert p["pat"] == pytest.approx(125.5, abs=0.05)

    def test_balance_sheet_ties(self):
        p = client.get(f"/api/v1/companies/{_titan_id()}/profile").json()
        assert p["balance_sheet_ties"] is True

    def test_full_canonical_coverage(self):
        p = client.get(f"/api/v1/companies/{_titan_id()}/profile").json()
        assert p["coverage"]["items_populated"] == 540  # 54 items x 10 years
        assert p["coverage"]["coverage"] == pytest.approx(1.0)

    def test_margins_are_derived_not_stored(self):
        p = client.get(f"/api/v1/companies/{_titan_id()}/profile").json()
        assert p["ebitda_margin"] == pytest.approx(p["ebitda"] / p["revenue"])


class TestDashboard:
    def test_overview_counts(self):
        body = client.get("/api/v1/dashboard/overview").json()
        assert body["coverage"]["companies"] == EXPECTED_COMPANIES
        assert body["coverage"]["companies_with_financials"] == EXPECTED_COMPANIES
        assert body["coverage"]["fact_rows"] == EXPECTED_COMPANIES * 54 * 10

    def test_sector_counts_sum_to_universe(self):
        body = client.get("/api/v1/dashboard/overview").json()
        assert sum(s["count"] for s in body["sectors"]) == EXPECTED_COMPANIES

    def test_largest_is_reliance(self):
        body = client.get("/api/v1/dashboard/overview").json()
        assert body["largest"][0]["ticker"] == "RELIANCE"
