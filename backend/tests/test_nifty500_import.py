"""Nifty 500 universe import.

The risks here are quiet ones. A duplicate company row for a security that
changed ticker; a BSE code joined on name and silently attached to the wrong
company; a market-cap category invented from a threshold rather than taken
from the exchange. Each produces a database that looks populated and is wrong.

Network calls are stubbed. The live fetch is exercised separately in the
import run itself; these must pass with no internet.
"""
from __future__ import annotations

import pytest

from app.services.universe.nifty500 import (
    CATEGORY_SOURCES, INDEX_NAME, Constituent, ImportReport, Nifty500Importer,
)

CONSTITUENTS = [
    Constituent("ALPHA", "Alpha Industries Ltd.", "Capital Goods",
                "INE000A01001", "largecap", "500001"),
    Constituent("BETA", "Beta Finance Ltd.", "Financial Services",
                "INE000B01002", "midcap", "500002"),
    Constituent("GAMMA", "Gamma Pharma Ltd.", "Healthcare",
                "INE000C01003", "smallcap", None),
]


@pytest.fixture()
def db():
    from tests.conftest import TestingSession

    from app.models.company import Company

    session = TestingSession()
    symbols = [c.symbol for c in CONSTITUENTS] + ["DELTA"]
    try:
        yield session
    finally:
        session.query(Company).filter(Company.ticker.in_(symbols)).delete(
            synchronize_session=False
        )
        session.commit()
        session.close()


def _import(db, monkeypatch, universe=None, **kwargs):
    monkeypatch.setattr(
        "app.services.universe.nifty500.build_universe",
        lambda: list(universe if universe is not None else CONSTITUENTS),
    )
    return Nifty500Importer(db).run(**kwargs)


class TestImport:
    def test_every_constituent_is_imported(self, db, monkeypatch):
        report = _import(db, monkeypatch)
        assert report.total_in_index == 3
        assert report.imported == 3
        assert report.failed == 0
        assert report.ok

    def test_all_required_fields_are_stored(self, db, monkeypatch):
        """The seven fields the brief names."""
        from app.models.company import Company

        _import(db, monkeypatch)
        company = db.query(Company).filter_by(ticker="ALPHA").one()
        assert company.ticker == "ALPHA"                      # NSE symbol
        assert company.bse_code == "500001"                   # BSE code
        assert company.isin == "INE000A01001"                 # ISIN
        assert company.name == "Alpha Industries Ltd."        # name
        assert company.sector == "Capital Goods"              # sector
        assert company.market_cap_category == "largecap"      # cap category
        assert company.listing_status == "active"             # listing status

    def test_index_membership_is_recorded(self, db, monkeypatch):
        """Records why a company is in the universe, so a later change is
        auditable."""
        from app.models.company import Company

        _import(db, monkeypatch)
        assert db.query(Company).filter_by(
            ticker="ALPHA"
        ).one().index_membership == INDEX_NAME

    def test_a_missing_bse_code_is_not_fatal(self, db, monkeypatch):
        """BSE Ltd and CDSL are genuinely NSE-only."""
        from app.models.company import Company

        report = _import(db, monkeypatch)
        assert db.query(Company).filter_by(ticker="GAMMA").one().bse_code is None
        assert "GAMMA" in report.bse_unmatched
        assert report.ok, "a missing BSE code must not fail the import"

    def test_bse_coverage_is_reported(self, db, monkeypatch):
        report = _import(db, monkeypatch)
        assert report.bse_matched == 2
        assert report.as_dict()["bse_coverage_percent"] == pytest.approx(66.7,
                                                                        abs=0.1)

    def test_industry_is_left_unset_rather_than_duplicating_sector(
        self, db, monkeypatch,
    ):
        """NSE supplies one taxonomy column. Writing it into both columns
        would make them agree by construction and tell a reader nothing."""
        from app.models.company import Company

        _import(db, monkeypatch)
        company = db.query(Company).filter_by(ticker="ALPHA").one()
        assert company.industry is None
        assert company.sector is not None


class TestIdempotency:
    def test_a_second_run_creates_nothing(self, db, monkeypatch):
        _import(db, monkeypatch)
        second = _import(db, monkeypatch)
        assert second.created == 0
        assert second.unchanged == 3

    def test_a_changed_field_is_reported_as_updated(self, db, monkeypatch):
        from app.models.company import Company

        _import(db, monkeypatch)
        renamed = list(CONSTITUENTS)
        renamed[0] = Constituent(
            "ALPHA", "Alpha Industries Limited", "Capital Goods",
            "INE000A01001", "largecap", "500001",
        )
        report = _import(db, monkeypatch, universe=renamed)
        assert report.updated == 1
        assert report.unchanged == 2
        assert db.query(Company).filter_by(
            ticker="ALPHA"
        ).one().name == "Alpha Industries Limited"

    def test_a_ticker_change_does_not_duplicate_the_security(self, db,
                                                             monkeypatch):
        """Matching on symbol alone would create a second row for the same
        company. ISIN is the security's legal identifier and is matched first.
        """
        from app.models.company import Company

        _import(db, monkeypatch)
        renamed = [Constituent("ALPHANEW", "Alpha Industries Ltd.",
                               "Capital Goods", "INE000A01001", "largecap",
                               "500001")]
        _import(db, monkeypatch, universe=renamed)

        rows = db.query(Company).filter(
            Company.isin == "INE000A01001"
        ).all()
        assert len(rows) == 1, "ticker change created a duplicate security"

    def test_a_dry_run_writes_nothing(self, db, monkeypatch):
        from app.models.company import Company

        report = _import(db, monkeypatch, dry_run=True)
        assert report.created == 3
        assert db.query(Company).filter_by(ticker="ALPHA").first() is None


class TestReportIntegrity:
    def test_categories_are_counted(self, db, monkeypatch):
        report = _import(db, monkeypatch)
        assert report.category_counts == {
            "largecap": 1, "midcap": 1, "smallcap": 1,
        }

    def test_sectors_are_counted(self, db, monkeypatch):
        report = _import(db, monkeypatch)
        assert report.sector_counts["Capital Goods"] == 1

    def test_a_failing_row_does_not_abandon_the_rest(self, db, monkeypatch):
        broken = list(CONSTITUENTS)
        # A name far longer than the column permits.
        broken.append(Constituent("DELTA", "D" * 500, "Services",
                                  "INE000D01004", "smallcap", "500004"))
        report = _import(db, monkeypatch, universe=broken)
        assert report.imported >= 3, "one bad row aborted the import"

    def test_the_report_serialises(self, db, monkeypatch):
        payload = _import(db, monkeypatch).as_dict()
        for key in ("total_in_index", "imported", "created", "updated",
                    "failed", "bse_matched", "bse_coverage_percent",
                    "category_counts", "sector_counts", "ok"):
            assert key in payload


class TestSourceProvenance:
    """Every field must come from an authoritative source."""

    def test_categories_come_from_nse_constituent_indices(self):
        """Not from a market-cap threshold we invented. The three lists
        partition the Nifty 500 exactly, so the classification is NSE's."""
        files = {name for name, _ in CATEGORY_SOURCES}
        assert files == {
            "ind_nifty100list.csv",
            "ind_niftymidcap150list.csv",
            "ind_niftysmallcap250list.csv",
        }
        labels = {label for _, label in CATEGORY_SOURCES}
        assert labels == {"largecap", "midcap", "smallcap"}

    def test_bse_codes_are_joined_on_isin(self):
        """Names differ across exchanges and symbols occasionally collide."""
        import inspect

        from app.services.universe import nifty500

        source = inspect.getsource(nifty500.fetch_bse_codes)
        assert "ISIN_NUMBER" in source
        assert "SCRIP_CD" in source

    def test_the_importer_does_not_ingest_or_score(self):
        """Import only, as instructed — no documents, no scoring, no R2."""
        import inspect

        from app.services.universe import nifty500

        source = inspect.getsource(nifty500)
        for forbidden in ("DocumentIngestionService", "ScoringService",
                          "ReplicationService", "collect_one"):
            assert forbidden not in source, (
                f"the importer references {forbidden}; it must import only"
            )
