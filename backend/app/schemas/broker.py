"""Typed contracts for the Angel One broker API.

Conventions, stated once:

* **Prices are INR, unrounded.** The broker returns them as strings
  (sometimes Indian-grouped); the service coerces before these models see
  them, and the client formats.
* **Order side is the broker's own vocabulary** — `B`/`S` — not an invented
  synonym. The trader knows what they sent.
* **Strict order validation lives here.** A `MARKET` order with a price, or
  a `SL-M` without a trigger, is refused at the API boundary with a 422
  long before any broker call is made. The broker would also refuse it, but
  spending a real order attempt on a validation the platform can answer
  itself is how pointless rejections accumulate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Side = Literal["B", "S"]
ProductType = Literal["CNC", "MIS", "MARGIN"]
OrderType = Literal["LIMIT", "MARKET", "SL-M", "SL-L"]
Validity = Literal["DAY", "IOC"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class BrokerStatusOut(BaseModel):
    connected: bool
    client_code: str | None = None
    client_name: str | None = None
    valid_upto: datetime | None = None
    last_postback_at: datetime | None = None
    last_connected_at: datetime | None = None


class BrokerDisconnectOut(BaseModel):
    #: "disconnected" or "no_session" (already disconnected — idempotent).
    status: str


# ---------------------------------------------------------------------------
# Broker data reads (normalised)
# ---------------------------------------------------------------------------
class BrokerProfileOut(BaseModel):
    client_code: str = ""
    client_name: str = ""
    email: str = ""
    exchanges: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class BrokerFundsOut(BaseModel):
    available: float = 0.0
    used: float = 0.0
    buckets: list[dict[str, Any]] = Field(default_factory=list)


class BrokerOrderOut(BaseModel):
    order_id: str
    exch_order_id: str | None = None
    symbol: str
    exchange: str | None = None
    market: str | None = None
    side: str | None = None
    product: str | None = None
    order_type: str | None = None
    validity: str | None = None
    quantity: int | None = None
    price: float | None = None
    trigger_price: float | None = None
    fill_quantity: int | None = None
    average_price: float | None = None
    status: str | None = None
    remark: str | None = None


class BrokerPositionOut(BaseModel):
    symbol: str
    exchange: str | None = None
    product: str | None = None
    net_quantity: int | None = None
    average_price: float | None = None


class BrokerTradeOut(BaseModel):
    symbol: str
    exchange: str | None = None
    product: str | None = None
    side: str | None = None
    quantity: int | None = None
    rate: float | None = None
    trade_time: str | None = None


class BrokerHoldingOut(BaseModel):
    symbol: str
    exchange: str | None = None
    product: str | None = None
    quantity: int | None = None
    average_price: float | None = None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
class BrokerOrderCreate(BaseModel):
    """A new order. Strict on purpose — see the module docstring."""

    symbol: str = Field(min_length=1, max_length=20)
    side: Side
    product: ProductType = "CNC"
    order_type: OrderType = "LIMIT"
    validity: Validity = "DAY"
    quantity: int = Field(gt=0, le=1_000_000)
    price: float | None = Field(default=None, gt=0, le=10_000_000)
    trigger_price: float | None = Field(default=None, ge=0, le=10_000_000)

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("symbol must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _check_combo(self) -> "BrokerOrderCreate":
        if self.order_type == "MARKET" and self.price is not None:
            raise ValueError("MARKET orders take no price.")
        if self.order_type in ("LIMIT", "SL-M", "SL-L") and self.price is None:
            raise ValueError(f"{self.order_type} orders require a price.")
        if self.order_type in ("SL-M", "SL-L") and not self.trigger_price:
            raise ValueError(
                f"{self.order_type} orders require a trigger_price."
            )
        return self


class BrokerOrderModify(BaseModel):
    """Modify an order the platform placed. At least one field required;
    the service re-sends the whole order object, as SmartAPI demands."""

    order_id: str = Field(min_length=1, max_length=40)
    price: float | None = Field(default=None, gt=0, le=10_000_000)
    quantity: int | None = Field(default=None, gt=0, le=1_000_000)
    trigger_price: float | None = Field(default=None, ge=0, le=10_000_000)
    validity: Validity | None = None
    order_type: OrderType | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "BrokerOrderModify":
        if (
            self.price is None
            and self.quantity is None
            and self.trigger_price is None
            and self.validity is None
            and self.order_type is None
        ):
            raise ValueError("Provide at least one field to modify.")
        if self.order_type == "MARKET" and self.price is not None:
            raise ValueError("MARKET orders take no price.")
        return self


class BrokerOrderAckOut(BaseModel):
    """The result of place / modify / cancel."""

    order_id: str
    status: str
    symbol: str
    exchange: str
    quantity: int
    price: float
    message: str | None = None


class BrokerPostbackOut(BaseModel):
    accepted: bool
    order_id: str
    status: str | None = None
