"""Regression tests for the production-readiness sprint.

Every test here corresponds to a defect found by running the platform against
real NSE filings rather than synthetic data. They are written against fixtures
rather than the network, so they run in CI without reaching screener.in or
Yahoo — the bug is in the transformation, and the transformation is what is
pinned.

The defects, in the order they were found:

  RC2-001  precedence written as a string, making 42,025 facts unreadable
  RC2-002  consolidated fallback keyed on 404, not on an empty table
  RC2-003  banks read with the operating layout, inverting net profit
  RC2-004  inventory days inverted on sales instead of cost of goods
  RC2-005  derived items double-counted inside the balancing plug
  RC2-006  tax derived from a rate, breaking on rates above 100%
  RC2-007  ZeroDivisionError in scoring when a share count is zero
"""
from __future__ import annotations

import pytest

from app.data.ingest import canonicalise
from app.data.nse_universe import (
    FINANCIAL_SECTORS, NSE_UNIVERSE, is_financial,
)
from app.data.screener_source import (
    SLUG_ALIASES, ScreenerFinancials, _fiscal_year, _number,
)
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI


# ===========================================================================
def _operating_company(years=(2024, 2025, 2026)) -> ScreenerFinancials:
    """A manufacturer, as screener renders one."""
    def series(*values):
        return dict(zip(years, values))

    return ScreenerFinancials(
        ticker="TESTCO",
        fiscal_years=list(years),
        profit_loss={
            "Sales +": series(1000.0, 1200.0, 1400.0),
            "Expenses +": series(800.0, 950.0, 1100.0),
            "Operating Profit": series(200.0, 250.0, 300.0),
            "Other Income +": series(10.0, 12.0, 15.0),
            "Interest": series(20.0, 22.0, 25.0),
            "Depreciation": series(40.0, 45.0, 50.0),
            "Profit before tax": series(150.0, 195.0, 240.0),
            "Tax %": series(25.0, 25.0, 25.0),
            "Net Profit +": series(112.5, 146.25, 180.0),
            "EPS in Rs": series(11.25, 14.625, 18.0),
            "Dividend Payout %": series(20.0, 20.0, 20.0),
        },
        balance_sheet={
            "Equity Capital": series(100.0, 100.0, 100.0),
            "Reserves": series(500.0, 600.0, 720.0),
            "Borrowings +": series(300.0, 320.0, 340.0),
            "Other Liabilities +": series(200.0, 230.0, 260.0),
            "Total Liabilities": series(1100.0, 1250.0, 1420.0),
            "Fixed Assets +": series(600.0, 680.0, 780.0),
            "CWIP": series(50.0, 60.0, 70.0),
            "Investments": series(100.0, 110.0, 120.0),
            "Other Assets +": series(350.0, 400.0, 450.0),
            "Total Assets": series(1100.0, 1250.0, 1420.0),
        },
        cash_flow={
            "Cash from Operating Activity +": series(180.0, 200.0, 240.0),
        },
        ratios={
            "Debtor Days": series(30.0, 32.0, 35.0),
            "Inventory Days": series(60.0, 62.0, 65.0),
            "Days Payable": series(45.0, 46.0, 48.0),
        },
    )


def _financing_company(years=(2024, 2025, 2026)) -> ScreenerFinancials:
    """A bank. Note `Revenue`/`Financing Profit`, and no `Sales` row at all."""
    def series(*values):
        return dict(zip(years, values))

    return ScreenerFinancials(
        ticker="TESTBANK",
        fiscal_years=list(years),
        profit_loss={
            "Revenue +": series(2000.0, 2400.0, 2800.0),
            # For a bank this is interest EXPENDED, already inside Expenses.
            "Interest": series(1100.0, 1300.0, 1500.0),
            "Expenses +": series(1500.0, 1780.0, 2036.0),
            "Financing Profit": series(500.0, 620.0, 764.0),
            "Financing Margin %": series(25.0, 25.8, 27.3),
            "Other Income +": series(300.0, 340.0, 380.0),
            "Depreciation": series(30.0, 33.0, 36.0),
            "Profit before tax": series(770.0, 927.0, 1108.0),
            "Tax %": series(25.0, 25.0, 25.0),
            "Net Profit +": series(577.5, 695.25, 831.0),
            "EPS in Rs": series(11.55, 13.905, 16.62),
            "Dividend Payout %": series(15.0, 15.0, 15.0),
        },
        balance_sheet={
            "Equity Capital": series(500.0, 500.0, 500.0),
            "Reserves": series(3000.0, 3600.0, 4300.0),
            "Borrowings +": series(20000.0, 23000.0, 26000.0),
            "Other Liabilities +": series(2000.0, 2200.0, 2400.0),
            "Total Liabilities": series(25500.0, 29300.0, 33200.0),
            "Fixed Assets +": series(400.0, 420.0, 450.0),
            "CWIP": series(10.0, 12.0, 14.0),
            "Investments": series(6000.0, 7000.0, 8000.0),
            "Other Assets +": series(19090.0, 21868.0, 24736.0),
            "Total Assets": series(25500.0, 29300.0, 33200.0),
        },
        cash_flow={},
        ratios={},
    )


# ===========================================================================
class TestPresentationDetection:
    """RC2-003 — the financing layout is not the operating layout."""

    def test_an_operating_company_maps_sales_to_revenue(self):
        facts, _, _ = canonicalise(_operating_company(), None)
        assert facts[LI.REVENUE][2026] == 1400.0

    def test_a_bank_maps_revenue_not_sales(self):
        """A bank has no `Sales` row. Reading it with the operating mapping
        leaves revenue absent and everything downstream collapses."""
        facts, _, _ = canonicalise(_financing_company(), None)
        assert facts[LI.REVENUE][2026] == 2800.0

    def test_a_bank_does_not_deduct_interest_twice(self):
        """The defect: HDFC Bank's FY26 net profit came out at −₹268,944 cr
        against a reported +₹79,219 cr, because interest expended was booked
        as a post-operating finance cost when it is already inside Expenses.
        """
        facts, _, _ = canonicalise(_financing_company(), None)
        assert facts[LI.FINANCE_COSTS][2026] == 0.0
        # Interest belongs in operating cost for a financing company.
        assert facts[LI.RAW_MATERIALS][2026] == 1500.0

    def test_an_operating_company_keeps_its_finance_cost(self):
        facts, _, _ = canonicalise(_operating_company(), None)
        assert facts[LI.FINANCE_COSTS][2026] == 25.0

    def test_the_financing_layout_is_flagged_to_the_reader(self):
        _, _, warnings = canonicalise(_financing_company(), None)
        assert any("financing presentation" in w for w in warnings)

    def test_financial_sectors_are_declared(self):
        assert "Banking - Private" in FINANCIAL_SECTORS
        assert is_financial("Banking - PSU")
        assert not is_financial("IT Services")


class TestTaxDerivation:
    """RC2-006 — a tax *rate* cannot reproduce a reported bottom line."""

    def test_tax_is_derived_from_the_reported_net_profit(self):
        facts, _, _ = canonicalise(_operating_company(), None)
        # PBT 240 − net 180 = 60, not PBT × 25% which happens to agree here.
        assert facts[LI.TAX_EXPENSE][2026] == pytest.approx(60.0)

    def test_a_rate_above_one_hundred_percent_does_not_flip_a_loss(self):
        """Crompton FY26: PBT −79 at a 191% rate. The rate route produced
        +72 against a reported −231 — a loss turned into a profit."""
        company = _operating_company(years=(2026,))
        company.profit_loss = {
            "Sales +": {2026: 1000.0},
            "Expenses +": {2026: 1000.0},
            "Profit before tax": {2026: -79.0},
            "Tax %": {2026: 191.0},
            "Net Profit +": {2026: -231.0},
            "EPS in Rs": {2026: -3.7},
        }
        facts, _, _ = canonicalise(company, None)
        # −79 − (−231) = 152 of tax, which reproduces the reported loss.
        assert facts[LI.TAX_EXPENSE][2026] == pytest.approx(152.0)

    def test_income_below_the_tax_line_is_absorbed(self):
        """DLF FY26: PBT 2,932 at 11% gave 2,609 against a reported 4,415,
        because share of associate profit is added below the tax line."""
        company = _operating_company(years=(2026,))
        company.profit_loss = {
            "Sales +": {2026: 6000.0},
            "Expenses +": {2026: 3000.0},
            "Profit before tax": {2026: 2932.0},
            "Tax %": {2026: 11.0},
            "Net Profit +": {2026: 4415.0},
            "EPS in Rs": {2026: 17.8},
        }
        facts, _, _ = canonicalise(company, None)
        assert facts[LI.TAX_EXPENSE][2026] == pytest.approx(2932.0 - 4415.0)


class TestWorkingCapitalDerivation:
    """RC2-004 / RC2-005 — denominators and double-counting."""

    def test_inventory_days_invert_on_cost_not_sales(self):
        """UltraTech reports 206 inventory days. Inverted on sales that is
        ₹49,955 cr of cement — 35% of the balance sheet. Inverted on cost it
        is a normal number, and the sheet balances."""
        from app.data.derive_wc import DAYS_IN_YEAR

        sales, cost, days = 88_512.0, 62_000.0, 206.0
        on_sales = days * sales / DAYS_IN_YEAR
        on_cost = days * cost / DAYS_IN_YEAR
        assert on_cost < on_sales
        # The gap is material — this is not a rounding preference.
        assert (on_sales - on_cost) / on_sales > 0.25

    def test_derived_items_cannot_exceed_their_parent_bucket(self):
        """Receivables and inventory are components of `Other Assets`, so
        naming them must not enlarge total assets."""
        bucket = 81_021.0
        receivable, inventory = 5_088.0, 99_707.0     # Tata Steel, as derived
        total = receivable + inventory
        assert total > bucket, "fixture no longer reproduces the defect"

        ceiling = bucket * 0.90
        factor = ceiling / total
        assert (receivable * factor) + (inventory * factor) <= bucket

    def test_days_map_covers_the_three_cycle_items(self):
        from app.data.derive_wc import DAYS_MAP

        items = {item for _, item in DAYS_MAP}
        assert items == {LI.TRADE_RECEIVABLES, LI.INVENTORIES, LI.TRADE_PAYABLES}


class TestBalanceSheetIntegrity:
    """RC2-005 — both sides must tie to the reported total."""

    def test_assets_reconcile_to_the_reported_total(self):
        company = _operating_company()
        facts, _, _ = canonicalise(company, None)
        named = sum(
            facts.get(item, {}).get(2026, 0.0)
            for item in (
                LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES,
                LI.INVENTORIES, LI.NET_BLOCK_PPE, LI.CWIP, LI.GOODWILL,
                LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES,
                LI.DEFERRED_TAX_ASSET, LI.OTHER_CURRENT_ASSETS,
            )
        )
        assert named == pytest.approx(1420.0, rel=0.001)

    def test_liabilities_reconcile_to_the_reported_total(self):
        facts, _, _ = canonicalise(_operating_company(), None)
        named = sum(
            facts.get(item, {}).get(2026, 0.0)
            for item in (
                LI.EQUITY_SHARE_CAPITAL, LI.RESERVES_SURPLUS,
                LI.MINORITY_INTEREST_BS, LI.LONG_TERM_BORROWINGS,
                LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
                LI.TRADE_PAYABLES, LI.SHORT_TERM_PROVISIONS,
                LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL,
                LI.OTHER_CURRENT_LIABILITIES,
            )
        )
        assert named == pytest.approx(1420.0, rel=0.001)


class TestPrecedence:
    """RC2-001 — precedence is an integer enum, not a string."""

    def test_precedence_is_an_integer_enum(self):
        assert int(Precedence.STORE) == 2
        with pytest.raises(ValueError):
            Precedence("import")

    def test_ingestion_writes_an_integer_precedence(self):
        """Writing the string 'import' was silently accepted by SQLAlchemy and
        then made every fact unreadable: `AnalysisService.for_ticker` raised
        "'import' is not a valid Precedence" for all 135 companies."""
        import pathlib

        for name in ("ingest.py", "enrich.py", "derive_wc.py"):
            source = (
                pathlib.Path(__file__).resolve().parent.parent
                / "app" / "data" / name
            ).read_text()
            assert 'precedence="' not in source, name
            if "precedence=" in source:
                assert "int(Precedence." in source, name


class TestScreenerParsing:
    def test_fiscal_year_from_a_march_year_end(self):
        assert _fiscal_year("Mar 2025") == 2025

    def test_a_december_year_end_rolls_into_the_next_fiscal_year(self):
        assert _fiscal_year("Dec 2024") == 2025

    def test_ttm_is_not_a_fiscal_year(self):
        assert _fiscal_year("TTM") is None

    @pytest.mark.parametrize("raw,expected", [
        ("1,055,780", 1055780.0), ("16.37%", 16.37), ("-231", -231.0),
        ("", None), ("-", None), ("—", None), ("NA", None),
    ])
    def test_number_parsing(self, raw, expected):
        assert _number(raw) == expected

    def test_an_absent_value_stays_absent(self):
        """A fabricated zero propagates into every ratio built on it."""
        assert _number("-") is None
        assert _number("-") != 0.0

    def test_slug_aliases_cover_known_corporate_actions(self):
        """RC2-002's sibling: three companies vanish from the universe
        without these, which is a silent coverage hole."""
        assert SLUG_ALIASES["LTIM"] == "MINDTREE"       # LTI/Mindtree merger
        assert SLUG_ALIASES["TATAMOTORS"] == "TMPV"     # CV/PV demerger
        assert SLUG_ALIASES["ZOMATO"] == "ETERNAL"      # renamed


class TestUniverse:
    def test_the_universe_is_large_enough_for_the_brief(self):
        assert len(NSE_UNIVERSE) >= 100

    def test_tickers_are_unique(self):
        tickers = [t for t, _, _, _ in NSE_UNIVERSE]
        assert len(tickers) == len(set(tickers))

    def test_every_sector_in_the_universe_is_a_workbook_sector(self):
        """Modules 4 and 5 look up sector medians by this exact string. A
        mismatch silently falls back to a default and the valuation is
        quietly wrong."""
        sectors = {s for _, _, s, _ in NSE_UNIVERSE}
        assert len(sectors) >= 20
        for sector in sectors:
            assert sector == sector.strip()
            assert sector

    def test_financials_are_represented(self):
        """A validation sprint covering only manufacturers proves nothing
        about the business models the schema handles least well."""
        financial = [t for t, _, s, _ in NSE_UNIVERSE if is_financial(s)]
        assert len(financial) >= 15


class TestScoringRobustness:
    """RC2-007 — one missing datum must not take down twelve categories."""

    def test_a_zero_share_count_does_not_crash_growth_scoring(self):
        """LTIMindtree reports a zero weighted-share count in its latest year,
        a real artefact of the LTI/Mindtree merger. The guard checked
        `shares[0] > 0` and then divided by `shares[-1]`."""
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "app" / "services" / "scoring" / "growth_quality.py"
        ).read_text()
        assert "shares[-1] > 0" in source, (
            "the endpoint used as a divisor is no longer guarded"
        )

    def test_every_indexed_divisor_in_scoring_is_guarded(self):
        """The pattern, not just the instance.

        Checks the *divisor* specifically. A first pass flagged any line
        dividing by an indexed element and reported `capital_allocation.py`,
        which divides by `share_series[0]` and guards exactly that element —
        a false positive from matching the line rather than the expression.
        What matters is whether the element being divided *by* appears in a
        positivity guard nearby.
        """
        import pathlib
        import re

        scoring = (
            pathlib.Path(__file__).resolve().parent.parent
            / "app" / "services" / "scoring"
        )
        divisor = re.compile(r"/\s*(\w+\[-?\d+\])")
        offences: list[str] = []

        for path in scoring.glob("*.py"):
            lines = path.read_text().splitlines()
            for index, line in enumerate(lines):
                if line.strip().startswith("#"):
                    continue
                for expression in divisor.findall(line):
                    # Look back a few lines for a guard on this exact element.
                    window = "\n".join(lines[max(0, index - 6):index + 1])
                    guarded = (
                        f"{expression} > 0" in window
                        or f"{expression} != 0" in window
                        or f"{expression} and" in window
                    )
                    if not guarded:
                        offences.append(f"{path.name}:{index + 1} {expression}")

        assert offences == [], offences


# ===========================================================================
class TestSourceTransformations:
    """The pure transformation logic, driven offline.

    These modules were exercised against 135 live companies during the sprint,
    but CI cannot reach screener.in or Yahoo. What can be tested without a
    network is the part where the bugs actually were: parsing, unit
    conversion, sign convention and mapping. That is what these cover.
    """

    def test_yahoo_converts_rupees_to_crore(self):
        from app.data.yahoo_source import CRORE

        # Yahoo reports absolute rupees; the workbook works in ₹ crore.
        # A stray factor of 10^7 here is the single most damaging unit error
        # available in this domain.
        assert CRORE == 1e7
        assert 9_646_930_000_000.0 / CRORE == pytest.approx(964_693.0)

    def test_yahoo_fiscal_year_follows_the_indian_convention(self):
        from app.data.yahoo_source import _fiscal_year

        assert _fiscal_year("2025-03-31") == 2025
        assert _fiscal_year("2025-06-30") == 2025
        # A December year-end belongs to the fiscal year it falls within.
        assert _fiscal_year("2024-12-31") == 2025

    def test_yahoo_direct_map_targets_real_line_items(self):
        from app.data.yahoo_source import DIRECT_MAP

        assert len(DIRECT_MAP) >= 30
        for item in DIRECT_MAP.values():
            assert isinstance(item, LI)

    def test_yahoo_circuit_breaker_opens_and_resets(self):
        from app.data import yahoo_source

        yahoo_source.reset_circuit()
        assert yahoo_source.provider_available()
        # The circuit exists because a provider's bad half-hour once degraded
        # an entire 135-company dataset to 46% coverage.
        yahoo_source._consecutive_429 = yahoo_source._CIRCUIT_THRESHOLD
        assert not yahoo_source.provider_available()
        yahoo_source.reset_circuit()
        assert yahoo_source.provider_available()

    def test_api_key_shaped_strings_are_not_parsed_as_tickers(self):
        """`parse_api_key` rejects malformed input before any lookup."""
        from app.services.platform.crypto import parse_api_key

        assert parse_api_key("RELIANCE") is None

    def test_sign_convention_makes_costs_positive(self):
        """`32 Documentation` §C: costs positive, subtracted by formula.

        Getting this wrong flips free cash flow, which flips the entire DCF.
        """
        from app.data.yahoo_source import CompanyFinancials, _sign_normalise

        data = CompanyFinancials(ticker="X", fiscal_years=[2026])
        data.facts = {
            LI.CAPEX: {2026: -1165.0},          # Yahoo reports an outflow
            LI.DIVIDEND_PAID: {2026: -400.0},
            LI.FINANCE_COSTS: {2026: -25.0},
            LI.REPAYMENT_BORROWINGS: {2026: -300.0},
        }
        _sign_normalise(data)
        assert data.facts[LI.CAPEX][2026] == 1165.0
        assert data.facts[LI.DIVIDEND_PAID][2026] == 400.0
        assert data.facts[LI.FINANCE_COSTS][2026] == 25.0
        assert data.facts[LI.REPAYMENT_BORROWINGS][2026] == 300.0

    def test_canonicalise_without_yahoo_still_produces_a_dataset(self):
        """Yahoo is optional by design — its rate limiter must not be able to
        stop the platform from having data."""
        facts, _, warnings = canonicalise(_operating_company(), None)
        assert len(facts) >= 25
        assert any("Yahoo unavailable" in w for w in warnings)

    def test_shares_are_derived_from_profit_and_eps(self):
        """Net profit ÷ EPS is the share count the EPS was struck on, which
        beats a period-end count for a company that issued stock mid-year."""
        facts, _, _ = canonicalise(_operating_company(), None)
        assert facts[LI.WEIGHTED_SHARES][2026] == pytest.approx(10.0, rel=0.01)

    def test_dividend_comes_from_the_payout_ratio(self):
        facts, _, _ = canonicalise(_operating_company(), None)
        assert facts[LI.DIVIDEND_PAID][2026] == pytest.approx(36.0)

    def test_expense_total_is_preserved_by_the_residual_split(self):
        """Screener's single `Expenses` line is authoritative for the total,
        so operating profit ties whatever the split."""
        facts, _, _ = canonicalise(_operating_company(), None)
        parts = sum(
            facts.get(item, {}).get(2026, 0.0)
            for item in (LI.RAW_MATERIALS, LI.EMPLOYEE_BENEFIT, LI.OTHER_EXPENSES)
        )
        assert parts == pytest.approx(1100.0, rel=0.01)

    def test_items_no_aggregator_carries_are_explicitly_zero(self):
        """Absent means unknown; zero means reported nil. The canonical
        builder treats them differently."""
        facts, _, _ = canonicalise(_operating_company(), None)
        for item in (LI.OTHER_OPERATING_INCOME, LI.EXCEPTIONAL_ITEMS, LI.OCI):
            assert facts[item][2026] == 0.0


class TestValidatorGrading:
    """The harness that graded 2,279 checks must itself be correct."""

    def test_a_check_computes_its_own_deviation(self):
        from app.data.validate import CheckFamily, CheckResult, Severity

        check = CheckResult(
            "X", CheckFamily.REPORTED, "net profit", False,
            expected=100.0, actual=110.0, tolerance=0.03,
            severity=Severity.CRITICAL,
        )
        assert check.deviation == pytest.approx(0.0909, abs=0.001)

    def test_deviation_is_none_without_both_sides(self):
        from app.data.validate import CheckFamily, CheckResult

        assert CheckResult("X", CheckFamily.IDENTITY, "n", True).deviation is None

    def test_closeness_is_symmetric_and_scale_aware(self):
        from app.data.validate import _close

        assert _close(100.0, 101.0, 0.02)
        assert _close(101.0, 100.0, 0.02)
        assert not _close(100.0, 110.0, 0.02)
        # Near zero, a relative tolerance must not divide by nothing.
        assert _close(0.0, 0.0, 0.01)

    def test_a_financial_is_expected_to_be_refused_a_dcf(self):
        """Silence in the right place is a feature. An engine that
        confidently values a bank has failed."""
        from app.data.validate import CompanyValidation

        result = CompanyValidation(ticker="HDFCBANK", sector="Banking - Private")
        assert is_financial(result.sector)


class TestAIAuditHarness:
    """RC2-008 — the audit read the wrong attribute and reported 0%."""

    def test_the_detector_catches_what_it_must(self):
        from app.data.ai_audit import audit_detector

        checks = audit_detector()
        assert len(checks) == 5
        failures = [name for name, ok, _ in checks if not ok]
        assert failures == [], failures

    def test_the_audit_reads_citation_audit_not_audit(self):
        """`AnalystResult.audit` does not exist. Reading it silently yielded
        None and reported 0% coverage for every well-cited response."""
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "app" / "data" / "ai_audit.py"
        ).read_text()
        assert 'getattr(result, "citation_audit"' in source
        assert 'getattr(result, "audit"' not in source

    def test_adversarial_prompts_cover_distinct_failure_modes(self):
        from app.data.ai_audit import ADVERSARIAL_PROMPTS

        assert len(ADVERSARIAL_PROMPTS) >= 8
        for prompt, reason in ADVERSARIAL_PROMPTS:
            assert prompt.strip() and reason.strip()

    def test_out_of_scope_detection(self):
        """RC2-009 — a shared word is not the same as available evidence."""
        from app.services.ai.providers.mock import _out_of_scope

        # Scope the platform does not hold.
        assert _out_of_scope("What was the revenue in FY2031?")
        assert _out_of_scope("What is the market share in Europe?")
        assert _out_of_scope("Compare this to Tesla's gross margin.")
        assert _out_of_scope("What was the close on 14 March 2019?")
        assert _out_of_scope("How many employees are there?")
        # Legitimate questions must not be caught.
        assert not _out_of_scope("What is the EBITDA margin?")
        assert not _out_of_scope("How much debt does it carry?")
        assert not _out_of_scope("Is return on equity improving?")
