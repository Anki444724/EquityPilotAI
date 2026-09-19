"""Phase-2 discovery: bounded same-host BFS, and what it refuses.

The pipeline under test is the contract documented in
`app/services/web/discovery.py`: every security control is delegated to the
injected Phase-1 `WebFetcher` (safety, robots, politeness, redirects, the
streamed byte cap), and the crawler adds only *selection* — which URLs are
candidates — plus the hardening set the review produced (H1 encoded
delimiters, M1/M2 fail-closed flags, M3/F-3 byte-ceiling coherence, F-1/F-2
rejections before any network activity).

No network: the transport, the robots fetch, the resolver and the politeness
clock/sleep are all injected, and every collaborator records what it was
asked for — which is how "refused before DNS" is an assertion here, not an
adjective.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from app.domain.web.types import (
    CITATION_KEY_PATTERN,
    WebFetchPolicy,
    WebRejectionReason,
    WebSourceClass,
)
from app.services.web.discovery import (
    CrawlPolicyError,
    CrawlRejectionReason,
    CrawlSeed,
    WebCrawlerDiscovery,
    seed_from_url,
)
from app.services.web.fetcher import (
    HostPoliteness,
    TransportResponse,
    WebFetcher,
)
from app.services.web.robots import RobotsPolicy
from app.services.web.safety import UrlSafetyPolicy

HOST = "www.acme.example"


def full(url_path: str) -> str:
    return f"https://{HOST}{url_path}"


SEED = full("/investors")


def page(title: str, text: str, links: tuple[str, ...] = ()) -> bytes:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    body = " ".join(text.split())
    if len(body) < 260:  # keep the quality floor out of unrelated assertions
        body = body.ljust(260, "x")
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<main><p>{body}</p>{anchors}</main></body></html>"
    ).encode()


ROBOTS_ALLOW = "User-agent: *\nDisallow: /private\n"
NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)


class RecordingFetcherWorld:
    """The whole fetch side of a crawl, with every collaborator counted."""

    def __init__(
        self, responses: dict[str, object], *, policy: WebFetchPolicy | None = None,
        robots_body: str = ROBOTS_ALLOW, robots_status: int = 200,
        addresses: tuple[str, ...] = ("93.184.216.34",),
    ) -> None:
        self.responses = dict(responses)
        self.transport_calls: list[str] = []
        self.robots_calls: list[str] = []
        self.dns_calls: list[str] = []
        self.sleeps: list[float] = []

        world = self

        class _Transport:
            def get(self, url, *, headers, timeout, max_bytes):
                world.transport_calls.append(url)
                answer = world.responses.get(url)
                if answer is None:
                    raise AssertionError(f"unexpected transport call for {url}")
                if isinstance(answer, Exception):
                    raise answer
                assert isinstance(answer, TransportResponse)
                return answer

        def robots_fetch(url, *, timeout, max_bytes, user_agent):
            world.robots_calls.append(url)
            return robots_status, robots_body.encode()

        def resolver(name, port):
            world.dns_calls.append(name)
            return list(addresses)

        self.transport = _Transport()
        policy = policy or WebFetchPolicy()
        self.robots = RobotsPolicy(fetch=robots_fetch, user_agent="EquityPilotAI")
        self.safety = UrlSafetyPolicy(allowed_hosts=(HOST,), resolver=resolver)
        self.policy = policy
        self.fetcher = WebFetcher(
            policy=policy,
            safety=self.safety,
            robots=self.robots,
            transport=self.transport,
            politeness=HostPoliteness(
                default_delay=0.0, sleep=self.sleeps.append, clock=lambda: 0.0,
            ),
            now=lambda: NOW,
            sleep=self.sleeps.append,
        )

    def ok(self, url: str, body: bytes, *, content_type="text/html; charset=utf-8",
           final_url: str | None = None, status: int = 200) -> None:
        self.responses[url] = TransportResponse(
            status_code=status,
            headers={"content-type": content_type,
                     "content-length": str(len(body))},
            content=body,
            final_url=final_url or url,
            elapsed_ms=5.0,
            truncated=False,
            peer_address=("93.184.216.34", 443),
        )

    def redirect(self, url: str, location: str) -> None:
        self.responses[url] = TransportResponse(
            status_code=302,
            headers={
                "content-type": "text/html; charset=utf-8",
                "location": location,
                "content-length": "0",
            },
            content=b"",
            final_url=url,
            elapsed_ms=1.0,
            truncated=False,
            peer_address=("93.184.216.34", 443),
        )


def make_crawler(
    responses: dict[str, object], **world_kwargs,
) -> tuple[WebCrawlerDiscovery, RecordingFetcherWorld]:
    world = RecordingFetcherWorld(responses, **world_kwargs)
    discovery = WebCrawlerDiscovery(fetcher=world.fetcher)
    return discovery, world


def crawl(
    seed: CrawlSeed, responses: dict[str, object], **world_kwargs,
) -> tuple[object, RecordingFetcherWorld]:
    discovery, world = make_crawler(responses, **world_kwargs)
    return discovery.crawl(seed), world


# ===========================================================================
# Seed construction — F-1, and the checks every entry point shares
# ===========================================================================
class TestSeedConstruction:
    def test_a_clean_seed_canonicalizes_the_url(self):
        seed = seed_from_url(f"https://{HOST}/investors?utm_source=nl#q3")
        assert seed.url == full("/investors")  # tracking + fragment dropped

    @pytest.mark.parametrize("encoded", ["%2e", "%2E", "%2f", "%2F", "%5c", "%5C"])
    def test_encoded_path_delimiters_are_rejected_before_any_activity(
        self, encoded,
    ):
        # F-1: the rejection is pure string work; no collaborator exists for
        # it to have touched.
        world = RecordingFetcherWorld({})
        with pytest.raises(CrawlPolicyError) as exc_info:
            seed_from_url(f"https://{HOST}/a{encoded}b")
        assert exc_info.value.reason is CrawlRejectionReason.MALFORMED_HREF
        assert encoded in str(exc_info.value)  # preserved as written
        assert world.transport_calls == []
        assert world.robots_calls == []
        assert world.dns_calls == []

    @pytest.mark.parametrize("bad", [
        "/relative/path", "ftp://example.com/x", "https:///no-host", "",
    ])
    def test_non_http_or_relative_seeds_are_rejected(self, bad):
        with pytest.raises(CrawlPolicyError):
            seed_from_url(bad)

    def test_query_encoded_delimiters_are_not_the_rule(self):
        # The rule is about paths; a query still faces the zero default
        # budget, but a seed query is the caller's own URL, not a discovery.
        seed = seed_from_url(f"https://{HOST}/investors?file=a%2Fb")
        assert seed.url == full("/investors?file=a%2Fb")

    def test_seed_from_url_cannot_express_unsafe_flags(self):
        import inspect

        params = inspect.signature(seed_from_url).parameters
        assert "same_host_only" not in params
        assert "respect_robots" not in params


# ===========================================================================
# Bounded BFS
# ===========================================================================
class TestBoundedBreadthFirstWalk:
    def test_links_are_walked_depth_first_in_breadth_order(self):
        world = RecordingFetcherWorld({})
        world.ok(SEED, page("Investors", "Acme investors hub.", ("/about",)))
        world.ok(full("/about"), page("About", "About Acme.", ("/contact",)))
        world.ok(full("/contact"), page("Contact", "Contact Acme.", ()))
        seed = CrawlSeed(url=SEED, max_depth=2, include_sitemap=False)
        report, world = crawl(seed, world.responses)

        fetched = [u for u in world.transport_calls]
        assert fetched == [SEED, full("/about"), full("/contact")]
        assert {p.url for p in report.pages} == {
            SEED, full("/about"), full("/contact"),
        }
        assert report.truncated is False

    def test_max_depth_stops_expansion_not_just_crawling(self):
        world = RecordingFetcherWorld({})
        world.ok(SEED, page("Investors", "hub.", ("/about",)))
        world.ok(full("/about"), page("About", "about.", ("/contact",)))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        report, world = crawl(seed, world.responses)

        assert world.transport_calls == [SEED, full("/about")]
        assert full("/contact") not in world.transport_calls

    def test_max_pages_caps_attempts_including_refusals(self):
        links = tuple(full(f"/p{i}") for i in range(20))
        seed = CrawlSeed(url=SEED, max_pages=3, max_depth=1, include_sitemap=False)
        report, world = crawl(
            seed, {SEED: None, **{u: None for u in links}},
        )
        # The transport is a table that raises on unknown entries; a crawl
        # that respects the budget must only ever ask for what we provide.
        assert report.pages_fetched <= 3

    def test_page_budget_bounds_total_request_count(self):
        links = tuple(f"/p{i}" for i in range(20))
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", links))
        for href in links:
            w.ok(full(href), page(
                href, "leaf page body text long enough to clear the quality "
                      "floor for accepted evidence pages in this package.", ()))
        seed = CrawlSeed(url=SEED, max_pages=3, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert len(w.transport_calls) == 3  # seed + two pages
        assert report.truncated is True
        assert report.links_enqueued == 20

    def test_per_page_fan_out_is_capped(self):
        links = tuple(f"/p{i}" for i in range(100))
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", links))
        for href in links:
            w.ok(full(href), page(href, "body.", ()))
        seed = CrawlSeed(
            url=SEED, max_pages=1, max_depth=1, max_links_per_page=5,
            include_sitemap=False,
        )
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)
        assert report.links_enqueued == 5


# ===========================================================================
# Enqueue-time dedup and canonicalization
# ===========================================================================
class TestDeduplicationAndCanonicalization:
    def test_one_page_is_fetched_once_however_it_is_linked(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page(
            "Seed", "hub.",
            ("/news", "/news#top", "/news?utm_source=x", "/news/"),
        ))
        w.ok(full("/news"), page("/news", "news body.", ("/news?utm_source=y",)))
        w.ok(full("/news/"), page("news slash", "different page body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert w.transport_calls.count(full("/news")) == 1
        # "/news" and "/news/" are *different* pages: the canonicalizer keeps
        # the path verbatim, and dedup must not get cleverer than the server.
        assert full("/news/") in w.transport_calls
        assert report.deduplicated >= 2

    def test_tracking_parameters_never_spend_the_query_budget(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/x?utm_source=a", "/x?gclid=b", "/x")))
        w.ok(full("/x"), page("X", "x body.", ()))
        seed = CrawlSeed(
            url=SEED, max_depth=1, include_sitemap=False, max_query_urls=0,
        )
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert w.transport_calls == [SEED, full("/x")]  # sitemap probe off
        assert report.refusals == ()  # canonicalised to the same clean URL


# ===========================================================================
# Strict same-host (and M1)
# ===========================================================================
class TestStrictSameHost:
    def test_links_to_other_hosts_are_refused_not_fetched(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page(
            "Seed", "hub.",
            ("https://evil.example/x", "http://acme.example/y", "https://other.acme.example/z"),
        ))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        offsite = [r for r in report.refusals
                   if r.reason is CrawlRejectionReason.OFFSITE_LINK]
        assert len(offsite) == 3
        assert not any("evil" in u or "other." in u or u.startswith("http://")
                       for u in w.transport_calls)

    def test_apex_is_not_www_even_though_the_pinned_allowlist_may_allow_it(self):
        # Phase 1's *safety* policy matches label boundaries; the crawler's
        # BFS rule is stricter: exactly this host. The link is refused even
        # though the fetcher would have allowed the hop.
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("https://acme.example/x",)))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        offsite = [r for r in report.refusals
                   if r.reason is CrawlRejectionReason.OFFSITE_LINK]
        assert len(offsite) == 1
        assert offsite[0].url == "https://acme.example/x"
        assert "https://acme.example/x" not in w.transport_calls

    def test_same_host_only_false_fails_closed_without_any_activity(self):
        # M1: the flag is refused, not honoured.
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        object.__setattr__(seed, "same_host_only", False)  # frozen-seed bypass
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        with pytest.raises(CrawlPolicyError):
            discovery.crawl(seed)
        assert w.transport_calls == []
        assert w.robots_calls == []
        assert w.dns_calls == []


# ===========================================================================
# Robots — M2, and the delegation to the single evaluator
# ===========================================================================
class TestRobots:
    def test_disallowed_paths_never_reach_the_transport(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/private/minutes", "/public/news")))
        w.ok(full("/public/news"), page("News", "news body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert full("/private/minutes") not in w.transport_calls
        refusal = next(r for r in report.refusals
                       if r.url == full("/private/minutes"))
        assert refusal.reason is WebRejectionReason.ROBOTS_DISALLOWED

    def test_a_host_with_unreadable_robots_yields_no_pages(self):
        w = RecordingFetcherWorld({}, robots_status=503)
        w.ok(SEED, page("Seed", "hub.", ()))
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)
        assert report.pages == ()
        assert report.refusals[0].reason is WebRejectionReason.ROBOTS_DISALLOWED

    def test_respect_robots_false_fails_closed(self):
        # M2
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        object.__setattr__(seed, "respect_robots", False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        with pytest.raises(CrawlPolicyError):
            discovery.crawl(seed)
        assert w.transport_calls == []
        assert w.robots_calls == []


# ===========================================================================
# H1 — encoded path delimiters, as written
# ===========================================================================
class TestEncodedPathDelimiters:
    @pytest.mark.parametrize("href", [
        "/%2e%2e/admin", "/down%2floads/report.pdf", "/back%5cslash/x",
        "./%2E/", "%2f leading", "/mixed/a%2Fb%5cc%2Ed",
    ])
    def test_discovered_links_are_refused_as_written_never_decoded(self, href):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", (href, "/ok")))
        w.ok(full("/ok"), page("OK", "ok body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        malformed = [r for r in report.refusals
                     if r.reason is CrawlRejectionReason.MALFORMED_HREF]
        assert len(malformed) == 1
        lowered = malformed[0].url.lower()
        assert any(tok in lowered for tok in ("%2e", "%2f", "%5c"))  # untouched
        assert not any(
            any(tok in u.lower() for tok in ("%2e", "%2f", "%5c"))
            for u in w.transport_calls
        )
        assert full("/ok") in w.transport_calls

    def test_direct_construction_cannot_skip_the_seed_check(self):
        # The F-1 bypass fix: a caller who builds CrawlSeed directly gets the
        # same rejection from crawl() — before DNS, robots or transport.
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        seed = CrawlSeed(url=full("/bad%2fpath"), include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        with pytest.raises(CrawlPolicyError) as exc_info:
            discovery.crawl(seed)
        assert exc_info.value.reason is CrawlRejectionReason.MALFORMED_HREF
        assert w.transport_calls == []
        assert w.robots_calls == []
        assert w.dns_calls == []

    def test_sitemap_loc_paths_are_checked_too(self):
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?><urlset>'
            "<url><loc>https://www.acme.example/a%2fb.html</loc></url>"
            "<url><loc>https://www.acme.example/ok.html</loc></url>"
            "</urlset>"
        ).encode()
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        w.ok(full("/sitemap.xml"), sitemap, content_type="text/plain; charset=utf-8")
        w.ok(full("/ok.html"), page("OK", "ok body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=0, include_sitemap=True)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert any(r.reason is CrawlRejectionReason.MALFORMED_HREF
                   and "%2f" in r.url for r in report.refusals)
        assert full("/a%2fb.html") not in w.transport_calls
        assert full("/ok.html") in w.transport_calls


# ===========================================================================
# F-2 — the landing URL of a redirect
# ===========================================================================
class TestRedirectLanding:
    def test_a_redirect_onto_an_encoded_path_is_refused_before_acceptance(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.redirect(SEED, full("/report%2epdf"))
        w.ok(full("/report%2epdf"), page("R", "report body.", ("/trap",)),
             final_url=full("/report%2epdf"))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert report.pages == ()
        refusal = next(r for r in report.refusals if r.source == "redirect")
        assert refusal.reason is CrawlRejectionReason.MALFORMED_HREF
        assert "%2e" in refusal.url          # as reported, not decoded
        assert "%2e" in refusal.detail        # diagnostic preserves the URL
        # Accepted → link expansion did not happen either:
        assert full("/trap") not in w.transport_calls

    def test_a_clean_redirect_is_accepted_and_walked(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.redirect(SEED, full("/investors/"))
        w.ok(full("/investors/"), page("I", "investor hub.", ("/ir-one",)))
        w.ok(full("/ir-one"), page("one", "one body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert report.pages[0].final_url == full("/investors/")
        assert full("/ir-one") in w.transport_calls

    def test_links_are_not_expanded_from_a_cross_host_landing_page(self):
        # A redirect that lands on another host was validated per hop by the
        # fetcher's safety policy, so the page is citable — but expanding its
        # links would let one pinned host launder the whole web into the
        # crawl. The crawler accepts the page and declines to walk it.
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.responses[SEED] = TransportResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=page("Seed", "hub.", ("https://cdn.example/more",)),
            final_url="https://cdn.example/mirror",
            peer_address=("93.184.216.34", 443),
            elapsed_ms=1.0, truncated=False,
        )
        w.ok("https://cdn.example/more", page("more", "body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=2, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert len(report.pages) == 1  # the mirror page is still citable
        # `WebDocumentRef.host` keeps the Phase-1 fetcher semantics: the host
        # of the URL that was *requested*. The landing is visible where
        # Phase-1 put it — in `final_url` — and it is that mismatch the
        # crawler uses to refuse link expansion.
        assert report.pages[0].final_url == "https://cdn.example/mirror"
        assert report.pages[0].host == HOST
        assert "https://cdn.example/more" not in w.transport_calls


# ===========================================================================
# Byte ceilings — M3 and F-3
# ===========================================================================
class TestByteCeilings:
    def test_a_ceiling_above_the_injected_fetcher_is_refused_at_construction(self):
        fetcher_world = RecordingFetcherWorld(
            {}, policy=WebFetchPolicy(max_bytes=1024),
        )
        with pytest.raises(CrawlPolicyError):
            WebCrawlerDiscovery(fetcher=fetcher_world.fetcher, max_bytes=4096)

    def test_a_ceiling_equal_to_the_fetchers_is_admitted(self):
        fetcher_world = RecordingFetcherWorld(
            {}, policy=WebFetchPolicy(max_bytes=1024),
        )
        discovery = WebCrawlerDiscovery(
            fetcher=fetcher_world.fetcher, max_bytes=1024,
        )
        assert discovery.effective_max_bytes() == 1024

    def test_crawl_revalidates_the_live_ceiling_after_a_swap(self):
        # F-3: construction-time equality does not entitle a crawler to a
        # larger ceiling when the collaborator changes underneath it.
        big = RecordingFetcherWorld(
            {}, policy=WebFetchPolicy(max_bytes=1_000_000),
        )
        small = RecordingFetcherWorld(
            {}, policy=WebFetchPolicy(max_bytes=64 * 1024),
        )
        discovery = WebCrawlerDiscovery(fetcher=big.fetcher, max_bytes=1_000_000)
        seed = CrawlSeed(url=SEED, include_sitemap=False)

        big.ok(SEED, page("Seed", "hub.", ()))
        discovery.crawl(seed)  # fine while the big fetcher is wired in

        discovery.fetcher = small.fetcher
        with pytest.raises(CrawlPolicyError):
            discovery.crawl(seed)  # refused before the first hop of crawl #2
        assert small.transport_calls == []

    def test_a_smaller_crawler_ceiling_rejects_after_fetching(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "x" * 6000, ()))  # over the crawler ceiling
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher, max_bytes=2048)
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        report = discovery.crawl(seed)

        assert report.pages == ()
        assert report.refusals[0].reason is WebRejectionReason.TOO_LARGE


# ===========================================================================
# Sitemap discovery — bounded, non-recursive, and optional
# ===========================================================================
class TestSitemapDiscovery:
    def test_urlset_entries_are_enqueued_as_depth_zero_seeds(self):
        sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://www.acme.example/a.html</loc>"
            "<lastmod>2026-01-01</lastmod></url>"
            "<url><loc>https://www.acme.example/b.html</loc></url>"
            "</urlset>"
        ).encode()
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        w.ok(full("/sitemap.xml"), sitemap, content_type="text/plain; charset=utf-8")
        w.ok(full("/a.html"), page("A", "a body.", ()))
        w.ok(full("/b.html"), page("B", "b body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=0, include_sitemap=True,
                         max_pages=4)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert report.sitemap_status == "ok"
        assert {p.url for p in report.pages} == {
            SEED, full("/a.html"), full("/b.html"),
        }
        assert report.links_enqueued == 2

    def test_a_sitemapindex_is_recorded_and_not_recursed(self):
        index = (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://www.acme.example/sm-2024.xml</loc></sitemap>"
            "<sitemap><loc>https://www.acme.example/sm-2025.xml</loc></sitemap>"
            "</sitemapindex>"
        ).encode()
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        w.ok(full("/sitemap.xml"), index, content_type="text/plain; charset=utf-8")
        w.ok(full("/sm-2024.xml"), b"<urlset/>", content_type="text/plain; charset=utf-8")
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=True)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert report.sitemap_status == "index"
        skipped = [r for r in report.refusals
                   if r.reason is CrawlRejectionReason.CHILD_SITEMAP_SKIPPED]
        assert len(skipped) == 2
        assert full("/sm-2024.xml") not in w.transport_calls
        assert full("/sm-2025.xml") not in w.transport_calls

    def test_a_missing_sitemap_does_not_cancel_the_crawl(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/x",)))
        w.ok(full("/x"), page("X", "x body.", ()))
        w.responses[full("/sitemap.xml")] = TransportResponse(
            status_code=404, headers={"content-type": "text/plain"},
            content=b"", final_url=full("/sitemap.xml"),
            peer_address=("93.184.216.34", 443), elapsed_ms=1.0, truncated=False,
        )
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=True)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert report.sitemap_status == "unavailable"
        assert any(r.reason is CrawlRejectionReason.SITEMAP_UNAVAILABLE
                   for r in report.refusals)
        assert full("/x") in w.transport_calls

    def test_the_sitemap_entry_cap_is_enforced(self):
        locs = "".join(
            f"<url><loc>https://www.acme.example/p{i}.html</loc></url>"
            for i in range(30)
        )
        sitemap = f"<urlset>{locs}</urlset>".encode()
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        w.ok(full("/sitemap.xml"), sitemap, content_type="text/plain; charset=utf-8")
        for i in range(30):
            w.ok(full(f"/p{i}.html"), page(f"p{i}", "body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=0, include_sitemap=True,
                         max_sitemap_urls=2, max_pages=4)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        assert report.links_enqueued == 2
        assert any(r.reason is CrawlRejectionReason.SITEMAP_CAP_REACHED
                   and "28" in r.detail for r in report.refusals)

    def test_relative_loc_entries_are_refused_as_written(self):
        sitemap = b'<urlset><url><loc>/relative.html</loc></url></urlset>'
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        w.ok(full("/sitemap.xml"), sitemap, content_type="text/plain; charset=utf-8")
        seed = CrawlSeed(url=SEED, max_depth=0, include_sitemap=True)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)
        assert any(r.reason is CrawlRejectionReason.SITEMAP_LOC_MALFORMED
                   and r.url == "/relative.html" for r in report.refusals)

    def test_the_probe_consumes_page_budget(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        w.ok(full("/sitemap.xml"), b"<urlset/>", content_type="text/plain; charset=utf-8")
        seed = CrawlSeed(url=SEED, max_pages=1, include_sitemap=True)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        # One unit of budget, spent; the seed itself never gets fetched.
        assert report.pages_fetched == 1
        assert w.transport_calls == [full("/sitemap.xml")]
        assert report.truncated is True


# ===========================================================================
# Query-explosion prevention
# ===========================================================================
class TestQueryBudget:
    def test_query_urls_are_queued_until_the_budget_is_spent(self):
        links = tuple(f"/archive?page={n}" for n in range(1, 6))
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", links))
        for href in links:
            w.ok(full(href), page(href, "archive body.", ()))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False,
                         max_query_urls=2)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        query_fetches = [u for u in w.transport_calls if "?" in u]
        assert len(query_fetches) == 2
        exhausted = [r for r in report.refusals
                     if r.reason is CrawlRejectionReason.QUERY_BUDGET_EXCEEDED]
        assert len(exhausted) == 3

    def test_the_zero_default_refuses_every_query_url(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/calendar?month=1", "/calendar?month=2")))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)
        assert w.transport_calls == [SEED]
        assert len([r for r in report.refusals
                    if r.reason is CrawlRejectionReason.QUERY_BUDGET_EXCEEDED]) == 2


# ===========================================================================
# Quality, dedup of content, and what an accepted page looks like
# ===========================================================================
class TestAcceptanceAndQuality:
    def test_accepted_pages_carry_phase_one_refs_and_no_document_id(self):
        body = ("Acme reported revenue of 1,234 crore for FY2026, with an "
                "operating margin of 18.4 per cent, and reaffirmed guidance "
                "for the coming year in its investor deck presented to the "
                "exchange last quarter of the fiscal cycle ahead.")
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, (f"<html><head><title>Acme — Investors</title></head>"
                    f"<body><main><p>{body}</p></main></body></html>").encode()),
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)

        (ref,) = report.pages
        assert ref.document_id is None  # no persistence inside the crawler
        assert ref.title == "Acme — Investors"
        assert ref.published_at is None  # never invented
        assert ref.author is None
        assert ref.retrieved_at == NOW
        assert ref.status_code == 200
        assert len(ref.content_hash) == 64
        assert ref.size_bytes > 0
        assert CITATION_KEY_PATTERN.fullmatch(f"[{ref.citation_key()}]")
        assert ref.preview and len(ref.preview) <= 600

    def test_source_class_flows_from_pinned_records_not_page_claims(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        seed = CrawlSeed(url=SEED, include_sitemap=False,
                         company_hosts=(HOST,))
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        report = discovery.crawl(seed)
        assert report.pages[0].source_class is WebSourceClass.COMPANY_WEBSITE

        w2 = RecordingFetcherWorld(w.responses)  # share the populated table
        seed2 = CrawlSeed(url=SEED, include_sitemap=False)  # nothing pinned
        report2 = WebCrawlerDiscovery(fetcher=w2.fetcher).crawl(seed2)
        assert report2.pages[0].source_class is WebSourceClass.UNKNOWN

    def test_a_regulator_origin_keeps_its_stronger_class(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.safety = UrlSafetyPolicy(
            allowed_hosts=("www.sec.gov",),
            resolver=lambda name, port: ["198.99.99.99"],
        )
        w.fetcher = WebFetcher(
            policy=w.policy, safety=w.safety, robots=w.robots,
            transport=w.transport,
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda s: None,
                                      clock=lambda: 0.0),
            now=lambda: NOW,
        )
        sec = "https://www.sec.gov/Archives/x.html"
        w.ok(sec, page("SEC", "EDGAR body.", ()))
        seed = CrawlSeed(url=sec, include_sitemap=False, company_hosts=(HOST,))
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)
        assert report.pages[0].source_class is WebSourceClass.REGULATOR

    def test_stub_pages_are_refused_below_the_quality_floor(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, b"<html><body><main><p>Too short.</p></main></body></html>")
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)
        assert report.pages == ()
        assert report.refusals[0].reason is WebRejectionReason.CONTENT_TOO_SHORT

    def test_consent_walls_are_unsupported_content_not_evidence(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "Accept cookies to continue. " * 20))
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)
        assert report.pages == ()
        assert report.refusals[0].reason is WebRejectionReason.UNSUPPORTED_CONTENT

    def test_identical_bytes_are_one_page_per_crawl(self):
        twin = (
            b"<html><head><title>Two doors</title></head><body><main><p>"
            b"The same announcement body text served at two paths, byte for "
            b"byte, so the content hash is the identity that decides here. "
            b"Repeated once more so the quality floor is out of the picture: "
            b"the duplicate check, not the stub check, is what we are pinning."
            b"</p></main></body></html>"
        )
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/dup1", "/dup2")))
        w.ok(full("/dup1"), twin)
        w.ok(full("/dup2"), twin)
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)
        dup = [r for r in report.refusals
               if r.reason is WebRejectionReason.DUPLICATE_CONTENT]
        assert len(dup) == 1
        assert "identical bytes" in dup[0].detail
        assert len(report.pages) == 2  # the seed + the *first* twin only

    def test_near_duplicates_are_refused_with_the_first_url_named(self):
        # Same letters, different punctuation and raw bytes: the sha256
        # differs, the near-duplicate key does not. The floor check passes
        # both pages, so the *only* thing that can decline the second is the
        # reuse of `quality.near_duplicate_key`.
        first = (
            "<html><head><title>P1</title></head><body><main><p>"
            + "ACME reported revenue 1,234 crore — FY2026!! " * 6
            + "</p></main></body></html>"
        ).encode()
        second = (
            "<html><head><title>P2</title></head><body><main><p>"
            + "acme (reported) revenue: 1234 crore, fy2026. " * 6
            + "</p></main></body></html>"
        ).encode()
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/p1", "/p2")))
        w.ok(full("/p1"), first)
        w.ok(full("/p2"), second)
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)

        dup = [r for r in report.refusals
               if r.reason is WebRejectionReason.DUPLICATE_CONTENT
               and "same text as" in r.detail]
        assert len(dup) == 1
        assert full("/p1") in dup[0].detail


# ===========================================================================
# Bounds by construction
# ===========================================================================
class TestBoundsAreEnforced:
    @pytest.mark.parametrize("field_name,value", [
        ("max_pages", 0), ("max_pages", CrawlSeed.MAX_PAGES_CEILING + 1),
        ("max_depth", -1), ("max_depth", CrawlSeed.MAX_DEPTH_CEILING + 1),
        ("max_links_per_page", 0),
        ("max_query_urls", -1),
        ("max_sitemap_urls", CrawlSeed.MAX_SITEMAP_URLS_CEILING + 1),
        ("max_pages", True),  # a bool is not a bound
        ("max_pages", "12"),
    ])
    def test_out_of_range_bounds_raise_at_construction(self, field_name, value):
        with pytest.raises(CrawlPolicyError):
            CrawlSeed(url=SEED, **{field_name: value})

    def test_a_mutated_bound_is_caught_at_crawl_time(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ()))
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        object.__setattr__(seed, "max_pages", 10**6)  # past every ceiling
        discovery = WebCrawlerDiscovery(fetcher=w.fetcher)
        with pytest.raises(CrawlPolicyError):
            discovery.crawl(seed)
        assert w.transport_calls == []


# ===========================================================================
# The report as data
# ===========================================================================
class TestReportShape:
    def test_report_is_json_serialisable_and_pair_shaped(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        w.ok(SEED, page("Seed", "hub.", ("/private/x",)))
        seed = CrawlSeed(url=SEED, max_depth=1, include_sitemap=False)
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)

        payload = json.loads(json.dumps(report.as_dict()))
        assert payload["refusals"][0]["reason"] == "robots_disallowed"
        assert report.rejected == ((full("/private/x"),
                                    WebRejectionReason.ROBOTS_DISALLOWED),)
        assert dict(report.details)[full("/private/x")]

    def test_empty_crawl_reports_what_it_never_started(self):
        responses: dict[str, object] = {}
        w = RecordingFetcherWorld(responses)
        # Seed fetch fails (transport would raise on missing entry):
        w.responses[SEED] = ConnectionError("socket died")
        seed = CrawlSeed(url=SEED, include_sitemap=False)
        report = WebCrawlerDiscovery(fetcher=w.fetcher).crawl(seed)
        assert report.pages == ()
        assert report.pages_fetched == 1
        assert report.refusals[0].reason is WebRejectionReason.TRANSPORT_ERROR
        assert report.truncated is False
