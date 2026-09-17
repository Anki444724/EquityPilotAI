"""The robots.txt decision table, branch by branch.

Robots is the one control in this layer that a *server* can use to refuse the
platform, so its failure mode is asymmetric: wrongly allowing a page means
crawling something a site asked not to be crawled, wrongly refusing means
losing evidence that was available. Every branch of the table documented in
`app/services/web/robots.py` is pinned here, including the ones where the
honest answer is "no policy could be read, so do not fetch".

No network: every fetch is injected.
"""
from __future__ import annotations

import pytest

from app.services.web.robots import RobotsPolicy, RobotsStatus

DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ALLOW_ALL = "User-agent: *\nDisallow: /private\n"
AGENT_SPECIFIC = (
    "User-agent: *\nDisallow: /\n\n"
    "User-agent: EquityPilotAI\nAllow: /investors\nDisallow: /investors/private\n"
)
CRAWL_DELAY = "User-agent: *\nCrawl-delay: 7\nDisallow: /private\n"


def _policy(responses, *, user_agent="EquityPilotAI", **kwargs):
    """A policy whose transport answers from ``responses`` keyed by host."""
    calls: list[str] = []

    def fetch(url, *, timeout, max_bytes, user_agent):  # noqa: A002
        calls.append(url)
        host = url.split("//", 1)[1].split("/", 1)[0]
        answer = responses.get(host)
        if answer is None:
            raise OSError("no such host")
        if isinstance(answer, Exception):
            raise answer
        return answer

    policy = RobotsPolicy(
        fetch=fetch, user_agent=user_agent, max_bytes=1024, clock=lambda: 0.0,
        **kwargs,
    )
    return policy, calls


# ===========================================================================
class TestPolicyBranches:
    def test_disallow_all_refuses_every_path(self):
        policy, _ = _policy({"a.example": (200, DISALLOW_ALL.encode())})
        assert policy.allowed("https://a.example/investors") is False

    def test_an_explicit_allow_for_our_agent_wins_over_the_wildcard(self):
        """RFC 9309: the group naming us applies instead of the wildcard group.

        Which is why ``/other`` is permitted here and not blocked by the
        wildcard group's ``Disallow: /``: a group that names this crawler is
        the site speaking to it directly, and the specification says the
        wildcard group no longer applies. Reading the two together would
        discard the allowance the site wrote for exactly this case.
        """
        policy, _ = _policy({"b.example": (200, AGENT_SPECIFIC.encode())})
        assert policy.allowed("https://b.example/investors") is True
        assert policy.allowed("https://b.example/investors/private/x") is False
        assert policy.allowed("https://b.example/other") is True

    def test_a_404_means_there_is_no_policy_and_the_page_may_be_fetched(self):
        policy, _ = _policy({"c.example": (404, b"")})
        outcome = policy.outcome_for("https://c.example/x")
        assert outcome.allowed is True
        assert outcome.status is RobotsStatus.ABSENT

    def test_a_410_is_treated_as_absent_too(self):
        policy, _ = _policy({"d.example": (410, b"")})
        assert policy.allowed("https://d.example/x") is True

    def test_a_5xx_is_a_complete_disallow(self):
        """RFC 9309 says assume disallow when the policy is unreachable."""
        policy, _ = _policy({"e.example": (503, b"")})
        outcome = policy.outcome_for("https://e.example/x")
        assert outcome.allowed is False
        assert outcome.status is RobotsStatus.POLICY_UNAVAILABLE

    def test_a_429_is_a_disallow_not_a_retry_later(self):
        policy, _ = _policy({"f.example": (429, b"")})
        assert policy.allowed("https://f.example/x") is False

    def test_an_unexpected_4xx_is_a_disallow(self):
        policy, _ = _policy({"g.example": (403, b"")})
        assert policy.allowed("https://g.example/x") is False

    def test_a_redirect_is_not_followed_and_counts_as_unavailable(self):
        policy, _ = _policy({"h.example": (301, b"")})
        outcome = policy.outcome_for("https://h.example/x")
        assert outcome.allowed is False
        assert "redirect" in outcome.reason

    def test_an_oversized_policy_is_unavailable_rather_than_truncated(self):
        """A partial policy file is a different policy, so it is not parsed."""
        policy, _ = _policy({"i.example": (200, b"User-agent: *\n" + b"#" * 4096)})
        outcome = policy.outcome_for("https://i.example/x")
        assert outcome.allowed is False
        assert outcome.status is RobotsStatus.POLICY_UNAVAILABLE

    def test_a_transport_failure_is_a_disallow(self):
        policy, _ = _policy({"j.example": OSError("connection refused")})
        assert policy.allowed("https://j.example/x") is False

    def test_an_empty_policy_file_allows_everything(self):
        policy, _ = _policy({"k.example": (200, b"")})
        assert policy.allowed("https://k.example/x") is True

    def test_a_url_without_a_host_is_refused(self):
        policy, _ = _policy({})
        assert policy.allowed("/relative/path") is False


# ===========================================================================
class TestRulePrecedence:
    """RFC 9309 longest-match, which the standard library does not implement.

    ``RobotFileParser`` resolves a path with the first matching rule; the
    specification resolves it with the longest. Where the two differ the
    specification governs, and the cases below are the ones where it shows.
    """

    def test_a_longer_disallow_beats_an_earlier_allow(self):
        """The permissive deviation, which is the one that matters."""
        policy, _ = _policy({
            "u.example": (
                200, b"User-agent: *\nAllow: /investors\n"
                     b"Disallow: /investors/private\n",
            )
        })
        assert policy.allowed("https://u.example/investors") is True
        assert policy.allowed("https://u.example/investors/private/x") is False

    def test_a_longer_allow_beats_a_disallow(self):
        """The site explicitly allowed this path; first-match would refuse it."""
        policy, _ = _policy({
            "v.example": (
                200, b"User-agent: *\nDisallow: /a\nAllow: /a/public\n",
            )
        })
        assert policy.allowed("https://v.example/a/public/x") is True
        assert policy.allowed("https://v.example/a/other") is False

    def test_the_common_disallow_everything_then_allow_the_ir_page_shape(self):
        """``Disallow: /`` plus an explicit ``Allow`` is how sites publish IR."""
        policy, _ = _policy({
            "w.example": (
                200, b"User-agent: *\nDisallow: /\nAllow: /investors\n",
            )
        })
        assert policy.allowed("https://w.example/investors") is True
        assert policy.allowed("https://w.example/investors/annual-report") is True
        assert policy.allowed("https://w.example/private") is False

    def test_a_wildcard_rule_is_left_to_the_library(self):
        """Guessing at wildcard semantics could widen the decision."""
        policy, _ = _policy({
            "x.example": (
                200, b"User-agent: *\nAllow: /docs\nDisallow: /docs/*.pdf\n",
            )
        })
        assert policy.allowed("https://x.example/docs/x.pdf") is True

    def test_a_named_group_replaces_the_wildcard_group(self):
        """A merged reading would let a wildcard Allow widen our named group."""
        policy, _ = _policy({
            "y.example": (
                200, b"User-agent: *\nAllow: /a/public\n\n"
                     b"User-agent: EquityPilotAI\nDisallow: /a\n",
            )
        })
        assert policy.allowed("https://y.example/a/public/x") is False
        assert policy.allowed("https://y.example/a") is False

    def test_precedence_is_silent_when_no_plain_rule_matches(self):
        from app.services.web.robots import precedence_decision

        assert precedence_decision((), (), "/anything") is None
        assert precedence_decision(("/a",), (), "/b") is None
        assert precedence_decision((), ("/a",), "/b") is None

    def test_precedence_tie_break_permits_as_the_specification_says(self):
        from app.services.web.robots import precedence_decision

        assert precedence_decision(("/x",), ("/x",), "/x") is True
        assert precedence_decision(("/a/b",), ("/a",), "/a/b") is True
        assert precedence_decision(("/a",), ("/a/b",), "/a/b/c") is False


# ===========================================================================
class TestCaching:
    def test_a_second_question_about_a_host_does_not_refetch(self):
        policy, calls = _policy({"l.example": (200, ALLOW_ALL.encode())})
        policy.allowed("https://l.example/a")
        policy.allowed("https://l.example/b")
        assert calls == ["https://l.example/robots.txt"]

    def test_a_refusal_is_cached_as_long_as_an_allow(self):
        """Re-asking a site that just said no is the behaviour to prevent."""
        policy, calls = _policy({"m.example": (200, DISALLOW_ALL.encode())})
        assert policy.allowed("https://m.example/x") is False
        assert policy.allowed("https://m.example/y") is False
        assert len(calls) == 1

    def test_clearing_the_cache_refetches(self):
        policy, calls = _policy({"n.example": (200, ALLOW_ALL.encode())})
        policy.allowed("https://n.example/x")
        policy.clear()
        policy.allowed("https://n.example/x")
        assert len(calls) == 2

    def test_a_different_host_is_a_different_decision(self):
        policy, calls = _policy({
            "o.example": (200, DISALLOW_ALL.encode()),
            "p.example": (200, ALLOW_ALL.encode()),
        })
        assert policy.allowed("https://o.example/x") is False
        assert policy.allowed("https://p.example/x") is True
        assert len(calls) == 2


# ===========================================================================
class TestCrawlDelay:
    def test_a_stated_crawl_delay_is_reported(self):
        policy, _ = _policy({"q.example": (200, CRAWL_DELAY.encode())})
        assert policy.crawl_delay_for("https://q.example/x") == 7.0

    def test_no_stated_delay_is_none_rather_than_zero(self):
        """``None`` means "the site did not ask"; the fetcher applies its floor."""
        policy, _ = _policy({"r.example": (200, ALLOW_ALL.encode())})
        assert policy.crawl_delay_for("https://r.example/x") is None

    def test_a_request_rate_is_read_as_the_delay_it_states(self):
        """The other spelling of the same directive, honoured, not invented."""
        policy, _ = _policy({
            "s.example": (200, b"User-agent: *\nRequest-rate: 1/10\nDisallow: /x\n")
        })
        assert policy.crawl_delay_for("https://s.example/y") == 10.0

    def test_a_malformed_request_rate_yields_no_delay(self):
        policy, _ = _policy({
            "t.example": (200, b"User-agent: *\nRequest-rate: soon\nDisallow: /x\n")
        })
        assert policy.crawl_delay_for("https://t.example/y") is None
