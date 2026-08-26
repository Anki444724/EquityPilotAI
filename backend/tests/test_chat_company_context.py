"""Company-context resolution in the AI chat flow.

Regression for the production failure where a question about BHARATCP was
answered from RELIANCE's record — the response carried RELIANCE's data
quality warning and RELIANCE's debt. The chat URL scopes a conversation to
one company, but the question may name a different one; the company being
asked about must decide whose record answers, and the response must carry
that company's name, ticker, facts and citations throughout.

The API tests run against an isolated, authenticated in-memory database
(NATIVE_AUTH login, per the test_admin_ai.py pattern) seeded with THREE
companies whose debt figures are all different, so "the answer used the
right company" is asserted on the numbers themselves, not just the labels.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, get_db
from app.domain.platform.identity import Role
from app.domain.platform.plans import PlanTier
from app.main import app
from app.services.platform.entitlements import EntitlementService
from app.services.platform.identity_service import IdentityService
from app.services.platform.tenancy import TenantService
from app.services.company_service import CompanyService
from app.services.analysis_service import AnalysisService

import app.models as _models  # noqa: F401  (create_all must see every table)

PASSWORD = "a-strong-test-password-1"
EMAIL = "ctx@test.com"

#: FY2024 facts per company. The three companies carry DELIBERATELY distinct
#: debt figures so a response can be proven to have used one company's record
#: rather than another's: gross_debt = short_term_borrowings +
#: current_maturities_ltd + long_term_borrowings.
#:   RELIANCE -> 10,000 + 5,000 + 490,000 = 505,000
#:   BHARATCP ->    250 +   100 +   2,500 =   2,850
#:   TCS      ->      0 +     0 +  6,000 =   6,000
FACTS = {
    "RELIANCE": {
        "name": "Reliance Industries", "isin": "INE002A01021",
        "rows": {
            "revenue": 1_000_000.0, "net_block_ppe": 600_000.0,
            "cash_and_bank": 100_000.0, "equity_share_capital": 100_000.0,
            "long_term_borrowings": 490_000.0, "short_term_borrowings": 10_000.0,
            "current_maturities_ltd": 5_000.0, "trade_receivables": 60_000.0,
            "inventories": 40_000.0, "trade_payables": 50_000.0,
            "raw_materials": 400_000.0, "employee_benefit": 80_000.0,
        },
    },
    "BHARATCP": {
        "name": "Bharat Consumer Products Ltd", "isin": "INE818181819",
        "rows": {
            "revenue": 12_000.0, "net_block_ppe": 6_000.0,
            "cash_and_bank": 1_000.0, "equity_share_capital": 500.0,
            "long_term_borrowings": 2_500.0, "short_term_borrowings": 250.0,
            "current_maturities_ltd": 100.0, "trade_receivables": 600.0,
            "inventories": 400.0, "trade_payables": 500.0,
            "raw_materials": 4_000.0, "employee_benefit": 800.0,
        },
    },
    "TCS": {
        "name": "Tata Consultancy Services", "isin": "INE467B01029",
        "rows": {
            "revenue": 50_000.0, "net_block_ppe": 10_000.0,
            "cash_and_bank": 4_000.0, "equity_share_capital": 5_000.0,
            "long_term_borrowings": 6_000.0, "trade_receivables": 3_000.0,
            "trade_payables": 2_000.0, "raw_materials": 10_000.0,
            "employee_benefit": 2_000.0,
        },
    },
}


@pytest.fixture(scope="module")
def ctx():
    """Authenticated client + three seeded companies on an isolated DB."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenant = TenantService(db).create(
            "Company Context Capital", tier=PlanTier.ENTERPRISE)
        IdentityService(db).register(
            email=EMAIL, password=PASSWORD, name="Ctx Test",
            tenant_id=tenant.id, role=Role.ADMIN, auto_verify=True,
        )

    def _override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    prev_override = app.dependency_overrides.get(get_db)
    prev_native = settings.NATIVE_AUTH
    app.dependency_overrides[get_db] = _override
    settings.NATIVE_AUTH = True

    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers.update({
        "Authorization": f"Bearer {login.json()['access_token']}",
    })

    ids: dict[str, str] = {}
    for ticker, spec in FACTS.items():
        created = client.post("/api/v1/admin/companies", json={
            "name": spec["name"], "ticker": ticker, "isin": spec["isin"],
        })
        assert created.status_code == 201, created.text
        company_id = created.json()["id"]
        ids[ticker] = company_id
        facts = [
            {"fiscal_year": 2024, "line_item": item, "value": value}
            for item, value in spec["rows"].items()
        ]
        seeded = client.put(
            f"/api/v1/admin/financials/{company_id}/facts", json=facts)
        assert seeded.status_code == 200, seeded.text

    yield {"client": client, "ids": ids, "session": Session}

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


# ---------------------------------------------------------------------------
# Service level: which companies does free text actually name?
# ---------------------------------------------------------------------------

class TestNamedIn:
    def test_ticker_match_is_case_insensitive_whole_token(self, ctx):
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(
                "bharatcp ka total debt kitna hai?",
                exclude_id=ctx["ids"]["RELIANCE"],
            )
        assert [c.ticker for c in named] == ["BHARATCP"]

    def test_full_name_match(self, ctx):
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(
                "What is the total debt of Reliance Industries?",
                exclude_id=ctx["ids"]["RELIANCE"],
            )
        assert [c.ticker for c in named] == []  # the scoped one is excluded

        named = CompanyService(db).named_in(
            "What is the total debt of Reliance Industries?",
            exclude_id=ctx["ids"]["BHARATCP"],
        )
        assert [c.ticker for c in named] == ["RELIANCE"]

    def test_partial_name_does_not_match(self, ctx):
        """A name fragment that is not itself a stored ticker is not an
        unambiguous company reference. ('Reliance' WOULD match here, because
        RELIANCE is a stored ticker — a ticker in any case is intent.)"""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in("What does Tata do?")
        assert named == []

    def test_several_companies_are_all_returned(self, ctx):
        """Ambiguity is reported, not resolved by a guess."""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(
                "Compare RELIANCE and TCS on debt")
        assert {c.ticker for c in named} == {"RELIANCE", "TCS"}

    def test_unknown_name_matches_nothing(self, ctx):
        with ctx["session"]() as db:
            assert CompanyService(db).named_in("Debt of XYZCORP?") == []

    def test_empty_text_matches_nothing(self, ctx):
        with ctx["session"]() as db:
            assert CompanyService(db).named_in("") == []


# ---------------------------------------------------------------------------
# API: the chat flow uses the named company throughout
# ---------------------------------------------------------------------------

def _chat(client, ticker, question, session_id):
    response = client.post(
        f"/api/v1/company/{ticker}/ai/chat",
        json={"question": question, "session_id": session_id},
    )
    assert response.status_code == 200, response.text[:500]
    return response.json()


class TestChatCompanyContext:
    def test_bharatcp_chat_uses_bharatcp_throughout(self, ctx):
        """The exact regression: /company/BHARATCP/ai/chat must answer from
        BHARATCP's record — name, ticker, facts and citations — with no
        RELIANCE anywhere in the response."""
        client, ids = ctx["client"], ctx["ids"]
        body = _chat(
            client, "BHARATCP",
            "What is the total debt of BHARATCP?", "ctx-bharat",
        )

        # Company identity throughout the response.
        assert body["company"]["ticker"] == "BHARATCP"
        assert body["company"]["name"] == "Bharat Consumer Products Ltd"
        assert body["company"]["id"] == ids["BHARATCP"]
        assert "BHARATCP" in body["session_state"]

        # The pinned session company is the one answered, not the URL's twin.
        assert "RELIANCE" not in body["session_state"].upper()

        # No RELIANCE leakage in the answer or its warnings.
        for field in ("content", "display_content"):
            assert "reliance" not in body[field].lower()
        assert not any("reliance" in w.lower() for w in body["warnings"])

        # Data quality (when present) names the company that was used.
        quality = body.get("data_quality") or {}
        if quality.get("warning"):
            assert "BHARATCP" in quality["warning"]
            assert "RELIANCE" not in quality["warning"]

        # BHARATCP's facts, not RELIANCE's: gross debt 2500+250+100 = 2,850.
        assert "2,850.00" in body["content"]
        assert "505,000" not in body["content"]
        # Citations resolve to debt evidence of the answered company.
        assert body["citations"]
        assert any("debt" in c["label"].lower() for c in body["citations"])

    def test_question_names_another_company_resolves_it(self, ctx):
        """The production failure, fixed: a BHARATCP question typed into a
        RELIANCE-scoped chat is answered from BHARATCP's record."""
        client, ids = ctx["client"], ctx["ids"]
        body = _chat(
            client, "RELIANCE",
            "What is the total debt of BHARATCP?", "ctx-cross",
        )

        assert body["company"]["ticker"] == "BHARATCP"
        assert body["company"]["name"] == "Bharat Consumer Products Ltd"
        assert body["company"]["id"] == ids["BHARATCP"]
        assert "BHARATCP" in body["session_state"]

        # BHARATCP's figures, never RELIANCE's.
        assert "2,850.00" in body["content"]
        assert "505,000" not in body["content"]
        assert "reliance" not in body["content"].lower()

        # Data quality follows the answered company too.
        quality = body.get("data_quality") or {}
        if quality.get("warning"):
            assert "BHARATCP" in quality["warning"]
            assert "RELIANCE" not in quality["warning"]

        # The switch is stated in the response, not silent.
        notes = [w for w in body["warnings"] if "named BHARATCP" in w]
        assert notes, f"expected a company-switch note, got {body['warnings']}"
        assert "scoped to RELIANCE" in notes[0]

    def test_scoped_company_unchanged_when_question_names_none(self, ctx):
        client, _ = ctx["client"], ctx["ids"]
        body = _chat(
            client, "RELIANCE", "What is the total debt?", "ctx-self",
        )
        assert body["company"]["ticker"] == "RELIANCE"
        assert "505,000.00" in body["content"]
        assert "2,850.00" not in body["content"]
        # No switch note: the URL's company answered, as before.
        assert not any("named" in w.lower() and "record" in w.lower()
                       for w in body["warnings"])

    def test_naming_the_scoped_company_is_not_a_switch(self, ctx):
        """'What is the total debt of RELIANCE?' in a RELIANCE chat is the
        same company — no switch, no note."""
        client, _ = ctx["client"], ctx["ids"]
        body = _chat(
            client, "RELIANCE", "What is the total debt of RELIANCE?",
            "ctx-same",
        )
        assert body["company"]["ticker"] == "RELIANCE"
        assert "505,000.00" in body["content"]
        assert not any("named" in w.lower() and "record" in w.lower()
                       for w in body["warnings"])

    def test_ambiguous_question_keeps_scoped_company(self, ctx):
        """Two companies named is an ambiguity: the chat keeps its scope
        rather than guessing which one was meant."""
        client, _ = ctx["client"], ctx["ids"]
        body = _chat(
            client, "TCS",
            "Compare the debt of RELIANCE and BHARATCP", "ctx-ambig",
        )
        assert body["company"]["ticker"] == "TCS"
        assert "6,000.00" in body["content"]
        assert not any("named" in w.lower() and "record" in w.lower()
                       for w in body["warnings"])

    def test_unknown_company_mention_keeps_scoped_company(self, ctx):
        """A name the platform does not hold must not retarget the answer."""
        client, _ = ctx["client"], ctx["ids"]
        body = _chat(
            client, "RELIANCE", "What is the total debt of XYZCORP?",
            "ctx-unknown",
        )
        assert body["company"]["ticker"] == "RELIANCE"
        assert "505,000.00" in body["content"]
        assert not any("named" in w.lower() and "record" in w.lower()
                       for w in body["warnings"])
