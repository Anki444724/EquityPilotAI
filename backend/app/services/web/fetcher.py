"""Bounded, policy-checked HTTP fetching for web evidence.

This generalises the discipline of
:class:`app.services.filings.downloader.FilingDownloader` — which already
enforces a streamed size cap, a timeout and a magic-byte check for filings —
into a fetcher that handles the three content classes Phase 1 accepts. It does
not replace that class: filings keep their own downloader, because a filing is
required to be a PDF and a web page is not.

The order of operations is the contract
---------------------------------------
::

    URL safety (resolve + address rules)
      -> robots.txt decision
        -> per-host politeness delay
          -> HTTP request (timeout, streamed byte cap)
            -> redirect? re-validate from the top for the new hop
              -> status classification (403 is never content)
                -> content-type allowlist
                  -> decode / charset
                    -> SHA-256, final URL, retrieved_at

Nothing is skipped and nothing is reordered, because each step assumes the one
before it has run. Robots is checked *before* the socket is opened; the
address rules are re-applied to every redirect target, so a pinned host that
redirects to ``http://127.0.0.1`` is refused at the hop, not discovered after
the fact.

What "no bypass" means concretely
---------------------------------
The client sends an honest, identifying user agent and nothing else. There is
no browser impersonation, no ``X-Forwarded-For``, no cookie priming, no
referer spoofing, no retry-around-a-challenge and no CAPTCHA handling of any
kind. A site that answers 403 has refused; the fetcher raises
:data:`~app.domain.web.types.WebRejectionReason.FORBIDDEN_BY_SERVER` and the
page is not persisted. That is the whole of the response.

Residual risk, stated rather than implied
-----------------------------------------
Between this module resolving a host and ``httpx`` resolving it again for the
request there is a DNS-rebinding window. Three things bound it: every address
in the answer must be public (so a rebinding attacker must control the whole
record, not add one entry), the answer is validated immediately before each
hop, and the peer address the socket actually connected to is re-checked
afterwards when the client exposes it. Closing the window completely needs a
connect-time guard inside the HTTP stack; that is a Phase-2 item, listed in
the phase report, and it is deliberately **not** addressed by writing a second
HTTP client here.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

import structlog

from app.domain.web.types import (
    WebContentClass,
    WebFetchPolicy,
    WebRejectionReason,
    content_class_for,
)
from app.services.web.robots import RobotsOutcome, RobotsPolicy
from app.services.web.safety import (
    ResolvedTarget,
    UrlSafetyError,
    UrlSafetyPolicy,
    peer_address_is_safe,
)

log = structlog.get_logger(__name__)

#: Statuses that mean "look somewhere else".
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Statuses a retry may help with. 429 is included: the server is asking for
#: less traffic, and the polite response is to wait, not to hammer.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class WebFetchError(Exception):
    """A fetch failed, with a typed reason and whether a retry may help.

    ``retryable`` is decided once, at the point the failure is classified, and
    the retry loop only acts on it. Nothing retries a policy refusal, a 4xx or
    an oversized body.
    """

    def __init__(
        self,
        reason: WebRejectionReason,
        detail: str,
        *,
        retryable: bool = False,
        url: str = "",
    ) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
        self.retryable = retryable
        self.url = url


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """What an HTTP transport hands back. Deliberately small.

    ``truncated`` is set by the transport when the body exceeded the ceiling
    *while streaming* — the fetch is then refused outright rather than
    silently ingesting a prefix of a page.
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    final_url: str
    elapsed_ms: float = 0.0
    truncated: bool = False
    #: The address the socket actually connected to, when the client exposes
    #: it. Used for the rebinding check; ``None`` means "not available".
    peer_address: Any = None


class Transport(Protocol):
    """The seam that makes every test in this package offline."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> TransportResponse: ...


class HttpxTransport:
    """The real transport, over the ``httpx`` the platform already depends on.

    Redirects are **not** followed by the client (``follow_redirects=False``)
    because each hop must pass safety and robots validation; a client that
    followed them internally would make that impossible.
    """

    name = "httpx"

    def __init__(self, client: Any | None = None, *, timeout: float = 20.0) -> None:
        self._client = client
        self._timeout = timeout

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> TransportResponse:
        import httpx

        client = self._get_client()
        started = time.perf_counter()
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            with client.stream(
                "GET", url, headers=dict(headers),
                timeout=httpx.Timeout(timeout),
            ) as response:
                # Enforced against bytes actually received. Content-Length is
                # the server's claim and may be absent, wrong, or a lie; a cap
                # that trusts it is not a cap.
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
                return TransportResponse(
                    status_code=response.status_code,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    content=b"".join(chunks),
                    final_url=str(response.url),
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    truncated=truncated,
                    peer_address=_peer_address(response),
                )
        except httpx.TimeoutException as exc:
            raise WebFetchError(
                WebRejectionReason.TIMEOUT, f"timed out after {timeout}s",
                retryable=True, url=url,
            ) from exc
        except httpx.HTTPError as exc:
            raise WebFetchError(
                WebRejectionReason.TRANSPORT_ERROR,
                f"{type(exc).__name__}: {exc}", retryable=True, url=url,
            ) from exc


def _peer_address(response: Any) -> Any:
    """Best-effort peer address of the connection behind a response.

    ``httpx`` exposes the underlying network stream in ``extensions``; the
    exact key and accessor are implementation details, so everything here is
    guarded. ``None`` means "not available", never "safe" — the caller treats
    the two differently on purpose.
    """
    try:
        stream = (getattr(response, "extensions", None) or {}).get("network_stream")
        if stream is None:
            return None
        return stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 - an unavailable extension is not a failure
        return None


class HostPoliteness:
    """Minimum spacing between requests to the same host.

    The site's ``Crawl-delay`` when it states one, the policy floor otherwise,
    and the delay is applied per host rather than globally so a slow site
    cannot starve a fast one. ``clock`` and ``sleep`` are injectable, which is
    what lets the pacing be tested without the tests taking seconds.
    """

    def __init__(
        self,
        *,
        default_delay: float = 1.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_delay: float = 30.0,
    ) -> None:
        self.default_delay = default_delay
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._max_delay = max_delay
        self._last: dict[str, float] = {}

    def delay_for(self, host: str, crawl_delay: float | None) -> float:
        """The delay that applies to this host, clamped to something sane."""
        requested = crawl_delay if crawl_delay is not None else self.default_delay
        return max(0.0, min(float(requested), self._max_delay))

    def wait(self, host: str, crawl_delay: float | None) -> float:
        """Block until this host may be contacted again. Returns seconds waited."""
        delay = self.delay_for(host, crawl_delay)
        now = self._clock()
        last = self._last.get(host)
        waited = 0.0
        if last is not None:
            remaining = delay - (now - last)
            if remaining > 0:
                self._sleep(remaining)
                waited = remaining
                now = self._clock()
        self._last[host] = now
        return waited


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """A successfully fetched, policy-checked response."""

    url: str
    final_url: str
    host: str
    status_code: int
    content_type: str
    content_class: WebContentClass
    charset: str | None
    content: bytes
    text: str
    sha256: str
    size_bytes: int
    retrieved_at: datetime
    latency_ms: float
    robots: RobotsOutcome
    redirect_chain: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def content_type_media(self) -> str:
        return (self.content_type or "").split(";", 1)[0].strip().lower()


class WebFetcher:
    """Fetches one URL inside the Phase-1 policy, or refuses it with a reason."""

    def __init__(
        self,
        *,
        policy: WebFetchPolicy | None = None,
        safety: UrlSafetyPolicy,
        robots: RobotsPolicy,
        transport: Transport | None = None,
        politeness: HostPoliteness | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.policy = policy or WebFetchPolicy()
        self.safety = safety
        self.robots = robots
        self.transport: Transport = transport or HttpxTransport()
        self.politeness = politeness or HostPoliteness(
            default_delay=self.policy.default_crawl_delay,
            sleep=sleep,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep

    # ------------------------------------------------------------- helpers
    def headers(self) -> dict[str, str]:
        """The exact headers sent. Honest, identifying, and nothing else."""
        return {
            "User-Agent": self.policy.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,text/plain,"
                "application/pdf;q=0.9,*/*;q=0.1"
            ),
            "Accept-Language": "en",
            "From": "research-bot@equitypilotai.invalid",
        }

    # ---------------------------------------------------------------- fetch
    def fetch(self, url: str) -> FetchedDocument:
        """Fetch ``url``, following at most ``max_redirects`` validated hops."""
        original = url
        current = url
        chain: list[str] = []
        redirects = 0
        robots_outcome: RobotsOutcome | None = None

        while True:
            target = self._check_safety(current)

            robots_outcome = self.robots.outcome_for(current)
            if not robots_outcome.allowed:
                log.info(
                    "web fetch refused by robots", url=current,
                    host=target.host, reason=robots_outcome.reason,
                )
                raise WebFetchError(
                    WebRejectionReason.ROBOTS_DISALLOWED, robots_outcome.reason,
                    retryable=False, url=current,
                )

            self.politeness.wait(target.host, robots_outcome.crawl_delay)

            response = self._request(current)

            if response.peer_address is not None and not peer_address_is_safe(
                response.peer_address
            ):
                # The socket reached an address the resolver did not vouch for.
                raise WebFetchError(
                    WebRejectionReason.ADDRESS_NOT_PUBLIC,
                    f"connected to a non-public peer address "
                    f"({response.peer_address!r}) for '{current}'",
                    retryable=False, url=current,
                )

            if response.status_code in _REDIRECT_STATUSES:
                redirects += 1
                if redirects > self.policy.max_redirects:
                    raise WebFetchError(
                        WebRejectionReason.REDIRECT_LIMIT,
                        f"more than {self.policy.max_redirects} redirects",
                        retryable=False, url=current,
                    )
                location = _header(response.headers, "location")
                if not location:
                    raise WebFetchError(
                        WebRejectionReason.REDIRECT_WITHOUT_LOCATION,
                        f"HTTP {response.status_code} carried no Location",
                        retryable=False, url=current,
                    )
                if current in chain:
                    raise WebFetchError(
                        WebRejectionReason.REDIRECT_LIMIT,
                        f"redirect loop at '{current}'",
                        retryable=False, url=current,
                    )
                chain.append(current)
                current = urljoin(current, location)
                # Loop back to the top: the new hop is safety-checked and
                # robots-checked exactly as the first one was. This is the
                # control that stops a pinned host redirecting anywhere.
                continue

            return self._build(original, current, response, robots_outcome, chain)

    # ------------------------------------------------------------ internals
    def _check_safety(self, url: str) -> ResolvedTarget:
        try:
            return self.safety.check(url)
        except UrlSafetyError as exc:
            raise WebFetchError(
                exc.reason, exc.detail, retryable=False, url=url,
            ) from exc

    def _request(self, url: str) -> TransportResponse:
        """One hop, retried only when the failure was classified retryable."""
        attempt = 0
        last: WebFetchError | None = None
        while attempt < self.policy.max_attempts:
            attempt += 1
            try:
                response = self.transport.get(
                    url,
                    headers=self.headers(),
                    timeout=self.policy.timeout_seconds,
                    max_bytes=self.policy.max_bytes,
                )
            except WebFetchError as exc:
                last = exc
            else:
                if response.truncated:
                    raise WebFetchError(
                        WebRejectionReason.TOO_LARGE,
                        f"response exceeded {self.policy.max_bytes:,} bytes "
                        "while streaming",
                        retryable=False, url=url,
                    )
                if 200 <= response.status_code < 400:
                    return response
                last = self._status_error(response, url)

            if not last.retryable or attempt >= self.policy.max_attempts:
                raise last
            log.info(
                "retrying web fetch", url=url, attempt=attempt,
                reason=last.reason.value, detail=last.detail,
            )
            self._sleep(min(2.0 * attempt, 5.0))
        raise last  # pragma: no cover - loop always returns or raises

    @staticmethod
    def _status_error(response: TransportResponse, url: str) -> WebFetchError:
        """Classify a non-success status. A 403 is a refusal, never content."""
        code = response.status_code
        if code in (401, 403):
            return WebFetchError(
                WebRejectionReason.FORBIDDEN_BY_SERVER,
                f"HTTP {code}: the server refused automated access",
                retryable=False, url=url,
            )
        if code in _RETRYABLE_STATUSES:
            return WebFetchError(
                WebRejectionReason.HTTP_ERROR, f"HTTP {code}",
                retryable=True, url=url,
            )
        return WebFetchError(
            WebRejectionReason.HTTP_ERROR, f"HTTP {code}",
            retryable=False, url=url,
        )

    def _build(
        self,
        original: str,
        final: str,
        response: TransportResponse,
        robots_outcome: RobotsOutcome,
        chain: list[str],
    ) -> FetchedDocument:
        content_type = _header(response.headers, "content-type") or ""
        content_class = content_class_for(content_type)
        if content_class is None:
            raise WebFetchError(
                WebRejectionReason.CONTENT_TYPE_NOT_ALLOWED,
                f"'{content_type or 'no content-type'}' is not an accepted "
                "content type",
                retryable=False, url=final,
            )

        content = response.content
        if not content:
            raise WebFetchError(
                WebRejectionReason.EMPTY_RESPONSE, "the response body was empty",
                retryable=False, url=final,
            )
        size = len(content)
        if size > self.policy.max_bytes:
            raise WebFetchError(
                WebRejectionReason.TOO_LARGE,
                f"{size:,} bytes exceeds {self.policy.max_bytes:,}",
                retryable=False, url=final,
            )

        charset = _charset_of(content_type)
        return FetchedDocument(
            url=original,
            final_url=response.final_url or final,
            host=(urlsplit(final).hostname or "").lower(),
            status_code=response.status_code,
            content_type=content_type,
            content_class=content_class,
            charset=charset,
            content=content,
            text=_decode(content, charset),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=size,
            retrieved_at=self._now(),
            latency_ms=response.elapsed_ms,
            robots=robots_outcome,
            redirect_chain=tuple(chain),
            headers=response.headers,
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup over a plain mapping."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return value
    return None


def _charset_of(content_type: str) -> str | None:
    """The ``charset`` parameter of a Content-Type, if it declares one."""
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').strip("'") or None
    return None


def _decode(payload: bytes, charset: str | None) -> str:
    """Decode a body, preferring the declared charset then the usual order."""
    for candidate in (charset, "utf-8", "latin-1"):
        if not candidate:
            continue
        try:
            return payload.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")
