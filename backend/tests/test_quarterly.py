"""Tests for quarterly results: storage, comparison arithmetic and the API."""

from __future__ import annotations

import importlib
import pkgutil
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models_pkg
from app.data.screener_source import ScreenerFinancials
from app.db.base import Base
from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
from app.models.company import Company
from app.services.quarterly.service import QuarterlyService, _growth
from app.services.universe.periodic_backfill import PeriodicBackfillService

for _module in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_module.name}")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def company(db):
    row = Company(
        id=str(uuid.uuid4()), name="Test Ltd.", ticker="TEST",
        exchange="NSE", listing_status="active", market_cap_category="largecap",
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------- arithmetic

def test_growth_is_fractional():
    assert _growth(110.0, 100.0) == pytest.approx(0.10)


def test_growth_against_zero_is_undefined_not_infinite():
    assert _growth(50.0, 0.0) is None


def test_growth_across_a_sign_change_is_refused():
    """A swing from a loss to a profit is not a percentage.

    Reporting −50 → +25 as "150% growth" is a number that invites a wrong
    conclusion, so it is withheld.
    """
    assert _growth(25.0, -50.0) is None
    assert _growth(-25.0, 50.0) is None


def test_growth_between_two_losses_is_still_meaningful():
    # A loss narrowing from −100 to −50 is a real 50% improvement.
    assert _growth(-50.0, -100.0) == pytest.approx(0.5)


# ------------------------------------------------------------------ sequence

def _quarter(db, company, fy, q, revenue, profit):
    db.add(QuarterlyResult(
        company_id=company.id, fiscal_year=fy, quarter=q,
        revenue=revenue, net_profit=profit, source="test",
    ))
    db.commit()


def test_rows_are_ordered_oldest_first(db, company):
    _quarter(db, company, 2026, 2, 200.0, 20.0)
    _quarter(db, company, 2025, 4, 100.0, 10.0)
    _quarter(db, company, 2026, 1, 150.0, 15.0)

    labels = [r.label for r in QuarterlyService(db).rows(company.id)]
    assert labels == ["Q4 FY25", "Q1 FY26", "Q2 FY26"]


def test_qoq_compares_with_the_preceding_quarter(db, company):
    _quarter(db, company, 2026, 1, 100.0, 10.0)
    _quarter(db, company, 2026, 2, 120.0, 12.0)

    rows = QuarterlyService(db).rows(company.id)
    assert rows[0].revenue_qoq is None          # nothing before it
    assert rows[1].revenue_qoq == pytest.approx(0.20)


def test_yoy_matches_the_same_quarter_last_year(db, company):
    _quarter(db, company, 2025, 2, 100.0, 10.0)
    _quarter(db, company, 2026, 2, 130.0, 14.0)

    latest = QuarterlyService(db).latest(company.id)
    assert latest.revenue_yoy == pytest.approx(0.30)
    assert latest.profit_yoy == pytest.approx(0.40)


def test_yoy_is_keyed_not_positional(db, company):
    """A gap in the history must not silently compare the wrong quarters.

    Indexing back four rows would pick Q1 FY25 here and report a nonsense
    year-on-year figure; keying on (year-1, quarter) returns None instead.
    """
    _quarter(db, company, 2025, 1, 90.0, 9.0)
    _quarter(db, company, 2025, 2, 95.0, 9.5)
    # FY2025 Q3 and Q4 are missing.
    _quarter(db, company, 2026, 1, 120.0, 12.0)
    _quarter(db, company, 2026, 3, 140.0, 14.0)

    rows = {r.label: r for r in QuarterlyService(db).rows(company.id)}
    assert rows["Q1 FY26"].revenue_yoy == pytest.approx(120 / 90 - 1)
    assert rows["Q3 FY26"].revenue_yoy is None


def test_has_data_is_false_for_a_company_with_no_quarters(db, company):
    assert QuarterlyService(db).has_data(company.id) is False


# ------------------------------------------------------------------ backfill

def _screener(quarters=None, shareholding=None):
    data = ScreenerFinancials(ticker="TEST")
    data.profit_loss = {"Sales": {2026: 1000.0}}
    data.quarters = quarters or {}
    data.shareholding = shareholding or {}
    return data


def test_percentages_are_stored_as_fractions(db, company):
    """Screener reports 14.0 for 14%; the platform stores 0.14 everywhere."""
    data = _screener(quarters={
        "Sales +": {(2026, 1): 500.0},
        "OPM %": {(2026, 1): 14.0},
        "Tax %": {(2026, 1): 25.0},
        "Net Profit": {(2026, 1): 50.0},
    })
    PeriodicBackfillService(
        db, delay_seconds=0, fetch=lambda _t: data,
    ).run([company], progress=False)

    row = db.query(QuarterlyResult).one()
    assert row.operating_margin == pytest.approx(0.14)
    assert row.tax_rate == pytest.approx(0.25)


def test_a_period_with_no_reported_figure_is_not_written(db, company):
    """The no-placeholder rule, at quarterly granularity."""
    data = _screener(quarters={
        "Sales +": {(2026, 1): 500.0},
        # Screener lists Q2 as a column but reports nothing in it.
        "OPM %": {(2026, 2): 0.0},
    })
    PeriodicBackfillService(
        db, delay_seconds=0, fetch=lambda _t: data,
    ).run([company], progress=False)

    stored = db.query(QuarterlyResult).all()
    assert [(r.fiscal_year, r.quarter) for r in stored] == [(2026, 1)]
    assert all(r.has_data for r in stored)


def test_financing_layout_maps_to_the_same_columns(db, company):
    """Banks and NBFCs report `Revenue`/`Financing Profit` where an operating
    company reports `Sales`/`Operating Profit`. Both must land identically."""
    data = _screener(quarters={
        "Revenue": {(2026, 1): 800.0},
        "Financing Profit": {(2026, 1): 200.0},
        "Financing Margin %": {(2026, 1): 25.0},
        "Net Profit": {(2026, 1): 150.0},
    })
    PeriodicBackfillService(
        db, delay_seconds=0, fetch=lambda _t: data,
    ).run([company], progress=False)

    row = db.query(QuarterlyResult).one()
    assert row.revenue == 800.0
    assert row.operating_profit == 200.0
    assert row.operating_margin == pytest.approx(0.25)


def test_rerunning_updates_rather_than_duplicating(db, company):
    data = _screener(quarters={
        "Sales +": {(2026, 1): 500.0}, "Net Profit": {(2026, 1): 50.0},
    })
    service = PeriodicBackfillService(db, delay_seconds=0, fetch=lambda _t: data)
    service.run([company], progress=False)

    revised = _screener(quarters={
        "Sales +": {(2026, 1): 525.0}, "Net Profit": {(2026, 1): 55.0},
    })
    PeriodicBackfillService(
        db, delay_seconds=0, fetch=lambda _t: revised,
    ).run([company], progress=False)

    rows = db.query(QuarterlyResult).all()
    assert len(rows) == 1, "restatement created a duplicate period"
    assert rows[0].revenue == 525.0


def test_shareholding_is_stored_as_fractions(db, company):
    data = _screener(shareholding={
        "Promoters +": {(2026, 1): 58.11},
        "FIIs +": {(2026, 1): 15.18},
        "DIIs +": {(2026, 1): 10.09},
        "No. of Shareholders": {(2026, 1): 258587.0},
    })
    PeriodicBackfillService(
        db, delay_seconds=0, fetch=lambda _t: data,
    ).run([company], progress=False)

    snap = db.query(ShareholdingSnapshot).one()
    assert snap.promoter_indian == pytest.approx(0.5811)
    assert snap.fii_fpi == pytest.approx(0.1518)


def test_unmapped_sebi_columns_are_left_alone_not_guessed(db, company):
    """Screener publishes one combined DII figure, not the SEBI split.

    Apportioning it across mutual_funds / insurance / banks_fis_aif would be
    invented data indistinguishable from a disclosed figure.
    """
    data = _screener(shareholding={
        "Promoters +": {(2026, 1): 58.0},
        "DIIs +": {(2026, 1): 12.0},
    })
    PeriodicBackfillService(
        db, delay_seconds=0, fetch=lambda _t: data,
    ).run([company], progress=False)

    snap = db.query(ShareholdingSnapshot).one()
    assert snap.mutual_funds == 0.0
    assert snap.insurance == 0.0
    assert snap.promoter_pledged == 0.0


def test_a_failing_company_does_not_stop_the_sweep(db):
    rows = []
    for ticker in ("AAA", "BBB"):
        row = Company(id=str(uuid.uuid4()), name=f"{ticker} Ltd.",
                      ticker=ticker, exchange="NSE", listing_status="active")
        db.add(row)
        rows.append(row)
    db.commit()

    def flaky(ticker):
        if ticker == "AAA":
            raise RuntimeError("connection reset")
        return _screener(quarters={
            "Sales +": {(2026, 1): 10.0}, "Net Profit": {(2026, 1): 1.0},
        })

    report = PeriodicBackfillService(
        db, delay_seconds=0, fetch=flaky,
    ).run(rows, progress=False)

    assert len(report.failed) == 1
    assert len(report.succeeded) == 1
    assert "RuntimeError" in report.failed[0].reason


# ----------------------------------------------- stale consolidated fallback

def _page(years, *, section="profit-loss"):
    """Minimal screener-shaped HTML with one section and the given columns."""
    heads = "".join(f"<th>Mar {y}</th>" for y in years)
    cells = "".join(f"<td>{100 + y}</td>" for y in years)
    return (
        f'<section id="{section}"><table>'
        f"<thead><tr><th></th>{heads}</tr></thead>"
        f"<tbody><tr><td>Sales</td>{cells}</tr></tbody>"
        f"</table></section>"
    )


def test_stale_consolidated_page_falls_back_to_standalone(monkeypatch):
    """A consolidated page can be non-empty AND useless.

    GE Vernova T&D India serves a consolidated page carrying a single
    `Dec 2010` column, left from a corporate history several renames ago,
    while its standalone page carries Mar 2015…Mar 2026. The old test only
    caught an EMPTY consolidated table, so this page was accepted and one
    fiscal year from sixteen years ago was stored — worse than a failure,
    because it looks like coverage.
    """
    from app.data import screener_source as source

    requested: list[str] = []

    def fake_fetch(url, **_kwargs):
        requested.append(url)
        if url.endswith("/consolidated/"):
            return _page([2011])
        return _page(range(2015, 2027))

    monkeypatch.setattr(source, "_fetch", fake_fetch)
    result = source.fetch_screener("GVT&D")

    assert any(u.endswith("/consolidated/") for u in requested)
    assert len(result.fiscal_years) == 12
    assert result.fiscal_years[0] == 2015
    assert "stale consolidated page" in " ".join(result.warnings)


def test_one_extra_standalone_year_does_not_abandon_consolidated(monkeypatch):
    """Consolidated accounts remain the correct basis for a group.

    A company reporting both will often have one more standalone year; that
    is not a reason to switch, so the threshold is deliberately `+1`.
    """
    from app.data import screener_source as source

    def fake_fetch(url, **_kwargs):
        if url.endswith("/consolidated/"):
            return _page([2025, 2026])
        return _page([2024, 2025, 2026])

    monkeypatch.setattr(source, "_fetch", fake_fetch)
    result = source.fetch_screener("SOMECO")

    assert result.fiscal_years == [2025, 2026], "abandoned consolidated too eagerly"
    assert not any("stale" in w for w in result.warnings)


def test_a_healthy_consolidated_page_is_not_refetched(monkeypatch):
    """The extra standalone probe must only fire for suspiciously short
    consolidated histories, or every company costs two requests.

    The page must include a quarters block: a consolidated page with NO
    quarterly periods legitimately triggers the stub fallback, so omitting it
    here would be testing the wrong thing.
    """
    from app.data import screener_source as source

    requested: list[str] = []

    def fake_fetch(url, **_kwargs):
        requested.append(url)
        return (_page(range(2015, 2027))
                + _page([2025, 2026], section="quarters"))

    monkeypatch.setattr(source, "_fetch", fake_fetch)
    source.fetch_screener("HEALTHY")

    assert len(requested) == 1, f"made {len(requested)} requests, expected 1"


def test_stub_consolidated_quarters_block_falls_back_to_standalone(monkeypatch):
    """A consolidated page can be complete annually and a stub quarterly.

    Colgate Palmolive India, AU Small Finance Bank and Five-Star Business
    Finance each serve a consolidated `quarters` section containing two `<th>`
    cells and a "View Standalone" link where the date columns should be. The
    annual tables on the same page are complete, so the stale-page fallback
    correctly leaves them alone — and the company silently showed no quarterly
    results at all.
    """
    from app.data import screener_source as source

    def fake_fetch(url, **_kwargs):
        annual = _page(range(2015, 2027))
        if url.endswith("/consolidated/"):
            # Annual data present; quarterly block is a header-less stub.
            stub = ('<section id="quarters"><table><thead><tr>'
                    '<th class="text"></th></tr></thead>'
                    "<tbody><tr><td>Sales +</td></tr></tbody>"
                    "</table></section>")
            return annual + stub
        return annual + _page([2025, 2026], section="quarters")

    monkeypatch.setattr(source, "_fetch", fake_fetch)
    result = source.fetch_screener("COLPAL")

    assert result.quarters, "quarterly block was left empty"
    assert "consolidated quarterly block is a stub" in " ".join(result.warnings)
    # The annual statements must still come from the consolidated page.
    assert len(result.fiscal_years) == 12


def test_no_extra_request_when_consolidated_quarters_are_present(monkeypatch):
    from app.data import screener_source as source

    requested: list[str] = []

    def fake_fetch(url, **_kwargs):
        requested.append(url)
        return _page(range(2015, 2027)) + _page([2025, 2026], section="quarters")

    monkeypatch.setattr(source, "_fetch", fake_fetch)
    source.fetch_screener("FINE")

    assert len(requested) == 1, f"made {len(requested)} requests, expected 1"


# ----------------------------------------------------------------- endpoint

def test_quarterly_endpoint_serialises_a_slots_dataclass(api_client):
    """Regression for a 500 that reached production.

    `QuarterRow` is declared `@dataclass(slots=True)` and therefore has NO
    instance `__dict__`. The handler built its response with
    `QuarterRowOut(**row.__dict__)`, which raises AttributeError, so the
    endpoint returned HTTP 500 for every company that HAD quarters — while
    the stored data was perfectly good.

    The first version of this test asked the API for any company and checked
    for a 200. It passed with the bug still in place, because the seeded test
    companies have no quarterly rows at all, so the list comprehension never
    executed. A test that cannot fail is worse than no test: it certifies the
    defect. This one INSERTS a quarter first, which is what makes the
    serialisation path run.
    """
    from app.models.analysis import QuarterlyResult
    from tests.conftest import TestingSession

    listing = api_client.get("/api/v1/companies", params={"page_size": 5})
    assert listing.status_code == 200
    results = listing.json()["results"]
    assert results, "no companies available to test against"
    company = results[0]

    session = TestingSession()
    try:
        session.add(QuarterlyResult(
            company_id=company["id"], fiscal_year=2026, quarter=2,
            revenue=500.0, operating_profit=70.0, operating_margin=0.14,
            net_profit=50.0, eps=4.2, source="test",
        ))
        session.commit()

        response = api_client.get(f"/api/v1/company/{company['ticker']}/quarterly")
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["has_data"] is True
        assert body["unavailable_reason"] is None
        row = next(q for q in body["quarters"] if q["label"] == "Q2 FY26")
        assert row["revenue"] == 500.0
        assert row["operating_margin"] == 0.14
    finally:
        session.query(QuarterlyResult).filter_by(
            company_id=company["id"], fiscal_year=2026, quarter=2,
        ).delete()
        session.commit()
        session.close()


def test_quarterly_endpoint_explains_an_empty_result(api_client):
    """An empty list must say why, never be left ambiguous."""
    listing = api_client.get("/api/v1/companies", params={"page_size": 5})
    ticker = listing.json()["results"][0]["ticker"]

    body = api_client.get(f"/api/v1/company/{ticker}/quarterly").json()
    if not body["has_data"]:
        assert body["unavailable_reason"]


def test_quarterly_endpoint_404s_for_an_unknown_ticker(api_client):
    response = api_client.get("/api/v1/company/NOSUCHTICKER/quarterly")
    assert response.status_code == 404
