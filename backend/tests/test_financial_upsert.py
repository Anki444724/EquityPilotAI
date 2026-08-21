"""Phase 1 — financial_facts upsert semantics (requirement F).

The pre-Phase-1 path deleted a company's facts and re-inserted them. The new
path must: preserve existing facts, update changed facts, insert new facts,
retain fetched_at / data_version / consolidated metadata, and be idempotent
under repeated ingestion — with zero duplicate rows, verified by count.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.data.mock_financials import generate_mock_facts, upsert_mock_financials
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI
from app.models.company import Company, FinancialFact


def _company(phase1_db, ticker="MCK0100") -> Company:
    row = phase1_db.scalar(select(Company).where(Company.ticker == ticker))
    if row is None:
        row = Company(
            id=f"id-{ticker}", ticker=ticker, name=f"{ticker} Ltd",
            exchange="NSE",
        )
        phase1_db.add(row)
        phase1_db.commit()
    return row


class TestUpsertSemantics:
    def test_repeated_ingestion_is_a_no_op(self, phase1_db):
        company = _company(phase1_db)
        first = upsert_mock_financials(phase1_db, company.ticker)
        second = upsert_mock_financials(phase1_db, company.ticker)

        assert first["ok"] and second["ok"]
        assert first["inserted"] > 0
        assert second["inserted"] == 0
        assert second["updated"] == 0
        assert second["unchanged"] == first["inserted"]
        total = phase1_db.scalar(
            select(func.count()).select_from(FinancialFact)
            .where(FinancialFact.company_id == company.id)
        )
        assert total == first["inserted"]   # zero duplicates, by count

    def test_changed_values_update_and_bump_row_version(self, phase1_db):
        company = _company(phase1_db, "MCK0101")
        upsert_mock_financials(phase1_db, company.ticker)

        facts = generate_mock_facts(company.ticker, years=5)
        revenue_2026 = facts[LI.REVENUE][max(facts[LI.REVENUE])]
        bumped = {**facts[LI.REVENUE]}
        latest_year = max(bumped)
        bumped[latest_year] = bumped[latest_year] + 500.0

        from app.data.ingest import _upsert_facts

        patched = dict(facts)
        patched[LI.REVENUE] = bumped
        inserted, updated, unchanged = _upsert_facts(
            phase1_db, company.id, patched, "mock (synthetic)",
        )

        assert inserted == 0
        assert updated == 1
        assert unchanged > 0

        rows = phase1_db.execute(
            select(FinancialFact).where(
                FinancialFact.company_id == company.id,
                FinancialFact.line_item == LI.REVENUE.value,
            )
        ).scalars().all()
        by_year = {r.fiscal_year: r for r in rows}
        assert by_year[latest_year].value == revenue_2026 + 500.0
        assert by_year[latest_year].data_version == 2
        another = min(by_year)
        assert by_year[another].data_version == 1   # untouched rows untouched

    def test_metadata_columns_are_populated(self, phase1_db):
        company = _company(phase1_db, "MCK0102")
        upsert_mock_financials(phase1_db, company.ticker)
        rows = phase1_db.scalars(
            select(FinancialFact).where(FinancialFact.company_id == company.id)
        ).all()
        assert rows
        for row in rows:
            assert row.consolidated is True
            assert row.fetched_at is not None
            assert row.data_version == 1
            assert row.source == "mock (synthetic)"

    def test_provider_failure_leaves_existing_data_intact(self, phase1_db):
        """A provider that dies mid-run must not corrupt what is already
        stored: the upsert path either writes a whole valid fact or nothing."""
        company = _company(phase1_db, "MCK0103")
        before = upsert_mock_financials(phase1_db, company.ticker)
        assert before["ok"]

        from app.data.ingest import _upsert_facts

        # A facts dict containing an invalid line item explodes before any
        # write lands; the previously stored rows survive verbatim.
        count_before = phase1_db.scalar(
            select(func.count()).select_from(FinancialFact)
            .where(FinancialFact.company_id == company.id)
        )
        try:
            _upsert_facts(phase1_db, company.id, {"NOT-A-LINE-ITEM": {2025: 1.0}}, "mock")
            raised = False
        except Exception:
            raised = True
            phase1_db.rollback()
        assert raised

        count_after = phase1_db.scalar(
            select(func.count()).select_from(FinancialFact)
            .where(FinancialFact.company_id == company.id)
        )
        assert count_after == count_before

    def test_facts_survive_a_source_that_stops_reporting(self, phase1_db):
        """Pre-Phase-1 delete-and-replace erased history when a provider
        narrowed its report. The upsert retains rows the source no longer
        sends — that is the point of the change."""
        company = _company(phase1_db, "MCK0104")
        full = generate_mock_facts(company.ticker, years=10)
        from app.data.ingest import _upsert_facts

        _upsert_facts(phase1_db, company.id, full, "mock (synthetic)")
        narrower = {item: dict(sorted(series.items())[:5])
                    for item, series in full.items()}
        _upsert_facts(phase1_db, company.id, narrower, "mock (synthetic)")

        count = phase1_db.scalar(
            select(func.count()).select_from(FinancialFact)
            .where(FinancialFact.company_id == company.id)
        )
        assert count > sum(len(s) for s in narrower.values())
