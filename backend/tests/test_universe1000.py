"""Universe 1000 scoping of the public companies list.

The public ``GET /api/v1/companies`` endpoint must expose exactly the
EquityPilotAI Universe 1000 — every NIFTY500 constituent plus the top 500
non-NIFTY500 companies by market cap — with the universe established *before*
the sector filter, the count and pagination.

The seed below mirrors the verified production shape in miniature: 500
NIFTY500 members and 568 non-NIFTY500 companies with a positive market cap,
with the non-NIFTY rank-500/rank-501 boundary carrying the production figures
(Kiri Industries 3249.6 / Bhansali Engineering Polymers 3131.0).

Read-only with respect to any real data: everything lives in a private
in-memory SQLite database built by this module.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.company import Company
from app.services.company_admin_service import CompanyAdminService
from app.services.company_service import CompanyService

PAGE_SIZE = 25

#: Sector shared by a known subset of universe and non-universe companies.
BOUNDARY_SECTOR = "Chemicals - Boundary"


def _company(
    name: str,
    ticker: str,
    market_cap: float | None,
    index_membership: str | None,
    sector: str | None = "General",
) -> Company:
    return Company(
        id=str(uuid.uuid4()),
        name=name,
        ticker=ticker,
        exchange="NSE",
        sector=sector,
        market_cap=market_cap,
        index_membership=index_membership,
    )


def _seed_universe(db) -> list[Company]:
    """Insert the production-shaped corpus and return it in seed order."""
    rows: list[Company] = []

    # 500 NIFTY500 constituents. The first ten deliberately carry market caps
    # far BELOW the non-NIFTY cutoff: index membership is authoritative, so
    # they must stay listed regardless of the cutoff (behaviour 8).
    for i in range(500):
        low_tail = i < 10
        rows.append(_company(
            f"Nifty Member {i:03d} Ltd",
            f"NIFTY{i:03d}",
            100.0 + i if low_tail else 100_000.0 - i,
            "NIFTY500",
            BOUNDARY_SECTOR if i < 2 else "General",
        ))

    # 568 non-NIFTY500 companies with a positive market cap.
    #   ranks 1..499   -> 3250.6 .. 3748.6 (descending)
    #   rank  500      -> Kiri Industries, 3249.6   (production boundary)
    #   rank  501      -> Bhansali Engineering, 3131.0 (must be excluded)
    #   ranks 502..568 -> 3130.0 .. 3070.0 (descending)
    for r in range(1, 500):
        rows.append(_company(
            f"Next Tier {r:03d} Ltd", f"NEXT{r:03d}",
            3249.6 + (500 - r), None,
            BOUNDARY_SECTOR if r <= 7 else "General",
        ))
    rows.append(_company(
        "Kiri Industries Ltd", "532967", 3249.6, None, BOUNDARY_SECTOR,
    ))
    rows.append(_company(
        "Bhansali Engineering Polymers Ltd", "500052", 3131.0, None,
    ))
    for r in range(502, 569):
        rows.append(_company(
            f"Next Tier {r:03d} Ltd", f"NEXT{r:03d}", 3131.0 - (r - 501), None,
        ))

    # Non-NIFTY rows with no positive market cap cannot be ranked and — with
    # 568 positive-cap candidates available — must never reach the universe.
    rows.append(_company("Null Cap Ltd", "NULLCAP", None, None))
    rows.append(_company("Negative Cap Ltd", "NEGCAP", -5.0, None))

    db.add_all(rows)
    db.commit()
    return rows


@pytest.fixture(scope="module")
def universe_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.db.base import Base
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    seeded = _seed_universe(db)
    yield db, seeded
    db.close()
    engine.dispose()


def _endpoint_order(c: Company) -> tuple:
    """Mirror of ORDER BY market_cap DESC NULLS LAST, name ASC, id ASC."""
    cap = c.market_cap
    return (cap is None, -(cap if cap is not None else 0.0), c.name, c.id)


def _expected_universe(seeded: list[Company]) -> list[str]:
    """The universe as defined on paper, ordered like the endpoint."""
    universe = _expected_universe_ids(seeded)
    return [c.id for c in sorted(universe, key=_endpoint_order)]


class TestUniverse1000:
    def test_total_is_exactly_1000(self, universe_db):
        db, seeded = universe_db
        total, rows = CompanyService(db).list_companies(page=1, page_size=1)
        assert total == 1000
        assert len(rows) == 1

    def test_first_page_is_highest_market_cap(self, universe_db):
        db, seeded = universe_db
        expected = _expected_universe(seeded)
        _, rows = CompanyService(db).list_companies(page=1, page_size=PAGE_SIZE)
        assert [r.id for r in rows] == expected[:PAGE_SIZE]
        caps = [r.market_cap for r in rows]
        assert caps == sorted(caps, reverse=True)

    def test_full_walk_covers_universe_with_no_duplicates(self, universe_db):
        db, seeded = universe_db
        expected = _expected_universe(seeded)
        svc = CompanyService(db)
        seen: list[str] = []
        page = 1
        while True:
            total, rows = svc.list_companies(page=page, page_size=PAGE_SIZE)
            assert total == 1000
            if not rows:
                break
            seen.extend(r.id for r in rows)
            page += 1
        assert len(seen) == 1000
        assert sorted(seen) == sorted(expected)

    def test_page_40_is_final_25_companies(self, universe_db):
        db, seeded = universe_db
        expected = _expected_universe(seeded)
        total, rows = CompanyService(db).list_companies(page=40, page_size=PAGE_SIZE)
        assert total == 1000
        assert [r.id for r in rows] == expected[975:1000]

    def test_page_41_is_empty(self, universe_db):
        db, _ = universe_db
        total, rows = CompanyService(db).list_companies(page=41, page_size=PAGE_SIZE)
        assert total == 1000
        assert rows == []

    def test_sector_filter_operates_inside_the_universe(self, universe_db):
        db, seeded = universe_db
        expected_in_sector = {
            c.id for c in _expected_universe_ids(seeded)
            if c.sector == BOUNDARY_SECTOR
        }
        total, rows = CompanyService(db).list_companies(
            page=1, page_size=1000, sector=BOUNDARY_SECTOR,
        )
        assert total == len(expected_in_sector)
        assert {r.id for r in rows} == expected_in_sector
        assert all(r.sector == BOUNDARY_SECTOR for r in rows)

    def test_kiri_rank_500_non_nifty_is_included(self, universe_db):
        db, _ = universe_db
        svc = CompanyService(db)
        _, rows = svc.list_companies(page=1, page_size=1000)
        assert any(r.ticker == "532967" for r in rows)

    def test_bhansali_rank_501_non_nifty_is_excluded(self, universe_db):
        db, _ = universe_db
        svc = CompanyService(db)
        _, rows = svc.list_companies(page=1, page_size=1000)
        assert all(r.ticker != "500052" for r in rows)

    def test_low_cap_nifty500_members_stay_included(self, universe_db):
        db, seeded = universe_db
        _, rows = CompanyService(db).list_companies(page=1, page_size=1000)
        listed = {r.id for r in rows}
        low_tails = {
            c.id for c in seeded
            if c.index_membership == "NIFTY500" and c.market_cap < 3249.6
        }
        assert low_tails
        assert low_tails <= listed

    def test_non_nifty_without_positive_cap_is_excluded(self, universe_db):
        db, _ = universe_db
        _, rows = CompanyService(db).list_companies(page=1, page_size=1000)
        tickers = {r.ticker for r in rows}
        assert "NULLCAP" not in tickers and "NEGCAP" not in tickers

    def test_admin_list_is_not_scoped_to_the_universe(self, universe_db):
        db, seeded = universe_db
        total, rows = CompanyAdminService(db).list_companies(page=1, page_size=5000)
        assert total == len(seeded)  # 1071 — the whole table, not 1000
        assert any(c.ticker == "500052" for c in rows)

    def test_search_is_not_scoped_to_the_universe(self, universe_db):
        db, _ = universe_db
        results = CompanyService(db).search("Bhansali")
        assert [c.ticker for c in results] == ["500052"]


def _expected_universe_ids(seeded: list[Company]) -> list[Company]:
    nifty = [c for c in seeded if c.index_membership == "NIFTY500"]
    non_nifty = sorted(
        (c for c in seeded if c.index_membership != "NIFTY500"
         and c.market_cap is not None and c.market_cap > 0),
        key=lambda c: (-c.market_cap, c.name, c.id),
    )[:500]
    return nifty + non_nifty
