"""Angel One SmartAPI broker endpoints.

    GET    /broker/angelone/login              start the broker login (302 to the broker)
    GET    /broker/angelone/callback           the broker's redirect home (state-validated)
    GET    /broker/angelone/status             the caller's session state
    POST   /broker/angelone/disconnect         end the session (tokens wiped)
    GET    /broker/angelone/profile            live broker profile
    GET    /broker/angelone/funds              live margin/funds
    GET    /broker/angelone/orders             today's order book
    GET    /broker/angelone/positions          live positions
    GET    /broker/angelone/trades             executed trades (?trade_date=YYYYMMDD)
    GET    /broker/angelone/holdings           holdings
    POST   /broker/angelone/orders             place an order            (broker:trade)
    POST   /broker/angelone/orders/modify      modify a platform order   (broker:trade)
    DELETE /broker/angelone/orders/{order_id}  cancel a platform order   (broker:trade)
    POST   /broker/angelone/postback           broker order-status postback (no auth)

Route order: the literal `/orders/modify` is declared before the parameterised
`/orders/{order_id}` — ROUTE-001, the same rule that keeps `/filings/dashboard`
out of the per-ticker filing chain. A `GET` on `/orders/modify` answers 405
by construction: modification is a write and there is exactly one method for
it.

The postback is the only unauthenticated endpoint: Angel One POSTs to it
directly, with no platform session. It is therefore the strictest endpoint
here — it updates only an order id this platform created, only when the
broker's client code matches the owning account, and it refuses and audits
everything else.
"""
from __future__ import annotations

import json as _json
from datetime import date

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Request, status,
)
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    Principal, _client_ip, get_current_user, require,
)
from app.db.base import get_db
from app.domain.platform.identity import Permission
from app.domain.platform.limits import RateScope
from app.schemas.broker import (
    BrokerDisconnectOut, BrokerFundsOut, BrokerHoldingOut, BrokerOrderAckOut,
    BrokerOrderCreate, BrokerOrderModify, BrokerOrderOut, BrokerPositionOut,
    BrokerProfileOut, BrokerPostbackOut, BrokerStatusOut, BrokerTradeOut,
)
from app.services.broker.angelone_service import AngelOneService
from app.services.broker.exceptions import (
    BrokerError, BrokerRateLimitError,
)
from app.services.platform.audit_service import RequestContext
from app.services.platform import rate_limit

router = APIRouter(prefix="/broker/angelone", tags=["broker-angelone"])


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def _context(request: Request) -> RequestContext:
    return RequestContext(
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


def _service(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_user),
) -> AngelOneService:
    return AngelOneService(db, principal, context=_context(request))


def _broker_http(exc: BrokerError) -> HTTPException:
    headers = None
    if isinstance(exc, BrokerRateLimitError):
        headers = {"Retry-After": "5"}
    return HTTPException(exc.status_code, detail=exc.message, headers=headers)


def _enforce(rule: str, request: Request, principal: Principal | None) -> None:
    """The platform's rate limiting, broker-specific rules.

    Postbacks are keyed on IP (they carry no platform identity); everything
    else is keyed on the user.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return
    if rule == "broker.postback" or principal is None:
        decision = rate_limit.check(rule, _client_ip(request), scope=RateScope.IP)
    else:
        decision = rate_limit.check(
            rule, principal.user_id, scope=RateScope.USER,
        )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many broker requests. Please wait and try again.",
            headers=decision.headers(),
        )


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
@router.get(
    "/login",
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Start the Angel One login",
)
def broker_login(
    request: Request,
    service: AngelOneService = Depends(_service),
):
    """Mint the single-use state token and send the browser to the broker's
    own sign-in page. The browser returns to `/callback` carrying the
    broker's session."""
    _enforce("broker.login", request, service.principal)
    _state, login_url = service.request_login()
    return RedirectResponse(login_url, status_code=status.HTTP_302_FOUND)


@router.get(
    "/callback",
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Broker authorization callback",
)
def broker_callback(
    request: Request,
    service: AngelOneService = Depends(_service),
):
    """The broker's redirect home. Validates and consumes the state token,
    persists the session (enveloped), then redirects to the *configured*
    frontend URL — never a caller-supplied one."""
    try:
        service.complete_login(dict(request.query_params))
    except BrokerError as exc:
        raise _broker_http(exc) from exc
    return RedirectResponse(
        settings.ANGELONE_FRONTEND_REDIRECT, status_code=status.HTTP_302_FOUND,
    )


@router.get(
    "/status",
    response_model=BrokerStatusOut,
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Session state",
)
def broker_status(service: AngelOneService = Depends(_service)) -> BrokerStatusOut:
    return BrokerStatusOut(**service.status())


@router.post(
    "/disconnect",
    response_model=BrokerDisconnectOut,
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="End the broker session",
)
def broker_disconnect(
    service: AngelOneService = Depends(_service),
) -> BrokerDisconnectOut:
    return BrokerDisconnectOut(**service.disconnect())


# ---------------------------------------------------------------------------
# Data reads
# ---------------------------------------------------------------------------
@router.get(
    "/profile",
    response_model=BrokerProfileOut,
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Live broker profile",
)
def broker_profile(service: AngelOneService = Depends(_service)) -> BrokerProfileOut:
    _enforce("broker.read", None, service.principal)
    try:
        return BrokerProfileOut(**service.profile())
    except BrokerError as exc:
        raise _broker_http(exc) from exc


@router.get(
    "/funds",
    response_model=BrokerFundsOut,
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Live funds and margin",
)
def broker_funds(service: AngelOneService = Depends(_service)) -> BrokerFundsOut:
    _enforce("broker.read", None, service.principal)
    try:
        return BrokerFundsOut(**service.funds())
    except BrokerError as exc:
        raise _broker_http(exc) from exc


@router.get(
    "/orders",
    response_model=list[BrokerOrderOut],
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Today's order book",
)
def broker_orders(service: AngelOneService = Depends(_service)) -> list[BrokerOrderOut]:
    _enforce("broker.read", None, service.principal)
    try:
        return [BrokerOrderOut(**row) for row in service.orders()]
    except BrokerError as exc:
        raise _broker_http(exc) from exc


@router.get(
    "/positions",
    response_model=list[BrokerPositionOut],
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Live positions",
)
def broker_positions(
    service: AngelOneService = Depends(_service),
) -> list[BrokerPositionOut]:
    _enforce("broker.read", None, service.principal)
    try:
        return [BrokerPositionOut(**row) for row in service.positions()]
    except BrokerError as exc:
        raise _broker_http(exc) from exc


@router.get(
    "/trades",
    response_model=list[BrokerTradeOut],
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Executed trades for one day",
)
def broker_trades(
    trade_date: date | None = Query(default=None, description="YYYY-MM-DD; defaults to today"),
    service: AngelOneService = Depends(_service),
) -> list[BrokerTradeOut]:
    _enforce("broker.read", None, service.principal)
    try:
        return [BrokerTradeOut(**row) for row in service.trades(trade_date)]
    except BrokerError as exc:
        raise _broker_http(exc) from exc


@router.get(
    "/holdings",
    response_model=list[BrokerHoldingOut],
    dependencies=[Depends(require(Permission.BROKER_READ))],
    summary="Holdings",
)
def broker_holdings(
    service: AngelOneService = Depends(_service),
) -> list[BrokerHoldingOut]:
    _enforce("broker.read", None, service.principal)
    try:
        return [BrokerHoldingOut(**row) for row in service.holdings()]
    except BrokerError as exc:
        raise _broker_http(exc) from exc


# ---------------------------------------------------------------------------
# Order operations (broker:trade)
# ---------------------------------------------------------------------------
@router.post(
    "/orders",
    response_model=BrokerOrderAckOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.BROKER_TRADE))],
    summary="Place an order",
)
def broker_place_order(
    payload: BrokerOrderCreate,
    request: Request,
    service: AngelOneService = Depends(_service),
) -> BrokerOrderAckOut:
    _enforce("broker.trade", request, service.principal)
    try:
        order = service.place_order(
            symbol=payload.symbol, side=payload.side, product=payload.product,
            order_type=payload.order_type, validity=payload.validity,
            quantity=payload.quantity, price=payload.price,
            trigger_price=payload.trigger_price,
        )
    except BrokerError as exc:
        raise _broker_http(exc) from exc
    return BrokerOrderAckOut(
        order_id=order.angel_order_id or "", status=order.status,
        symbol=order.symbol, exchange=order.exchange,
        quantity=order.quantity, price=order.price,
        message="Order accepted by the broker.",
    )


# Literal path BEFORE `/orders/{order_id}` — ROUTE-001.
@router.post(
    "/orders/modify",
    response_model=BrokerOrderAckOut,
    dependencies=[Depends(require(Permission.BROKER_TRADE))],
    summary="Modify a platform-created order",
)
def broker_modify_order(
    payload: BrokerOrderModify,
    request: Request,
    service: AngelOneService = Depends(_service),
) -> BrokerOrderAckOut:
    _enforce("broker.trade", request, service.principal)
    try:
        order = service.modify_order(
            payload.order_id,
            price=payload.price, quantity=payload.quantity,
            trigger_price=payload.trigger_price, validity=payload.validity,
            order_type=payload.order_type,
        )
    except BrokerError as exc:
        raise _broker_http(exc) from exc
    return BrokerOrderAckOut(
        order_id=order.angel_order_id or "", status=order.status,
        symbol=order.symbol, exchange=order.exchange,
        quantity=order.quantity, price=order.price,
        message="Order modified.",
    )


@router.delete(
    "/orders/{order_id}",
    response_model=BrokerOrderAckOut,
    dependencies=[Depends(require(Permission.BROKER_TRADE))],
    summary="Cancel a platform-created order",
)
def broker_cancel_order(
    order_id: str,
    request: Request,
    service: AngelOneService = Depends(_service),
) -> BrokerOrderAckOut:
    _enforce("broker.trade", request, service.principal)
    try:
        order = service.cancel_order(order_id)
    except BrokerError as exc:
        raise _broker_http(exc) from exc
    return BrokerOrderAckOut(
        order_id=order.angel_order_id or "", status=order.status,
        symbol=order.symbol, exchange=order.exchange,
        quantity=order.quantity, price=order.price,
        message="Order cancellation sent to the broker.",
    )


# ---------------------------------------------------------------------------
# Postback (broker-to-platform, no platform identity)
# ---------------------------------------------------------------------------
@router.post(
    "/postback",
    response_model=BrokerPostbackOut,
    summary="Broker order-status postback",
)
async def broker_postback(
    request: Request,
    db: Session = Depends(get_db),
):
    """Angel One POSTs order-status changes here. No platform session
    travels with it, so it is trusted only insofar as it references an order
    this platform created for the matching account. See the module
    docstring; everything refused is audited as `broker.postback.rejected`.

    Async because reading the body is async in this Starlette
    (`Request.body()` is a coroutine); the handler's database work is one
    short transaction and runs at postback rates (bounded by the
    `broker.postback` limit), not user-facing read rates.
    """
    _enforce("broker.postback", request, None)
    service = AngelOneService(db, None, context=_context(request))
    try:
        payload = _json.loads(await request.body())
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Postback body must be JSON.",
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Postback body must be a JSON object.",
        )
    try:
        return BrokerPostbackOut(**service.apply_postback(payload))
    except BrokerError as exc:
        raise _broker_http(exc) from exc
