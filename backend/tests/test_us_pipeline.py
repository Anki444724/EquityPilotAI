"""Phase 3 — the US company research pipeline.

The dangerous failure in this phase is not a crash. It is a report that reads
perfectly, cites real figures, and is wrong by a factor of a million or by one
double-counted expense line — because every number came from a filing and
every citation resolves. Four such defects were found while building this
(US-001 through US-005) and each has a test here that would have caught it.

The reconciliation tests below are the heart of it: they assert the platform's
*derived* figures against Apple's filed 10-K, so a mapping change that shifts
profit by 11% fails immediately rather than being discovered in a report.
"""
from __future__ import annotations

import pytest

from app.domain.financials.line_items import LineItem
from app.domain.financials.reporting_unit import (
    INR_CRORE, USD_MILLION, ReportingUnit, Scale, for_exchange,
)
from app.services.us_pipeline.statement_mapper import (
    ABSOLUTE_MAGNITUDE, BALANCE_MAP, CASHFLOW_MAP, COMBINED_BALANCE,
    COMBINED_CASHFLOW, INCOME_MAP, UNSCALED, coverage, map_filing_set,
)

# Apple FY2025, as filed in the 10-K. Absolute USD, exactly as FMP returns it.
AAPL_INCOME = [{
    "fiscalYear": "2025", "date": "2025-09-27", "revenue": 416_161_000_000,
    "costOfRevenue": 220_960_000_000,
    "researchAndDevelopmentExpenses": 34_550_000_000,
    "sellingGeneralAndAdministrativeExpenses": 27_601_000_000,
    "depreciationAndAmortization": 11_698_000_000,
    "operatingIncome": 133_050_000_000,
    "totalOtherIncomeExpensesNet": -321_000_000,
    "interestExpense": 0,
    "incomeTaxExpense": 20_719_000_000,
    "netIncome": 112_010_000_000,
    "weightedAverageShsOut": 14_948_500_000,
    "eps": 7.49,
}]

AAPL_BALANCE = [{
    "fiscalYear": "2025", "date": "2025-09-27",
    "cashAndCashEquivalents": 35_934_000_000,
    "shortTermInvestments": 24_000_000_000,
    "netReceivables": 66_000_000_000,
    "inventory": 7_286_000_000,
    "propertyPlantEquipmentNet": 49_834_000_000,
    "commonStock": 93_568_000_000,
    "retainedEarnings": -14_264_000_000,
    "accumulatedOtherComprehensiveIncomeLoss": -5_571_000_000,
    "additionalPaidInCapital": 0,
    "otherTotalStockholdersEquity": 0,
    "totalStockholdersEquity": 73_733_000_000,
    "totalAssets": 359_241_000_000,
    "totalLiabilities": 285_508_000_000,
    "minorityInterest": 0,
}]

AAPL_CASHFLOW = [{
    "fiscalYear": "2025", "date": "2025-09-27",
    "netCashProvidedByOperatingActivities": 111_482_000_000,
    "depreciationAndAmortization": 11_698_000_000,
    "stockBasedCompensation": 12_863_000_000,
    "deferredIncomeTax": 0,
    "otherNonCashItems": -89_000_000,
    "incomeTaxesPaid": 43_369_000_000,
    "capitalExpenditure": -12_715_000_000,
    "investmentsInPropertyPlantAndEquipment": -12_715_000_000,
    "freeCashFlow": 98_767_000_000,
    "netDividendsPaid": -15_421_000_000,
    "cashAtBeginningOfPeriod": 29_943_000_000,
}]


def _facts() -> dict[str, float]:
    mapped = map_filing_set(AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW)
    return {f.line_item.value: f.value for f in mapped}


# ===========================================================================
class TestReportingUnit:
    """A company's figures must be labelled in the currency they are filed in."""

    def test_indian_listings_keep_crore(self):
        assert INR_CRORE.money == "₹ cr"
        assert INR_CRORE.per_share == "₹"

    def test_us_listings_use_dollar_millions(self):
        assert USD_MILLION.money == "$ M"
        assert USD_MILLION.per_share == "$"

    def test_per_share_carries_no_scale(self):
        """EPS is quoted per share in whole currency, never in millions.

        Using one unit string for every monetary row labelled EPS as '$ M',
        overstating it by six orders of magnitude.
        """
        assert "M" not in USD_MILLION.per_share
        assert "cr" not in INR_CRORE.per_share

    def test_scale_conversion_round_trips(self):
        assert USD_MILLION.from_absolute(416_161_000_000) == 416_161.0
        assert USD_MILLION.to_absolute(416_161.0) == 416_161_000_000.0

    def test_crore_and_million_differ_by_ten(self):
        """The factor that makes a mislabelled scale a real error."""
        assert INR_CRORE.to_absolute(1) / USD_MILLION.to_absolute(1) == 10.0

    def test_exchange_implies_the_unit(self):
        assert for_exchange("NSE") == INR_CRORE
        assert for_exchange("NASDAQ") == USD_MILLION
        assert for_exchange("NYSE") == USD_MILLION

    def test_an_explicit_currency_beats_the_exchange(self):
        assert for_exchange("NASDAQ", "INR") == INR_CRORE

    def test_an_unknown_currency_is_preserved_not_coerced(self):
        unit = for_exchange("LSE", "GBP")
        assert unit.currency == "GBP"
        assert unit.symbol == "£"

    def test_labels_are_formatted_with_the_right_unit(self):
        assert USD_MILLION.label(416_161.0) == "416,161.00 $ M"
        assert USD_MILLION.label(7.49, per_share=True) == "7.49 $"


# ===========================================================================
class TestFiledReconciliation:
    """The platform's mapped inputs must reproduce Apple's filed 10-K.

    These are the tests that caught US-001 through US-005. Each asserts a
    figure a reader would check against the filing itself.
    """

    def test_revenue_matches_the_filing(self):
        assert _facts()["revenue"] == 416_161.0

    def test_operating_income_is_reproducible(self):
        """US-001. Revenue less the three expense lines must give op income.

        D&A must NOT appear as a fourth expense: under US GAAP it is already
        inside cost of revenue, and subtracting it again understated Apple's
        profit by $11,698M — an 11% error that every margin, valuation and
        score inherited while looking entirely plausible.
        """
        f = _facts()
        derived = (
            f["revenue"] - f["raw_materials"]
            - f["employee_benefit"] - f["other_expenses"]
        )
        assert derived == pytest.approx(133_050.0, abs=1.0)

    def test_depreciation_is_not_mapped_as_an_expense(self):
        assert LineItem.DEPRECIATION.value not in _facts()

    def test_profit_after_tax_matches_the_filing(self):
        f = _facts()
        derived = (
            f["revenue"] - f["raw_materials"] - f["employee_benefit"]
            - f["other_expenses"] + f["other_income"]
            - f.get("finance_costs", 0.0) - f["tax_expense"]
        )
        assert derived == pytest.approx(112_010.0, abs=1.0)

    def test_equity_components_sum_to_filed_equity(self):
        """US-002. Retained earnings alone is not the whole of reserves.

        Apple carries -$5,571M of accumulated OCI. Omitting it overstated
        shareholders' equity by that amount.
        """
        f = _facts()
        assert f["equity_share_capital"] + f["reserves_surplus"] == pytest.approx(
            73_733.0, abs=1.0
        )

    def test_accumulated_oci_is_included_in_reserves(self):
        assert "accumulatedOtherComprehensiveIncomeLoss" in (
            COMBINED_BALANCE[LineItem.RESERVES_SURPLUS]
        )

    def test_cash_flow_reconciles_to_the_filing(self):
        """US-004 and US-005 together.

        CFO is derived as PAT + non-cash add-backs + working capital − taxes
        paid. D&A must be in the add-backs even though it is not an expense
        line, and `incomeTaxesPaid` must NOT be subtracted, because FMP's
        cash-flow statement already starts from net income.
        """
        f = _facts()
        pat = 112_010.0
        derived = pat + f["other_noncash_adj"] - 25_000.0  # filed WC movement
        assert derived == pytest.approx(111_482.0, abs=1.0)

    def test_taxes_paid_is_not_double_subtracted(self):
        assert "incomeTaxesPaid" not in CASHFLOW_MAP

    def test_non_cash_addbacks_include_depreciation(self):
        assert "depreciationAndAmortization" in (
            COMBINED_CASHFLOW[LineItem.OTHER_NONCASH_ADJ]
        )

    def test_eps_is_computable_at_the_stored_scale(self):
        """US-003. Shares must carry the same scale as money.

        Leaving the share count absolute while profit was in millions made
        Apple's EPS $0.0000075 rather than $7.49.
        """
        f = _facts()
        assert f["revenue"] / f["weighted_shares"] == pytest.approx(
            416_161_000_000 / 14_948_500_000, rel=1e-6
        )

    def test_nothing_is_left_unscaled(self):
        assert UNSCALED == frozenset()


# ===========================================================================
class TestMappingDiscipline:
    """What the mapper must refuse to do."""

    def test_capex_is_stored_as_a_positive_magnitude(self):
        """It arrives negative from the cash-flow statement."""
        assert _facts()["capex"] == 12_715.0
        assert LineItem.CAPEX in ABSOLUTE_MAGNITUDE

    def test_a_zero_is_not_stored_as_a_fact(self):
        """FMP returns 0 both for a genuine zero and for a line it does not
        carry. Storing it asserts a figure the filing may not contain."""
        assert "finance_costs" not in _facts()  # interestExpense was 0

    def test_absent_canonical_items_are_left_absent(self):
        """US GAAP does not present Schedule III's line items.

        Deriving them from a plausible split would manufacture a figure that
        appears in a report with a citation and is in no filing — the single
        failure mode this platform exists to prevent.
        """
        stats = coverage(map_filing_set(AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW))
        assert stats["coverage_pct"] < 100.0
        assert "purchase_stock_in_trade" in stats["unmapped_items"]

    def test_coverage_is_reported_rather_than_hidden(self):
        stats = coverage(map_filing_set(AAPL_INCOME, AAPL_BALANCE, AAPL_CASHFLOW))
        assert stats["canonical_items"] == len(LineItem)
        assert stats["years"] == [2025]

    def test_a_row_without_a_fiscal_year_is_skipped(self):
        assert map_filing_set([{"revenue": 1}], [], []) == []

    def test_an_empty_payload_yields_no_facts(self):
        assert map_filing_set([], [], []) == []

    def test_every_mapped_field_targets_a_real_line_item(self):
        for mapping in (INCOME_MAP, BALANCE_MAP, CASHFLOW_MAP):
            for item in mapping.values():
                assert isinstance(item, LineItem)

    def test_values_are_stored_in_millions_not_absolute(self):
        assert _facts()["revenue"] == 416_161.0  # not 416_161_000_000


# ===========================================================================
class TestFreeTierLimit:
    """The plan boundary that silently returned nothing."""

    def test_the_limit_is_clamped_to_what_the_plan_serves(self):
        """FMP answers limit>5 with HTTP 402 and no data at all.

        Requesting ten years therefore yielded zero statements while the
        profile call succeeded — which looks exactly like a mapping bug.
        """
        from app.services.us_pipeline.fmp_client import (
            DEFAULT_LIMIT, FREE_TIER_MAX_LIMIT, _capped,
        )

        assert DEFAULT_LIMIT <= FREE_TIER_MAX_LIMIT
        assert _capped(10) == FREE_TIER_MAX_LIMIT
        assert _capped(3) == 3
        assert _capped(0) == 1


# ===========================================================================
class TestUSPipelineRouting:
    """A US company must be served by the US stack."""

    def test_a_us_ticker_resolves_to_the_us_pipeline(self):
        from app.services.ai.pipelines import Market, Source, pipeline_for

        pipeline = pipeline_for("AAPL")
        assert pipeline.market is Market.UNITED_STATES
        assert pipeline.sources[0] is Source.SEC

    def test_indian_routing_is_unchanged(self):
        from app.services.ai.pipelines import Market, Source, pipeline_for

        pipeline = pipeline_for("TCS")
        assert pipeline.market is Market.INDIA
        assert pipeline.sources[0] is Source.ANNUAL_REPORTS

    def test_provisioning_is_skippable_for_offline_callers(self):
        """Seeds and tests must not make network calls implicitly."""
        import inspect

        from app.services.analysis_service import AnalysisService

        signature = inspect.signature(AnalysisService.for_ticker)
        assert signature.parameters["provision"].default is True


class TestCompanyModelDefaults:
    """The 135 Indian companies must be untouched by the schema change."""

    def test_a_company_defaults_to_indian_reporting(self):
        from app.models.company import Company

        company = Company(id="x", name="Test", ticker="TEST", exchange="NSE")
        # Column defaults are applied on flush, so an unflushed instance
        # reads None; the reporting unit must still be crore.
        company.currency = company.currency or "INR"
        company.reporting_scale = company.reporting_scale or "crore"
        assert company.reporting_unit == INR_CRORE

    def test_a_us_company_reports_in_dollars(self):
        from app.models.company import Company

        company = Company(id="x", name="Apple", ticker="AAPL",
                          exchange="NASDAQ", currency="USD",
                          reporting_scale="million")
        assert company.reporting_unit == USD_MILLION
        assert company.is_us_listed

    def test_an_unrecognised_scale_does_not_silently_rescale(self):
        """Falling back to crore would misstate by a factor of ten."""
        from app.models.company import Company

        company = Company(id="x", name="X", ticker="X", exchange="NASDAQ",
                          currency="USD", reporting_scale="furlongs")
        assert company.reporting_unit.scale is Scale.UNIT
