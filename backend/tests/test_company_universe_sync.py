"""Phase 1 — company universe sync: identity, batching, idempotency, scale.

The identity proofs the brief demands:
* 5,000 companies can be loaded
* repeated sync does not duplicate
* existing companies retain their IDs
* one company may arrive from multiple providers without a duplicate identity
* ISIN-first, (ticker, exchange) fallback, BSE code last
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.models.company import Company
from app.services.universe.company_universe import (
    CompanyUniverseService, UniverseRecord, generate_mock_universe,
)

# ---------------------------------------------------------------------------
# Identity ladder
# ---------------------------------------------------------------------------
class TestIdentityResolution:
    def test_isin_match_preserves_id(self, phase1_db):
        existing = Company(
            id="keep-me", ticker="MCK0001", name="Old Name Ltd",
            exchange="NSE", isin="INM000000123",
        )
        phase1_db.add(existing)
        phase1_db.commit()

        svc = CompanyUniverseService(phase1_db)
        record = UniverseRecord(
            ticker="RENAMED", name="New Name Ltd", exchange="NSE",
            isin="INM000000123", source="mock",
        )
        svc.sync([record], source="mock", batch_size=10)

        row = phase1_db.scalar(select(Company).where(Company.id == "keep-me"))
        assert row is not None, "ISIN match must update the existing row"
        assert row.ticker == "MCK0001"      # ticker not overwritten by the ladder
        assert row.name == "New Name Ltd"
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 1

    def test_ticker_exchange_match_when_no_isin(self, phase1_db):
        existing = Company(
            id="orig", ticker="MCK0007", name="Same", exchange="NSE",
        )
        phase1_db.add(existing)
        phase1_db.commit()

        record = UniverseRecord(
            ticker="MCK0007", name="Same Updated", exchange="NSE",
            isin=None, source="mock",
        )
        CompanyUniverseService(phase1_db).sync([record], source="mock", batch_size=10)

        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 1
        row = phase1_db.get(Company, "orig")
        assert row.name == "Same Updated"
        assert row.id == "orig"

    def test_bse_code_match(self, phase1_db):
        existing = Company(
            id="by-bse", ticker="MCK0009", name="Dual Listed", exchange="NSE",
            bse_code="900123",
        )
        phase1_db.add(existing)
        phase1_db.commit()

        # Same company, no ISIN, different ticker spelling: BSE code catches it.
        record = UniverseRecord(
            ticker="MCK0009X", name="Dual Listed Ltd", exchange="NSE",
            bse_code="900123", source="mock",
        )
        CompanyUniverseService(phase1_db).sync([record], source="mock", batch_size=10)
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 1

    def test_same_ticker_different_exchange_is_a_new_company(self, phase1_db):
        records = [
            UniverseRecord(ticker="SAME", name="NSE Co", exchange="NSE"),
            UniverseRecord(ticker="SAME", name="US Co", exchange="NASDAQ"),
        ]
        CompanyUniverseService(phase1_db).sync(records, source="mock", batch_size=10)
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 2

    def test_one_company_multiple_providers_no_duplicate(self, phase1_db):
        """The same security arriving from two masters must converge on one
        row — the brief's 'multiple data providers without duplicate identity'."""
        isin = "INM765432109"
        first = UniverseRecord(
            ticker="MCK8001", name="Multi Source Ltd", exchange="NSE",
            isin=isin, bse_code="900801", sector="Healthcare", source="nse_master",
        )
        second = UniverseRecord(
            ticker="MCK8001", name="Multi Source Limited", exchange="NSE",
            isin=isin, bse_code="900801", industry="Pharmaceuticals",
            source="bse_master",
        )
        svc = CompanyUniverseService(phase1_db)
        report = svc.sync([first, second], source="mock", batch_size=10)
        assert report.inserted == 1
        assert report.duplicates_prevented == 1
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 1
        row = phase1_db.scalar(select(Company).where(Company.isin == isin))
        assert row.name == "Multi Source Limited"   # second source refreshed it
        assert row.industry == "Pharmaceuticals"


# ---------------------------------------------------------------------------
# Idempotency + integrity
# ---------------------------------------------------------------------------
class TestIdempotencyAndIntegrity:
    def test_repeated_sync_creates_nothing_and_updates_nothing(self, phase1_db):
        records = generate_mock_universe(300)
        svc = CompanyUniverseService(phase1_db)
        first = svc.sync(records, source="mock", batch_size=100)
        second = svc.sync(records, source="mock", batch_size=100)

        assert first.inserted == 300
        assert second.inserted == 0
        assert second.updated == 0
        assert second.unchanged == 300
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 300
        assert second.duplicate_identities == 0

    def test_existing_500_style_records_survive_a_new_universe(self, phase1_db):
        """The production shape: 500 real companies already present, then a
        5,000-company sync runs. No id changes, no duplicates, every original
        row still findable."""
        originals = []
        for i in range(500):
            originals.append(Company(
                id=f"real-{i:04d}",
                ticker=f"PRE{i:04d}", name=f"Pre-existing {i} Ltd",
                exchange="NSE", isin=f"INE{i:09d}",
                metadata_source="nse_master",
            ))
        phase1_db.add_all(originals)
        phase1_db.commit()
        original_ids = {c.id for c in originals}

        records = generate_mock_universe(5_000)
        svc = CompanyUniverseService(phase1_db)
        report = svc.sync(records, source="mock", batch_size=500)

        assert report.inserted == 5_000
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 5_500
        # Every original row intact and unchanged in identity.
        survivors = {
            c.id for c in phase1_db.scalars(
                select(Company).where(Company.id.in_(original_ids))
            ).all()
        }
        assert survivors == original_ids
        assert report.duplicate_identities == 0

    def test_report_counts_missing_isin_and_exchange(self, phase1_db):
        records = [
            UniverseRecord(ticker="NOISIN1", name="No ISIN Ltd", isin=None),
            UniverseRecord(ticker="NOISIN2", name="Also No ISIN", isin=None),
        ]
        report = CompanyUniverseService(phase1_db).sync(records, source="mock", batch_size=10)
        assert report.missing_isin == ["NOISIN1", "NOISIN2"]
        assert report.missing_exchange == []

    def test_failure_is_isolated_and_recorded(self, phase1_db, monkeypatch):
        good = UniverseRecord(ticker="MCKGOOD", name="Good Ltd", isin="INM111111111")
        svc = CompanyUniverseService(phase1_db)

        # A record that explodes mid-upsert. Injected rather than relying on
        # a long name: SQLite does not enforce VARCHAR lengths, so a length
        # violation only fails on Postgres — the isolation must be provable
        # on both engines.
        bad = UniverseRecord(ticker="MCKBAD", name="Bad Ltd", isin="INM222222222")
        original = svc._upsert_record

        def exploding(record, report):
            if record.ticker == "MCKBAD":
                raise RuntimeError("simulated provider write failure: 429")
            return original(record, report)

        monkeypatch.setattr(svc, "_upsert_record", exploding)
        report = svc.sync([good, bad], source="mock", batch_size=10)

        assert report.failed == 1
        assert report.inserted == 1
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 1

        from app.models.ingestion import IngestionFailure, IngestionRun

        failures = phase1_db.scalars(select(IngestionFailure)).all()
        assert len(failures) == 1
        assert failures[0].symbol == "MCKBAD"
        assert failures[0].kind == "company_universe_sync"
        assert failures[0].payload["record"]["ticker"] == "MCKBAD"
        runs = phase1_db.scalars(select(IngestionRun)).all()
        assert runs and runs[0].failed == 1


# ---------------------------------------------------------------------------
# Resumability
# ---------------------------------------------------------------------------
class TestResumability:
    def test_max_batches_bound_a_run_and_it_resumes(self, phase1_db):
        records = generate_mock_universe(250)
        svc = CompanyUniverseService(phase1_db)

        first = svc.sync(records, source="mock", batch_size=100, max_batches=1)
        assert first.inserted == 100
        assert first.next_index == 100

        # Simulate the job handler reading the resume position.
        assert svc.resume_position() == 0  # first run FINISHED (bounded) → new run restarts
        second = svc.sync(
            records, source="mock", batch_size=100,
            start_index=first.next_index,
        )
        assert second.inserted == 150
        assert second.next_index == 250
        assert phase1_db.scalar(select(func.count()).select_from(Company)) == 250


# ---------------------------------------------------------------------------
# Full scale
# ---------------------------------------------------------------------------
class TestFiveThousand:
    def test_five_thousand_companies_load_and_sync_twice(self, big_db):
        """The headline Phase-1 gate: 5,000 deterministic companies, twice."""
        records = generate_mock_universe(5_000)
        svc = CompanyUniverseService(big_db)

        first = svc.sync(records, source="mock", batch_size=500)
        assert first.inserted == 5_000
        assert first.failed == 0
        assert first.duplicate_identities == 0

        second = svc.sync(records, source="mock", batch_size=500)
        assert second.inserted == 0
        assert second.updated == 0
        assert second.unchanged == 5_000

        total = big_db.scalar(select(func.count()).select_from(Company))
        assert total == 5_000

        # Identity uniqueness at scale, exactly as the schema enforces it.
        pairs = big_db.execute(
            select(Company.ticker, Company.exchange, func.count())
            .group_by(Company.ticker, Company.exchange)
            .having(func.count() > 1)
        ).all()
        assert pairs == []

        # The report's financial-coverage question answers correctly.
        assert first.companies_without_financials == 5_000

    def test_mock_identities_cannot_collide_with_real_ones(self, big_db):
        """Reserved prefixes: mock tickers/ISINs live in bands real securities
        do not occupy, so mock rows and real rows coexist unambiguously."""
        for record in generate_mock_universe(1_000):
            assert record.ticker.startswith("MCK")
            assert record.isin is None or record.isin.startswith("INM")
            assert record.bse_code is None or record.bse_code.startswith("9")
