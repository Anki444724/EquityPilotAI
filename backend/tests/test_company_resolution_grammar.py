"""Company-resolution grammar-word regression tests.

Production bug: a public/authenticated chat question phrased in Hinglish —
"BEL ki valuation … ke basis par explain karo" — was silently retargeted from
BEL to a stored company whose ticker is PAR, because the Hindi postposition
"par" was read as the ticker PAR. The answer then carried PAR's data-quality
warning and PAR's (empty) record, and the BEL Blogger evidence vanished.

`CompanyService.named_in()` now treats grammar/function words written as
ordinary text as words, not tickers: only an ALL-CAPS occurrence of those
letters ("PAR ka debt kya hai?") is deliberate ticker intent. Lowercase
ticker usage ("tcs ka debt kya hai?") is unchanged.

These tests pin the service-level resolution and the end-to-end chat flow
against an isolated in-memory database seeded with BEL, PAR and TCS.
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

import app.models as _models  # noqa: F401  (create_all must see every table)

PASSWORD = "a-strong-test-password-1"
EMAIL = "grammar@test.com"

#: The Hindi postposition "par" must never resolve this stored ticker.
PAR_NAME = "Par Drugs and Chemicals Ltd"

#: Distinct revenue/debt figures so an answer can be proven to have used one
#: company's record rather than another's. The full key set mirrors
#: test_chat_company_context.py — the ratio engine needs a complete balance
#: sheet to compute Altman-Z without a TypeError.
FACTS = {
    "BEL": {
        "name": "Bharat Electronics Ltd", "isin": "INE263A01024",
        "rows": {
            "revenue": 27_610.0, "net_block_ppe": 12_000.0,
            "cash_and_bank": 5_000.0, "equity_share_capital": 8_000.0,
            "long_term_borrowings": 0.0, "short_term_borrowings": 0.0,
            "current_maturities_ltd": 0.0, "trade_receivables": 6_000.0,
            "inventories": 4_000.0, "trade_payables": 5_000.0,
            "raw_materials": 12_000.0, "employee_benefit": 4_000.0,
        },
    },
    "PAR": {
        "name": PAR_NAME, "isin": "INE200000000",
        "rows": {
            "revenue": 950.0, "net_block_ppe": 200.0,
            "cash_and_bank": 40.0, "equity_share_capital": 100.0,
            "long_term_borrowings": 400.0, "short_term_borrowings": 50.0,
            "current_maturities_ltd": 10.0, "trade_receivables": 100.0,
            "inventories": 80.0, "trade_payables": 90.0,
            "raw_materials": 300.0, "employee_benefit": 100.0,
        },
    },
    "TCS": {
        "name": "Tata Consultancy Services", "isin": "INE467B01029",
        "rows": {
            "revenue": 50_000.0, "net_block_ppe": 10_000.0,
            "cash_and_bank": 4_000.0, "equity_share_capital": 5_000.0,
            "long_term_borrowings": 6_000.0, "short_term_borrowings": 0.0,
            "current_maturities_ltd": 0.0, "trade_receivables": 3_000.0,
            "inventories": 0.0, "trade_payables": 2_000.0,
            "raw_materials": 10_000.0, "employee_benefit": 2_000.0,
        },
    },
}

#: Every grammar/function word the fix must treat as a word, not a ticker.
GRAMMAR_WORDS = [
    "par", "ka", "ki", "ke", "ko", "se", "me", "mein", "hai", "hain",
    "ya", "hi", "na", "the",
]


@pytest.fixture(scope="module")
def ctx():
    """Authenticated client + BEL/PAR/TCS seeded on an isolated DB."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenant = TenantService(db).create(
            "Grammar Words Capital", tier=PlanTier.ENTERPRISE)
        IdentityService(db).register(
            email=EMAIL, password=PASSWORD, name="Grammar Test",
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

class TestNamedInGrammarWords:
    def test_hindi_postposition_par_does_not_name_par(self, ctx):
        """The exact production bug: 'par' is grammar, not the PAR ticker."""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(
                "BEL ki valuation expensive hai ya reasonable? "
                "P/E, ROE aur ROCE ke basis par explain karo.",
                exclude_id=ctx["ids"]["BEL"],
            )
        assert named == []

    def test_all_caps_par_still_resolves_par(self, ctx):
        """Deliberate all-caps ticker intent is preserved."""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(
                "PAR ka debt kya hai?", exclude_id=ctx["ids"]["BEL"],
            )
        assert [c.ticker for c in named] == ["PAR"]

    def test_lowercase_ticker_still_resolves(self, ctx):
        """The existing lowercase-ticker contract is unchanged."""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in("tcs ka debt kya hai?")
        assert [c.ticker for c in named] == ["TCS"]

    @pytest.mark.parametrize("word", GRAMMAR_WORDS)
    def test_grammar_word_names_no_company(self, ctx, word):
        """Every listed grammar word, written as ordinary text, resolves nothing."""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(f"{word} ke baare mein batao")
        assert named == [], f"grammar word {word!r} was read as a ticker"

    def test_explicit_metrics_question_does_not_name_par(self, ctx):
        """The second failing question, with P/E/ROE/ROCE figures, names nobody."""
        with ctx["session"]() as db:
            named = CompanyService(db).named_in(
                "BEL ka P/E 48.2x, ROE 27.4% aur ROCE 36.4% hai. "
                "In figures ke basis par BEL ki valuation expensive hai "
                "ya reasonable?",
                exclude_id=ctx["ids"]["BEL"],
            )
        assert named == []

    def test_order_book_and_revenue_questions_do_not_name_par(self, ctx):
        with ctx["session"]() as db:
            for question in (
                "Q1 FY27 ke start par BEL ka latest order book kitna hai?",
                "BEL ka FY26 revenue aur provisional standalone turnover "
                "kitna raha?",
            ):
                named = CompanyService(db).named_in(
                    question, exclude_id=ctx["ids"]["BEL"],
                )
                assert named == [], question


# ---------------------------------------------------------------------------
# API: the chat flow stays scoped to BEL despite the Hindi "par"
# ---------------------------------------------------------------------------

def _chat(client, ticker, question, session_id):
    response = client.post(
        f"/api/v1/company/{ticker}/ai/chat",
        json={"question": question, "session_id": session_id},
    )
    assert response.status_code == 200, response.text[:500]
    return response.json()


class TestChatStaysScopedToBEL:
    def test_valuation_question_with_hindi_par_stays_bel(self, ctx):
        client, ids = ctx["client"], ctx["ids"]
        body = _chat(
            client, "BEL",
            "BEL ki valuation expensive hai ya reasonable? "
            "P/E, ROE aur ROCE ke basis par explain karo.",
            "grammar-valuation",
        )

        # Company identity throughout the response: BEL, never PAR.
        assert body["company"]["ticker"] == "BEL"
        assert body["company"]["name"] == "Bharat Electronics Ltd"
        assert body["company"]["id"] == ids["BEL"]
        assert "BEL" in body["session_state"]

        # PAR is never silently substituted.
        assert "PAR" not in body["session_state"].upper()
        assert "PAR" not in body["company"]["ticker"].upper()

        # No switch note: the URL's company answered.
        assert not any(
            "named" in w.lower() and "record" in w.lower()
            for w in body["warnings"]
        )

    def test_explicit_metrics_question_stays_bel(self, ctx):
        client, _ = ctx["client"], ctx["ids"]
        body = _chat(
            client, "BEL",
            "BEL ka P/E 48.2x, ROE 27.4% aur ROCE 36.4% hai. "
            "In figures ke basis par BEL ki valuation expensive hai "
            "ya reasonable?",
            "grammar-metrics",
        )
        assert body["company"]["ticker"] == "BEL"
        assert "PAR" not in body["session_state"].upper()

    def test_deliberate_all_caps_par_still_switches(self, ctx):
        """The existing switch behaviour survives for genuine ticker mentions."""
        client, ids = ctx["client"], ctx["ids"]
        body = _chat(client, "BEL", "PAR ka debt kya hai?", "grammar-switch")

        assert body["company"]["ticker"] == "PAR"
        assert body["company"]["name"] == PAR_NAME
        assert body["company"]["id"] == ids["PAR"]

        # The switch is stated in the response, not silent.
        notes = [w for w in body["warnings"] if "named PAR" in w]
        assert notes, f"expected a company-switch note, got {body['warnings']}"
        assert "scoped to BEL" in notes[0]
