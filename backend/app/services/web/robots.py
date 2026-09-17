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

So rules are resolved here by longest match, per the specification, and that
answer governs whenever it has something to say:

* a longer ``Disallow`` refuses a path the library would permit — closing the
  permissive deviation, which is the one that matters;
* a longer ``Allow`` permits a path the library would refuse. That direction
  is not a bypass: the path is explicitly allowed by the policy that was
  fetched and parsed, under the precedence the specification defines, and
  refusing it would silently discard evidence a site published for exactly
  this purpose;
* a tie permits, as the specification says.

That includes the ``*`` wildcard and the trailing ``$`` anchor, evaluated by
:func:`_match_length` rather than left to the library. Leaving them to the
library was a bug, not a safety margin: the library's answer depends on the
interpreter (Python 3.13 implements the specification's patterns, 3.12 and
earlier ignore them entirely), so the same ``robots.txt`` produced two
different decisions, and the older one was the permissive one.

Group selection follows the specification too: a group naming this user agent
replaces the wildcard group rather than merging with it. It is resolved here
from the parsed groups, not by asking the library, because the library's own
group lookup is version-dependent in exactly the same way — 3.13 keeps a
wildcard group in ``entries`` and never populates ``default_entry``, 3.12 and
earlier do the opposite, and ``Entry.applies_to`` does not match a wildcard
token on 3.13. Where no rule in the applicable group matches the path at all,
the library's verdict stands, as before.

A DISALLOW here is never overridden by a caller. There is no "force" flag, no
per-host exception list and no bypass header anywhere in this package.
"""
from __future__ import annotations

import re
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


#: RFC 9309 §2.2.2 markers, in both spellings a rule may carry. The literal
#: ``*`` and ``$`` are what the specification writes; the percent-encoded
#: forms are what the older standard-library parser stores, because it unquotes
#: the rule text and re-quotes it (``*`` becomes ``%2A``). Decoding both to the
#: same marker is what makes one policy mean one thing on every interpreter.
_WILDCARD_MARKER = re.compile(r"\*|%2[aA]")
_ANCHOR_MARKER = re.compile(r"\$|%24")


def _normalise_pattern(raw: str) -> str:
    """A rule path with its markers in the spelling this module matches on.

    RFC 9309 §2.2.2 unencodes a percent-encoded octet before comparing it, so
    ``%2A`` *is* the wildcard and ``%24`` *is* the anchor. Both spellings are
    therefore decoded, which also makes the two standard-library generations
    agree: 3.13 hands over ``/docs/*.pdf``, 3.12 and earlier hand over
    ``/docs/%2A.pdf``, and this function turns both into ``/docs/*.pdf``.
    """
    return _ANCHOR_MARKER.sub("$", _WILDCARD_MARKER.sub("*", raw))


def _match_length(pattern: str, path: str) -> int | None:
    """How much of ``path`` a rule matches, or ``None`` when it does not.

    The length is the quantity RFC 9309 §2.2.2 calls the *most specific
    match*; the caller compares lengths between rules. Semantics, in full:

    * a trailing ``$`` anchors the pattern to the end of the path;
    * ``*`` matches any run of characters, including none;
    * everything else is a literal, matched from the start of the path;
    * an empty pattern matches nothing, because ``Disallow:`` with no path is
      the specification's "allow all" and adds nothing to refuse.
    """
    pattern = _normalise_pattern(pattern)
    if not pattern:
        return None

    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
        if not pattern:
            return None

    if "*" not in pattern:
        if anchored:
            return len(pattern) if path == pattern else None
        return len(pattern) if path.startswith(pattern) else None

    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    if anchored:
        expression += r"\Z"
    match = re.match(expression, path, re.DOTALL)
    return match.end() if match else None


def rules_decision(
    rules: tuple[tuple[bool, str], ...], path: str,
) -> bool | None:
    """RFC 9309 §2.2.2 longest match over one group's rules.

    ``rules`` is ``(allowance, pattern)`` in the order the file stated them,
    which does not matter here: the longest match decides and only an exact
    tie is left to the specification's rule that ``Allow`` wins.

    Returns ``None`` when no rule matches the path, which is the one case
    where this reading has nothing to say. The caller then falls back to the
    library's verdict, exactly as it did before — a group that expresses no
    opinion about a path is not a refusal.
    """
    longest_allow = -1
    longest_disallow = -1
    for allowance, pattern in rules:
        length = _match_length(pattern, path)
        if length is None:
            continue
        if allowance:
            longest_allow = max(longest_allow, length)
        else:
            longest_disallow = max(longest_disallow, length)
    if longest_allow < 0 and longest_disallow < 0:
        return None
    return longest_allow >= longest_disallow


def precedence_decision(
    allow_prefixes: tuple[str, ...],
    disallow_prefixes: tuple[str, ...],
    path: str,
) -> bool | None:
    """The same decision for a caller that already holds bare prefixes.

    Kept because the plain-prefix case is the one most easily read at a call
    site and is pinned by its own tests. ``None`` means "this reading has
    nothing to say about the path" — no rule matched — and the caller then
    uses the library's verdict. ``True`` and ``False`` are the
    specification's answers: the longest matching ``Allow`` wins, a longer
    ``Disallow`` beats it, and an exact tie permits.
    """
    rules = tuple((True, prefix) for prefix in allow_prefixes) + tuple(
        (False, prefix) for prefix in disallow_prefixes
    )
    return rules_decision(rules, path)


def _agent_token(value: str) -> str:
    """The product token of a user-agent string, lowercased.

    ``EquityPilotAI/1.0`` and ``EquityPilotAI`` are the same crawler as far as
    a robots.txt group is concerned; the version suffix is not part of the
    token that identifies it.
    """
    return str(value or "").split("/")[0].strip().lower()


def _group_specificity(entry: object, token: str) -> int:
    """How specifically an entry's group names this crawler (0 = not at all).

    The comparison is the library's and the specification's: the group's
    user-agent value must appear, case-insensitively, in this crawler's
    product token. Longer values are more specific, which is how a group for
    ``equitypilotai`` is preferred over one for ``equity``. A ``*`` names
    nobody in particular and is scored zero — it is the fallback, not a match.
    """
    best = 0
    for agent in getattr(entry, "useragents", ()) or ():
        candidate = _agent_token(str(agent))
        if not candidate or candidate == "*":
            continue
        if candidate in token:
            best = max(best, len(candidate))
    return best


def _is_wildcard_group(entry: object) -> bool:
    """Whether an entry is the ``User-agent: *`` group."""
    return any(
        _agent_token(str(agent)) == "*"
        for agent in getattr(entry, "useragents", ()) or ()
    )


def _applicable_groups(
    parser: RobotFileParser, user_agent: str,
) -> tuple[object, ...]:
    """The groups of ``robots.txt`` that apply to this crawler.

    Group selection is the specification's, and is done here rather than asked
    of the library because the library answers differently per interpreter
    (the delay directive rides the same lookup, which is why it is read from
    this group rather than through ``parser.crawl_delay``):

    * the wildcard group lives in ``default_entry`` on Python 3.12 and earlier
      but in ``entries`` on 3.13, and ``default_entry`` is never populated
      there — reading only the former, as this module used to, finds no group
      at all on a newer interpreter and drops the site's policy on the floor;
    * ``Entry.applies_to`` returns ``False`` for a wildcard-only group from
      3.13 onwards, so a group selection delegated to it silently selects
      nothing.

    Reading both places and matching the agent tokens here gives one answer on
    every interpreter: the most specific named group that names us, else the
    wildcard group, else no group at all.

    Rules are taken from every group at that specificity, which matters when a
    file states the same user-agent twice — the specification merges those,
    and dropping one of them would drop a refusal the site wrote.
    """
    candidates = list(getattr(parser, "entries", ()) or ())
    default_entry = getattr(parser, "default_entry", None)
    if default_entry is not None and not any(
        candidate is default_entry for candidate in candidates
    ):
        candidates.append(default_entry)

    token = _agent_token(user_agent)
    named = [
        (specificity, entry)
        for entry in candidates
        if (specificity := _group_specificity(entry, token))
    ]
    if named:
        most_specific = max(specificity for specificity, _ in named)
        groups = [
            entry for specificity, entry in named if specificity == most_specific
        ]
    else:
        groups = [entry for entry in candidates if _is_wildcard_group(entry)]

    return tuple(groups)


def _rules_of(groups: tuple[object, ...]) -> tuple[tuple[bool, str], ...]:
    """The ``(allowance, pattern)`` rules across the applicable groups.

    The end anchor is read from whichever attribute the parser keeps it in.
    Python 3.13's ``RuleLine`` records ``$`` in ``line.fullmatch`` and *removes
    it from* ``line.path`` (compiling an end-anchored matcher), while 3.12 and
    earlier leave the ``$`` in the path and have no such attribute. Reading
    only the path — as this function used to — silently turned
    ``Disallow: /*.pdf$`` into the unanchored ``/*.pdf`` on 3.13, which then
    also matched a query string and refused a page the site had allowed. The
    anchor is therefore restored when the parser reports it separately, so one
    robots.txt means one thing on every interpreter.

    A pattern that already carries the anchor — literally, or as the older
    parser's ``%24`` — is left exactly as parsed, so the spelling is preserved
    and no second ``$`` is appended.
    """
    rules: list[tuple[bool, str]] = []
    for group in groups:
        for line in getattr(group, "rulelines", ()) or ():
            pattern = str(getattr(line, "path", "") or "")
            if not pattern:
                # ``Disallow:`` with an empty path means "allow all": a line
                # that forbids nothing, so it is not carried as a rule.
                continue
            if (
                getattr(line, "fullmatch", False)
                and not _normalise_pattern(pattern).endswith("$")
            ):
                pattern += "$"
            rules.append((bool(getattr(line, "allowance", True)), pattern))
    return tuple(rules)


@dataclass(slots=True)
class _Cached:
    """A parsed policy for one host, with the moment it stops being current."""

    parser: RobotFileParser | None
    outcome_status: RobotsStatus
    reason: str
    crawl_delay: float | None
    expires_at: float
    fetched_at: datetime
    #: ``(allowance, pattern)`` for the group that applies to this agent,
    #: resolved once at parse time so every decision on this host reads the
    #: same rules — including the wildcard and anchor patterns the library
    #: does not implement on older interpreters.
    rules: tuple[tuple[bool, str], ...] = ()


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

        groups = _applicable_groups(parser, self.user_agent)
        delay = self._crawl_delay(groups)
        rules = _rules_of(groups)
        return _Cached(
            parser=parser, outcome_status=RobotsStatus.ALLOWED,
            reason=f"robots.txt at {host} read and applied",
            crawl_delay=delay, expires_at=now + self.ttl_seconds,
            fetched_at=self._now(),
            rules=rules,
        )

    def _unavailable(self, now: float, reason: str) -> _Cached:
        return _Cached(
            parser=None, outcome_status=RobotsStatus.POLICY_UNAVAILABLE,
            reason=reason, crawl_delay=None,
            expires_at=now + self.ttl_seconds, fetched_at=self._now(),
        )

    def _crawl_delay(self, groups: tuple[object, ...]) -> float | None:
        """The site's crawl delay, from either of the two ways it is stated.

        ``Crawl-delay`` is the widely-implemented non-standard directive;
        ``Request-rate`` is the other spelling. Both are read so a site using
        either is honoured, and neither is invented when absent.

        Read from the groups resolved above rather than through
        ``parser.crawl_delay`` for the same reason the rules are: the
        library's lookup is version-dependent, and a delay that silently
        disappears on one interpreter is the platform pacing itself faster
        than the site asked. The first stated directive wins, in file order,
        which is what the single lookups did.
        """
        for group in groups:
            delay = getattr(group, "delay", None)
            if delay:
                return float(delay)
        for group in groups:
            rate = getattr(group, "req_rate", None)
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

        # The library's reading is superseded wherever the applicable group's
        # rules have an answer under RFC 9309 precedence — see the module
        # docstring, which states both directions and why neither is a bypass.
        precedence = rules_decision(cached.rules, path)
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
