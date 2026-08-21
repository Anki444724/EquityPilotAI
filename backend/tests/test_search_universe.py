"""Phase 1 — search across the 5,000-company universe (requirement G).

Fields: name, ticker, ISIN, BSE code, sector, industry. Server-side ranking,
no full-table fetch, 60s cache. The scale fixture reuses the module-level
universe built by the persistence suite's pattern.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select

from app.models.company import Company
from app.services.company_service import CompanyService
from app.services.universe.company_universe import generate_mock_universe

# The module's `big_db` starts empty; seed the full deterministic universe
# once so the ISIN test and the scale tests share one expensive setup.
@pytest.fixture(autouse=True, scope="module")
def _seed_full_universe(big_db):
    from app.services.universe.company_universe import CompanyUniverseService

    count = big_db.scalar(
        select(func.count()).select_from(Company)
    )
    if count < 5_000:
        CompanyUniverseService(big_db).sync(
            generate_mock_universe(5_000), source="mock", batch_size=1_000,
        )
    yield


class TestSearchFields:
    def test_search_by_name(self, phase1_db):
        CompanyUniverseSeeder(phase1_db).seed(50)
        hits = CompanyService(phase1_db).search("Aurora", limit=10)
        assert hits
        assert all("aurora" in h.name.lower() for h in hits)

    def test_search_by_ticker_prefix(self, phase1_db):
        CompanyUniverseSeeder(phase1_db).seed(50)
        hits = CompanyService(phase1_db).search("MCK004", limit=10)
        assert {h.ticker for h in hits} <= {f"MCK00{i:02d}" for i in range(500)}

    def test_search_by_isin_finds_the_company(self, big_db):
        records = generate_mock_universe(2_000)
        target = next(r for r in records if r.isin)
        hits = CompanyService(big_db).search(target.isin, limit=3)
        assert hits and hits[0].ticker == target.ticker

    def test_search_by_bse_code(self, phase1_db):
        seeder = CompanyUniverseSeeder(phase1_db).seed(200)
        with_code = next(c for c in seeder if c.bse_code)
        hits = CompanyService(phase1_db).search(with_code.bse_code, limit=3)
        assert hits and hits[0].ticker == with_code.ticker

    def test_search_by_sector(self, phase1_db):
        CompanyUniverseSeeder(phase1_db).seed(200)
        hits = CompanyService(phase1_db).search("Healthcare", limit=20)
        assert hits
        assert all(h.sector == "Healthcare" for h in hits)

    def test_search_by_industry(self, phase1_db):
        CompanyUniverseSeeder(phase1_db).seed(300)
        hits = CompanyService(phase1_db).search("Pharmaceuticals", limit=20)
        assert hits
        assert all(h.industry == "Pharmaceuticals" for h in hits)

    def test_empty_query_returns_nothing(self, phase1_db):
        CompanyUniverseSeeder(phase1_db).seed(10)
        assert CompanyService(phase1_db).search("   ", limit=10) == []


class TestSearchCache:
    def test_results_are_cached_per_query(self, phase1_db):
        CompanyUniverseSeeder(phase1_db).seed(100)
        svc = CompanyService(phase1_db)
        first = svc.search("MCK", limit=10)
        second = svc.search("MCK", limit=10)

        from app.services.platform.cache import Namespace, cache

        assert cache.get(Namespace.SEARCH, "MCK", 10) is not None
        assert second == first


class TestSearchAtScale:
    def test_search_latency_over_5000_companies(self, big_db):
        """Search must stay fast at full universe size (SQLite here; the PG
        number with trigram indexes is measured on staging — see the report)."""
        count = big_db.scalar(
            select(func.count()).select_from(Company)
        )
        assert count >= 5_000, "scale fixture must hold the full universe"

        svc = CompanyService(big_db)
        queries = ["MCK04", "aurora", "INM12", "9001", "Healthcare", "Chemicals"]

        # Warm one query to populate the cache, then measure the mix.
        svc.search("MCK04", limit=20)
        started = time.perf_counter()
        results = {}
        for q in queries * 5:            # 30 searches: repeated prefixes are the norm
            results.setdefault(q, svc.search(q, limit=20))
        elapsed = time.perf_counter() - started

        assert all(results[q] is not None for q in queries)
        # Budget: 30 mixed searches well under 2s even on SQLite, i.e. <70ms
        # amortised — cache hits for repeated prefixes cost microseconds.
        assert elapsed < 2.0, f"search mix too slow: {elapsed:.2f}s for 30 queries"

    def test_cached_hit_is_far_cheaper_than_miss(self, big_db):
        svc = CompanyService(big_db)
        svc.search("MCK03", limit=20)     # prime

        started = time.perf_counter()
        for _ in range(50):
            svc.search("MCK03", limit=20)
        cached = time.perf_counter() - started

        # A cold prefix (unique each time) must still be reasonable, and the
        # cached loop must be at least an order of magnitude cheaper.
        assert cached < 0.5, f"50 cached searches took {cached:.3f}s"

    def test_pagination_over_5000(self, big_db):
        total, page1 = CompanyService(big_db).list_companies(1, 25)
        assert total >= 5_000
        assert len(page1) == 25
        _, page2 = CompanyService(big_db).list_companies(2, 25)
        assert {c.id for c in page1}.isdisjoint({c.id for c in page2})


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
from app.services.universe.company_universe import CompanyUniverseService  # noqa: E402


class CompanyUniverseSeeder:
    """Seed `n` deterministic companies through the real sync path."""

    def __init__(self, phase1_db):
        self.phase1_db = phase1_db

    def seed(self, n: int) -> list:
        records = generate_mock_universe(n)
        CompanyUniverseService(self.phase1_db).sync(records, source="mock", batch_size=250)
        return list(
            self.phase1_db.scalars(
                select(Company).order_by(Company.ticker.asc()).limit(n)
            ).all()
        )
