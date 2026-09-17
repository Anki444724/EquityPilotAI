"""URL safety — the SSRF boundary for the web evidence layer.

This is the module that decides whether the platform is allowed to open a
socket at all. It is the only place in the web package that makes that
decision, and it is deliberately **dependency-free** — stdlib only, no
structlog, no settings, no HTTP client — for the same reason
:mod:`app.domain.ai.sourcing` is: a control that cannot be exercised without
standing up the application is a control that gets tested in production.

Threat model
------------
The URLs reaching this module are attacker-influenced in a specific way. They
are not arbitrary internet input: Phase 1 only ever fetches a *pinned* host,
derived from the company row, the crawl state or the filing providers. But
three things can still go wrong, and each is handled explicitly.

1. **A pinned hostname that resolves somewhere it should not.** A company's
   ``website`` field is operator-entered data. ``http://localhost:8000``,
   ``http://169.254.169.254/latest/meta-data/`` and ``http://10.0.0.5/admin``
   are all valid URLs that point at the platform's own infrastructure. Every
   address a hostname resolves to is therefore checked against the reserved,
   private, loopback and link-local ranges *before* a connection is made.

2. **A redirect that leaves the pin.** Safety is enforced per hop, not once at
   the start: ``https://pinned.example`` may redirect to ``http://127.0.0.1``
   and the only defence is to re-run this check on the ``Location`` target.
   The fetcher does exactly that, and
   ``WebRejectionReason.ADDRESS_NOT_PUBLIC`` is the refusal it produces.

3. **DNS rebinding.** Validating a hostname's addresses and *then* letting the
   HTTP client resolve it again leaves a window in which a second answer can
   differ from the first. Two things narrow it here: every address in the
   answer must be public (so an attacker must control the whole record, not
   just add one entry), and the fetcher verifies the peer address the socket
   actually connected to whenever the client exposes it
   (:func:`peer_address_is_safe`). The residual window is documented in the
   fetcher rather than papered over.

What this module does **not** do: it does not rewrite URLs to IP literals, and
it does not implement its own HTTP client. Both would break certificate
verification or duplicate the pinned stack (see `docs/` conventions on reusing
``httpx``).
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

from app.domain.web.types import WebRejectionReason

#: Schemes a pinned host may be fetched over. Anything else — ``file``,
#: ``ftp``, ``data``, ``gopher`` — is refused before any other processing,
#: because those schemes reach a different kind of resource entirely.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Default port per scheme, used when the URL omits one.
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

#: The cloud metadata endpoint, named explicitly. It is already covered by the
#: link-local range, but it is the single highest-value SSRF target in a
#: hosted deployment and naming it means a regression that widens the
#: link-local rule still cannot quietly re-admit this address.
METADATA_ADDRESSES: frozenset[str] = frozenset({
    "169.254.169.254",        # AWS / GCP / Azure IMDS
    "169.254.170.2",          # ECS task metadata
    "fd00:ec2::254",          # AWS IMDS over IPv6
})

#: ``socket.getaddrinfo`` shape. A resolver takes a host and port and returns
#: the addresses it found. Injectable so every refusal below can be tested
#: without a network.
Resolver = Callable[[str, int], "list[str]"]


class UrlSafetyError(Exception):
    """A URL was refused, with a typed reason and an operator-readable detail.

    ``RuntimeError`` rather than a bespoke hierarchy because every caller that
    already survives a fetch failure catches ``Exception``; the typed
    ``reason`` is what a caller branches on, not the class.
    """

    def __init__(self, reason: WebRejectionReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


def default_resolver(host: str, port: int) -> list[str]:
    """Resolve a host to every address it answers with.

    Every address, not the first: a host that answers with one public and one
    private address is a rebinding attempt, and taking the first would let it
    succeed half the time. Uses ``getaddrinfo`` rather than ``gethostbyname``
    so IPv6 answers are seen too.
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    addresses: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            address = sockaddr[0]
            if address not in addresses:
                addresses.append(address)
    return addresses


def is_public_address(address: str) -> bool:
    """Whether an IP literal is a destination the platform may connect to.

    Refuses everything that is not globally routable. Written as an allowlist
    over ``is_global`` with the dangerous families named for the reader:
    ``ipaddress``'s flags are exhaustive today, but the failure mode of a
    future flag being added is silent, so the explicit checks are kept.
    """
    if address in METADATA_ADDRESSES:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
        return False
    if parsed.is_reserved or parsed.is_multicast or parsed.is_unspecified:
        return False
    # IPv6 unique-local (fc00::/7) is reported by `is_private`, but the
    # site-local predicate is checked separately because its deprecation
    # status has changed across Python versions.
    if getattr(parsed, "is_site_local", False):
        return False
    # IPv4-mapped IPv6 (`::ffff:127.0.0.1`) carries a v4 address inside a v6
    # literal and would otherwise be judged on the v6 wrapper.
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        return is_public_address(str(mapped))
    return parsed.is_global


def peer_address_is_safe(peer: object) -> bool:
    """Whether the address a socket actually connected to is permitted.

    Defence in depth against DNS rebinding. ``peer`` is whatever the HTTP
    client exposes for the connected socket — commonly a ``(host, port)``
    tuple, sometimes a bare string. An unrecognised shape returns ``False``,
    so a change in the client's extension API fails closed rather than
    silently disabling the check.
    """
    if isinstance(peer, str):
        return is_public_address(peer)
    if isinstance(peer, (tuple, list)) and peer:
        candidate = peer[0]
        if isinstance(candidate, str):
            return is_public_address(candidate)
        # An address object (e.g. `ipaddress.IPv4Address`).
        if candidate is not None and hasattr(candidate, "compressed"):
            return is_public_address(str(candidate))
    return False


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """A URL that passed every safety check, with the addresses it resolved to.

    Returning the addresses rather than a boolean means the caller can log
    what was approved and can compare it against the peer it actually reached.
    """

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}" + (
            "" if self.port == DEFAULT_PORTS.get(self.scheme) else f":{self.port}"
        )


class UrlSafetyPolicy:
    """The pinned-host allowlist plus the address rules.

    The allowlist is a set of registrable hosts. A subdomain of a pinned host
    is admitted (``www.tcs.com`` under ``tcs.com``) because that is how
    corporate websites are actually arranged; a host that merely *ends with*
    the same letters (``eviltcs.com``) is not, which is why the check is on
    the label boundary and not a suffix match.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        allowed_ports: frozenset[int] | None = None,
        resolver: Resolver | None = None,
        allow_subdomains: bool = True,
    ) -> None:
        self.allowed_hosts = frozenset(
            h.strip().lower().lstrip(".") for h in allowed_hosts if h and h.strip()
        )
        self.allowed_ports = (
            allowed_ports if allowed_ports is not None else frozenset({80, 443})
        )
        self.resolver: Resolver = resolver or default_resolver
        self.allow_subdomains = allow_subdomains

    # ------------------------------------------------------------ allowlist
    def host_is_pinned(self, host: str) -> bool:
        """Whether a host is on the pinned allowlist or a subdomain of one."""
        candidate = (host or "").strip().lower().rstrip(".")
        if not candidate:
            return False
        if candidate in self.allowed_hosts:
            return True
        if not self.allow_subdomains:
            return False
        # Label-boundary match: `www.tcs.com` is under `tcs.com`,
        # `eviltcs.com` is not.
        return any(
            candidate.endswith(f".{pinned}") for pinned in self.allowed_hosts
        )

    # ---------------------------------------------------------------- check
    def check(self, url: str, *, resolve: bool = True) -> ResolvedTarget:
        """Validate a URL, resolving and checking every address it maps to.

        Raises :class:`UrlSafetyError` on refusal. ``resolve=False`` skips DNS
        and is used only by tests and by callers validating a URL shape they
        will resolve themselves.
        """
        parts = urlsplit((url or "").strip())
        scheme = (parts.scheme or "").lower()

        # Scheme first, then absoluteness. The order matters for the reason a
        # caller sees: `file:///etc/passwd` and `javascript:alert(1)` carry no
        # netloc, so testing absoluteness first would report them as
        # MALFORMED_URL — technically true, but it hides the fact that the
        # refusal was about the scheme, which is the security-relevant detail
        # an operator is reading these for.
        if scheme and scheme not in ALLOWED_SCHEMES:
            raise UrlSafetyError(
                WebRejectionReason.SCHEME_NOT_ALLOWED,
                f"scheme '{scheme}' is not one of {sorted(ALLOWED_SCHEMES)}",
            )
        if not scheme or not parts.netloc:
            raise UrlSafetyError(
                WebRejectionReason.MALFORMED_URL, f"'{url}' is not an absolute URL"
            )

        host = (parts.hostname or "").lower()
        if not host:
            raise UrlSafetyError(
                WebRejectionReason.MALFORMED_URL, f"'{url}' carries no host"
            )

        try:
            explicit_port = parts.port
        except ValueError as exc:  # malformed port such as ':abc'
            raise UrlSafetyError(
                WebRejectionReason.MALFORMED_URL, f"unparseable port in '{url}'"
            ) from exc
        port = explicit_port or DEFAULT_PORTS.get(scheme, 0)

        if not self.host_is_pinned(host):
            raise UrlSafetyError(
                WebRejectionReason.HOST_NOT_PINNED,
                f"host '{host}' is not on the pinned allowlist",
            )
        if port not in self.allowed_ports:
            raise UrlSafetyError(
                WebRejectionReason.PORT_NOT_ALLOWED,
                f"port {port} is not one of {sorted(self.allowed_ports)}",
            )

        if not resolve:
            return ResolvedTarget(url, scheme, host, port, ())

        # A literal IP in the URL is checked directly; there is nothing to
        # resolve, and asking a resolver about an address is a no-op that some
        # platforms treat as a name lookup.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            addresses = self._resolve(host, port)
        else:
            addresses = [host]

        if not addresses:
            raise UrlSafetyError(
                WebRejectionReason.UNRESOLVABLE_HOST,
                f"'{host}' did not resolve to any address",
            )

        offenders = [a for a in addresses if not is_public_address(a)]
        if offenders:
            raise UrlSafetyError(
                WebRejectionReason.ADDRESS_NOT_PUBLIC,
                f"'{host}' resolves to non-public address(es) "
                f"{', '.join(sorted(offenders))}",
            )

        return ResolvedTarget(url, scheme, host, port, tuple(addresses))

    def check_redirect(self, from_url: str, location: str) -> ResolvedTarget:
        """Validate the target of a redirect, as a fresh URL.

        Redirect targets are absolute after resolution, so this is not a
        different rule set — it is the same rule set applied again, which is
        the entire point: a redirect is the one way a caller-supplied URL can
        change after it was approved.
        """
        from urllib.parse import urljoin

        return self.check(urljoin(from_url, location))

    # ------------------------------------------------------------- internal
    def _resolve(self, host: str, port: int) -> list[str]:
        try:
            return list(self.resolver(host, port))
        except UrlSafetyError:
            raise
        except Exception as exc:  # noqa: BLE001 - any resolver failure is a refusal
            raise UrlSafetyError(
                WebRejectionReason.UNRESOLVABLE_HOST,
                f"could not resolve '{host}': {type(exc).__name__}",
            ) from exc
