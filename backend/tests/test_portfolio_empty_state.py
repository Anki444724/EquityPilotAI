"""The contract the Portfolio Intelligence page depends on.

The page reported "Loading…" and "No portfolios yet" at the same time, and the
first suspicion was authentication, because a hand-run

    fetch('/api/v1/portfolios', {credentials: 'include'})

in the browser console returned 401. It does — that call carries the refresh
cookie but no `Authorization: Bearer`, and the endpoint requires the bearer
token. It says nothing about what the application itself receives.

These tests separate the two, under *real* authentication (NATIVE_AUTH), so the
frontend's states can be built on facts:

* an authenticated user who owns nothing gets `200 []`, never a 401 and never
  an error — so "no portfolios" is an empty state, not a session problem;
* an unauthenticated caller gets exactly the 401 seen in the console;
* portfolios are per-owner, so one user's book never appears for another.
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

import app.models as _models  # noqa: F401

BASE = "/api/v1"
PASSWORD = "a-strong-test-password-1"
FRESH_USER = "nobody@alpha.com"
OTHER_USER = "somebody@alpha.com"


@pytest.fixture(scope="module")
def ctx():
    """Two verified users in one tenant, neither owning a portfolio yet."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenant = TenantService(db).create("Alpha Capital", tier=PlanTier.ENTERPRISE)
        for email, name in ((FRESH_USER, "Nobody"), (OTHER_USER, "Somebody")):
            IdentityService(db).register(
                email=email, password=PASSWORD, name=name,
                tenant_id=tenant.id, role=Role.ANALYST, auto_verify=True,
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

    yield TestClient(app)

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _sign_in(client: TestClient, email: str) -> str:
    response = client.post(
        f"{BASE}/auth/login", json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestPortfolioListContract:
    def test_an_unauthenticated_caller_is_refused(self, ctx):
        """What the browser console reproduced — and it is correct."""
        response = ctx.get(f"{BASE}/portfolios", headers={"Authorization": ""})
        assert response.status_code == 401
        assert response.json()["detail"] == "Authentication required."

    def test_a_user_who_owns_nothing_gets_an_empty_list_not_an_error(self, ctx):
        """The state the page must render as "no portfolios yet"."""
        token = _sign_in(ctx, FRESH_USER)
        response = ctx.get(f"{BASE}/portfolios", headers=_bearer(token))
        assert response.status_code == 200, response.text
        assert response.json() == []

    def test_the_bearer_token_is_what_authenticates_the_request(self, ctx):
        """Cookies alone are not enough; the access token is."""
        token = _sign_in(ctx, FRESH_USER)
        # The login response set the refresh cookie on the client, so this is
        # the console's exact request: cookie present, bearer absent.
        assert ctx.cookies, "login should set a refresh cookie"
        without = ctx.get(f"{BASE}/portfolios", headers={"Authorization": ""})
        assert without.status_code == 401
        with_token = ctx.get(f"{BASE}/portfolios", headers=_bearer(token))
        assert with_token.status_code == 200

    def test_creating_a_portfolio_makes_it_appear_in_the_list(self, ctx):
        """The empty state's Create Portfolio call, and its effect."""
        token = _sign_in(ctx, FRESH_USER)
        created = ctx.post(
            f"{BASE}/portfolios",
            json={"name": "Core equity", "benchmark": "NIFTY 50",
                  "base_currency": "INR"},
            headers=_bearer(token),
        )
        assert created.status_code == 201, created.text
        assert created.json()["name"] == "Core equity"

        listed = ctx.get(f"{BASE}/portfolios", headers=_bearer(token))
        assert listed.status_code == 200
        names = [p["name"] for p in listed.json()]
        assert names == ["Core equity"]

    def test_portfolios_are_scoped_to_their_owner(self, ctx):
        """The other user still sees an empty list, not someone else's book."""
        other = _sign_in(ctx, OTHER_USER)
        response = ctx.get(f"{BASE}/portfolios", headers=_bearer(other))
        assert response.status_code == 200
        assert response.json() == []
