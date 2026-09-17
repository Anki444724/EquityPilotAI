"""``robots.txt`` — the one and only implementation in this platform.

There is no other robots handling anywhere in the codebase (the filing
providers fetch pages directly), and there must not be: a second evaluator is
a second answer to "may we fetch this", and the two would eventually disagree.
Every web fetch in Phase 1 consults :class:`RobotsPolicy` before opening a
socket.

Dependency-free on purpose, for the same reason as
:mod:`app.services.web.safety`: the rules below are policy, they are
security-relevant, and they must be exercisable without a network, a database
or a running application.

Policy, stated exhaustively
---------------------------
The governing principle is **fail closed**: the platform fetches only when it
has positively established that the site's policy permits it. Any condition
where the policy could not be read, or could not be understood, is a refusal.
That is deliberately stricter than "no rule found means allowed", and it is
also what RFC 9309 permits — the specification has separate notions of
"unavailable" and "unreachable" precisely because the safe answer differs.

============================  ==============  ==================================
Server response                Decision        Why
============================  ==============  ==================================
2xx, parses                    Rules applied   Normal case. ``Allow``/``Disallow``
                                               for the matched user-agent group,
                                               plus ``Crawl-delay``.
2xx, empty body                ALLOW           A valid file that expresses no
                                               rule. Nothing is being bypassed.
2xx, unparseable / undecodable DISALLOW        The policy exists and we cannot
                                               understand it. Assuming it permits
                                               us is a guess in our own favour.
2xx, larger than the cap       DISALLOW        RFC 9309 allows ignoring content
                                               past 500 KiB; ignoring part of a
                                               policy is indistinguishable from
                                               misreading it, so the whole policy
                                               is treated as unavailable.
404, 410 (absent)              ALLOW           There is no policy to respect.
429 (rate limited)             DISALLOW        The site is explicitly asking for
                                               less traffic, not more.
401, 403 (refused)             DISALLOW        The site is refusing automated
                                               access. That is the answer.
5xx (server error)             DISALLOW        RFC 9309 "unreachable": assume
                                               complete disallow.
3xx redirect                   DISALLOW        Redirects are not followed for
                                               ``robots.txt``; the policy must be
                                               authoritative at the origin we
                                               validated, or it is not a policy.
Transport error / timeout      DISALLOW        Could not be established.
============================  ==============  ==================================

Rule precedence
---------------
The standard library's ``RobotFileParser`` resolves a path with the **first**
matching rule in a group. RFC 9309 §2.2.2 specifies the **longest** match,
with ``Allow`` winning a tie. The two disagree, in both directions::

    Allow: /investors          Disallow: /
    Disallow: /investors/private   Allow: /investors

First-match refuses the private path on the left policy only by luck of
ordering and permits it when the lines are swapped; on the right policy it
refuses ``/investors`` outright, though the site went out of its way to allow
it. Neither answer is the policy the site wrote.

So plain (wildcard-free) rules are resolved here by longest match, per the
specification, and that answer governs whenever it has something to say:

* a longer ``Disallow`` refuses a path the library would permit — closing the
  permissive deviation, which is the one that matters;
* a longer ``Allow`` permits a path the library would refuse. That direction
  is not a bypass: the path is explicitly allowed by the policy that was
  fetched and parsed, under the precedence the specification defines, and
  refusing it would silently discard evidence a site published for exactly
  this purpose;
* a tie permits, as the specification says.

Rules containing ``*`` or ``$`` are left entirely to the library, because
guessing at their semantics could produce a *more* permissive answer than the
library's. Where no plain rule matches, the library's verdict stands. Group
selection follows the specification too: a group naming this user agent
replaces the wildcard group rather than merging with it.

A DISALLOW here is never overridden by a caller. There is no "force" flag, no
per-host exception list and no bypass header anywhere in this package.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from app.domain.web.types import WebRejectionReason

#: What a ``robots.txt`` fetch returns: ``(status_code, body_bytes)``.
RobotsTransport = Callable[..., "tuple[int, bytes]"]

#: Statuses that mean "there is no robots.txt". Both are used in the wild;
#: an empty 404 is the median answer from a corporate webserver.
_ABSENT_STATUSES = frozenset({404, 410})

#: Ceiling on the cap, mirroring the policy default. A policy larger than this
#: cannot be honestly evaluated (see the table above).
DEFAULT_MAX_BYTES = 512 * 1024
DEFAULT_TIMEOUT = 10.0
DEFAULT_TTL_SECONDS = 3600.0


class RobotsStatus(StrEnum):
    """Why the decision came out the way it did."""

    ALLOWED = "allowed"
    #: Refused because a rule in the file forbids this path.
    DISALLOWED_BY_RULE = "disallowed_by_rule"
    #: Refused because no policy could be positively established.
    POLICY_UNAVAILABLE = "policy_unavailable"
    #: Allowed because the site publishes no ``robots.txt``.
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class RobotsOutcome:
    """A decision, with the evidence behind it.

    ``reason`` is the human sentence, ``status`` the machine-readable one.
    Both are kept because an operator asking "why was this page skipped" wants
    the sentence, and a test or a caller wants the enum.
    """

    host: str
    path: str
    allowed: bool
    status: RobotsStatus
    reason: str
    crawl_delay: float | None = None
    fetched_at: datetime | None = None

    @property
    def rejection(self) -> WebRejectionReason | None:
        """The typed refusal, when there is one."""
        return None if self.allowed else WebRejectionReason.ROBOTS_DISALLOWED


def precedence_decision(
    allow_prefixes: tuple[str, ...],
    disallow_prefixes: tuple[str, ...],
    path: str,
) -> bool | None:
    """RFC 9309 longest-match over plain rules, or ``None`` when silent.

    ``None`` means "this reading has nothing to say about the path" — no plain
    rule matched — and the caller then uses the library's verdict. ``True``
    and ``False`` are the specification's answers: the longest matching
    ``Allow`` wins, a longer ``Disallow`` beats it, and an exact tie permits.
    """
    longest_allow = max(
        (len(prefix) for prefix in allow_prefixes if path.startswith(prefix)),
        default=-1,
    )
    longest_disallow = max(
        (len(prefix) for prefix in disallow_prefixes if path.startswith(prefix)),
        default=-1,
    )
    if longest_allow < 0 and longest_disallow < 0:
        return None
    return longest_allow >= longest_disallow


def _plain_prefixes(
    parser: RobotFileParser, user_agent: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The plain allow/disallow prefixes of the group that applies to us.

    Group selection is the specification's: a group that names this user agent
    applies *instead of* the wildcard group. Merging the two would let a
    wildcard ``Allow`` widen a named group's ``Disallow``, which is not what
    the site wrote.

    Reads the library's own parsed entries rather than re-parsing the text, so
    path quoting and prefix matching keep exactly the semantics of the parser
    the rest of this module uses.
    """
    named = [
        entry for entry in (getattr(parser, "entries", ()) or ())
        if entry.applies_to(user_agent)
    ]
    default_entry = getattr(parser, "default_entry", None)
    if named:
        groups = named
    elif default_entry is not None and default_entry.applies_to(user_agent):
        groups = [default_entry]
    else:
        groups = []

    allow: list[str] = []
    disallow: list[str] = []
    for group in groups:
        for line in getattr(group, "rulelines", ()) or ():
            path = str(getattr(line, "path", "") or "")
            if not path or "*" in path or "$" in path:
                continue
            (allow if getattr(line, "allowance", True) else disallow).append(path)
    return tuple(allow), tuple(disallow)


@dataclass(slots=True)
class _Cached:
    """A parsed policy for one host, with the moment it stops being current."""

    parser: RobotFileParser | None
    outcome_status: RobotsStatus
    reason: str
    crawl_delay: float | None
    expires_at: float
    fetched_at: datetime
    #: Plain rule prefixes for this agent, used for the precedence check.
    allow_prefixes: tuple[str, ...] = ()
    disallow_prefixes: tuple[str, ...] = ()


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """An opener that refuses to follow redirects.

    ``urllib`` follows them by default, which for ``robots.txt`` would mean
    evaluating a policy served from somewhere other than the origin we
    validated. A 3xx therefore surfaces as an ``HTTPError`` and is treated as
    "policy unavailable".
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    return urllib.request.build_opener(_NoRedirect)


def _default_fetch(
    url: str, *, timeout: float, max_bytes: int, user_agent: str,
) -> tuple[int, bytes]:
    """One GET of ``robots.txt``, bounded, without following redirects."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            # No transparent decompression in urllib: advertising gzip yields
            # bytes that fail to decode. Same reasoning as FilingDownloader.
            "Accept-Encoding": "identity",
            "Accept": "text/plain,*/*;q=0.8",
        },
    )
    opener = _no_redirect_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return response.status, raw
            return response.status, raw
    except urllib.error.HTTPError as exc:
        # A 3xx reaches here as an HTTPError because the redirect handler
        # returned None. Statuses are returned, not raised, so the caller's
        # policy table is the single decision point.
        return exc.code, b""


class RobotsPolicy:
    """Fetches, parses and caches ``robots.txt`` decisions.

    Caching is per host and TTL-bounded. Negative decisions are cached too —
    a 404 that cost a round trip should not cost one per page — but a
    *refusal* is cached for the same TTL as an allow, because both are answers
    and re-asking a site that just told us no is the behaviour this module
    exists to prevent.
    """

    def __init__(
        self,
        *,
        fetch: RobotsTransport | None = None,
        user_agent: str = "EquityPilotAI",
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetch = fetch or (
            lambda url, *, timeout, max_bytes, user_agent: _default_fetch(
                url, timeout=timeout, max_bytes=max_bytes, user_agent=user_agent
            )
        )
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, _Cached] = {}

    # ------------------------------------------------------------- public
    def outcome_for(self, url: str) -> RobotsOutcome:
        """The decision for ``url``. Never raises: every failure is a refusal."""
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        if not host:
            return RobotsOutcome(
                host="", path=path, allowed=False,
                status=RobotsStatus.POLICY_UNAVAILABLE,
                reason="robots.txt: URL carries no host, so no policy can apply",
            )

        cached = self._cached_for(host)
        return self._decide(cached, host, path)

    def allowed(self, url: str) -> bool:
        """Convenience predicate. ``False`` for every refusal."""
        return self.outcome_for(url).allowed

    def crawl_delay_for(self, url: str) -> float | None:
        """The site's requested delay between requests, or ``None``.

        ``None`` means the site did not ask for one — it does not mean "no
        delay"; the fetcher substitutes its own floor so that an unstated
        delay still produces courteous spacing.
        """
        return self.outcome_for(url).crawl_delay

    def clear(self) -> None:
        """Drop the cache. For tests and for an explicit operator refresh."""
        self._cache.clear()

    # ------------------------------------------------------------ internal
    def _cached_for(self, host: str) -> _Cached:
        now = self._clock()
        entry = self._cache.get(host)
        if entry is not None and entry.expires_at > now:
            return entry
        fetched = self._fetch_policy(host, now)
        self._cache[host] = fetched
        return fetched

    def _fetch_policy(self, host: str, now: float) -> _Cached:
        url = f"https://{host}/robots.txt"
        try:
            status, body = self._fetch(
                url, timeout=self.timeout, max_bytes=self.max_bytes,
                user_agent=self.user_agent,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is "unavailable"
            return self._unavailable(
                now, f"robots.txt could not be fetched ({type(exc).__name__})",
            )

        if status in _ABSENT_STATUSES:
            return _Cached(
                parser=None, outcome_status=RobotsStatus.ABSENT,
                reason=f"no robots.txt at {host} (HTTP {status})",
                crawl_delay=None, expires_at=now + self.ttl_seconds,
                fetched_at=self._now(),
            )

        if len(body) > self.max_bytes:
            return self._unavailable(
                now,
                f"robots.txt at {host} is larger than {self.max_bytes:,} bytes, "
                "so the policy could not be read in full",
            )

        if 300 <= status < 400:
            return self._unavailable(
                now, f"robots.txt at {host} redirects (HTTP {status}); "
                "redirects are not followed for a policy file",
            )
        if status == 429:
            return self._unavailable(
                now, f"robots.txt at {host} rate-limited this client (HTTP 429)",
            )
        if 400 <= status < 500:
            return self._unavailable(
                now, f"robots.txt at {host} was refused (HTTP {status})",
            )
        if status >= 500 or status < 200:
            return self._unavailable(
                now, f"robots.txt at {host} is unreachable (HTTP {status}); "
                "RFC 9309 says assume complete disallow",
            )

        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._unavailable(
                now, f"robots.txt at {host} is not valid UTF-8 text",
            )

        parser = RobotFileParser()
        try:
            parser.parse(text.splitlines())
        except Exception as exc:  # noqa: BLE001 - a parser failure is a refusal
            return self._unavailable(
                now, f"robots.txt at {host} could not be parsed "
                     f"({type(exc).__name__})",
            )

        delay = self._crawl_delay(parser)
        allow_prefixes, disallow_prefixes = _plain_prefixes(parser, self.user_agent)
        return _Cached(
            parser=parser, outcome_status=RobotsStatus.ALLOWED,
            reason=f"robots.txt at {host} read and applied",
            crawl_delay=delay, expires_at=now + self.ttl_seconds,
            fetched_at=self._now(),
            allow_prefixes=allow_prefixes,
            disallow_prefixes=disallow_prefixes,
        )

    def _unavailable(self, now: float, reason: str) -> _Cached:
        return _Cached(
            parser=None, outcome_status=RobotsStatus.POLICY_UNAVAILABLE,
            reason=reason, crawl_delay=None,
            expires_at=now + self.ttl_seconds, fetched_at=self._now(),
        )

    def _crawl_delay(self, parser: RobotFileParser) -> float | None:
        """The site's crawl delay, from either of the two ways it is stated.

        ``Crawl-delay`` is the widely-implemented non-standard directive;
        ``Request-rate`` is the other spelling. Both are read so a site using
        either is honoured, and neither is invented when absent.
        """
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:  # noqa: BLE001
            delay = None
        if delay:
            return float(delay)
        try:
            rate = parser.request_rate(self.user_agent)
        except Exception:  # noqa: BLE001
            rate = None
        if rate and rate.requests:
            return float(rate.seconds) / float(rate.requests)
        return None

    def _decide(self, cached: _Cached, host: str, path: str) -> RobotsOutcome:
        if cached.parser is None:
            allowed = cached.outcome_status is RobotsStatus.ABSENT
            return RobotsOutcome(
                host=host, path=path, allowed=allowed,
                status=cached.outcome_status, reason=cached.reason,
                crawl_delay=cached.crawl_delay, fetched_at=cached.fetched_at,
            )

        try:
            permitted = cached.parser.can_fetch(self.user_agent, path)
        except Exception as exc:  # noqa: BLE001 - an evaluation failure is a refusal
            return RobotsOutcome(
                host=host, path=path, allowed=False,
                status=RobotsStatus.POLICY_UNAVAILABLE,
                reason=f"robots.txt at {host} could not be evaluated for "
                       f"'{path}' ({type(exc).__name__})",
                crawl_delay=cached.crawl_delay, fetched_at=cached.fetched_at,
            )

        # The library's first-match reading is superseded wherever the plain
        # rules have an answer under RFC 9309 precedence — see the module
        # docstring, which states both directions and why neither is a bypass.
        precedence = precedence_decision(
            cached.allow_prefixes, cached.disallow_prefixes, path,
        )
        if precedence is not None:
            return RobotsOutcome(
                host=host, path=path, allowed=precedence,
                status=(
                    RobotsStatus.ALLOWED if precedence
                    else RobotsStatus.DISALLOWED_BY_RULE
                ),
                reason=(
                    cached.reason
                    if precedence
                    else f"robots.txt at {host} disallows '{path}' for "
                         f"{self.user_agent} (RFC 9309 precedence over the "
                         "rules that match this path)"
                ),
                crawl_delay=cached.crawl_delay, fetched_at=cached.fetched_at,
            )

        if permitted:
            return RobotsOutcome(
                host=host, path=path, allowed=True,
                status=RobotsStatus.ALLOWED, reason=cached.reason,
                crawl_delay=cached.crawl_delay, fetched_at=cached.fetched_at,
            )
        return RobotsOutcome(
            host=host, path=path, allowed=False,
            status=RobotsStatus.DISALLOWED_BY_RULE,
            reason=f"robots.txt at {host} disallows '{path}' for "
                   f"{self.user_agent}",
            crawl_delay=cached.crawl_delay, fetched_at=cached.fetched_at,
        )
