"""A minimal Angel One SmartAPI REST client.

Speaks the official SmartAPI HTTP contract (base `https://api.angelone.com/pal`)
over `httpx` with exactly one job: turn a typed request into the broker's
`data` payload, or raise one of the exceptions in `exceptions.py`. All
product logic — session lifecycle, persistence, audit — lives in
`angelone_service.py`.

**Secrets never reach the log.** The access token is handled in exactly one
place — the `Authorization` header — and `_request` logs only the method,
path, HTTP status and the broker's own error code/message. It never logs
headers or bodies. The broker's `message` field is a human-facing reason
("Order rejected by exchange") and is safe; the error code is an opaque
string.

The client is synchronous because FastAPI serves sync routes from a
threadpool — the same place the rest of the platform already blocks on I/O —
and because unit tests can then exercise it with `httpx.MockTransport`
without an event loop.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.broker.exceptions import (
    BrokerError, BrokerInvalidRequestError, BrokerNetworkError,
    BrokerRateLimitError, BrokerSessionError,
)
from app.services.platform.observability import get_logger

log = get_logger("ierp.broker.angelone")

#: The application code every SmartAPI order call carries.
APP_CODE = "SMARTAPI"

#: Segment ids per the SmartAPI contract.
EXCH_ID = {"NSE": 91, "BSE": 92}
EXCHANGE_NAMES = {91: "NSE", 92: "BSE"}

DEFAULT_BASE_URL = "https://api.angelone.com/pal"
DEFAULT_LOGIN_URL = "https://risk.angelone.in/fund/auth"

#: A 10-digit trading client code is the broker's identifier for an account.
#: It is an identifier, not a secret — but it is *validated* here so a
#: caller can never send a free-text or another person's-shaped string into
#: the broker's identifier fields.
CLIENT_CODE_PATTERN = re.compile(r"^\d{10}$")

#: Envelope error codes that mean "success". SmartAPI is `errorcode: "0"`;
#: the wider set is tolerated because the broker has returned `""` and
#: `"000000"` in different eras, and treating a success code as a failure
#: disconnects live sessions for no reason.
_SUCCESS_CODES = frozenset({"0", "000000", ""})


def b64(value: str) -> str:
    """SmartAPI base64-encodes the `PASSWORD`, `TOTP` and `OTP` fields.

    `MA==` (b64 of `"0"`) is the broker's placeholder for a missing TOTP on
    a 1FA account.
    """
    return base64.b64encode(value.encode()).decode()


def parse_valid_upto(value: Any) -> datetime | None:
    """`20260826173000` → aware UTC datetime.

    Deliberately lenient and *safe-direction*: `None` stays `None`, and an
    unparseable value returns `None` too, which the service treats as
    "session expired". A session we cannot prove is live is not live.
    """
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _as_list(data: Any) -> list[dict]:
    """Broker list endpoints answer with `data` as a list; older responses
    wrapped it in a dict. Normalise without crashing either way.

    `None` and an empty dict both mean "no rows" — the broker's empty
    answer. An empty dict is *not* one row with no fields: treating it as
    a row would show traders a phantom position that does not exist.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "rows", "orders", "positions", "holdings", "trades"):
            if isinstance(data.get(key), list):
                return [row for row in data[key] if isinstance(row, dict)]
        return [data] if data else []
    return []


class AngelOneClient:
    """One SmartAPI conversation for one caller's session.

    `http_client` is injectable so tests can run the real serialization and
    envelope logic against `httpx.MockTransport` — no network, no event
    loop, and the token path is observable.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.access_token = access_token
        self.api_key = api_key or settings.ANGELONE_API_KEY
        self.base_url = (
            base_url or settings.ANGELONE_API_BASE or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout or settings.ANGELONE_TIMEOUT_SECONDS
        self._http = http_client or httpx.Client(timeout=self.timeout)
        self._owns_http = http_client is None

    # -- lifecycle ----------------------------------------------------
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # -- login (REST contract) -----------------------------------------
    def rest_login(
        self, client_code: str, password: str, totp: str = "",
    ) -> dict:
        """Direct REST login: client code + password + TOTP.

        The product's primary flow is the browser authorization flow (the
        user signs in on the broker's own page and we receive the session in
        the callback). This method exists because it is part of the official
        SmartAPI contract and it is the only way a headless deployment can
        establish a session.

        Never logs `password` or `totp` — the request body is not logged at
        all, by construction.
        """
        if not CLIENT_CODE_PATTERN.match(client_code or ""):
            raise BrokerInvalidRequestError(
                "client_code must be the 10-digit Angel One client code."
            )
        body = {
            "CLIENT_ID": client_code,
            "PASSWORD": b64(password),
            "TOTP": b64(totp) if totp else b64("0"),
        }
        data = self._request(
            "POST", "/api/v2/angel/one/authorize/login",
            json_body=body, auth_required=False,
        )
        return data if isinstance(data, dict) else {}

    def rest_validate(
        self, client_code: str, request_id: str, otp: str,
    ) -> dict:
        """Complete a 2FA login: the broker sent an OTP, the user enters it."""
        if not CLIENT_CODE_PATTERN.match(client_code or ""):
            raise BrokerInvalidRequestError(
                "client_code must be the 10-digit Angel One client code."
            )
        body = {
            "CLIENT_ID": client_code,
            "REQUEST_ID": request_id,
            "OTP": b64(otp),
        }
        data = self._request(
            "POST", "/api/v2/angel/one/authorize/login/validate",
            json_body=body, auth_required=False,
        )
        return data if isinstance(data, dict) else {}

    # -- authenticated data calls ---------------------------------------
    def get_profile(self) -> dict:
        data = self._request("GET", "/api/v2/angel/one/authorize/login/profile")
        return data if isinstance(data, dict) else {}

    def get_funds(self) -> list[dict]:
        return _as_list(self._request("GET", "/api/v2/angel/one/funds"))

    def get_intraday_orders(self) -> list[dict]:
        return _as_list(self._request("GET", "/api/v2/angel/one/orders/intraday"))

    def get_positions(self) -> list[dict]:
        return _as_list(self._request("GET", "/api/v2/angel/one/orders/positions"))

    def get_trades(self, trade_date: str) -> list[dict]:
        """`trade_date` is `YYYYMMDD`; the broker serves one day at a time."""
        return _as_list(
            self._request(
                "GET", "/api/v2/angel/one/orders/trades",
                params={"TRADE_DATE": trade_date},
            )
        )

    def get_holdings(self) -> list[dict]:
        return _as_list(self._request("GET", "/api/v2/angel/one/holdings"))

    # -- order operations ------------------------------------------------
    def place_order(self, order: dict) -> dict:
        data = self._request(
            "POST", "/api/v2/angel/one/orders/place", json_body=order,
        )
        return data if isinstance(data, dict) else {}

    def modify_order(self, order: dict) -> dict:
        data = self._request(
            "POST", "/api/v2/angel/one/orders/modify", json_body=order,
        )
        return data if isinstance(data, dict) else {}

    def cancel_order(self, order_id: str, exchange: str) -> dict:
        data = self._request(
            "POST", "/api/v2/angel/one/orders/cancel",
            json_body={
                "ORDER_ID": str(order_id),
                "EXCHANGE": exchange,
                "APP_CODE": APP_CODE,
            },
        )
        return data if isinstance(data, dict) else {}

    # -- transport ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        auth_required: bool = True,
    ) -> Any:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_required:
            if not self.access_token:
                raise BrokerSessionError(
                    "No live broker session token for this call."
                )
            headers["Authorization"] = f"Bearer {self.access_token}"

        url = self.base_url + path
        try:
            response = self._http.request(
                method, url, headers=headers, params=params, json=json_body,
            )
        except httpx.TimeoutException as exc:
            # No request details in the log — the path is safe, the headers
            # (which carry the token) are not.
            log.warning("broker request timed out",
                        method=method, path=path, timeout=self.timeout)
            raise BrokerNetworkError(
                f"Broker request timed out after {self.timeout:.0f}s."
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("broker request failed",
                        method=method, path=path,
                        error=type(exc).__name__)
            raise BrokerNetworkError(
                f"Could not reach the broker ({type(exc).__name__})."
            ) from exc

        try:
            payload = response.json()
        except ValueError:
            log.warning("broker returned a non-JSON body",
                        method=method, path=path,
                        status=response.status_code)
            raise BrokerNetworkError(
                "Broker returned a non-JSON response."
            )

        if not isinstance(payload, dict):
            raise BrokerNetworkError(
                "Broker returned a malformed response envelope."
            )

        error = payload.get("error", False)
        errorcode = str(payload.get("errorcode", "0"))
        message = str(payload.get("message") or "")

        if response.status_code == 429 or errorcode == "429":
            log.warning("broker rate limited the call",
                        method=method, path=path, errorcode=errorcode)
            raise BrokerRateLimitError(
                message or "Broker rate limit exceeded. Wait and retry.",
                errorcode=errorcode,
            )

        if error or errorcode not in _SUCCESS_CODES:
            # The broker's own message and code are safe to log (they are
            # what the trader sees); the request body and headers are not.
            log.warning("broker call failed",
                        method=method, path=path,
                        status=response.status_code,
                        errorcode=errorcode, message=message[:200])
            if response.status_code in (401, 403) or any(
                hint in message.lower()
                for hint in ("token", "session", "login", "expired", "unauthoris", "unauthorized")
            ):
                raise BrokerSessionError(
                    message or "Broker session is invalid or has expired.",
                    errorcode=errorcode,
                )
            raise BrokerError(
                message or f"Broker error {errorcode}", errorcode=errorcode,
            )

        return payload.get("data")
