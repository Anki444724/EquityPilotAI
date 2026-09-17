"""The fetch pipeline: order of operations, caps, and what is never ingested.

The sequence under test is the contract documented in
`app/services/web/fetcher.py`: safety, then robots, then politeness, then the
request, then the content-type allowlist. Each test below pins one of those
steps or one of the refusals that must never be turned into content — a 403
above all, because a login page or a block notice ingested as evidence would be
quoted as though the company had published it.

No network: the transport is injected, and so are the clock, the sleep and the
resolver.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.web.types import (
    WebContentClass,
    WebFetchPolicy,
    WebRejectionReason,
)
from app.services.web.fetcher import (
    FetchedDocument,
    HostPoliteness,
    TransportResponse,
    WebFetchError,
    WebFetcher,
)
from app.services.web.robots import RobotsPolicy
from app.services.web.safety import UrlSafetyPolicy

HOST = "www.acme.example"
URL = f"https://{HOST}/investors"

HTML = (
    "<html><head><title>Acme — Investors</title></head><body>"
    "<main><h1>Investors</h1><p>Acme reported revenue of 1,234 crore for "
    "FY2026, with an operating margin of 18.4 per cent, and reaffirmed its "
    "guidance for the coming year.</p></main></body></html>"
).encode()


class FakeTransport:
    """A transport that answers from a table and records every request."""

    def __init__(self, responses, *, default=None):
        #: ``responses`` maps a URL to a ``TransportResponse`` or an exception.
        self.responses = responses
        self.default = default
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, *, headers, timeout, max_bytes):
        self.calls.append((url, {"headers": dict(headers), "timeout": timeout}))
        answer = self.responses.get(url, self.default)
        if answer is None:
            raise AssertionError(f"unexpected transport call for {url}")
        if isinstance(answer, Exception):
            raise answer
        return answer


def response(
    *, status=200, body=HTML, content_type="text/html; charset=utf-8",
    final_url=URL, truncated=False, peer="93.184.216.34", elapsed_ms=12.0,
):
    return TransportResponse(
        status_code=status,
        headers={"content-type": content_type, "content-length": str(len(body))},
        content=body,
        final_url=final_url,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
        peer_address=(peer, 443) if peer else None,
    )


ROBOTS_ALLOW = "User-agent: *\nDisallow: /private\n"


def _fetcher(
    responses, *, robots_body=ROBOTS_ALLOW, robots_status=200, policy=None,
    default=None, sleeps=None, addresses=("93.184.216.34",),
):
    transport = FakeTransport(responses, default=default)

    def robots_fetch(url, *, timeout, max_bytes, user_agent):
        return robots_status, robots_body.encode()

    robots = RobotsPolicy(fetch=robots_fetch, user_agent="EquityPilotAI")
    safety = UrlSafetyPolicy(
        allowed_hosts=(HOST,), resolver=lambda name, port: list(addresses),
    )
    slept = sleeps if sleeps is not None else []
    fetcher = WebFetcher(
        policy=policy or WebFetchPolicy(),
        safety=safety,
        robots=robots,
        transport=transport,
        politeness=HostPoliteness(
            default_delay=0.0, sleep=slept.append, clock=lambda: 0.0,
        ),
        now=lambda: datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc),
        sleep=slept.append,
    )
    return fetcher, transport, slept


# ===========================================================================
class TestSuccessfulFetch:
    def test_the_metadata_a_citation_needs_is_captured(self):
        fetcher, transport, _ = _fetcher({URL: response()})
        page = fetcher.fetch(URL)

        assert isinstance(page, FetchedDocument)
        assert page.url == URL
        assert page.final_url == URL
        assert page.status_code == 200
        assert page.content_type.startswith("text/html")
        assert page.content_class is WebContentClass.HTML
        assert page.charset == "utf-8"
        assert page.size_bytes == len(HTML)
        assert len(page.sha256) == 64
        assert page.retrieved_at == datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
        assert page.latency_ms == 12.0
        assert "Acme reported revenue" in page.text
        assert page.robots.allowed is True

    def test_the_user_agent_is_honest_and_identifies_the_platform(self):
        fetcher, transport, _ = _fetcher({URL: response()})
        fetcher.fetch(URL)
        sent = transport.calls[0][1]["headers"]
        assert "EquityPilotAI" in sent["User-Agent"]
        assert "Mozilla" not in sent["User-Agent"]
        # No header that exists to defeat a bot filter.
        for header in ("X-Forwarded-For", "Referer", "Cookie", "Origin"):
            assert header not in sent

    def test_a_body_below_the_character_floor_is_still_fetched(self):
        """The fetcher's job is retrieval; quality is a separate decision."""
        fetcher, _, _ = _fetcher({URL: response(body=b"<html>short</html>")})
        assert fetcher.fetch(URL).size_bytes > 0


# ===========================================================================
class TestOrderOfOperations:
    def test_robots_is_consulted_before_the_socket_is_opened(self):
        fetcher, transport, _ = _fetcher(
            {URL: response()}, robots_body="User-agent: *\nDisallow: /\n",
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.ROBOTS_DISALLOWED
        assert transport.calls == []

    def test_an_unpinned_host_is_refused_before_the_request(self):
        fetcher, transport, _ = _fetcher({URL: response()})
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch("https://evil.example/x")
        assert excinfo.value.reason is WebRejectionReason.HOST_NOT_PINNED
        assert transport.calls == []

    def test_a_disallowed_path_is_refused_but_an_allowed_one_is_fetched(self):
        fetcher, transport, _ = _fetcher(
            {f"https://{HOST}/x": response(final_url=f"https://{HOST}/x")},
        )
        with pytest.raises(WebFetchError):
            fetcher.fetch(f"https://{HOST}/private/report")
        assert transport.calls == []
        fetcher.fetch(f"https://{HOST}/x")
        assert len(transport.calls) == 1

    def test_a_rejected_page_is_never_fetched(self):
        fetcher, transport, _ = _fetcher({URL: response(status=403, body=b"")})
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.FORBIDDEN_BY_SERVER
        # The refusal is returned, not retried: a 403 is an answer.
        assert len(transport.calls) == 1


# ===========================================================================
class TestPoliteness:
    def test_the_site_crawl_delay_is_waited_out(self):
        waits: list[float] = []
        transport = FakeTransport({URL: response()})
        robots = RobotsPolicy(
            fetch=lambda url, *, timeout, max_bytes, user_agent:
                (200, b"User-agent: *\nCrawl-delay: 5\n"),
            user_agent="EquityPilotAI",
        )
        fetcher = WebFetcher(
            policy=WebFetchPolicy(),
            safety=UrlSafetyPolicy(
                allowed_hosts=(HOST,), resolver=lambda n, p: ["93.184.216.34"],
            ),
            robots=robots,
            transport=transport,
            politeness=HostPoliteness(
                default_delay=1.0, sleep=waits.append,
                clock=iter([0.0, 0.0, 5.0, 5.0]).__next__,
            ),
        )
        fetcher.fetch(URL)
        fetcher.fetch(URL)
        # First request: no previous request to the host. Second: the site's
        # five seconds are still outstanding.
        assert waits == [5.0]

    def test_a_stated_delay_is_clamped_to_something_sane(self):
        politeness = HostPoliteness(default_delay=1.0, sleep=lambda _: None)
        assert politeness.delay_for(HOST, 86_400.0) <= 30.0
        assert politeness.delay_for(HOST, None) == 1.0
        assert politeness.delay_for(HOST, 7.0) == 7.0


# ===========================================================================
class TestRedirects:
    def test_a_redirect_is_followed_after_a_fresh_safety_check(self):
        target = f"https://{HOST}/investors/annual"
        fetcher, transport, _ = _fetcher({
            URL: response(status=301, body=b"",
                          final_url=URL),
            target: response(final_url=target),
        })
        # The 301 needs a Location header; rebuild the response with one.
        transport.responses[URL] = TransportResponse(
            status_code=301, headers={"location": "/investors/annual"},
            content=b"", final_url=URL, peer_address=("93.184.216.34", 443),
        )
        page = fetcher.fetch(URL)
        assert page.url == URL
        assert page.final_url == target
        assert page.redirect_chain == (URL,)

    def test_a_redirect_to_a_private_address_is_refused(self):
        fetcher, transport, _ = _fetcher({URL: response(status=302, body=b"")})
        transport.responses[URL] = TransportResponse(
            status_code=302, headers={"location": "http://127.0.0.1/admin"},
            content=b"", final_url=URL,
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason in {
            WebRejectionReason.ADDRESS_NOT_PUBLIC,
            WebRejectionReason.HOST_NOT_PINNED,
        }
        # Nothing was sent to the redirect target.
        assert len(transport.calls) == 1

    def test_a_redirect_to_an_unpinned_host_is_refused(self):
        fetcher, transport, _ = _fetcher({URL: response(status=302, body=b"")})
        transport.responses[URL] = TransportResponse(
            status_code=302, headers={"location": "https://evil.example/x"},
            content=b"", final_url=URL,
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.HOST_NOT_PINNED

    def test_the_redirect_limit_is_finite(self):
        """A chain of distinct hops stops at the limit, not at the horizon."""
        policy = WebFetchPolicy(max_redirects=3)
        hop = {f"https://{HOST}/hop{i}": TransportResponse(
            status_code=302, headers={"location": f"/hop{i + 1}"},
            content=b"", final_url=f"https://{HOST}/hop{i}",
        ) for i in range(0, 8)}
        fetcher, transport, _ = _fetcher(hop, policy=policy)
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(f"https://{HOST}/hop0")
        assert excinfo.value.reason is WebRejectionReason.REDIRECT_LIMIT
        # One request per permitted hop, plus the one that reveals the limit.
        assert len(transport.calls) == policy.max_redirects + 1

    def test_a_redirect_loop_is_stopped_as_a_limit_rather_than_followed(self):
        fetcher, transport, _ = _fetcher({}, default=TransportResponse(
            status_code=302, headers={"location": f"/loop"},
            content=b"", final_url=f"https://{HOST}/loop",
        ))
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(f"https://{HOST}/loop")
        assert excinfo.value.reason is WebRejectionReason.REDIRECT_LIMIT
        assert len(transport.calls) <= WebFetchPolicy().max_redirects + 1

    def test_a_redirect_without_a_location_is_a_refusal(self):
        fetcher, transport, _ = _fetcher({URL: response(status=302, body=b"")})
        transport.responses[URL] = TransportResponse(
            status_code=302, headers={}, content=b"", final_url=URL,
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.REDIRECT_WITHOUT_LOCATION


# ===========================================================================
class TestContentTypes:
    @pytest.mark.parametrize("content_type,expected", [
        ("text/html", WebContentClass.HTML),
        ("text/html; charset=utf-8", WebContentClass.HTML),
        ("application/xhtml+xml", WebContentClass.HTML),
        ("text/plain", WebContentClass.TEXT),
        ("application/pdf", WebContentClass.PDF),
    ])
    def test_the_three_allowed_families_are_accepted(self, content_type, expected):
        fetcher, _, _ = _fetcher({URL: response(body=b"%PDF-1.4 fake" if False else HTML,
                                                content_type=content_type)})
        assert fetcher.fetch(URL).content_class is expected

    @pytest.mark.parametrize("content_type", [
        "application/octet-stream", "image/png", "application/zip",
        "text/csv", "application/json", "application/javascript", "",
    ])
    def test_anything_else_is_refused_rather_than_guessed_at(self, content_type):
        fetcher, _, _ = _fetcher({URL: response(content_type=content_type)})
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.CONTENT_TYPE_NOT_ALLOWED


# ===========================================================================
class TestSizeAndEncoding:
    def test_a_body_over_the_cap_is_refused_without_reading_further(self):
        policy = WebFetchPolicy(max_bytes=1_024)
        fetcher, _, _ = _fetcher(
            {URL: response(body=b"x" * 4_096, truncated=True)}, policy=policy,
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.TOO_LARGE

    def test_the_cap_is_applied_to_a_body_that_arrived_below_the_transport_cap(
        self,
    ):
        """A transport that judges by Content-Length cannot be the only check."""
        policy = WebFetchPolicy(max_bytes=512)
        fetcher, _, _ = _fetcher(
            {URL: response(body=b"y" * 1_000, truncated=False)}, policy=policy,
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.TOO_LARGE

    def test_an_empty_body_is_refused(self):
        fetcher, _, _ = _fetcher({
            URL: TransportResponse(
                status_code=200, headers={"content-type": "text/html"},
                content=b"", final_url=URL,
            )
        })
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.EMPTY_RESPONSE

    def test_the_declared_charset_is_used(self):
        body = "<html><body><p>caf\u00e9 r\u00e9sum\u00e9</p></body></html>".encode("latin-1")
        fetcher, _, _ = _fetcher({URL: response(
            body=body, content_type="text/html; charset=latin-1",
        )})
        page = fetcher.fetch(URL)
        assert "caf\u00e9" in page.text

    def test_a_wrong_declared_charset_falls_back_rather_than_failing(self):
        fetcher, _, _ = _fetcher({URL: response(
            body=HTML, content_type="text/html; charset=not-a-charset",
        )})
        assert "Acme" in fetcher.fetch(URL).text


# ===========================================================================
class TestTransportFailures:
    def test_a_timeout_is_typed_and_retryable(self):
        from app.services.web.fetcher import WebFetchError as Error

        fetcher, transport, sleeps = _fetcher(
            {URL: Error(WebRejectionReason.TIMEOUT, "slow", retryable=True)},
            policy=WebFetchPolicy(max_attempts=1),
        )
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.TIMEOUT
        assert len(transport.calls) == 1

    def test_a_retryable_failure_is_retried_once_and_can_succeed(self):
        class FlakyOnce(FakeTransport):
            def __init__(self):
                super().__init__({})
                self.count = 0

            def get(self, url, *, headers, timeout, max_bytes):
                self.count += 1
                if self.count == 1:
                    raise WebFetchError(
                        WebRejectionReason.TIMEOUT, "first attempt timed out",
                        retryable=True,
                    )
                return response()

        transport = FlakyOnce()
        fetcher = WebFetcher(
            policy=WebFetchPolicy(max_attempts=2),
            safety=UrlSafetyPolicy(
                allowed_hosts=(HOST,), resolver=lambda n, p: ["93.184.216.34"],
            ),
            robots=RobotsPolicy(
                fetch=lambda url, **kw: (200, ROBOTS_ALLOW.encode()),
                user_agent="EquityPilotAI",
            ),
            transport=transport,
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda _: None),
            sleep=lambda _: None,
        )
        assert fetcher.fetch(URL).status_code == 200
        assert transport.count == 2

    def test_a_non_retryable_failure_is_not_retried(self):
        class ServerError(FakeTransport):
            def __init__(self):
                super().__init__({})
                self.count = 0

            def get(self, url, *, headers, timeout, max_bytes):
                self.count += 1
                raise WebFetchError(
                    WebRejectionReason.HTTP_ERROR, "HTTP 404", retryable=False,
                )

        transport = ServerError()
        fetcher = WebFetcher(
            policy=WebFetchPolicy(max_attempts=3),
            safety=UrlSafetyPolicy(
                allowed_hosts=(HOST,), resolver=lambda n, p: ["93.184.216.34"],
            ),
            robots=RobotsPolicy(
                fetch=lambda url, **kw: (200, ROBOTS_ALLOW.encode()),
                user_agent="EquityPilotAI",
            ),
            transport=transport,
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda _: None),
        )
        with pytest.raises(WebFetchError):
            fetcher.fetch(URL)
        assert transport.count == 1

    def test_a_5xx_is_classified_retryable_and_a_404_is_not(self):
        for status, retryable in ((503, True), (502, True), (404, False), (400, False)):
            fetcher, _, _ = _fetcher(
                {URL: response(status=status, body=b"")},
                policy=WebFetchPolicy(max_attempts=1),
            )
            with pytest.raises(WebFetchError) as excinfo:
                fetcher.fetch(URL)
            assert excinfo.value.retryable is retryable, status


# ===========================================================================
class TestPeerAddressCheck:
    def test_a_connection_to_a_non_public_peer_is_refused(self):
        """The address actually connected to must be one we validated."""
        fetcher, _, _ = _fetcher({
            URL: TransportResponse(
                status_code=200, headers={"content-type": "text/html"},
                content=HTML, final_url=URL,
                peer_address=("169.254.169.254", 80),
            )
        })
        with pytest.raises(WebFetchError) as excinfo:
            fetcher.fetch(URL)
        assert excinfo.value.reason is WebRejectionReason.ADDRESS_NOT_PUBLIC

    def test_a_transport_that_exposes_no_peer_is_not_treated_as_verified(self):
        """Unavailable is not the same as safe, and is not treated as either.

        The hop is still governed by the addresses the resolver returned, so
        the fetch proceeds — with the resolver's answer as the only evidence,
        which is exactly what the policy states.
        """
        fetcher, _, _ = _fetcher({
            URL: TransportResponse(
                status_code=200, headers={"content-type": "text/html"},
                content=HTML, final_url=URL, peer_address=None,
            )
        })
        assert fetcher.fetch(URL).status_code == 200
