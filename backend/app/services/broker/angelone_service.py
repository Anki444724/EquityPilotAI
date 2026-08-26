"""Product logic for the Angel One SmartAPI integration.

Sits between the API routes and `AngelOneClient` and owns the three things
a thin HTTP client must not know about:

1. **The session lifecycle.** Login starts by minting a single-use `state`
   token (cache, TTL from settings); the callback validates it, consumes it,
   and persists the broker session — the access token enveloped
   (AES-256-GCM) in `broker_accounts`, never plaintext, never in an
   environment variable. Disconnect wipes the tokens.
2. **Ownership.** Every read and order call resolves the caller's own
   `BrokerAccount`; order references resolve against the caller's own
   `BrokerOrder` rows, so a user can never reach another user's session or
   order.
3. **Postback honesty.** A postback is applied only to an order id the
   platform itself created, and only when the broker's client code matches
   the account that owns the order. Anything else is refused and audited.

Every security-relevant transition — login start/success, callback
rejection, disconnect, order placed/modified/cancelled/rejected, postback
accepted/refused — is written to the existing audit trail through
`AuditService`. The audit layer redacts sensitive key names on its own, but
this service never *passes* a token or password into metadata either.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.platform.audit import AuditAction
from app.domain.platform.identity import Principal
from app.models.broker import BrokerAccount, BrokerOrder
from app.services.broker.angelone_client import (
    AngelOneClient, APP_CODE, CLIENT_CODE_PATTERN, EXCH_ID,
    parse_valid_upto,
)
from app.services.broker.exceptions import (
    BrokerError, BrokerInvalidRequestError, BrokerOrderNotFound,
    BrokerOrderRejectedError, BrokerPostbackError, BrokerSessionError,
)
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.cache import Namespace, cache
from app.services.platform.crypto import (
    CURRENT_KEY_VERSION, decrypt_secret, encrypt_secret,
)

# Cache key parts. The key also embeds the user id at call time, so two
# users' login states can never collide.
_LOGIN_STATE = "login-state"
_SESSION_SNAPSHOT = "session-snapshot"

#: How long a *consumed* state token remains readable, so a replay within
#: the minute is recognisable (and audited) rather than merely unknown.
_CONSUMED_TTL_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Make a stored timestamp aware.

    SQLite does not persist tzinfo: a `DateTime(timezone=True)` column comes
    back naive, and comparing a naive instant against an aware one raises.
    The stored value is always UTC (that is the only instant we write), so
    re-attaching UTC is lossless. Same helper the rest of the platform uses.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def default_client_factory(access_token: str) -> AngelOneClient:
    """Build a live client. The one seam tests replace with a fake."""
    return AngelOneClient(access_token=access_token)


# ---------------------------------------------------------------------------
# Broker payload builders
# ---------------------------------------------------------------------------
def _place_payload(symbol: str, side: str, product: str, order_type: str,
                   validity: str, quantity: int, price: float | None,
                   trigger_price: float | None) -> dict[str, Any]:
    """The SmartAPI order-placement object. `ORDER_ID: 0` means *new*."""
    price = float(price or 0.0)
    trigger = float(trigger_price or 0.0)
    return {
        "ORDER_ID": 0,
        "EXCH_ID": EXCH_ID.get("NSE", 91),
        "TRANSAK_TYPE": side,
        "PRODUCT_TYPE": product,
        "SYMBOL": symbol,
        "ORDER_TYPE": order_type,
        "VALIDITY": validity,
        "QUANTITY": int(quantity),
        "PRICE": price,
        "TRIGGER_PRICE": trigger,
        "TIF": "DAY",
        "PRODUCT": product,
        "RELATIVE": 0,
        "STOP_LOSS_PRICE": trigger,
        "SQUARE_OFF": 0,
        "TIF_DURATION": 0,
        "MARKET": "NSE",
        "EXCHANGE": "NSE",
        "REMARK": "EquityPilot",
        "DURATION": 1,
        "APP_CODE": APP_CODE,
    }


def _modify_payload(order: BrokerOrder, *, price: float | None = None,
                    quantity: int | None = None,
                    trigger_price: float | None = None,
                    validity: str | None = None,
                    order_type: str | None = None) -> dict[str, Any]:
    """SmartAPI modifies by re-sending the whole order object with the
    existing `ORDER_ID` — there is no partial update."""
    base = _place_payload(
        order.symbol, order.transak_type, order.product_type,
        order.order_type, order.validity, order.quantity,
        order.price, order.trigger_price,
    )
    base["ORDER_ID"] = int(order.angel_order_id or 0)
    base["EXCHANGE"] = order.exchange
    base["MARKET"] = order.market
    if price is not None:
        base["PRICE"] = float(price)
    if quantity is not None:
        base["QUANTITY"] = int(quantity)
    if trigger_price is not None:
        base["TRIGGER_PRICE"] = float(trigger_price)
        base["STOP_LOSS_PRICE"] = float(trigger_price)
    if validity is not None:
        base["VALIDITY"] = validity
        base["TIF"] = validity
    if order_type is not None:
        base["ORDER_TYPE"] = order_type
    return base


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    """Broker numerics arrive as strings, sometimes Indian-grouped
    (`"1,23,456.00"`). Coerce, or None — never crash a read on one odd row."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def normalize_profile(data: dict[str, Any]) -> dict[str, Any]:
    exchanges = data.get("EXCH_ACCESS") or data.get("exch_access") or {}
    return {
        "client_code": str(data.get("CLIENT_CODE") or data.get("client_code") or ""),
        "client_name": str(data.get("CLIENT_NAME") or data.get("client_name") or ""),
        "email": str(data.get("EMAIL") or data.get("email") or ""),
        "exchanges": dict(exchanges) if isinstance(exchanges, dict) else {},
        "raw": {k: v for k, v in data.items()
                if k not in ("CLIENT_CODE", "CLIENT_NAME", "EMAIL", "EXCH_ACCESS",
                             "client_code", "client_name", "email", "exch_access")},
    }


def normalize_funds(rows: list[dict]) -> dict[str, Any]:
    buckets: list[dict[str, Any]] = []
    available = 0.0
    used = 0.0
    for row in rows:
        avail = _num(row.get("MARGIN_AVLBL_AMT", row.get("AVAILABLE_FUNDS")))
        used_amt = _num(row.get("MARGIN_USED_AMT"))
        available += avail or 0.0
        used += used_amt or 0.0
        buckets.append({
            "available": avail,
            "used": used_amt,
            "intraday_used": _num(row.get("MARGIN_USED_AMT_INTRADAY")),
            "delivery_used": _num(row.get("MARGIN_USED_AMT_DELIVERY")),
            "net_worth": _num(row.get("NET_WR")),
        })
    return {
        "available": available,
        "used": used,
        "buckets": buckets,
    }


def normalize_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": str(row.get("ORDER_ID") or row.get("order_id") or ""),
        "exch_order_id": str(row.get("EXCH_ORDER_ID") or row.get("exch_order_id") or "") or None,
        "symbol": str(row.get("SYMBOL") or row.get("symbol") or ""),
        "exchange": str(row.get("EXCHANGE") or row.get("exchange") or "") or None,
        "market": str(row.get("MARKET") or row.get("market") or "") or None,
        "side": str(row.get("TRANSAK_TYPE") or row.get("TRANS_TYPE") or row.get("side") or "") or None,
        "product": str(row.get("PRODUCT_TYPE") or row.get("product") or "") or None,
        "order_type": str(row.get("ORDER_TYPE") or row.get("order_type") or "") or None,
        "validity": str(row.get("VALIDITY") or row.get("validity") or "") or None,
        "quantity": _int(row.get("QUANTITY") or row.get("quantity")),
        "price": _num(row.get("PRICE") or row.get("price")),
        "trigger_price": _num(row.get("TRIGGER_PRICE") or row.get("trigger_price")),
        "fill_quantity": _int(row.get("FILL_QTY") or row.get("fill_qty") or row.get("fill_quantity")),
        "average_price": _num(row.get("AVG_PRICE") or row.get("avg_price") or row.get("average_price")),
        "status": str(row.get("STATUS") or row.get("TRANSACTION_TYPE") or row.get("status") or "") or None,
        "remark": str(row.get("REMARK") or row.get("remark") or "") or None,
    }


def normalize_position(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("SYMBOL") or row.get("symbol") or ""),
        "exchange": str(row.get("EXCHANGE") or row.get("exchange") or "") or None,
        "product": str(row.get("PRODUCT_TYPE") or row.get("product") or "") or None,
        "net_quantity": _int(row.get("NET_QTY") or row.get("quantity")),
        "average_price": _num(row.get("AVG_PRICE") or row.get("avg_price")),
    }


def normalize_trade(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("SYMBOL") or row.get("symbol") or ""),
        "exchange": str(row.get("EXCHANGE") or row.get("exchange") or "") or None,
        "product": str(row.get("PRODUCT_TYPE") or row.get("product") or "") or None,
        "side": str(row.get("TRANSAK_TYPE") or row.get("TRANS_TYPE") or "") or None,
        "quantity": _int(row.get("TRADE_QTY") or row.get("QUANTITY")),
        "rate": _num(row.get("TRADE_RATE") or row.get("PRICE")),
        "trade_time": str(row.get("TRADE_TIME") or row.get("trade_time") or "") or None,
    }


def normalize_holding(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("SYMBOL") or row.get("symbol") or ""),
        "exchange": str(row.get("EXCHANGE") or row.get("exchange") or "") or None,
        "product": str(row.get("PRODUCT_TYPE") or row.get("product") or "") or None,
        "quantity": _int(row.get("NET_QTY") or row.get("QUANTITY")),
        "average_price": _num(row.get("AVG_PRICE") or row.get("avg_price")),
    }


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------
class AngelOneService:
    """One request's worth of broker work for one principal.

    `client_factory` is the single seam the test suite replaces; production
    builds real `AngelOneClient`s from the decrypted session token.
    """

    def __init__(
        self,
        db: Session,
        principal: Principal | None = None,
        *,
        context: RequestContext | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.db = db
        self.principal = principal
        self.context = context or RequestContext.empty()
        self._client_factory = client_factory or default_client_factory

    # -- identity helpers ----------------------------------------------
    @property
    def _user_id(self) -> str:
        if not self.principal:
            raise BrokerInvalidRequestError("Broker calls require an authenticated caller.")
        return self.principal.user_id

    def _audit(
        self,
        action: AuditAction,
        *,
        outcome: str = "success",
        summary: str = "",
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        metadata: dict[str, Any] | None = None,
        actor_id: str | None = None,
        actor_email: str | None = None,
        tenant_id: int | None = None,
    ) -> None:
        AuditService(self.db).record(
            action,
            principal=self.principal,
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_email=actor_email,
            outcome=outcome,
            summary=summary,
            resource_type=resource_type,
            resource_id=resource_id,
            context=self.context,
            metadata=metadata,
        )

    # -- accounts ----------------------------------------------------------
    def get_account(self) -> BrokerAccount | None:
        return self.db.scalar(
            select(BrokerAccount).where(BrokerAccount.user_id == self._user_id)
        )

    def _upsert_account(
        self,
        *,
        client_code: str,
        access_token: str,
        trading_password: str,
        valid_upto: datetime | None,
        client_name: str | None = None,
    ) -> BrokerAccount:
        account = self.get_account() or BrokerAccount(user_id=self._user_id)
        account.tenant_id = (
            self.principal.tenant_id if self.principal else account.tenant_id
        )
        account.client_code = client_code
        # Enveloped at rest — a database leak yields ciphertext, not a
        # trading session.
        account.access_token_encrypted = encrypt_secret(access_token)
        account.access_token_key_version = CURRENT_KEY_VERSION
        if trading_password:
            account.trading_password_encrypted = encrypt_secret(trading_password)
            account.trading_password_key_version = CURRENT_KEY_VERSION
        else:
            account.trading_password_encrypted = None
            account.trading_password_key_version = None
        account.angel_login_at = _utcnow()
        account.angel_valid_upto = valid_upto
        account.connected = True
        if account.id is None:
            self.db.add(account)
        self.db.commit()
        self.db.refresh(account)

        # Light session snapshot for the status endpoint's read-through.
        cache.set(
            Namespace.BROKER_STATE,
            {
                "client_code": client_code,
                "valid_upto": valid_upto.isoformat() if valid_upto else None,
                "connected": True,
            },
            _SESSION_SNAPSHOT, self._user_id, ttl=300,
        )
        return account

    # -- session lifecycle -------------------------------------------------
    def request_login(self) -> tuple[str, str]:
        """Mint the single-use `state` and build the broker's login URL.

        Returns `(state, login_url)`. The state is bound to this user in the
        cache and single-use: the callback consumes it exactly once.
        """
        state = secrets.token_urlsafe(32)
        cache.set(
            Namespace.BROKER_STATE,
            {"state": state, "used": False},
            _LOGIN_STATE, self._user_id,
            ttl=settings.ANGELONE_STATE_TTL_SECONDS,
        )

        login_base = (settings.ANGELONE_LOGIN_URL or "https://risk.angelone.in/fund/auth")
        # Angel One's risk portal has been observed with both parameter
        # spellings across documentation eras; the API key is the platform's
        # SmartAPI application id (APP_ID in the console) and is an
        # identifier, not a per-user secret.
        separator = "&" if "?" in login_base else "?"
        login_url = (
            f"{login_base}{separator}APP_ID={settings.ANGELONE_API_KEY or ''}"
            f"&CLIENT_ID={settings.ANGELONE_API_KEY or ''}"
            f"&REDIRECT_URL={settings.ANGELONE_REDIRECT_URL}"
            f"&STATE={state}"
            f"&TIF=1"
        )
        self._audit(
            AuditAction.BROKER_LOGIN_STARTED,
            summary="Angel One login started",
            resource_type="broker_account",
            resource_id=self._user_id,
        )
        return state, login_url

    def complete_login(self, params: dict[str, str]) -> BrokerAccount:
        """Validate the callback, consume the state, persist the session."""
        presented_state = str(params.get("state") or "")
        stored = cache.get(Namespace.BROKER_STATE, _LOGIN_STATE, self._user_id)
        stored_state = stored.get("state") if isinstance(stored, dict) else None

        if not presented_state or stored_state is None:
            self._audit(
                AuditAction.BROKER_CALLBACK_REJECTED,
                outcome="rejected",
                summary="Callback arrived without a known state token",
                resource_type="broker_account", resource_id=self._user_id,
            )
            raise BrokerInvalidRequestError(
                "Login state is missing or unknown. Start the login again."
            )
        if not secrets.compare_digest(presented_state, stored_state):
            self._audit(
                AuditAction.BROKER_CALLBACK_REJECTED,
                outcome="rejected",
                summary="Callback state does not match any issued login",
                resource_type="broker_account", resource_id=self._user_id,
            )
            raise BrokerInvalidRequestError(
                "Login state does not match. Start the login again."
            )

        # Single-use: consume *before* anything else runs, so a slow request
        # or a concurrent duplicate can only ever complete once.
        cache.set(
            Namespace.BROKER_STATE,
            {"state": stored_state, "used": True},
            _LOGIN_STATE, self._user_id, ttl=_CONSUMED_TTL_SECONDS,
        )

        if isinstance(stored, dict) and stored.get("used"):
            self._audit(
                AuditAction.BROKER_CALLBACK_REJECTED,
                outcome="rejected",
                summary="Callback state already consumed (replay refused)",
                resource_type="broker_account", resource_id=self._user_id,
            )
            raise BrokerInvalidRequestError(
                "This login was already completed. Start a new login."
            )

        access_token = str(params.get("access_token") or "")
        client_code = str(params.get("client_code") or "").strip()
        if not access_token:
            self._audit(
                AuditAction.BROKER_CALLBACK_REJECTED,
                outcome="rejected",
                summary="Callback carried no broker access token",
                resource_type="broker_account", resource_id=self._user_id,
            )
            raise BrokerInvalidRequestError(
                "Callback is missing the broker session."
            )
        if not CLIENT_CODE_PATTERN.match(client_code):
            self._audit(
                AuditAction.BROKER_CALLBACK_REJECTED,
                outcome="rejected",
                summary="Callback carried an invalid client code",
                resource_type="broker_account", resource_id=self._user_id,
                metadata={"client_code_length": len(client_code)},
            )
            raise BrokerInvalidRequestError(
                "Callback is missing a valid 10-digit client code."
            )

        valid_upto = parse_valid_upto(params.get("valid_upto"))
        account = self._upsert_account(
            client_code=client_code,
            access_token=access_token,
            trading_password=str(params.get("trading_password") or ""),
            valid_upto=valid_upto,
            client_name=params.get("client_name"),
        )
        self._audit(
            AuditAction.BROKER_LOGIN_SUCCEEDED,
            summary=f"Angel One session established for client {client_code}",
            resource_type="broker_account", resource_id=self._user_id,
            metadata={"client_code": client_code,
                      "valid_upto": valid_upto.isoformat() if valid_upto else None},
        )
        return account

    def status(self) -> dict[str, Any]:
        """Session state for the UI.

        The database is the source of truth (one indexed lookup); the
        session snapshot in the broker-state namespace is refreshed here so
        `ensure_session`'s fast-fail pre-check never shadows a disconnect
        for longer than a single request.
        """
        account = self.get_account()

        if account is None:
            return {
                "connected": False, "client_code": None, "client_name": None,
                "valid_upto": None, "last_postback_at": None,
                "last_connected_at": None,
            }

        now = _utcnow()
        has_session = bool(
            account.connected and account.access_token_encrypted
        )
        valid_upto = _aware(account.angel_valid_upto)
        expired = (
            has_session
            and valid_upto is not None
            and valid_upto <= now
        )
        # A cached snapshot must never report "connected" when the database
        # says disconnected — the database wins, always.
        connected = has_session and not expired

        # Refresh the read-through so it cannot outlive the truth.
        cache.set(
            Namespace.BROKER_STATE,
            {
                "client_code": account.client_code,
                "valid_upto": valid_upto.isoformat() if valid_upto else None,
                "connected": connected,
            },
            _SESSION_SNAPSHOT, self._user_id, ttl=300,
        )

        return {
            "connected": connected,
            "client_code": account.client_code if has_session else None,
            "client_name": None,
            "valid_upto": valid_upto if has_session else None,
            "last_postback_at": _aware(account.last_postback_at),
            "last_connected_at": _aware(account.angel_login_at)
            if has_session else None,
        }

    def disconnect(self) -> dict[str, str]:
        """End the session: wipe the enveloped tokens, keep the identifier.

        The `client_code` stays so the UI can say which account was
        connected; the secret material is gone and the row can never mint a
        client again without a fresh login.
        """
        account = self.get_account()
        if account is None or not account.connected:
            return {"status": "no_session"}

        account.connected = False
        account.access_token_encrypted = None
        account.access_token_key_version = None
        account.trading_password_encrypted = None
        account.trading_password_key_version = None
        self.db.commit()

        # Overwrite *this user's* session snapshot rather than invalidating
        # the namespace: other users may be mid-login, and their single-use
        # state tokens would be dropped with them.
        cache.set(
            Namespace.BROKER_STATE,
            {"client_code": account.client_code, "valid_upto": None,
             "connected": False},
            _SESSION_SNAPSHOT, self._user_id, ttl=300,
        )
        self._audit(
            AuditAction.BROKER_DISCONNECTED,
            summary=f"Angel One session disconnected (client {account.client_code})",
            resource_type="broker_account", resource_id=self._user_id,
            metadata={"client_code": account.client_code},
        )
        return {"status": "disconnected"}

    def ensure_session(self) -> tuple[BrokerAccount, str]:
        """Resolve the caller's live session: account + decrypted token.

        Raises `BrokerSessionError` (401, "log in again") rather than
        failing on the wire: a call the broker is certain to refuse should
        say why before it spends the round trip.
        """
        # Fast-fail from the broker-state namespace: a disconnect wrote
        # `connected: False` here, and there is no point paying for a
        # database round trip plus a decrypt to say the same thing.
        cached = cache.get(Namespace.BROKER_STATE, _SESSION_SNAPSHOT, self._user_id)
        if isinstance(cached, dict) and cached.get("connected") is False:
            raise BrokerSessionError(
                "No active Angel One session. Connect your broker first."
            )

        account = self.get_account()
        if account is None or not account.connected or not account.access_token_encrypted:
            raise BrokerSessionError(
                "No active Angel One session. Connect your broker first."
            )
        valid_upto = _aware(account.angel_valid_upto)
        if valid_upto is not None and valid_upto <= _utcnow():
            raise BrokerSessionError(
                "Your broker session has expired. Connect your broker again."
            )
        try:
            token = decrypt_secret(account.access_token_encrypted)
        except (ValueError, KeyError) as exc:
            # Corrupt ciphertext or an unrotatable key: the only safe move
            # is a fresh login, and the row should be marked so the UI agrees.
            account.connected = False
            self.db.commit()
            raise BrokerSessionError(
                "Stored broker session is unreadable. Connect your broker again."
            ) from exc
        return account, token

    # -- data reads ---------------------------------------------------------
    def _with_client(self, call: Callable[[Any], Any]) -> Any:
        _, token = self.ensure_session()
        client = self._client_factory(token)
        try:
            return call(client)
        except BrokerSessionError:
            raise
        finally:
            client.close()

    def profile(self) -> dict[str, Any]:
        return self._with_client(lambda c: normalize_profile(c.get_profile()))

    def funds(self) -> dict[str, Any]:
        return self._with_client(lambda c: normalize_funds(c.get_funds()))

    def orders(self) -> list[dict[str, Any]]:
        return self._with_client(
            lambda c: [normalize_order(r) for r in c.get_intraday_orders()]
        )

    def positions(self) -> list[dict[str, Any]]:
        return self._with_client(
            lambda c: [normalize_position(r) for r in c.get_positions()]
        )

    def trades(self, trade_date: date | None = None) -> list[dict[str, Any]]:
        day = trade_date or datetime.now(timezone.utc).date()
        return self._with_client(
            lambda c: [normalize_trade(r) for r in c.get_trades(day.strftime("%Y%m%d"))]
        )

    def holdings(self) -> list[dict[str, Any]]:
        return self._with_client(
            lambda c: [normalize_holding(r) for r in c.get_holdings()]
        )

    # -- order lifecycle -----------------------------------------------------
    def _get_order(self, order_id: str) -> BrokerOrder:
        order = self.db.scalar(
            select(BrokerOrder).where(
                BrokerOrder.user_id == self._user_id,
                BrokerOrder.angel_order_id == str(order_id),
            )
        )
        if order is None:
            raise BrokerOrderNotFound("Order not found.")
        return order

    def place_order(self, *, symbol: str, side: str, product: str,
                    order_type: str, validity: str, quantity: int,
                    price: float | None, trigger_price: float | None,
                    ) -> BrokerOrder:
        account, _ = self.ensure_session()
        body = _place_payload(
            symbol, side, product, order_type, validity,
            quantity, price, trigger_price,
        )
        client = self._client_factory(self._decrypted_token(account))
        try:
            data = client.place_order(body)
        except BrokerSessionError:
            raise
        except BrokerError as exc:
            self._audit(
                AuditAction.BROKER_ORDER_REJECTED,
                outcome="rejected",
                summary=f"Broker rejected order for {symbol}",
                resource_type="broker_order", resource_id=self._user_id,
                metadata={"symbol": symbol, "side": side,
                          "quantity": quantity, "price": price,
                          "errorcode": exc.errorcode},
            )
            raise BrokerOrderRejectedError(
                exc.message or "Broker rejected the order.",
                errorcode=exc.errorcode,
            ) from exc
        finally:
            client.close()

        order_id = str((data or {}).get("ORDER_ID") or "").strip()
        if not order_id or order_id == "0":
            raise BrokerError(
                "Broker accepted the order but returned no order id."
            )

        order = BrokerOrder(
            user_id=account.user_id,
            tenant_id=account.tenant_id,
            angel_order_id=order_id,
            symbol=symbol,
            exchange="NSE",
            market="NSE",
            transak_type=side,
            product_type=product,
            order_type=order_type,
            validity=validity,
            quantity=quantity,
            price=float(price or 0.0),
            trigger_price=float(trigger_price or 0.0),
            status="OPEN",
        )
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)

        self._audit(
            AuditAction.BROKER_ORDER_PLACED,
            summary=f"Order placed: {side} {quantity} {symbol} "
                    f"({order_type} {product})",
            resource_type="broker_order", resource_id=order_id,
            metadata={
                "symbol": symbol, "side": side, "quantity": quantity,
                "price": price, "order_type": order_type, "product": product,
                "validity": validity, "trigger_price": trigger_price,
                "client_code": account.client_code,
            },
        )
        return order

    def modify_order(self, order_id: str, *, price: float | None = None,
                     quantity: int | None = None,
                     trigger_price: float | None = None,
                     validity: str | None = None,
                     order_type: str | None = None) -> BrokerOrder:
        account, _ = self.ensure_session()
        order = self._get_order(order_id)
        if order.status in ("COMPLETE", "CANCELLED", "REJECTED"):
            raise BrokerOrderRejectedError(
                f"Order is {order.status} and can no longer be modified."
            )
        body = _modify_payload(
            order, price=price, quantity=quantity,
            trigger_price=trigger_price, validity=validity,
            order_type=order_type,
        )
        client = self._client_factory(self._decrypted_token(account))
        try:
            client.modify_order(body)
        except BrokerSessionError:
            raise
        except BrokerError as exc:
            self._audit(
                AuditAction.BROKER_ORDER_REJECTED,
                outcome="rejected",
                summary=f"Broker rejected modification of order {order_id}",
                resource_type="broker_order", resource_id=order_id,
                metadata={"errorcode": exc.errorcode},
            )
            raise BrokerOrderRejectedError(
                exc.message or "Broker rejected the modification.",
                errorcode=exc.errorcode,
            ) from exc
        finally:
            client.close()

        changes = {}
        if price is not None:
            order.price = float(price)
            changes["price"] = price
        if quantity is not None:
            order.quantity = int(quantity)
            changes["quantity"] = quantity
        if trigger_price is not None:
            order.trigger_price = float(trigger_price)
            changes["trigger_price"] = trigger_price
        if validity is not None:
            order.validity = validity
            changes["validity"] = validity
        if order_type is not None:
            order.order_type = order_type
            changes["order_type"] = order_type
        self.db.commit()
        self.db.refresh(order)

        self._audit(
            AuditAction.BROKER_ORDER_MODIFIED,
            summary=f"Order modified: {order.symbol} {changes}",
            resource_type="broker_order", resource_id=order_id,
            metadata={"symbol": order.symbol, "changes": changes,
                      "client_code": account.client_code},
        )
        return order

    def cancel_order(self, order_id: str) -> BrokerOrder:
        account, _ = self.ensure_session()
        order = self._get_order(order_id)
        if order.status in ("COMPLETE", "CANCELLED", "REJECTED"):
            raise BrokerOrderRejectedError(
                f"Order is {order.status} and cannot be cancelled."
            )
        client = self._client_factory(self._decrypted_token(account))
        try:
            client.cancel_order(order.angel_order_id, order.exchange)
        except BrokerSessionError:
            raise
        except BrokerError as exc:
            self._audit(
                AuditAction.BROKER_ORDER_REJECTED,
                outcome="rejected",
                summary=f"Broker rejected cancellation of order {order_id}",
                resource_type="broker_order", resource_id=order_id,
                metadata={"errorcode": exc.errorcode},
            )
            raise BrokerOrderRejectedError(
                exc.message or "Broker rejected the cancellation.",
                errorcode=exc.errorcode,
            ) from exc
        finally:
            client.close()

        order.status = "CANCELLED"
        self.db.commit()
        self.db.refresh(order)

        self._audit(
            AuditAction.BROKER_ORDER_CANCELLED,
            summary=f"Order cancelled: {order.symbol} ({order_id})",
            resource_type="broker_order", resource_id=order_id,
            metadata={"symbol": order.symbol, "client_code": account.client_code},
        )
        return order

    @staticmethod
    def _decrypted_token(account: BrokerAccount) -> str:
        try:
            return decrypt_secret(account.access_token_encrypted or b"")
        except (ValueError, KeyError) as exc:
            raise BrokerSessionError(
                "Stored broker session is unreadable. Connect your broker again."
            ) from exc

    # -- postback ------------------------------------------------------------
    def apply_postback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one broker postback.

        Postbacks carry no platform identity — Angel One simply POSTs to the
        registered URL. The only things that make one trustworthy are (a) it
        references an order *this platform created* and (b) its client code
        matches the account that owns that order. Anything else is refused,
        audited, and answered with a status that does not confirm anything.
        """
        order_id = str(payload.get("ORDER_ID") or "").strip()
        if not order_id:
            self._audit(
                AuditAction.BROKER_POSTBACK_REJECTED,
                outcome="rejected",
                summary="Postback carried no order id",
                resource_type="broker_postback",
            )
            # Malformed (400) rather than "not found" (404): there is no
            # order id to look up, so there is nothing to be not found.
            raise BrokerInvalidRequestError("Postback has no order id.")

        order = self.db.scalar(
            select(BrokerOrder).where(BrokerOrder.angel_order_id == order_id)
        )
        if order is None:
            # An order id the platform never created. Do not confirm its
            # existence either way — 404 with a generic body.
            self._audit(
                AuditAction.BROKER_POSTBACK_REJECTED,
                outcome="rejected",
                summary="Postback for an order not created by this platform",
                resource_type="broker_postback", resource_id=order_id,
                metadata={"order_id": order_id},
            )
            raise BrokerPostbackError(
                "Postback references an unknown order."
            )

        account = self.db.scalar(
            select(BrokerAccount).where(BrokerAccount.user_id == order.user_id)
        )
        presented_client = str(payload.get("CLIENT_CODE") or "").strip()
        if presented_client and account is not None:
            if presented_client != account.client_code:
                self._audit(
                    AuditAction.BROKER_POSTBACK_REJECTED,
                    outcome="rejected",
                    summary="Postback client code does not match the account",
                    resource_type="broker_postback", resource_id=order_id,
                    metadata={"order_id": order_id},
                )
                raise BrokerPostbackError(
                    "Postback does not match the account that owns this order."
                )

        presented_symbol = str(payload.get("SYMBOL") or "").strip().upper()
        if presented_symbol and presented_symbol != order.symbol.upper():
            self._audit(
                AuditAction.BROKER_POSTBACK_REJECTED,
                outcome="rejected",
                summary="Postback symbol does not match the recorded order",
                resource_type="broker_postback", resource_id=order_id,
                metadata={"order_id": order_id},
            )
            raise BrokerPostbackError(
                "Postback does not match the recorded order."
            )

        new_status = str(payload.get("ORDER_STATUS") or payload.get("STATUS") or "").strip().upper()
        fill_qty = _int(payload.get("FILL_QTY"))
        avg_price = _num(payload.get("AVG_PRICE"))
        if new_status:
            order.status = new_status
        if fill_qty is not None:
            order.fill_quantity = fill_qty
        if avg_price is not None:
            order.average_price = avg_price
        exch_order_id = str(payload.get("EXCH_ORDER_ID") or "").strip()
        if exch_order_id:
            order.exchange_order_id = exch_order_id
        remark = str(payload.get("REMARK") or "").strip()
        if remark:
            order.remark = remark[:200]
        order.last_postback_at = _utcnow()
        if account is not None:
            account.last_postback_at = _utcnow()
        self.db.commit()

        self._audit(
            AuditAction.BROKER_POSTBACK_RECEIVED,
            summary=f"Postback applied: {order.symbol} → {order.status}",
            resource_type="broker_postback", resource_id=order_id,
            actor_id=order.user_id,
            tenant_id=order.tenant_id,
            metadata={
                "symbol": order.symbol, "status": order.status,
                "fill_quantity": order.fill_quantity,
                "average_price": order.average_price,
                "order_id": order_id,
            },
        )
        return {
            "accepted": True,
            "order_id": order_id,
            "status": order.status,
        }
