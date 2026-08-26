"""The broker error vocabulary.

One namespaced hierarchy so the API layer can map *any* broker failure to
an HTTP status in a single `except` clause, and so the failure a trader is
looking at cannot be silently confused with another:

* "your session lapsed" (401, log in again) is not "the broker is down"
  (502, try again);
* "the exchange rejected the order" (422, read the remark) is not "we are
  being rate limited" (429, wait).

These are deliberately *not* subclasses of `app.domain.ai.types`
exceptions: AI-provider and broker rate limiting have different meanings
and different remedies, and a shared base class is how one handler starts
swallowing the other's errors.

`status_code` is the HTTP status the API layer surfaces. It lives with the
exception, not in the router, so a new broker error cannot be invented with
an arbitrary status code at the call site.
"""
from __future__ import annotations

from typing import Any


class BrokerError(Exception):
    """Base of every broker failure."""

    status_code = 502  # generic broker problem → bad gateway

    def __init__(
        self,
        message: str,
        *,
        errorcode: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        #: The broker's own opaque error code, when it gave one.
        self.errorcode = errorcode
        self.details = details or {}
        super().__init__(message)

    def __str__(self) -> str:
        if self.errorcode:
            return f"{self.message} (broker code {self.errorcode})"
        return self.message


class BrokerAuthError(BrokerError):
    """Credentials the broker refused: wrong password, wrong TOTP, or a
    client code the broker does not recognise. The user should re-enter
    their broker credentials, not retry blindly."""

    status_code = 401


class BrokerSessionError(BrokerError):
    """The caller has no live broker session to operate on: never logged
    in, logged out, or the broker session has expired. The remedy is always
    "log in to your broker", never "retry"."""

    status_code = 401


class BrokerOrderNotFound(BrokerError):
    """A reference to an order this platform does not hold. 404 rather than
    403 on purpose: confirming that *some* order exists under an id is
    itself a disclosure when the order belongs to someone else."""

    status_code = 404


class BrokerNetworkError(BrokerError):
    """The broker could not be reached, timed out, or answered with
    something that is not the expected envelope. Transient: worth retrying."""

    status_code = 502


class BrokerRateLimitError(BrokerError):
    """The broker told us to slow down (HTTP 429). The caller should wait —
    the `Retry-After` guidance comes from the broker, not from us."""

    status_code = 429


class BrokerInvalidRequestError(BrokerError):
    """Our own request was malformed: a callback without its state token, a
    missing required field. Not a broker problem, not worth retrying."""

    status_code = 400


class BrokerOrderRejectedError(BrokerError):
    """The broker or the exchange refused an order: insufficient funds,
    symbol not tradable, price band, circuit breaker. The `errorcode` and
    message carry the broker's reason; the order was *not* placed."""

    status_code = 422


class BrokerPostbackError(BrokerError):
    """A postback the platform will not apply: no order id, an order id the
    platform never created, or a client code that does not match the
    account that owns the order. Refusing loudly is the point — a postback
    that silently succeeds is a forged fill."""

    status_code = 404
