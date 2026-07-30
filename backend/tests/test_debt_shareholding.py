"""Unit tests for the debt and shareholding services."""
from __future__ import annotations

import pytest

from app.models.analysis import DebtInstrument, ShareholdingSnapshot
from app.services.debt.service import DebtService
from app.services.shareholding.service import ShareholdingService


def make_instruments(gross: float, fy: int = 2025) -> list[DebtInstrument]:
    """A schedule that sums exactly to the balance-sheet gross debt."""
    spec = [
        ("Term loan A", "Secured", "Floating", 0.40, 0.0875, fy + 2, "INR"),
        ("NCD Series I", "Secured", "Fixed", 0.30, 0.0825, fy + 4, "INR"),
        ("ECB", "Secured", "Floating", 0.20, 0.0650, fy + 6, "USD"),
        ("Commercial paper", "Unsecured", "Fixed", 0.10, 0.0725, fy + 1, "INR"),
    ]
    out, allocated = [], 0.0
    for i, (name, sec, rt, share, rate, mat, ccy) in enumerate(spec):
        amount = round(gross - allocated, 2) if i == len(spec) - 1 else round(gross * share, 2)
        allocated += amount
        out.append(DebtInstrument(
            company_id="c", fiscal_year=fy, instrument=name, security=sec,
            rate_type=rt, amount=amount, interest_rate=rate,
            maturity_year=mat, currency=ccy,
        ))
    return out


@pytest.fixture(scope="module")
def debt(incomes, balances):
    return DebtService(incomes, balances, make_instruments(balances[-1].gross_debt))


class TestDebtProfile:
    def test_gross_debt_from_statements(self, debt, balances):
        row = next(r for r in debt.profile_section().rows if r.key == "gross_debt")
        assert row.values[-1] == pytest.approx(balances[-1].gross_debt)

    def test_implied_cost_of_debt_uses_average_balance(self, debt, incomes, balances):
        avg = (balances[-1].gross_debt + balances[-2].gross_debt) / 2
        assert debt._implied_cost(len(incomes) - 1) == pytest.approx(
            incomes[-1].finance_costs / avg
        )

    def test_net_debt_below_gross(self, debt):
        rows = {r.key: r.values[-1] for r in debt.profile_section().rows}
        assert rows["net_debt"] < rows["gross_debt"]


class TestInstrumentSchedule:
    def test_shares_sum_to_one(self, debt):
        total = sum(r["share_of_debt"] for r in debt.instrument_schedule())
        assert total == pytest.approx(1.0)

    def test_sorted_by_size_descending(self, debt):
        amounts = [r["amount"] for r in debt.instrument_schedule()]
        assert amounts == sorted(amounts, reverse=True)

    def test_blended_rate_is_amount_weighted(self, debt):
        rows = debt.instrument_schedule()
        expected = sum(r["amount"] * r["interest_rate"] for r in rows) / sum(
            r["amount"] for r in rows
        )
        assert debt.blended_rate() == pytest.approx(expected)
        # a simple mean would differ
        simple = sum(r["interest_rate"] for r in rows) / len(rows)
        assert debt.blended_rate() != pytest.approx(simple)

    def test_floating_share(self, debt):
        assert debt.floating_share() == pytest.approx(0.60, abs=0.01)

    def test_foreign_currency_share(self, debt):
        assert debt.foreign_currency_share() == pytest.approx(0.20, abs=0.01)

    def test_no_instruments_yields_none_not_zero(self, incomes, balances):
        bare = DebtService(incomes, balances, [])
        assert bare.blended_rate() is None
        assert bare.floating_share() is None
        assert bare.instrument_schedule() == []


class TestMaturityLadder:
    def test_amounts_reconcile_to_total(self, debt, balances):
        ladder = debt.maturity_ladder()
        assert sum(b["amount"] for b in ladder) == pytest.approx(balances[-1].gross_debt)

    def test_cumulative_is_monotonic(self, debt):
        cum = [b["cumulative"] for b in debt.maturity_ladder()]
        assert cum == sorted(cum)

    def test_final_cumulative_equals_total(self, debt, balances):
        assert debt.maturity_ladder()[-1]["cumulative"] == pytest.approx(
            balances[-1].gross_debt
        )

    def test_ordered_by_year(self, debt):
        years = [b["year"] for b in debt.maturity_ladder()]
        assert years == sorted(years)


class TestReconciliation:
    def test_matching_schedule_reconciles(self, debt):
        assert debt.reconciliation()["reconciled"] is True
        assert debt.reconciliation()["difference"] == pytest.approx(0.0, abs=0.01)

    def test_mismatch_is_reported_not_hidden(self, incomes, balances):
        wrong = make_instruments(balances[-1].gross_debt * 0.5)
        svc = DebtService(incomes, balances, wrong)
        rec = svc.reconciliation()
        assert rec["reconciled"] is False
        assert rec["difference"] < 0
        assert any(f.key == "debt_reconciliation" and f.triggered for f in svc.flags())


class TestCovenants:
    def test_five_covenants_evaluated(self, debt):
        assert len(debt.covenants()) == 5

    def test_headroom_direction(self, debt):
        for c in debt.covenants():
            if c.compliant:
                assert c.headroom >= 0
            elif c.compliant is False:
                assert c.headroom < 0

    def test_max_covenant_breaches_when_exceeded(self, incomes, balances):
        svc = DebtService(incomes, balances, [])
        cov = next(c for c in svc.covenants() if c.key == "net_debt_ebitda")
        assert cov.direction == "max"
        # net cash company ⇒ comfortably compliant
        assert cov.compliant is True


class TestShareholding:
    @staticmethod
    def snapshot(fy, q, promoter=0.50, fii=0.15, pledge=0.0):
        return ShareholdingSnapshot(
            company_id="c", fiscal_year=fy, quarter=q,
            promoter_indian=promoter * 0.94, promoter_foreign=promoter * 0.06,
            fii_fpi=fii, mutual_funds=0.09, insurance=0.035, banks_fis_aif=0.005,
            government=0.0, others_custodians=0.008, promoter_pledged=pledge,
        )

    def test_pattern_always_totals_one(self):
        svc = ShareholdingService([self.snapshot(2025, q) for q in range(1, 5)])
        row = next(r for r in svc.pattern_section().rows if r.key == "total")
        assert all(v == pytest.approx(1.0) for v in row.values)

    def test_retail_is_the_residual(self):
        s = self.snapshot(2025, 1)
        svc = ShareholdingService([s])
        expected = 1.0 - 0.50 - 0.15 - (0.09 + 0.035 + 0.005) - 0.0 - 0.008
        assert svc.public_retail(s) == pytest.approx(expected)

    def test_institutional_is_fii_plus_dii(self):
        s = self.snapshot(2025, 1)
        svc = ShareholdingService([s])
        assert svc.institutional_total(s) == pytest.approx(0.15 + 0.09 + 0.035 + 0.005)

    def test_pledge_converted_to_share_of_equity(self):
        """Pledge is disclosed against promoter holding, not total equity."""
        s = self.snapshot(2025, 1, promoter=0.60, pledge=0.20)
        svc = ShareholdingService([s])
        assert svc.pledge_of_equity(s) == pytest.approx(0.12)

    def test_free_float_complements_promoter(self):
        s = self.snapshot(2025, 1, promoter=0.62)
        svc = ShareholdingService([s])
        assert svc.free_float(s) == pytest.approx(0.38)

    def test_snapshots_sorted_chronologically(self):
        unsorted = [self.snapshot(2025, 3), self.snapshot(2024, 1), self.snapshot(2025, 1)]
        svc = ShareholdingService(unsorted)
        assert [(s.fiscal_year, s.quarter) for s in svc.snaps] == [(2024, 1), (2025, 1), (2025, 3)]

    def test_accumulation_signal(self):
        snaps = [
            self.snapshot(2024, 1, promoter=0.50, fii=0.14, pledge=0.10),
            self.snapshot(2024, 2, promoter=0.505, fii=0.145, pledge=0.08),
            self.snapshot(2024, 3, promoter=0.51, fii=0.15, pledge=0.06),
            self.snapshot(2024, 4, promoter=0.515, fii=0.155, pledge=0.04),
            self.snapshot(2025, 1, promoter=0.52, fii=0.16, pledge=0.02),
        ]
        result = ShareholdingService(snaps).ownership_signal()
        assert result["signal"] == "Accumulation"
        assert result["score"] == 3

    def test_distribution_signal(self):
        snaps = [
            self.snapshot(2024, 1, promoter=0.52, fii=0.16, pledge=0.02),
            self.snapshot(2024, 2, promoter=0.515, fii=0.155, pledge=0.04),
            self.snapshot(2024, 3, promoter=0.51, fii=0.15, pledge=0.06),
            self.snapshot(2024, 4, promoter=0.505, fii=0.145, pledge=0.08),
            self.snapshot(2025, 1, promoter=0.50, fii=0.14, pledge=0.10),
        ]
        result = ShareholdingService(snaps).ownership_signal()
        assert result["signal"] == "Distribution"
        assert result["score"] == -3

    def test_high_pledge_alert(self):
        svc = ShareholdingService([self.snapshot(2025, 1, pledge=0.40)])
        flag = next(f for f in svc.flags() if f.key == "high_pledge")
        assert flag.triggered and flag.severity == "alert"

    def test_low_promoter_stake_warning(self):
        svc = ShareholdingService([self.snapshot(2025, 1, promoter=0.20)])
        flag = next(f for f in svc.flags() if f.key == "low_promoter_stake")
        assert flag.triggered

    def test_empty_history_is_safe(self):
        svc = ShareholdingService([])
        assert svc.all_sections() == []
        assert svc.flags() == []
        assert svc.ownership_signal()["signal"] == "Insufficient history"
