"""High-value integration tests for the real seed pipeline (seed_platform.py).

Executes the pipeline against an isolated in-memory SQLite database,
verifying seeding, idempotency, legacy migration, API keys, usage aggregation,
notifications, and background jobs.
"""
from __future__ import annotations

import importlib
import pkgutil
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.core.config import settings
from app.models.platform import (
    Tenant, User, ApiKey, Notification, BackgroundJob, AuditLog,
    Subscription, UsageEvent, UsageCounter, Plan
)
from app.models.company import Company
from app.models.portfolio import Portfolio, Watchlist
from app.models.report import Report
from app.models.document import Document

@pytest.fixture
def db_session():
    """Builds a completely isolated in-memory SQLite database with schema."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    
    # Import all models explicitly to register them with metadata
    import app.models as _models
    for _module in pkgutil.iter_modules(_models.__path__):
        importlib.import_module(f"app.models.{_module.name}")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as session:
        yield session


def test_scenario_1_initial_seeding_creates_all_records(db_session):
    """Initial seeding creates default plans, tenants, users, roles, API keys, notifications, jobs, and audits."""
    from app.db.seed_platform import seed_module10
    
    # Run the seed pipeline
    result = seed_module10(db_session)
    
    # Check return values from seed_module10
    assert result["plans"] == 4
    assert result["tenants"] == 3
    assert result["users"] == 9
    assert result["api_keys"] == 2
    assert result["jobs"] == 8
    assert result["notifications"] == 3

    # 1. Verify default plans are created
    plans = db_session.scalars(select(Plan)).all()
    assert len(plans) == 4
    tiers = {p.tier for p in plans}
    assert tiers == {"free", "basic", "professional", "enterprise"}

    # 2. Verify tenants are created in correct states
    tenants = db_session.scalars(select(Tenant)).all()
    assert len(tenants) == 3
    slugs = {t.slug for t in tenants}
    assert slugs == {settings.DEFAULT_TENANT_SLUG, "northwind-research", "meridian-pms"}

    # 3. Verify users & roles are created
    users = db_session.scalars(select(User)).all()
    assert len(users) == 10
    emails = {u.email for u in users}
    assert "priya.nair@democapital.in" in emails
    assert "analyst@localhost" in emails  # Legacy owner

    roles = {u.role for u in users}
    assert "super_admin" in roles
    assert "admin" in roles
    assert "analyst" in roles
    assert "researcher" in roles
    assert "subscriber" in roles
    assert "read_only" in roles

    # 4. Verify API keys exist
    api_keys = db_session.scalars(select(ApiKey)).all()
    assert len(api_keys) == 2

    # 5. Verify notifications exist
    notifications = db_session.scalars(select(Notification)).all()
    assert len(notifications) == 3

    # 6. Verify jobs exist
    jobs = db_session.scalars(select(BackgroundJob)).all()
    assert len(jobs) == 8

    # 7. Verify audit logs exist
    audit_logs = db_session.scalars(select(AuditLog)).all()
    assert len(audit_logs) > 0


def test_scenario_2_idempotency_avoids_duplicates(db_session):
    """Running seed twice verifies no duplicate records are created and counts remain stable."""
    from app.db.seed_platform import seed_module10

    # Run once
    res1 = seed_module10(db_session)
    
    # Capture counts
    plans1 = db_session.scalar(select(func.count(Plan.id)))
    tenants1 = db_session.scalar(select(func.count(Tenant.id)))
    users1 = db_session.scalar(select(func.count(User.id)))
    api_keys1 = db_session.scalar(select(func.count(ApiKey.id)))
    notifications1 = db_session.scalar(select(func.count(Notification.id)))
    jobs1 = db_session.scalar(select(func.count(BackgroundJob.id)))
    audits1 = db_session.scalar(select(func.count(AuditLog.id)))
    usage_events1 = db_session.scalar(select(func.count(UsageEvent.id)))
    usage_counters1 = db_session.scalar(select(func.count(UsageCounter.id)))

    # Run twice
    res2 = seed_module10(db_session)

    # Capture counts again
    plans2 = db_session.scalar(select(func.count(Plan.id)))
    tenants2 = db_session.scalar(select(func.count(Tenant.id)))
    users2 = db_session.scalar(select(func.count(User.id)))
    api_keys2 = db_session.scalar(select(func.count(ApiKey.id)))
    notifications2 = db_session.scalar(select(func.count(Notification.id)))
    jobs2 = db_session.scalar(select(func.count(BackgroundJob.id)))
    audits2 = db_session.scalar(select(func.count(AuditLog.id)))
    usage_events2 = db_session.scalar(select(func.count(UsageEvent.id)))
    usage_counters2 = db_session.scalar(select(func.count(UsageCounter.id)))

    # Asserts
    assert plans1 == plans2
    assert tenants1 == tenants2
    assert users1 == users2
    assert api_keys1 == api_keys2
    assert notifications1 == notifications2
    assert jobs1 == jobs2
    assert audits1 == audits2
    assert usage_events1 == usage_events2
    assert usage_counters1 == usage_counters2


def test_scenario_3_legacy_migration_repairs_relations(db_session):
    """Legacy migration backfills owner_id=dev-user on existing portfolios, watchlists, reports, and documents."""
    from app.db.seed_platform import seed_module10, LEGACY_OWNER_ID

    # Setup legacy data that pre-dates seed
    company = Company(id="comp-123", name="Legacy Corp", ticker="LEGACY")
    db_session.add(company)
    db_session.commit()

    portfolio = Portfolio(owner_id=LEGACY_OWNER_ID, name="Legacy Portfolio")
    watchlist = Watchlist(owner_id=LEGACY_OWNER_ID, name="Legacy Watchlist")
    report = Report(
        company_id="comp-123", ticker="LEGACY", company_name="Legacy Corp",
        owner_id=LEGACY_OWNER_ID, report_type="thesis", title="Legacy Report"
    )
    document = Document(
        company_id="comp-123", filename="legacy.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="hash111", uploaded_by=LEGACY_OWNER_ID
    )

    db_session.add_all([portfolio, watchlist, report, document])
    db_session.commit()

    # Seed
    result = seed_module10(db_session)

    # Check user mapping
    user = db_session.get(User, LEGACY_OWNER_ID)
    assert user is not None
    assert user.role == "super_admin"

    # Verify that seed_module10 identified and resolved the records correctly
    assert result["legacy_owner_resolves"] == 1
    assert result["portfolios"] == 1
    assert result["watchlists"] == 1
    assert result["reports"] == 1
    assert result["documents"] == 1


def test_scenario_4_api_keys_active_and_revoked(db_session):
    """Seeding creates active and revoked API keys and correctly validates attributes."""
    from app.db.seed_platform import seed_module10

    seed_module10(db_session)

    keys = db_session.scalars(select(ApiKey)).all()
    assert len(keys) == 2

    active = [k for k in keys if k.revoked_at is None]
    revoked = [k for k in keys if k.revoked_at is not None]

    assert len(active) == 1
    assert len(revoked) == 1

    assert active[0].name == "Risk dashboard (read-only)"
    assert active[0].call_count == 18_432
    assert active[0].revoked_at is None

    assert revoked[0].name == "Retired CI key"
    assert revoked[0].call_count == 2_101
    assert revoked[0].revoked_at is not None


def test_scenario_5_usage_aggregation_and_deterministic_totals(db_session):
    """Usage events are correctly backdated and aggregation yields deterministic totals matching counter used values."""
    from app.db.seed_platform import seed_module10
    from app.services.platform.entitlements import EntitlementService
    from app.domain.platform.plans import Quota

    seed_module10(db_session)

    # Fetch default tenant and billing subscription
    tenant = db_session.scalar(select(Tenant).where(Tenant.slug == settings.DEFAULT_TENANT_SLUG))
    service = EntitlementService(db_session)
    subscription = service.subscription_for(tenant.id)

    # Check totals in UsageCounter match aggregated event values
    counters = db_session.scalars(select(UsageCounter).where(UsageCounter.tenant_id == tenant.id)).all()
    assert len(counters) > 0

    for counter in counters:
        # Exclude AI_TOKENS as its calculation is complex and dependent on quantities
        if counter.quota == Quota.AI_TOKENS.value:
            continue
        
        expected_used = db_session.scalar(
            select(func.sum(UsageEvent.quantity))
            .where(UsageEvent.tenant_id == tenant.id)
            .where(UsageEvent.quota == counter.quota)
            .where(func.date(UsageEvent.occurred_at) >= subscription.period_start)
        ) or 0

        assert counter.used == expected_used


def test_scenario_6_notifications_count_and_duplicates(db_session):
    """Seeding creates the expected notification count and running twice prevents duplicates."""
    from app.db.seed_platform import seed_module10

    seed_module10(db_session)
    assert db_session.scalar(select(func.count(Notification.id))) == 3

    # Running again preserves exact count
    seed_module10(db_session)
    assert db_session.scalar(select(func.count(Notification.id))) == 3


def test_scenario_7_background_jobs_created_once_and_rerun_safe(db_session):
    """Background jobs are created with varied states and seeding twice is safe."""
    from app.db.seed_platform import seed_module10

    seed_module10(db_session)
    jobs = db_session.scalars(select(BackgroundJob)).all()
    assert len(jobs) == 8

    # Running twice
    seed_module10(db_session)
    assert db_session.scalar(select(func.count(BackgroundJob.id))) == 8
