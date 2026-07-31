"""Market-data provider interface and shared plumbing.

Every provider implements `BaseMarketProvider`, so the router can try them in
priority order without knowing anything about the one it is calling. The
retry policy, timeout policy, throttling and circuit breaker live here rather
than in each provider: they are the same problem everywhere, and three
independent implementations of exponential backoff is three chances to get it
subtly wrong.

Two rules the whole layer follows:

* **Authentication failures are never retried.** A rejected key will still be
  rejected in two seconds, and burning the retry budget on it delays the
  fallback that would have worked.
* **Absent is not zero.** Providers differ in how they signal a missing
  figure — null, 0, "", omitted — and a zero market capitalisation reaching a
  valuation model is worse than an honest gap.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ===========================================================================
# Errors
# ===========================================================================
class ProviderError(Exception):
    """Any failure that should cause the router to try the next provider."""


class ProviderNotConfigured(ProviderError):
    """No credentials. Distinct so the report can say so precisely."""


class ProviderAuthError(ProviderError):
    """Credentials rejected. Never retried, and fatal for the provider."""


class ProviderRateLimited(ProviderError):
    """Quota or throttle. Retried once or twice, then the circuit opens."""


class SymbolNotFound(ProviderError):
    """The provider does not cover this symbol.

    Distinct from an outage: falling back is right, but the failure is about
    coverage and should be reported that way rather than as a fault.
    """


# ===========================================================================
# Policy
# ===========================================================================
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Configurable, per the brief, with defaults tuned to free tiers."""

    attempts: int = 3
    backoff_base: float = 1.6
    timeout_seconds: float = 15.0
    #: Minimum gap between calls to one provider.
    min_interval: float = 0.25
    #: Consecutive rate limits before the provider is skipped outright.
    circuit_threshold: int = 8

    def delay(self, attempt: int) -> float:
        return self.backoff_base ** (attempt + 1)


# ===========================================================================
# Normalised shapes
# ===========================================================================
@dataclass(slots=True)
class Quote:
    price: float | None = None
    change: float | None = None
    percent_change: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    previous_close: float | None = None
    volume: float | None = None


@dataclass(slots=True)
class CompanyProfile:
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None
    industry: str | None = None
    sector: str | None = None
    description: str | None = None
    website: str | None = None
    #: ₹ crore, the platform's unit throughout.
    market_cap: float | None = None
    shares_outstanding: float | None = None


@dataclass(slots=True)
class MarketSnapshot:
    """What every provider returns, whatever its native shape."""

    ticker: str
    source: str = "Unavailable"
    profile: CompanyProfile = field(default_factory=CompanyProfile)
    quote: Quote = field(default_factory=Quote)
    key_metrics: dict[str, Any] = field(default_factory=dict)
    ratios: dict[str, Any] = field(default_factory=dict)
    income_statement: list[dict[str, Any]] = field(default_factory=list)
    balance_sheet: list[dict[str, Any]] = field(default_factory=list)
    cash_flow: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    price_history: list[dict[str, Any]] = field(default_factory=list)
    earnings: list[dict[str, Any]] = field(default_factory=list)
    #: Endpoints that returned nothing, and why. Named, never hidden.
    unavailable: list[str] = field(default_factory=list)

    @property
    def has_quote(self) -> bool:
        return self.quote.price is not None and self.quote.price > 0

    @property
    def filled_sections(self) -> int:
        return sum(bool(x) for x in (
            self.profile.name, self.quote.price, self.key_metrics, self.ratios,
            self.income_statement, self.balance_sheet, self.cash_flow,
            self.news, self.price_history, self.earnings,
        ))


def to_float(value: Any, *, zero_is_absent: bool = False) -> float | None:
    """Coerce to float. Optionally treat 0 as absent.

    `zero_is_absent` is set for fields where zero is not a plausible reading —
    a market capitalisation, a share price, a P/E. It is deliberately *not*
    set for figures where zero is meaningful, such as a dividend or net debt.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if zero_is_absent and number == 0:
        return None
    return number


# ===========================================================================
# Base provider
# ===========================================================================
class BaseMarketProvider(ABC):
    """One external market-data source."""

    #: Human-readable, returned to the caller with every response.
    name: str = "abstract"
    #: Lower runs first.
    priority: int = 100

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()
        self._last_call = 0.0
        self._consecutive_rate_limits = 0

    # -- health ----------------------------------------------------------
    @property
    def available(self) -> bool:
        """False once the circuit has opened."""
        return self._consecutive_rate_limits < self.policy.circuit_threshold

    def reset_circuit(self) -> None:
        self._consecutive_rate_limits = 0

    @abstractmethod
    def configured(self) -> bool:
        """Are credentials present? Never returns the credential itself."""

    @abstractmethod
    def fetch(self, ticker: str, **kwargs) -> tuple[MarketSnapshot, dict[str, Any]]:
        """Return the normalised snapshot and the raw payloads behind it."""

    # -- shared HTTP ------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.policy.min_interval:
            time.sleep(self.policy.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get_json(
        self, url: str, *, headers: dict[str, str] | None = None,
        redact: str | None = None,
    ) -> Any:
        """One GET with the shared retry policy.

        `redact` is a secret that must never reach a log line. Some providers
        authenticate by query string, and a URL logged verbatim then leaks the
        key — exactly the defect found in the AI layer (PD-003).
        """
        if not self.available:
            raise ProviderRateLimited(
                f"{self.name}: circuit open after "
                f"{self._consecutive_rate_limits} rate limits"
            )

        def safe(text: str) -> str:
            return text.replace(redact, "<REDACTED>") if redact else text

        last: Exception | None = None
        for attempt in range(self.policy.attempts):
            self._throttle()
            request = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "IERP/1.0 (institutional-equity-research-platform)",
                **(headers or {}),
            })
            try:
                with urllib.request.urlopen(
                    request, timeout=self.policy.timeout_seconds,
                ) as response:
                    payload = json.load(response)
                self._consecutive_rate_limits = 0
                return payload

            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 401:
                    # The key itself is wrong. Never retried, and fatal for
                    # the provider: it will be wrong for every other symbol.
                    raise ProviderAuthError(
                        f"{self.name}: credentials rejected (HTTP 401)"
                    ) from exc
                if exc.code == 403:
                    # Ambiguous, and the distinction matters. Finnhub answers
                    # 403 "You don't have access to this resource" for a
                    # symbol outside the plan while serving US symbols
                    # perfectly — treating that as a bad key would disable a
                    # working provider for every ticker. A body naming access
                    # or the plan is a per-request entitlement boundary and
                    # falls through; anything else is treated as a credential
                    # failure.
                    body = ""
                    try:
                        body = exc.read().decode("utf-8", "replace")[:300].lower()
                    except Exception:  # noqa: BLE001
                        pass
                    entitlement = any(marker in body for marker in (
                        "access to this resource", "not available under",
                        "subscription", "premium", "upgrade", "your plan",
                        "restricted endpoint",
                    ))
                    if entitlement:
                        raise SymbolNotFound(
                            f"{self.name}: not included in this plan"
                        ) from exc
                    raise ProviderAuthError(
                        f"{self.name}: credentials rejected (HTTP 403)"
                    ) from exc
                if exc.code == 429:
                    self._consecutive_rate_limits += 1
                    time.sleep(self.policy.delay(attempt) * 2)
                    continue
                if exc.code == 404:
                    raise SymbolNotFound(f"{self.name}: not found") from exc
                if 500 <= exc.code < 600:
                    time.sleep(self.policy.delay(attempt))
                    continue
                raise ProviderError(
                    f"{self.name}: HTTP {exc.code} for {safe(url)[:120]}"
                ) from exc

            except (TimeoutError, urllib.error.URLError) as exc:
                # Transient by nature — a timeout, a reset, a DNS blip.
                last = exc
                time.sleep(self.policy.delay(attempt))

            except json.JSONDecodeError as exc:
                # A provider serving an HTML error page. Not worth retrying.
                raise ProviderError(f"{self.name}: malformed JSON response") from exc

        raise ProviderError(
            f"{self.name}: failed after {self.policy.attempts} attempts: "
            f"{safe(str(last))[:160]}"
        )

#: Suffixes that already identify an exchange. A bare symbol needs a default.
_KNOWN_SUFFIXES = (".NS", ".BO", ".L", ".TO", ".AX", ".HK", ".SS", ".SZ", ".DE", ".PA")

#: Bare symbols are assumed to be NSE, because that is the platform's
#: universe — but only when they look Indian. A US ticker is short, all
#: letters, and must be left alone: appending ".NS" to AAPL produced
#: "AAPL.NS", which every provider rejects, so a symbol the primary serves
#: perfectly was reported as unsupported.
def normalise_symbol(ticker: str, *, default_suffix: str = ".NS") -> str:
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise SymbolNotFound("empty ticker")
    if any(symbol.endswith(suffix) for suffix in _KNOWN_SUFFIXES) or "." in symbol:
        return symbol
    from app.data.nse_universe import NSE_UNIVERSE  # local: avoids a cycle

    if symbol in {row[0] for row in NSE_UNIVERSE}:
        return f"{symbol}{default_suffix}"
    # Not in the Indian universe: treat it as already fully qualified.
    return symbol
