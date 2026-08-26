"""Angel One SmartAPI broker integration.

Every external Angel One call is mocked — the suite runs against an
in-memory fake broker and, for client-level tests, `httpx.MockTransport`.
No real order is ever placed; no network call leaves the process.

The API-level tests run against the shared application client from
`conftest.py` (development identity, a super admin), so they also prove the
broker module did not disturb the rest of the API. Role-based authorisation
uses real API keys minted for real lower-privileged users, the same way
`test_platform_api.py` proves the permission matrix end to end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.security import Principal
from app.domain.platform.audit import AuditAction, redact
from app.domain.platform.identity import (
    Permission, Role, has_permission,
)
from app.models.broker import BrokerAccount, BrokerOrder
from app.models.platform import AuditLog, Tenant, User
from app.services.broker import angelone_service
from app.services.broker.angelone_client import (
    AngelOneClient, b64, parse_valid_upto,
)
from app.services.broker.exceptions import (
    BrokerInvalidRequestError, BrokerNetworkError, BrokerOrderRejectedError,
    BrokerRateLimitError, BrokerSessionError,
)
from app.services.platform import cache as cache_module
from app.services.platform.cache import Namespace
from app.services.platform.crypto import decrypt_secret, new_id
from tests.conftest import TestingSession

BASE = "/api/v1/broker/angelone"

FAKE_JWT = "fake.jwt.token-9f8e7d6c"
FAKE_CLIENT_CODE = "1234567890"
FAKE_TRADING_PASSWORD = "fake-trading-password-t2"
VALID_UPTO = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y%m%d%H%M%S")


# ---------------------------------------------------------------------------
# The fake broker
# ---------------------------------------------------------------------------
class FakeClient:
    """Stands in for `AngelOneClient`. Records every call it serves."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.last_token: str | None = None
        self.next_order_id = 1_000_000_001
        self.raise_exc: Exception | None = None
        self.profile_data = {
            "CLIENT_CODE": FAKE_CLIENT_CODE,
            "CLIENT_NAME": "Test Trader",
            "EMAIL": "trader@example.com",
            "EXCH_ACCESS": {"NSE": "EQ"},
        }
        self.funds_data = [{
            "MARGIN_AVLBL_AMT": "125000.50",
            "MARGIN_USED_AMT": "1234.00",
            "MARGIN_USED_AMT_INTRADAY": "1234.00",
            "MARGIN_USED_AMT_DELIVERY": "0",
            "NET_WR": "500000.00",
        }]
        self.orders_data = [{
            "ORDER_ID": "1000000001", "EXCH_ORDER_ID": "E1",
            "SYMBOL": "RELIANCE", "EXCHANGE": "NSE", "MARKET": "NSE",
            "TRANSAK_TYPE": "B", "PRODUCT_TYPE": "CNC",
            "ORDER_TYPE": "LIMIT", "VALIDITY": "DAY",
            "QUANTITY": "10", "PRICE": "2450.00", "TRIGGER_PRICE": "0",
            "FILL_QTY": "10", "AVG_PRICE": "2449.50",
            "STATUS": "COMPLETE", "REMARK": "",
        }]
        self.positions_data = [{
            "SYMBOL": "TCS", "EXCHANGE": "NSE", "PRODUCT_TYPE": "CNC",
            "NET_QTY": "5", "AVG_PRICE": "3900.00",
        }]
        self.trades_data = [{
            "SYMBOL": "RELIANCE", "EXCHANGE": "NSE", "PRODUCT_TYPE": "CNC",
            "TRANSAK_TYPE": "B", "TRADE_QTY": "10",
            "TRADE_RATE": "2449.50", "TRADE_TIME": "2026-08-26 10:30:00",
        }]
        self.holdings_data = [{
            "SYMBOL": "INFY", "EXCHANGE": "NSE", "PRODUCT_TYPE": "CNC",
            "NET_QTY": "25", "AVG_PRICE": "1650.75",
        }]

    # -- recording -----------------------------------------------------
    def _record(self, name: str, *args) -> None:
        self.calls.append((name, args))

    def _maybe_raise(self) -> None:
        if self.raise_exc is not None:
            exc, self.raise_exc = self.raise_exc, None
            raise exc

    # -- data -----------------------------------------------------------
    def get_profile(self):
        self._record("get_profile")
        self._maybe_raise()
        return self.profile_data

    def get_funds(self):
        self._record("get_funds")
        self._maybe_raise()
        return self.funds_data

    def get_intraday_orders(self):
        self._record("get_intraday_orders")
        self._maybe_raise()
        return self.orders_data

    def get_positions(self):
        self._record("get_positions")
        self._maybe_raise()
        return self.positions_data

    def get_trades(self, trade_date):
        self._record("get_trades", trade_date)
        self._maybe_raise()
        return self.trades_data

    def get_holdings(self):
        self._record("get_holdings")
        self._maybe_raise()
        return self.holdings_data

    # -- orders -----------------------------------------------------------
    def place_order(self, order):
        self._record("place_order", dict(order))
        self._maybe_raise()
        order_id = str(self.next_order_id)
        self.next_order_id += 1
        return {"ORDER_ID": order_id}

    def modify_order(self, order):
        self._record("modify_order", dict(order))
        self._maybe_raise()
        return {"ORDER_ID": str(order.get("ORDER_ID"))}

    def cancel_order(self, order_id, exchange):
        self._record("cancel_order", order_id, exchange)
        self._maybe_raise()
        return {"ORDER_ID": str(order_id)}

    def close(self) -> None:
        self._record("close")


@pytest.fixture
def fake_broker(monkeypatch) -> FakeClient:
    fake = FakeClient()

    def factory(token: str) -> FakeClient:
        fake.last_token = token
        return fake

    monkeypatch.setattr(angelone_service, "default_client_factory", factory)
    return fake


@pytest.fixture(autouse=True)
def _clean_broker_state():
    """Broker rows and broker-state cache entries never leak between tests."""
    yield
    with TestingSession() as db:
        db.execute(delete(BrokerOrder))
        db.execute(delete(BrokerAccount))
        db.commit()
    cache_module.cache.invalidate(Namespace.BROKER_STATE)


# ---------------------------------------------------------------------------
# Flow helpers
# ---------------------------------------------------------------------------
def _start_login(client: TestClient) -> str:
    response = client.get(f"{BASE}/login", follow_redirects=False)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(settings.ANGELONE_LOGIN_URL), location
    query = parse_qs(urlparse(location).query)
    assert query.get("REDIRECT_URL") == [settings.ANGELONE_REDIRECT_URL]
    state = query["STATE"][0]
    assert state, "login must mint a state token"
    return state


def _complete_login(client: TestClient, state: str, **overrides) -> TestClient.Response:
    params = {
        "state": state,
        "access_token": FAKE_JWT,
        "client_code": FAKE_CLIENT_CODE,
        "valid_upto": VALID_UPTO,
        "trading_password": FAKE_TRADING_PASSWORD,
    }
    params.update(overrides)
    return client.get(f"{BASE}/callback", params=params, follow_redirects=False)


def _logged_in(api_client: TestClient, fake_broker: FakeClient) -> None:
    state = _start_login(api_client)
    response = _complete_login(api_client, state)
    assert response.status_code == 302, response.text
    assert response.headers["location"] == settings.ANGELONE_FRONTEND_REDIRECT
    assert api_client.get(f"{BASE}/status").json()["connected"] is True


def _audit_actions(db, action: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def _last_call(fake_broker: FakeClient, name: str) -> tuple:
    """The most recent recorded call of `name` (the fake also records the
    trailing `close` after every served call)."""
    calls = [call for call in fake_broker.calls if call[0] == name]
    assert calls, f"the broker was never asked to {name}"
    return calls[-1]


def _dev_account(db) -> BrokerAccount | None:
    return db.scalar(select(BrokerAccount).where(BrokerAccount.user_id == "dev-user"))


# ===========================================================================
# Login and callback
# ===========================================================================
class TestLoginCallback:
    def test_login_mints_state_and_redirects_to_broker(self, api_client, fake_broker):
        state = _start_login(api_client)
        stored = cache_module.cache.get(
            Namespace.BROKER_STATE, "login-state", "dev-user",
        )
        assert stored == {"state": state, "used": False}
        with TestingSession() as db:
            assert _audit_actions(db, AuditAction.BROKER_LOGIN_STARTED.value)

    def test_callback_persists_enveloped_session(self, api_client, fake_broker):
        state = _start_login(api_client)
        response = _complete_login(api_client, state)
        assert response.status_code == 302, response.text
        assert response.headers["location"] == settings.ANGELONE_FRONTEND_REDIRECT

        with TestingSession() as db:
            account = _dev_account(db)
            assert account is not None
            assert account.connected is True
            assert account.client_code == FAKE_CLIENT_CODE
            assert account.angel_valid_upto is not None
            # The token is enveloped at rest: ciphertext, not plaintext.
            assert account.access_token_encrypted
            assert account.access_token_encrypted != FAKE_JWT.encode()
            assert decrypt_secret(account.access_token_encrypted) == FAKE_JWT
            assert account.trading_password_encrypted is not None
            assert decrypt_secret(account.trading_password_encrypted) == FAKE_TRADING_PASSWORD
            assert _audit_actions(db, AuditAction.BROKER_LOGIN_SUCCEEDED.value)

        # The next broker call must carry the decrypted token.
        api_client.get(f"{BASE}/profile")
        assert fake_broker.last_token == FAKE_JWT

    def test_callback_state_is_single_use(self, api_client, fake_broker):
        state = _start_login(api_client)
        assert _complete_login(api_client, state).status_code == 302
        replay = _complete_login(api_client, state)
        assert replay.status_code == 400
        with TestingSession() as db:
            assert _audit_actions(db, AuditAction.BROKER_CALLBACK_REJECTED.value)

    def test_callback_unknown_state_rejected(self, api_client, fake_broker):
        response = _complete_login(api_client, "not-a-state-we-issued")
        assert response.status_code == 400
        with TestingSession() as db:
            assert _dev_account(db) is None
            assert _audit_actions(db, AuditAction.BROKER_CALLBACK_REJECTED.value)

    def test_callback_expired_state_rejected(self, api_client, fake_broker, monkeypatch):
        monkeypatch.setattr(settings, "ANGELONE_STATE_TTL_SECONDS", 0)
        state = _start_login(api_client)
        response = _complete_login(api_client, state)
        assert response.status_code == 400
        with TestingSession() as db:
            assert _dev_account(db) is None

    def test_callback_without_token_rejected(self, api_client, fake_broker):
        state = _start_login(api_client)
        response = _complete_login(api_client, state, access_token="")
        assert response.status_code == 400
        with TestingSession() as db:
            assert _dev_account(db) is None

    def test_callback_invalid_client_code_rejected(self, api_client, fake_broker):
        state = _start_login(api_client)
        response = _complete_login(api_client, state, client_code="not-tent-digit")
        assert response.status_code == 400
        with TestingSession() as db:
            assert _dev_account(db) is None


# ===========================================================================
# Session lifecycle
# ===========================================================================
class TestSessionLifecycle:
    def test_status_reports_disconnected_before_login(self, api_client, fake_broker):
        body = api_client.get(f"{BASE}/status").json()
        assert body["connected"] is False
        assert body["client_code"] is None

    def test_status_reports_connected_after_login(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/status").json()
        assert body["connected"] is True
        assert body["client_code"] == FAKE_CLIENT_CODE
        assert body["valid_upto"] is not None

    def test_disconnect_wipes_tokens(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.post(f"{BASE}/disconnect")
        assert response.status_code == 200
        assert response.json()["status"] == "disconnected"

        with TestingSession() as db:
            account = _dev_account(db)
            assert account.connected is False
            assert account.access_token_encrypted is None
            assert account.trading_password_encrypted is None
            assert _audit_actions(db, AuditAction.BROKER_DISCONNECTED.value)

        assert api_client.get(f"{BASE}/status").json()["connected"] is False
        # The wiped session can no longer mint a broker call.
        profile = api_client.get(f"{BASE}/profile")
        assert profile.status_code == 401
        assert fake_broker.calls == []

    def test_data_read_without_session_returns_401(self, api_client, fake_broker):
        for path in ("/profile", "/funds", "/orders", "/positions", "/holdings", "/trades"):
            response = api_client.get(f"{BASE}{path}")
            assert response.status_code == 401, path
        assert fake_broker.calls == []


# ===========================================================================
# Data reads and normalisation
# ===========================================================================
class TestDataReads:
    def test_profile_normalised(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/profile").json()
        assert body["client_code"] == FAKE_CLIENT_CODE
        assert body["client_name"] == "Test Trader"
        assert body["exchanges"] == {"NSE": "EQ"}

    def test_funds_normalised_coerces_strings(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/funds").json()
        assert body["available"] == 125000.5
        assert body["used"] == 1234.0
        assert body["buckets"][0]["net_worth"] == 500000.0

    def test_orders_normalised(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/orders").json()
        assert len(body) == 1
        row = body[0]
        assert row["order_id"] == "1000000001"
        assert row["symbol"] == "RELIANCE"
        assert row["quantity"] == 10
        assert row["price"] == 2450.0
        assert row["fill_quantity"] == 10
        assert row["average_price"] == 2449.5
        assert row["status"] == "COMPLETE"

    def test_positions_normalised(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/positions").json()
        assert body == [{
            "symbol": "TCS", "exchange": "NSE", "product": "CNC",
            "net_quantity": 5, "average_price": 3900.0,
        }]

    def test_trades_default_to_today(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/trades").json()
        assert body[0]["symbol"] == "RELIANCE"
        assert body[0]["quantity"] == 10
        assert body[0]["rate"] == 2449.5
        trades_calls = [call for call in fake_broker.calls if call[0] == "get_trades"]
        assert trades_calls, "the broker was never asked for trades"
        (name, args) = trades_calls[-1]
        assert args[0] == datetime.now(timezone.utc).date().strftime("%Y%m%d")

    def test_holdings_normalised(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        body = api_client.get(f"{BASE}/holdings").json()
        assert body == [{
            "symbol": "INFY", "exchange": "NSE", "product": "CNC",
            "quantity": 25, "average_price": 1650.75,
        }]


# ===========================================================================
# Orders
# ===========================================================================
ORDER_PAYLOAD = {
    "symbol": "reliance",  # lower-case on purpose: must arrive upper-case
    "side": "B",
    "product": "CNC",
    "order_type": "LIMIT",
    "validity": "DAY",
    "quantity": 10,
    "price": 2450.0,
}


class TestOrders:
    def test_place_order_persists_and_audits(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["order_id"] == "1000000001"
        assert body["status"] == "OPEN"
        assert body["symbol"] == "RELIANCE"

        (name, args) = _last_call(fake_broker, "place_order")
        sent = args[0]
        assert name == "place_order"
        assert sent["SYMBOL"] == "RELIANCE"
        assert sent["TRANSAK_TYPE"] == "B"
        assert sent["ORDER_ID"] == 0
        assert sent["APP_CODE"] == "SMARTAPI"

        with TestingSession() as db:
            order = db.scalar(
                select(BrokerOrder).where(BrokerOrder.user_id == "dev-user")
            )
            assert order is not None
            assert order.angel_order_id == "1000000001"
            assert order.status == "OPEN"
            assert _audit_actions(db, AuditAction.BROKER_ORDER_PLACED.value)

    def test_place_order_requires_session(self, api_client, fake_broker):
        response = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD)
        assert response.status_code == 401
        assert fake_broker.calls == []

    def test_market_order_with_price_rejected(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.post(
            f"{BASE}/orders",
            json={**ORDER_PAYLOAD, "order_type": "MARKET", "price": 2450.0},
        )
        assert response.status_code == 422
        assert fake_broker.calls == []

    def test_sl_order_without_trigger_rejected(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.post(
            f"{BASE}/orders",
            json={**ORDER_PAYLOAD, "order_type": "SL-M"},
        )
        assert response.status_code == 422
        assert fake_broker.calls == []

    def test_modify_order_sends_full_payload_and_updates_row(
        self, api_client, fake_broker,
    ):
        _logged_in(api_client, fake_broker)
        placed = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD)
        order_id = placed.json()["order_id"]

        response = api_client.post(
            f"{BASE}/orders/modify",
            json={"order_id": order_id, "price": 2480.0, "quantity": 15},
        )
        assert response.status_code == 200, response.text
        assert response.json()["price"] == 2480.0

        (name, args) = _last_call(fake_broker, "modify_order")
        sent = args[0]
        assert name == "modify_order"
        assert sent["ORDER_ID"] == int(order_id)
        assert sent["PRICE"] == 2480.0
        assert sent["QUANTITY"] == 15
        assert sent["SYMBOL"] == "RELIANCE"

        with TestingSession() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user"))
            assert order.price == 2480.0
            assert order.quantity == 15
            assert _audit_actions(db, AuditAction.BROKER_ORDER_MODIFIED.value)

    def test_modify_unknown_order_404(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.post(
            f"{BASE}/orders/modify", json={"order_id": "9999999999", "price": 1.0},
        )
        assert response.status_code == 404
        assert fake_broker.calls == []

    def test_cancel_order(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        placed = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD)
        order_id = placed.json()["order_id"]

        response = api_client.delete(f"{BASE}/orders/{order_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELLED"

        (name, args) = _last_call(fake_broker, "cancel_order")
        assert name == "cancel_order"
        assert args[0] == order_id
        assert args[1] == "NSE"

        with TestingSession() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user"))
            assert order.status == "CANCELLED"
            assert _audit_actions(db, AuditAction.BROKER_ORDER_CANCELLED.value)

    def test_cancel_unknown_order_404(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.delete(f"{BASE}/orders/424242")
        assert response.status_code == 404
        assert fake_broker.calls == []

    def test_broker_rejection_maps_to_422(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        fake_broker.raise_exc = BrokerOrderRejectedError(
            "Insufficient funds", errorcode="9001",
        )
        response = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD)
        assert response.status_code == 422
        assert "Insufficient funds" in response.json()["detail"]
        with TestingSession() as db:
            assert _audit_actions(db, AuditAction.BROKER_ORDER_REJECTED.value)
            assert db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user")) is None

    def test_broker_session_error_maps_to_401(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        fake_broker.raise_exc = BrokerSessionError("Token expired")
        response = api_client.get(f"{BASE}/funds")
        assert response.status_code == 401

    def test_broker_network_error_maps_to_502(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        fake_broker.raise_exc = BrokerNetworkError("Broker unreachable")
        response = api_client.get(f"{BASE}/funds")
        assert response.status_code == 502

    def test_broker_rate_limit_maps_to_429(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        fake_broker.raise_exc = BrokerRateLimitError("Slow down", errorcode="429")
        response = api_client.get(f"{BASE}/funds")
        assert response.status_code == 429
        assert "Retry-After" in response.headers


# ===========================================================================
# Platform rate limiting
# ===========================================================================
class TestRateLimiting:
    def test_trade_rate_limit_trips_429(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        codes = [
            api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD).status_code
            for _ in range(11)
        ]
        assert codes.count(201) == 10
        assert 429 in codes

    def test_login_rate_limit_trips_429(self, api_client, fake_broker):
        codes = [
            api_client.get(f"{BASE}/login", follow_redirects=False).status_code
            for _ in range(6)
        ]
        assert codes.count(302) == 5
        assert 429 in codes


# ===========================================================================
# Authorisation
# ===========================================================================
def _issue_key_for_role(role: Role, email: str) -> str:
    """A real API key for a real user holding `role` in a real tenant.

    The shared test database is seeded without any tenant, so the helper
    creates one (fixed slug, reused across tests) — the same way
    `test_platform_api.py` builds its own two-tenant world.
    """
    from app.domain.platform.plans import PlanTier
    from app.services.platform.entitlements import EntitlementService
    from app.services.platform.tenancy import TenantService

    with TestingSession() as db:
        EntitlementService(db).sync_catalogue()
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "broker-test-org"))
        if tenant is None:
            tenant = TenantService(db).create(
                "Broker Test Org", slug="broker-test-org", tier=PlanTier.FREE,
            )
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                id=new_id(), tenant_id=tenant.id, email=email,
                name=email.split("@")[0], role=role.value, status="active",
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        creator = Principal(
            user_id=user.id, email=user.email, name=user.name,
            role=role, tenant_id=tenant.id, tenant_slug=tenant.slug,
        )
        from app.services.platform.api_keys import ApiKeyService

        issued = ApiKeyService(db).create(
            principal=creator, name=f"broker-test-{role.value}", role=role,
        )
        return issued.plaintext


class TestAuthorization:
    def test_researcher_can_read_cannot_trade(self, api_client):
        key = _issue_key_for_role(Role.RESEARCHER, "broker-researcher@test.io")
        headers = {"X-API-Key": key}

        status = api_client.get(f"{BASE}/status", headers=headers)
        assert status.status_code == 200

        order = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD, headers=headers)
        assert order.status_code == 403

        disconnect = api_client.post(f"{BASE}/disconnect", headers=headers)
        assert disconnect.status_code == 200

    def test_subscriber_cannot_read_broker(self, api_client):
        key = _issue_key_for_role(Role.SUBSCRIBER, "broker-subscriber@test.io")
        headers = {"X-API-Key": key}
        assert api_client.get(f"{BASE}/status", headers=headers).status_code == 403
        assert api_client.get(f"{BASE}/profile", headers=headers).status_code == 403

    def test_permission_matrix_grants(self):
        # Read: researcher and above. Trade: analyst and above.
        assert has_permission(Role.RESEARCHER, Permission.BROKER_READ)
        assert not has_permission(Role.RESEARCHER, Permission.BROKER_TRADE)
        assert has_permission(Role.ANALYST, Permission.BROKER_READ)
        assert has_permission(Role.ANALYST, Permission.BROKER_TRADE)
        assert has_permission(Role.ADMIN, Permission.BROKER_TRADE)
        assert has_permission(Role.SUPER_ADMIN, Permission.BROKER_TRADE)
        assert not has_permission(Role.SUBSCRIBER, Permission.BROKER_READ)
        assert not has_permission(Role.READ_ONLY, Permission.BROKER_READ)
        assert not has_permission(Role.GUEST, Permission.BROKER_READ)


# ===========================================================================
# Postbacks
# ===========================================================================
def _place_order(api_client: TestClient) -> str:
    response = api_client.post(f"{BASE}/orders", json=ORDER_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()["order_id"]


def _postback_payload(order_id: str, **overrides) -> dict:
    payload = {
        "ORDER_ID": order_id,
        "EXCH_ORDER_ID": "EXCH-77",
        "CLIENT_CODE": FAKE_CLIENT_CODE,
        "SYMBOL": "RELIANCE",
        "TRANS_TYPE": "B",
        "ORDER_STATUS": "COMPLETE",
        "QUANTITY": "10",
        "FILL_QTY": "10",
        "PRICE": "2450.00",
        "AVG_PRICE": "2449.50",
        "ORDER_TYPE": "LIMIT",
        "PRODUCT": "CNC",
        "VALIDITY": "DAY",
    }
    payload.update(overrides)
    return payload


class TestPostback:
    def test_postback_updates_platform_order(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        order_id = _place_order(api_client)

        response = api_client.post(f"{BASE}/postback", json=_postback_payload(order_id))
        assert response.status_code == 200, response.text
        assert response.json() == {
            "accepted": True, "order_id": order_id, "status": "COMPLETE",
        }

        with TestingSession() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user"))
            assert order.status == "COMPLETE"
            assert order.fill_quantity == 10
            assert order.average_price == 2449.5
            assert order.exchange_order_id == "EXCH-77"
            assert order.last_postback_at is not None
            account = _dev_account(db)
            assert account.last_postback_at is not None
            rows = _audit_actions(db, AuditAction.BROKER_POSTBACK_RECEIVED.value)
            assert rows and rows[0].actor_id == "dev-user"

    def test_postback_unknown_order_refused(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        response = api_client.post(
            f"{BASE}/postback", json=_postback_payload("8888888888"),
        )
        assert response.status_code == 404
        with TestingSession() as db:
            assert db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user")) is None
            assert _audit_actions(db, AuditAction.BROKER_POSTBACK_REJECTED.value)

    def test_postback_missing_order_id_refused(self, api_client, fake_broker):
        response = api_client.post(f"{BASE}/postback", json={"SYMBOL": "RELIANCE"})
        assert response.status_code == 400
        with TestingSession() as db:
            assert _audit_actions(db, AuditAction.BROKER_POSTBACK_REJECTED.value)

    def test_postback_client_code_mismatch_refused(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        order_id = _place_order(api_client)
        response = api_client.post(
            f"{BASE}/postback",
            json=_postback_payload(order_id, CLIENT_CODE="9999999999"),
        )
        assert response.status_code == 404
        with TestingSession() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user"))
            assert order.status == "OPEN"  # untouched
            assert _audit_actions(db, AuditAction.BROKER_POSTBACK_REJECTED.value)

    def test_postback_symbol_mismatch_refused(self, api_client, fake_broker):
        _logged_in(api_client, fake_broker)
        order_id = _place_order(api_client)
        response = api_client.post(
            f"{BASE}/postback",
            json=_postback_payload(order_id, SYMBOL="TCS"),
        )
        assert response.status_code == 404
        with TestingSession() as db:
            order = db.scalar(select(BrokerOrder).where(BrokerOrder.user_id == "dev-user"))
            assert order.status == "OPEN"  # untouched

    def test_postback_requires_json(self, api_client, fake_broker):
        response = api_client.post(
            f"{BASE}/postback", content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400


# ===========================================================================
# Client-level behaviour (real serialization, mocked transport)
# ===========================================================================
_UNSET = object()


class TestBrokerClient:
    def _envelope(self, data=_UNSET, *, error=False, errorcode="0", message=""):
        return {
            "data": data if data is not _UNSET else {},
            "error": error,
            "errorcode": errorcode,
            "message": message,
        }

    def test_access_token_travels_only_in_authorization_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["url"] = str(request.url)
            return httpx.Response(200, json=self._envelope({"CLIENT_CODE": "1"}))

        client = AngelOneClient(
            access_token="super-secret-jwt",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        client.get_profile()
        assert seen["headers"]["authorization"] == "Bearer super-secret-jwt"
        assert "super-secret-jwt" not in seen["url"]

    def test_secrets_never_reach_the_logger(self, monkeypatch):
        records: list = []

        class Recorder:
            def __getattr__(self, name):
                def call(event=None, **kw):
                    records.append((name, event, kw))
                return call

        import app.services.broker.angelone_client as client_module

        monkeypatch.setattr(client_module, "log", Recorder())

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json=self._envelope(error=True, errorcode="9105",
                                         message="Invalid login credentials"),
            )

        client = AngelOneClient(
            access_token="super-secret-jwt",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(BrokerSessionError):
            client.get_profile()

        blob = repr(records)
        assert "super-secret-jwt" not in blob
        # The broker's error reason is safe to log; the token is not.
        assert "Invalid login credentials" in blob
        assert "errorcode" in blob or "9105" in blob

    def test_non_json_response_is_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>gateway</html>")

        client = AngelOneClient(
            access_token="t",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(BrokerNetworkError):
            client.get_profile()

    def test_http_429_is_rate_limit_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json=self._envelope(error=True, errorcode="429",
                                                            message="Too many requests"))

        client = AngelOneClient(
            access_token="t",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(BrokerRateLimitError):
            client.get_funds()

    def test_envelope_error_becomes_broker_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=self._envelope(error=True, errorcode="9001",
                                    message="Insufficient funds"),
            )

        client = AngelOneClient(
            access_token="t",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        from app.services.broker.exceptions import BrokerError

        with pytest.raises(BrokerError) as excinfo:
            client.place_order({"ORDER_ID": 0})
        assert excinfo.value.errorcode == "9001"

    def test_list_endpoints_tolerate_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._envelope(None))

        client = AngelOneClient(
            access_token="t",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert client.get_funds() == []
        assert client.get_holdings() == []

    def test_rest_login_validates_client_code(self):
        client = AngelOneClient(access_token=None)
        with pytest.raises(BrokerInvalidRequestError):
            client.rest_login("not-a-code", "password")
        with pytest.raises(BrokerInvalidRequestError):
            client.rest_validate("not-a-code", "req-1", "123456")

    def test_rest_login_encodes_credentials_and_parses_session(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.read().decode()
            return httpx.Response(200, json=self._envelope({
                "CLIENT_CODE": FAKE_CLIENT_CODE,
                "ACCESS_TOKEN": "fresh-jwt",
                "TRADING_PASSWORD": "t2",
                "VALID_UPTO": "20260826200000",
            }))

        client = AngelOneClient(
            access_token=None,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        data = client.rest_login(FAKE_CLIENT_CODE, "S3cret!", "246810")
        assert data["ACCESS_TOKEN"] == "fresh-jwt"
        assert b64("S3cret!") in seen["body"]
        assert b64("246810") in seen["body"]
        assert "S3cret!" not in seen["body"]

    def test_parse_valid_upto(self):
        assert parse_valid_upto("20260826173000") == datetime(
            2026, 8, 26, 17, 30, 0, tzinfo=timezone.utc,
        )
        assert parse_valid_upto(None) is None
        assert parse_valid_upto("garbage") is None
        assert parse_valid_upto("") is None

    def test_b64_matches_smartapi_placeholder(self):
        assert b64("0") == "MA=="


# ===========================================================================
# Redaction
# ===========================================================================
class TestRedaction:
    def test_audit_redaction_covers_broker_secret_names(self):
        payload = {
            "access_token": FAKE_JWT,
            "trading_password": FAKE_TRADING_PASSWORD,
            "client_code": FAKE_CLIENT_CODE,
            "order": {"TOTP": "246810", "symbol": "RELIANCE"},
        }
        redacted = redact(payload)
        assert redacted["access_token"] == "[redacted]"
        assert redacted["trading_password"] == "[redacted]"
        assert redacted["order"]["TOTP"] == "[redacted]"
        assert redacted["client_code"] == FAKE_CLIENT_CODE
        assert redacted["order"]["symbol"] == "RELIANCE"

    def test_jwt_shaped_value_redacted_under_innocent_name(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sigpart"
        assert redact({"note": jwt})["note"] == "[redacted]"
