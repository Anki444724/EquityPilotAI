"""Official filings layer: categories, confidence, classification, routing."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.data.filings.base import (
    CATEGORY_CONFIDENCE, Filing, FilingType, SourceCategory, classify_filing,
    confidence_for, parse_date, recency_factor,
)
from app.data.filings.router import FilingRouter, category_for_source


class TestSourceCategories:
    def test_every_category_the_brief_names_exists(self):
        values = {c.value for c in SourceCategory}
        assert values == {
            "Annual Report", "NSE Filing", "BSE Filing", "SEC Filing",
            "Market Data", "Internal Database",
        }

    def test_official_filings_outrank_third_party_apis(self):
        """The substance of the confidence model.

        An aggregator can be stale, can mis-map a ticker, and has no legal
        exposure if it is wrong. A regulator-lodged document has all three
        the other way round.
        """
        for official in (SourceCategory.ANNUAL_REPORT, SourceCategory.SEC_FILING,
                         SourceCategory.NSE_FILING, SourceCategory.BSE_FILING):
            assert (CATEGORY_CONFIDENCE[official]
                    > CATEGORY_CONFIDENCE[SourceCategory.MARKET_DATA])
            assert (CATEGORY_CONFIDENCE[official]
                    > CATEGORY_CONFIDENCE[SourceCategory.INTERNAL_DATABASE])

    def test_an_annual_report_is_the_strongest_evidence(self):
        assert CATEGORY_CONFIDENCE[SourceCategory.ANNUAL_REPORT] == max(
            CATEGORY_CONFIDENCE.values()
        )

    def test_market_data_names_are_mapped_to_categories(self):
        assert category_for_source("Finnhub") is SourceCategory.MARKET_DATA
        assert (category_for_source("Internal Financial Database")
                is SourceCategory.INTERNAL_DATABASE)
        assert category_for_source("SEC EDGAR") is SourceCategory.SEC_FILING
        assert category_for_source("NSE Corporate Filings") is SourceCategory.NSE_FILING


class TestConfidenceScoring:
    def test_recency_discounts_without_punishing(self):
        today = date(2026, 7, 31)
        assert recency_factor(today - timedelta(days=30), today=today) == 1.00
        assert recency_factor(today - timedelta(days=300), today=today) == 0.95
        assert recency_factor(today - timedelta(days=3000), today=today) == 0.70

    def test_an_unknown_date_is_mildly_discounted(self):
        assert 0.8 <= recency_factor(None) < 1.0

    def test_a_recent_annual_report_scores_highest(self):
        fresh = confidence_for(SourceCategory.ANNUAL_REPORT, filed=date.today())
        api = confidence_for(SourceCategory.MARKET_DATA, filed=date.today())
        assert fresh == pytest.approx(1.0)
        assert fresh > api

    def test_partial_evidence_scores_lower(self):
        full = confidence_for(SourceCategory.SEC_FILING, filed=date.today(),
                              completeness=1.0)
        partial = confidence_for(SourceCategory.SEC_FILING, filed=date.today(),
                                 completeness=0.4)
        assert partial < full

    def test_scores_stay_within_bounds(self):
        for category in SourceCategory:
            for completeness in (0.0, 0.5, 1.0, 2.0):
                score = confidence_for(category, completeness=completeness)
                assert 0.0 <= score <= 1.0


class TestFilingClassification:
    @pytest.mark.parametrize("form,expected", [
        ("10-K", FilingType.ANNUAL_REPORT),
        ("20-F", FilingType.ANNUAL_REPORT),
        ("10-Q", FilingType.QUARTERLY_RESULTS),
        ("8-K", FilingType.PRESS_RELEASE),
    ])
    def test_the_regulator_form_code_wins(self, form, expected):
        """A form code is unambiguous in a way a free-text title never is."""
        assert classify_filing("anything at all", form=form) is expected

    @pytest.mark.parametrize("title,expected", [
        ("Integrated Annual Report 2025-26", FilingType.ANNUAL_REPORT),
        ("Unaudited Financial Results for the quarter ended June 2026",
         FilingType.QUARTERLY_RESULTS),
        ("Investor Presentation Q1 FY27", FilingType.INVESTOR_PRESENTATION),
        ("Press Release: new energy complex", FilingType.PRESS_RELEASE),
        ("Shareholders meeting", FilingType.CORPORATE_ANNOUNCEMENT),
    ])
    def test_titles_are_classified(self, title, expected):
        assert classify_filing(title) is expected

    def test_a_presentation_about_results_is_a_presentation(self):
        """Ordering matters: the more specific pattern is tested first."""
        assert (classify_filing("Earnings presentation for Q1 results")
                is FilingType.INVESTOR_PRESENTATION)


class TestDateParsing:
    @pytest.mark.parametrize("value,expected", [
        ("2026-07-31", date(2026, 7, 31)),
        ("25-Jul-2026 14:37:56", date(2026, 7, 25)),
        ("31/07/2026", date(2026, 7, 31)),
        ("", None),
        (None, None),
        ("not a date", None),
    ])
    def test_the_shapes_these_sources_actually_use(self, value, expected):
        assert parse_date(value) == expected


class TestCitations:
    def test_a_citation_is_checkable(self):
        """Category, document and regulator reference — enough to find the
        original without trusting this platform's rendering of it."""
        filing = Filing(
            category=SourceCategory.SEC_FILING,
            filing_type=FilingType.ANNUAL_REPORT,
            title="10-K — Apple Inc.",
            reference="0000320193-25-000079",
            filed_on=date(2025, 10, 31),
            period="2025-09-27",
        )
        citation = filing.citation()
        assert "SEC Filing" in citation
        assert "0000320193-25-000079" in citation
        assert "2025-10-31" in citation

    def test_serialisation_carries_confidence_and_citation(self):
        payload = Filing(
            category=SourceCategory.NSE_FILING,
            filing_type=FilingType.CORPORATE_ANNOUNCEMENT,
            title="Shareholders meeting",
            filed_on=date.today(),
        ).as_dict()
        assert payload["category"] == "NSE Filing"
        assert payload["confidence"] > 0
        assert payload["citation"]


class TestRouting:
    def test_indian_companies_prefer_uploaded_reports_then_the_exchanges(self):
        chain = [p.name for p in FilingRouter().chain_for("India")]
        assert chain[0] == "Uploaded Annual Reports (RAG)"
        assert "NSE Corporate Filings" in chain
        assert "BSE Corporate Announcements" in chain

    def test_us_companies_prefer_sec_edgar(self):
        chain = [p.name for p in FilingRouter().chain_for("United States")]
        assert chain[0] == "SEC EDGAR"

    def test_sec_is_not_consulted_for_an_indian_listing(self):
        """Asking EDGAR about an NSE listing wastes a request and muddies
        the audit trail."""
        assert "SEC EDGAR" not in [
            p.name for p in FilingRouter().chain_for("India")
        ]

    def test_providers_declare_the_markets_they_cover(self):
        router = FilingRouter()
        assert "United States" in router.sec.markets
        assert "India" in router.nse.markets
        assert "India" in router.bse.markets

    def test_an_unreachable_provider_degrades_rather_than_raising(self):
        """Neither exchange publishes a documented API, so failure is
        expected and must never propagate."""
        router = FilingRouter()
        result = router.bse.fetch("NOSUCHSYMBOL")
        assert result.found is False
        assert result.error


class TestSECProvider:
    def test_identity_encoding_is_requested(self):
        """urllib does not decompress transparently.

        Advertising gzip made EDGAR reply with a gzip stream that json.load
        rejected, and every US filing silently returned nothing.
        """
        from app.data.filings.sec import _HEADERS

        assert _HEADERS["Accept-Encoding"] == "identity"

    def test_a_user_agent_identifies_the_caller(self):
        """EDGAR's fair-access policy refuses anonymous clients."""
        from app.data.filings.sec import _HEADERS

        assert "IERP" in _HEADERS["User-Agent"]
        assert "@" in _HEADERS["User-Agent"]
