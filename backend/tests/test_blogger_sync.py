"""Blogger posts → the platform's existing document pipeline.

The properties under test are the ones that decide whether this integration is
safe to leave running unattended on a schedule:

* the feed is parsed the way Blogger actually serves it, including paging and
  the four links an entry carries;
* a post's identity is stable, so the same post is recognised across runs;
* company mapping resolves a name or a ticker to a whole token, never to a
  fragment of a longer word, and refuses to guess when two companies fit;
* an unchanged post writes nothing;
* a changed post becomes a new version, and the old one is superseded rather
  than deleted, so a citation issued against it still resolves;
* a post that names no company is skipped rather than filed against something;
* one bad post cannot stop the run, and an unreachable feed cannot damage the
  corpus;
* an ingested post really does become retrievable knowledge — the last test
  drives the actual document worker and the actual retrieval engine, because
  "a row was created" is not the same claim as "the chatbot can now answer
  from it".
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.domain.documents.types import DocumentStatus, DocumentType
from app.domain.platform.jobs import (
    DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES, SCHEDULES, JobKind,
)
from app.models.company import Company
from app.models.document import Document, DocumentChunk, DocumentJob
from app.services.blogger.document import (
    FILENAME_PREFIX, post_metadata, render_document, stable_filename,
)
from app.services.blogger.feed import (
    BloggerFeedClient, BloggerFeedError, iso_utc, parse_feed,
)
from app.services.blogger.mapping import (
    CompanyMapper, name_keys, normalise_name, ticker_key,
)
from app.services.blogger.sync import (
    CHANGED_ACTIONS, BloggerSyncService, main,
)
from app.services.documents.ingestion import DocumentIngestionService
from app.services.documents.storage import LocalFileStorage
from app.services.documents.worker import DocumentWorker
from tests.fixtures.blogger_feed import (
    CORPUS, FEED_URL, FakeBloggerServer, FakePost, client_for, entry, feed,
    parsed_post,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _purge(session) -> None:
    """Remove every document, chunk and job.

    The suite shares one seeded database, and `accept()` commits — so a
    rollback cannot isolate these tests. Without the purge a later test's worker
    claims an earlier test's job, whose bytes were written into a tmp_path that
    no longer exists.
    """
    session.rollback()
    session.query(DocumentChunk).delete()
    session.query(DocumentJob).delete()
    session.query(Document).delete()
    session.commit()


@pytest.fixture()
def db_session():
    from tests.conftest import TestingSession

    session = TestingSession()
    _purge(session)
    try:
        yield session
    finally:
        _purge(session)
        session.close()


@pytest.fixture()
def add_company(db_session):
    """Create companies the seeded universe does not contain, then remove them.

    The mapping tests need ITC beside ITC Infotech, and a renamed company beside
    its old name — none of which the seed provides. Created rows are deleted
    afterwards, with their documents first, so nothing leaks into another module.
    """
    created: list[str] = []

    def add(ticker: str, name: str, *, deleted: bool = False,
            listing_status: str = "active") -> Company:
        row = Company(
            id=str(uuid.uuid4()), ticker=ticker, name=name, exchange="NSE",
            listing_status=listing_status,
            deleted_at=(
                datetime.now(timezone.utc) if deleted else None
            ),
        )
        db_session.add(row)
        db_session.commit()
        created.append(row.id)
        return row

    yield add

    if created:
        db_session.query(Document).filter(
            Document.company_id.in_(created)
        ).delete(synchronize_session=False)
        db_session.query(Company).filter(
            Company.id.in_(created)
        ).delete(synchronize_session=False)
        db_session.commit()


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture(autouse=True)
def blogger_settings(monkeypatch):
    """The feature is on, and pointed at the fixture feed, for every test."""
    monkeypatch.setattr(settings, "BLOGGER_ENABLED", True)
    monkeypatch.setattr(settings, "BLOGGER_FEED_URL", FEED_URL)
    monkeypatch.setattr(settings, "BLOGGER_MAX_POSTS", 100)
    monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "")
    monkeypatch.setattr(settings, "BLOGGER_SYNC_TIMEOUT_SECONDS", 5.0)


@pytest.fixture()
def server() -> FakeBloggerServer:
    return FakeBloggerServer()


@pytest.fixture()
def sync(db_session, storage, server, monkeypatch) -> BloggerSyncService:
    """A service wired to the fixture feed and a temporary volume."""
    monkeypatch.setattr(
        "app.services.documents.ingestion.get_storage", lambda: storage,
    )
    return BloggerSyncService(db_session, feed=client_for(server), storage=storage)


@pytest.fixture()
def shriramfin(add_company) -> Company:
    return add_company("SHRIRAMFIN", "Shriram Finance Ltd")


def _drain(db_session, storage, *, limit: int = 12) -> int:
    """Run the real document worker until the queue is empty."""
    worker = DocumentWorker(lambda: db_session, storage=storage)
    runs = 0
    while runs < limit and worker.run_once():
        runs += 1
    return runs


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------
class TestFeedParsing:
    def test_the_post_id_comes_out_of_the_entry_id(self):
        page = parse_feed(feed([entry("4850132895787140759", "A post")]))
        post = page.posts[0]
        assert post.post_id == "4850132895787140759"
        assert post.blog_id == "2807216376160982705"
        # The full Atom id is kept too: it is the only thing that ties a stored
        # document back to the exact entry the feed served.
        assert post.entry_id.endswith("post-4850132895787140759")

    def test_the_post_link_wins_over_the_comment_and_edit_links(self):
        page = parse_feed(feed([entry("777", "A post", url="https://equitypilot.blogspot.com/2026/09/a-post.html")]))
        url = page.posts[0].url
        assert url == "https://equitypilot.blogspot.com/2026/09/a-post.html"
        # Neither of these is a citation a reader could use: one needs an
        # account, the other is Blogger's own comment page.
        assert "blogger.com" not in url
        assert "comment/fullpage" not in url

    def test_a_multi_line_label_is_split_into_the_labels_it_holds(self):
        page = parse_feed(feed([
            entry("1", "T", labels=("Shriram Finance\nStock Analysis", "NBFC")),
        ]))
        assert page.posts[0].labels == ("Shriram Finance", "Stock Analysis", "NBFC")

    def test_repeated_labels_are_deduplicated_without_losing_the_casing(self):
        page = parse_feed(feed([
            entry("1", "T", labels=("TCS", "tcs", "Quarterly Results")),
        ]))
        assert page.posts[0].labels == ("TCS", "Quarterly Results")

    def test_timestamps_arrive_as_aware_utc(self):
        published = datetime(2026, 9, 5, 8, 57, tzinfo=timezone.utc)
        page = parse_feed(feed([entry("1", "T", published=published)]))
        post = page.posts[0]
        assert post.published is not None and post.published.tzinfo is not None
        # Blogger stamps in its own offset; the platform stores UTC.
        assert post.published == published
        assert iso_utc(post.updated).endswith("Z")

    def test_the_body_is_kept_verbatim_for_the_parser_to_reduce(self):
        page = parse_feed(feed([entry("1", "T", content="<p>Revenue grew.</p>")]))
        assert "<p>Revenue grew.</p>" in page.posts[0].content_html

    def test_paging_metadata_is_read_from_the_opensearch_namespace(self):
        page = parse_feed(feed(
            [entry("1", "T")], total=32, start_index=3, items_per_page=2,
            next_url=f"{FEED_URL}?start-index=5&max-results=2",
        ))
        assert page.total_results == 32
        assert page.start_index == 3
        assert page.next_url is not None and "start-index=5" in page.next_url

    def test_the_blog_identity_is_read_from_the_feed_header(self):
        page = parse_feed(feed([entry("1", "T")]))
        assert page.blog_id == "2807216376160982705"
        assert "EquityPilot" in page.blog_title

    def test_an_rss_view_of_the_feed_is_refused_with_a_fix(self):
        # Blogger serves the same posts as RSS at `?alt=rss`, under different
        # element names. Reading that shape half-way would report a successful
        # sync that ingested nothing; an error that names the fix is worth more.
        payload = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>EquityPilot</title>
<item><title>An RSS post</title></item>
</channel></rss>"""
        with pytest.raises(BloggerFeedError, match="alt=rss"):
            parse_feed(payload)

    def test_an_entry_with_no_id_is_skipped_and_the_page_survives(self):
        payload = feed([
            "<entry><title>No identity</title><content type='html'>x</content></entry>",
            entry("999", "A good post"),
        ])
        page = parse_feed(payload)
        assert [post.post_id for post in page.posts] == ["999"]

    def test_an_empty_response_is_an_error_not_an_empty_corpus(self):
        with pytest.raises(BloggerFeedError, match="empty"):
            parse_feed(b"   ")

    def test_a_response_that_is_not_xml_is_an_error(self):
        with pytest.raises(BloggerFeedError, match="not valid XML"):
            parse_feed(b"<feed><entry><title>truncated mid-tag")

    def test_an_html_error_page_is_an_error(self):
        # What a rate-limited or proxied request actually returns: valid XML
        # that is not a feed.
        with pytest.raises(BloggerFeedError, match="not an Atom feed"):
            parse_feed(b"<html><body>503 Service Unavailable</body></html>")

    def test_a_root_that_is_not_a_feed_is_an_error(self):
        with pytest.raises(BloggerFeedError, match="not an Atom feed"):
            parse_feed('<?xml version="1.0"?><error><message>nope</message></error>')

    def test_the_error_never_carries_the_payload(self):
        # A feed response holds an author's whole post. A parse error that
        # quoted it would put the blog's content into the platform's logs.
        with pytest.raises(BloggerFeedError) as info:
            parse_feed("<html><body>SECRET-CORPUS-TEXT</body></html>")
        assert "SECRET-CORPUS-TEXT" not in str(info.value)


class TestFeedPaging:
    def test_pages_are_followed_until_the_corpus_is_exhausted(self, server):
        client = client_for(server, page_size=2)
        posts = client.fetch_posts(max_posts=100)

        assert [post.post_id for post in posts] == [p.post_id for p in CORPUS]
        assert len(server.requests) == 3
        # Paging replaces the parameters rather than appending a second pair.
        assert "start-index=1&max-results=2" in server.requests[0]
        assert "start-index=3&max-results=2" in server.requests[1]
        assert "start-index=5&max-results=2" in server.requests[2]

    def test_the_post_budget_is_respected_exactly(self, server):
        client = client_for(server, page_size=2)
        posts = client.fetch_posts(max_posts=3)
        assert len(posts) == 3
        assert [post.post_id for post in posts] == [p.post_id for p in CORPUS[:3]]

    def test_a_feed_that_serves_the_same_page_twice_stops(self):
        # A `next` link pointing back at the page just read is real Blogger
        # behaviour on some configurations. Without the guard this is an
        # infinite loop inside a scheduled job nobody is watching.
        looping = FakeBloggerServer(posts=CORPUS[:2])
        payload = feed(
            (post.xml() for post in CORPUS[:2]), total=100,
            next_url=f"{FEED_URL}?start-index=1&max-results=2",
        ).encode()
        client = BloggerFeedClient(
            url=FEED_URL, page_size=2,
            fetcher=lambda url: (looping.requests.append(url), payload)[1],
        )
        posts = client.fetch_posts(max_posts=100)
        assert len(posts) == 2
        assert len(looping.requests) == 2

    def test_a_zero_budget_reads_nothing(self, server):
        assert client_for(server).fetch_posts(max_posts=0) == []
        assert server.requests == []

    def test_page_urls_replace_existing_paging_parameters(self):
        client = BloggerFeedClient(
            url=f"{FEED_URL}?alt=json&max-results=7", fetcher=lambda url: b"",
        )
        url = client.page_url(start_index=8, max_results=3)
        assert url.count("max-results") == 1
        assert "max-results=3" in url and "start-index=8" in url
        assert "alt=json" in url

    def test_a_network_failure_surfaces_as_a_feed_error(self):
        def boom(url: str) -> bytes:
            raise OSError("connection reset by peer")

        client = BloggerFeedClient(url=FEED_URL, fetcher=boom)
        with pytest.raises(BloggerFeedError, match="could not fetch"):
            client.fetch_posts(max_posts=10)

    def test_an_oversized_response_is_refused(self, monkeypatch):
        monkeypatch.setattr("app.services.blogger.feed.MAX_FEED_BYTES", 64)
        client = BloggerFeedClient(
            url=FEED_URL, fetcher=lambda url: b"x" * 65,
        )
        with pytest.raises(BloggerFeedError, match="ceiling"):
            client.fetch_posts(max_posts=10)

    def test_a_client_without_a_url_refuses_to_be_built(self):
        with pytest.raises(BloggerFeedError, match="no feed URL"):
            BloggerFeedClient(url="   ")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
class TestStableIdentity:
    def test_the_filename_is_derived_from_the_post_id(self):
        assert stable_filename("4850132895787140759") == (
            "blogger-4850132895787140759.html"
        )

    def test_the_filename_is_stable_and_format_correct(self):
        first = stable_filename("123")
        assert first == stable_filename("123")
        assert first.startswith(f"{FILENAME_PREFIX}-")
        # `.html`, because that is what it is: the parser is chosen by
        # extension, and a blog post renamed .pdf would be sent to a PDF
        # extractor that cannot read it.
        assert first.endswith(".html")

    def test_an_empty_post_id_cannot_produce_a_filename(self):
        # One shared filename for every id-less post would collapse the whole
        # blog into a single version chain.
        with pytest.raises(ValueError):
            stable_filename("")

    def test_the_fingerprint_sees_a_content_change(self):
        post = parsed_post(text="Revenue grew 12%.")
        edited = parsed_post(text="Revenue grew 14%.")
        assert post.fingerprint() != edited.fingerprint()

    def test_the_fingerprint_ignores_the_updated_stamp(self):
        # Blogger rewrites `updated` when an author moves a label or fixes a
        # typo in the template. Treating that as a new version would replace
        # every document in the corpus on a day the author tidied the blog.
        post = parsed_post(updated=datetime(2026, 9, 1, tzinfo=timezone.utc))
        restamped = parsed_post(updated=datetime(2026, 9, 9, tzinfo=timezone.utc))
        assert post.fingerprint() == restamped.fingerprint()

    def test_the_fingerprint_ignores_presentation_whitespace(self):
        # Blogger re-indents markup when an author switches editors. The prose
        # is identical, so the platform must not treat it as a new version.
        post = parsed_post(
            content="<div><p>Revenue grew.</p><p>Margins held.</p></div>",
        )
        reformatted = parsed_post(
            content="<div>\n  <p>Revenue grew.</p>\n\n  <p>Margins held.</p>\n</div>",
        )
        assert post.fingerprint() == reformatted.fingerprint()

    def test_the_fingerprint_sees_a_title_or_label_change(self):
        base = parsed_post(title="TCS Q1 Review", labels=("TCS",))
        assert base.fingerprint() != parsed_post(
            title="TCS Q2 Review", labels=("TCS",)
        ).fingerprint()
        assert base.fingerprint() != parsed_post(
            title="TCS Q1 Review", labels=("TCS", "IT Sector")
        ).fingerprint()

    def test_metadata_carries_every_field_the_brief_names(self):
        post = parsed_post(
            post_id="42", title="Shriram Finance Share Price",
            labels=("Shriram Finance", "NBFC"),
        )
        metadata = post_metadata(
            post, company_ticker="SHRIRAMFIN", company_name="Shriram Finance Ltd",
            mapped_by="label_name", matched_on="shriram finance",
        )
        assert metadata["source"] == "blogger"
        assert metadata["blogger_post_id"] == "42"
        assert metadata["blogger_url"].startswith("https://equitypilot.blogspot.com/")
        assert metadata["blogger_published"].endswith("Z")
        assert metadata["blogger_updated"].endswith("Z")
        assert metadata["blogger_labels"] == ["Shriram Finance", "NBFC"]
        assert metadata["blogger_mapped_ticker"] == "SHRIRAMFIN"

    def test_metadata_holds_no_ingestion_timestamp(self):
        # An ingest time in the stored metadata would make every render differ
        # from the last, and content-hash deduplication would never fire: the
        # same post would become a new version on every run.
        first = post_metadata(parsed_post(), company_ticker="TCS")
        second = post_metadata(parsed_post(), company_ticker="TCS")
        assert first == second
        assert not any(
            "ingested_at" in key or "synced_at" in key for key in first
        )

    def test_the_rendered_document_is_a_complete_html_document(self):
        post = parsed_post(title="TCS Q1 Review", text="Margins held at 24%.")
        payload = render_document(post, company_ticker="TCS")
        text = payload.decode("utf-8")
        assert text.startswith("<!DOCTYPE html>")
        assert "<title>TCS Q1 Review</title>" in text
        assert "Margins held at 24%." in text
        assert 'name="source" content="blogger"' in text
        assert 'name="blogger_post_id"' in text

    def test_rendering_is_deterministic(self):
        post = parsed_post(text="Stable body.")
        assert render_document(post, company_ticker="TCS") == (
            render_document(post, company_ticker="TCS")
        )

    def test_a_post_title_is_escaped_not_interpolated(self):
        post = parsed_post(title="</title><script>alert(1)</script>")
        text = render_document(post, company_ticker="TCS").decode()
        assert "<script>alert(1)</script>" not in text
        assert "&lt;/title&gt;" in text


# ---------------------------------------------------------------------------
# Company mapping
# ---------------------------------------------------------------------------
class TestCompanyMapping:
    def test_an_explicit_ticker_label_maps(self, db_session):
        mapper = CompanyMapper(db_session)
        match = mapper.resolve(title="Quarterly review", labels=("TCS", "IT Sector"))
        assert match.company is not None and match.company.ticker == "TCS"
        assert match.method == "ticker_label"

    def test_a_ticker_label_is_matched_case_insensitively(self, db_session):
        match = CompanyMapper(db_session).resolve(title="t", labels=("tcs",))
        assert match.company is not None and match.company.ticker == "TCS"

    def test_a_company_name_label_maps(self, db_session, shriramfin):
        match = CompanyMapper(db_session).resolve(
            title="Share price analysis", labels=("Shriram Finance", "NBFC"),
        )
        assert match.company is not None
        assert match.company.ticker == "SHRIRAMFIN"
        assert match.method == "name_label"

    def test_a_name_label_maps_without_its_legal_suffix(self, db_session):
        # Authors write "Nestle India", the register says "Nestle India Ltd".
        match = CompanyMapper(db_session).resolve(title="t", labels=("Nestle India",))
        assert match.company is not None and match.company.ticker == "NESTLEIND"

    def test_an_accented_label_maps_to_the_ascii_name(self, db_session):
        match = CompanyMapper(db_session).resolve(title="t", labels=("Nestlé India",))
        assert match.company is not None and match.company.ticker == "NESTLEIND"

    def test_an_ampersand_label_maps_to_the_stored_ticker(self, db_session):
        # The label is "L&T"; the register's ticker is "LT".
        match = CompanyMapper(db_session).resolve(
            title="L&T Stock Analysis", labels=("L&T", "Infrastructure"),
        )
        assert match.company is not None and match.company.ticker == "LT"

    def test_a_ticker_spelled_out_across_leading_words_maps(self, db_session):
        # "JSW Steel …" on the real blog; TATASTEEL here, same shape.
        match = CompanyMapper(db_session).resolve(
            title="Tata Steel Quarterly Review", labels=("Tata Steel Quarterly Results",),
        )
        assert match.company is not None and match.company.ticker == "TATASTEEL"

    def test_the_title_maps_when_the_labels_are_silent(self, db_session):
        match = CompanyMapper(db_session).resolve(
            title="Nestlé India: Rural Demand", labels=("FMCG", "Consumption"),
        )
        assert match.company is not None and match.company.ticker == "NESTLEIND"
        assert match.method == "title_name"

    def test_a_ticker_inside_a_longer_word_does_not_match(self, db_session, add_company):
        """The failure the brief names explicitly.

        "ITC" is a substring of "switching" and of "pitcher". A substring scan
        files a post about bank switching costs against a tobacco company, and
        does it silently — the post looks mapped, the answer looks grounded, and
        both are about the wrong company.
        """
        add_company("ITC", "ITC Ltd")
        match = CompanyMapper(db_session).resolve(
            title="Switching costs are rising for pitcher makers",
            labels=("Banking", "Policy"),
        )
        assert match.company is None
        # And the reason says so plainly, because "skipped" on its own gives an
        # operator nothing to act on.
        assert match.reason

    def test_the_longer_name_wins_over_the_shorter_one(self, db_session, add_company):
        add_company("ITC", "ITC Ltd")
        infotech = add_company("ITCINFOTECH", "ITC Infotech Ltd")
        match = CompanyMapper(db_session).resolve(
            title="ITC Infotech wins a contract", labels=("ITC Infotech",),
        )
        assert match.company is not None
        assert match.company.id == infotech.id

    def test_the_short_name_still_maps_when_that_is_all_there_is(
        self, db_session, add_company,
    ):
        add_company("ITC", "ITC Ltd")
        add_company("ITCINFOTECH", "ITC Infotech Ltd")
        match = CompanyMapper(db_session).resolve(title="t", labels=("ITC Ltd",))
        assert match.company is not None and match.company.ticker == "ITC"

    def test_two_equally_specific_companies_are_refused_rather_than_chosen(
        self, db_session,
    ):
        match = CompanyMapper(db_session).resolve(
            title="TCS and Wipro both reported", labels=("TCS", "WIPRO"),
        )
        assert match.company is None
        assert "ambiguous" in (match.reason or "").lower()

    def test_a_topic_only_post_maps_to_nothing(self, db_session):
        match = CompanyMapper(db_session).resolve(
            title="What NBFCs Expect From The Budget",
            labels=("Budget", "NBFC", "Policy"),
        )
        assert match.company is None
        assert match.reason

    def test_a_soft_deleted_company_is_not_matched(self, db_session, add_company):
        add_company("GONECO", "Gone Company Ltd", deleted=True)
        match = CompanyMapper(db_session).resolve(title="t", labels=("GONECO",))
        assert match.company is None

    def test_a_renamed_company_still_resolves_from_its_old_name(
        self, db_session, add_company,
    ):
        # Zomato became Eternal. The blog's older posts — and its older labels —
        # still say Zomato, and they are still about the same company.
        eternal = add_company("ETERNAL", "Eternal Ltd")
        match = CompanyMapper(db_session).resolve(
            title="Eternal Ltd Stock Analysis: Zomato",
            labels=("Eternal Ltd Stock Analysis: Zomato",),
        )
        assert match.company is not None and match.company.id == eternal.id

    def test_a_rename_that_left_both_rows_resolves_to_the_active_one(
        self, db_session, add_company,
    ):
        """The state a universe import actually leaves behind.

        Zomato was renamed Eternal Ltd in 2025. An import that matches on ISIN
        keeps the old row and adds the new one, so the register holds `ZOMATO`
        delisted and `ETERNAL` active, both named "Eternal Ltd" — and the live
        post below reaches *both* at once, because its label names the company
        and its old symbol in the same phrase. Without collapsing that pair the
        post is reported ambiguous and never indexed at all.

        Title and labels are the live feed's, read 2026-09-05.
        """
        add_company("ZOMATO", "Eternal Ltd", listing_status="delisted")
        eternal = add_company("ETERNAL", "Eternal Ltd")
        match = CompanyMapper(db_session).resolve(
            title=(
                "Eternal Ltd Stock Analysis: Zomato, Blinkit, District और "
                "Hyperpure का पूरा बिजनेस और Financial Analysis"
            ),
            labels=(
                "Blinkit",
                "District और Hyperpure का पूरा बिजनेस और Financial Analysis",
                "Eternal Ltd Stock Analysis: Zomato",
            ),
        )
        assert match.company is not None and match.company.id == eternal.id
        assert match.reason == ""

    def test_two_active_rows_sharing_a_name_are_not_silently_collapsed(
        self, db_session, add_company,
    ):
        # The collapse is about one company under two symbols, distinguished by
        # listing status. Two *active* rows with the same name is a data problem,
        # and picking one of them would hide it.
        add_company("ZOMATO", "Eternal Ltd")
        add_company("ETERNAL", "Eternal Ltd")
        match = CompanyMapper(db_session).resolve(
            title="Eternal Ltd quarterly", labels=("Eternal Ltd Stock Analysis",),
        )
        assert match.company is None
        assert "ambiguous" in match.reason
        assert set(match.considered) == {"ZOMATO", "ETERNAL"}

    def test_a_post_naming_only_the_old_symbol_maps_to_that_row(
        self, db_session, add_company,
    ):
        """Documented edge, not a gap to paper over.

        A label reading only "Zomato" is a ticker label, and tier 1 settles it
        before the alias tier can prefer the renamed listing — so the post is
        filed under the delisted row. It is not lost and not misfiled against a
        different company, and it is logged as a warning, because a document
        attached to a listing the platform has stopped surfacing is indexed and
        never retrieved. The remedy is the register's: merge or delete the old
        row, or label the post with the current name.
        """
        from structlog.testing import capture_logs

        old = add_company("ZOMATO", "Eternal Ltd", listing_status="delisted")
        add_company("ETERNAL", "Eternal Ltd")
        with capture_logs() as logs:
            match = CompanyMapper(db_session).resolve(
                title="Quick commerce margins", labels=("Zomato", "Blinkit"),
            )
        assert match.company is not None and match.company.id == old.id
        assert match.method == "ticker_label"
        assert any(
            entry["event"] == "blogger post mapped to a non-active listing"
            and entry["ticker"] == "ZOMATO"
            for entry in logs
        )

    def test_an_alias_with_no_company_behind_it_maps_to_nothing(
        self, db_session,
    ):
        # The alias table must not invent a company the register does not have.
        match = CompanyMapper(db_session).resolve(
            title="Zomato quarterly", labels=("Zomato",),
        )
        assert match.company is None

    def test_the_default_ticker_is_used_only_when_configured(
        self, db_session, monkeypatch,
    ):
        monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "TCS")
        match = CompanyMapper(
            db_session, default_ticker=settings.BLOGGER_DEFAULT_TICKER,
        ).resolve(title="A market-wide note", labels=("Budget",))
        assert match.company is not None and match.company.ticker == "TCS"
        assert match.method == "default_ticker"

    def test_a_single_ambiguous_word_is_not_a_company_name(self, db_session):
        # "Finance" and "Stocks" are labels on nearly every post in the corpus.
        # Matching one of them to whichever company happens to have it in its
        # name would file half the blog against one lender.
        match = CompanyMapper(db_session).resolve(
            title="Market wrap", labels=("Finance", "Stocks"),
        )
        assert match.company is None

    def test_normalisation_folds_case_accents_and_legal_suffixes(self):
        assert normalise_name("Larsen & Toubro Ltd") == "larsen and toubro ltd"
        assert normalise_name("  Nestlé   India ") == "nestle india"
        assert "nestle india" in name_keys("Nestlé India Ltd")
        assert ticker_key(" l&t ") == "L&T"
        assert ticker_key("tcs.") == "TCS"

    def test_the_mapping_summary_counts_what_happened(self, db_session):
        from app.services.blogger.mapping import MappingSummary

        mapper = CompanyMapper(db_session)
        summary = MappingSummary()
        summary.add(mapper.resolve(title="t", labels=("TCS",)))
        summary.add(mapper.resolve(title="t", labels=("Budget",)))

        assert summary.resolved == 1 and summary.unresolved == 1
        assert summary.by_method["ticker_label"] == 1
        assert summary.by_ticker["TCS"] == 1
        assert summary.as_dict()["unresolved"] == 1


# ---------------------------------------------------------------------------
# The live feed
# ---------------------------------------------------------------------------
#: Real posts from equitypilot.blogspot.com, read 2026-09-05 from
#: `feeds/posts/summary` (Atom and `alt=json`), plus two label-scoped queries
#: that settled which post carries "Bajaj Finance" and which carries
#: "ITC Infotech". Titles and labels are verbatim, including the Devanagari and
#: the accented forms. Company names are the register's own, from
#: `app.data.nse_universe`, so this asserts the mapping against the data a real
#: deployment holds rather than against a convenient invention.
LIVE_POSTS = (
    pytest.param(
        "SHRIRAMFIN", "Shriram Finance Ltd",
        "Shriram Finance Share Price, Fundamental Analysis, Future Growth "
        "& Investment Outlook",
        ("Finance Stocks", "Fundamental Analysis", "NBFC", "Share Price",
         "Shriram Finance", "Stock Analysis"),
        id="shriramfin",
    ),
    pytest.param(
        "NESTLEIND", "Nestle India Ltd",
        "Nestlé India Share Price & Stock Analysis 2026 – NESTLEIND "
        "Financials, Valuation & Future Outlook",
        ("Dividend Stocks", "FMCG Stocks", "Food Stocks", "Fundamental Analysis",
         "Indian Stocks", "Long Term Investment", "NESTLEIND", "Nestlé India",
         "Nestlé India Share Price", "NSE Stocks", "Stock Analysis",
         "Stock Market India"),
        id="nestleind",
    ),
    pytest.param(
        "BEL", "Bharat Electronics Ltd",
        "Bharat Electronics Ltd (BEL) Stock Analysis: Financial Strength, "
        "Order Book, Growth, Valuation & Future Outlook",
        ("BEL", "BEL Stock Analysis", "Bharat Electronics", "Defence Electronics",
         "Defence PSU", "Defence Stocks", "Indian Defence Stocks",
         "Long Term Investment", "NSE Stocks", "Stock Analysis"),
        id="bel",
    ),
    pytest.param(
        # The live ONGC post carries no labels at all, and its `updated` differs
        # from its `published` — so it is the title tier and the new-version path
        # in one real post.
        "ONGC", "Oil & Natural Gas Corporation Ltd",
        "Oil and Natural Gas Corporation Ltd.", (),
        id="ongc-no-labels",
    ),
    pytest.param(
        "ZOMATO", "Eternal Ltd",
        "Eternal Ltd Stock Analysis: Zomato, Blinkit, District और Hyperpure "
        "का पूरा बिजनेस और Financial Analysis",
        ("Blinkit",
         "District और Hyperpure का पूरा बिजनेस और Financial Analysis",
         "Eternal Ltd Stock Analysis: Zomato"),
        id="eternal-renamed-from-zomato",
    ),
    pytest.param(
        "JSWSTEEL", "JSW Steel Ltd",
        "JSW Steel Ltd Stock Analysis 2026: Financial Results, Valuation, "
        "Growth, Ratios & Future Outlook",
        ("JSW Steel", "JSW Steel Stock Analysis", "JSWSTEEL", "Steel Stocks",
         "Stock Analysis"),
        id="jswsteel",
    ),
    pytest.param(
        "LT", "Larsen & Toubro Ltd",
        "Larsen & Toubro Ltd Stock Analysis: ₹8 Lakh Crore+ Order Book, FY27 "
        "Growth, Financials, Valuation & Lakshya 2031",
        ("L&T", "L&T Stock Analysis", "Larsen & Toubro", "Infrastructure",
         "EPC", "Engineering", "Order Book"),
        id="lt",
    ),
    pytest.param(
        # Must not be captured by the "Bajaj Finance" company: the label-scoped
        # feed query showed that label belongs to a different post, and this one
        # is decided by its title.
        "BAJAJFINSV", "Bajaj Finserv Ltd",
        "Bajaj Finserv Stock Analysis: Q1 FY27 Profit Growth, Financials, "
        "Valuation, Insurance, Lending & Long-Term Outlook",
        ("Financial Services", "Insurance Stocks", "NBFC", "Q1 FY27",
         "Stock Analysis"),
        id="bajajfinsv",
    ),
    pytest.param(
        "NTPC", "NTPC Ltd",
        "NTPC Ltd Stock Analysis: Q1 FY27 Profit Growth, Strong Cash Flow, "
        "89 GW Scale और ₹16.86 Lakh Crore Expansion Plan",
        ("NTPC Ltd", "Power Sector", "PSU Stocks", "Q1 FY27", "Stock Analysis"),
        id="ntpc",
    ),
    pytest.param(
        # Carries "ITC Infotech" and "Happiest Minds Merger" beside "ITC Ltd".
        # Neither of those is a listed company in the register, so the post is
        # about ITC and must land there.
        "ITC", "ITC Ltd",
        "ITC Ltd Stock Analysis: Strong Cash Flow, FMCG Expansion & ITC "
        "Infotech–Happiest Minds Merger Boost Growth Outlook",
        ("Business Update", "Dividend Stocks", "Financial Analysis", "FMCG",
         "Happiest Minds Merger", "Indian Stocks", "Investment Research",
         "ITC Infotech", "ITC Ltd", "Quarterly Results", "Stock Analysis",
         "Stock Market"),
        id="itc",
    ),
    pytest.param(
        # The register's symbol for Bajaj Finance Ltd is BAJFINANCE; the older
        # BAJAJFINANCE is what the label spells out, and tier 1 finds it either
        # way. Both rows must never exist at once — two active companies named
        # "Bajaj Finance Ltd" is a genuine ambiguity, and the mapper reports it
        # rather than choosing.
        "BAJFINANCE", "Bajaj Finance Ltd",
        "Bajaj Finance Share Price & Stock Analysis 2026: Financials, "
        "Valuation, Growth, Ratios & Shareholding",
        ("Bajaj Finance", "Bajaj Finance Stock Analysis", "Financial Services",
         "Fundamental Analysis", "Indian Stocks", "NBFC", "Share Price",
         "Stock Market"),
        id="bajajfinance",
    ),
)


class TestTheLiveFeedMaps:
    """Every company the operator asked about, against the feed as it is."""

    @staticmethod
    def _in_register(db_session, add_company, ticker: str, name: str) -> Company:
        """The row the register holds for this company, creating it only if absent.

        Looked up by ticker and then by normalised name, because the register's
        symbol for a company is not always the one a feed spells out — and adding
        a second row under a different symbol would create two active companies
        with one name, which the mapper correctly refuses to choose between.
        """
        existing = (
            db_session.query(Company).filter(Company.ticker == ticker).first()
        )
        if existing is not None:
            return existing
        wanted = normalise_name(name)
        for row in db_session.query(Company).all():
            if wanted in name_keys(row.name or ""):
                return row
        return add_company(ticker, name)

    @pytest.mark.parametrize("ticker,name,title,labels", LIVE_POSTS)
    def test_the_post_maps_to_the_company_it_is_about(
        self, db_session, add_company, ticker, name, title, labels,
    ):
        company = self._in_register(db_session, add_company, ticker, name)
        # The Bajaj pair is the collision worth proving, so both are present.
        if ticker in {"BAJAJFINSV", "BAJFINANCE"}:
            self._in_register(db_session, add_company, "BAJFINANCE", "Bajaj Finance Ltd")
            self._in_register(db_session, add_company, "BAJAJFINSV", "Bajaj Finserv Ltd")

        match = CompanyMapper(db_session).resolve(title=title, labels=labels)

        assert match.company is not None, match.reason
        assert match.company.id == company.id
        assert match.company.ticker == ticker
        assert match.reason == ""

    @pytest.mark.parametrize("ticker,name,title,labels", LIVE_POSTS)
    def test_the_title_alone_is_enough_for_every_one_of_them(
        self, db_session, add_company, ticker, name, title, labels,
    ):
        # Labels are the operator's to manage and can be edited away; the title
        # is the post's own statement of its subject. A blog whose labels were
        # all deleted should still map, which is what this asserts.
        company = self._in_register(db_session, add_company, ticker, name)
        if ticker in {"BAJAJFINSV", "BAJFINANCE"}:
            self._in_register(db_session, add_company, "BAJFINANCE", "Bajaj Finance Ltd")
            self._in_register(db_session, add_company, "BAJAJFINSV", "Bajaj Finserv Ltd")

        match = CompanyMapper(db_session).resolve(title=title, labels=())

        # Bajaj Finance's title names the company but so does its label tier in
        # the case above; here only the title can speak, and it does.
        assert match.company is not None, match.reason
        assert match.company.id == company.id


# ---------------------------------------------------------------------------
# Ingesting
# ---------------------------------------------------------------------------
class TestSyncIngests:
    def test_new_posts_become_queued_documents(self, sync, db_session, shriramfin):
        result = sync.sync()

        assert result.error is None
        assert result.fetched == len(CORPUS)
        assert result.ingested == 4
        assert result.skipped == 1

        document = db_session.query(Document).filter(
            Document.filename == stable_filename(CORPUS[0].post_id)
        ).one()
        assert document.status == DocumentStatus.QUEUED.value
        assert document.doc_type == DocumentType.RESEARCH_NOTE.value
        assert document.company_id == shriramfin.id
        assert document.version == 1
        # Nothing was parsed here: that is the document worker's job, on the
        # existing queue, at the existing priority.
        assert document.chunk_count == 0
        assert db_session.query(DocumentJob).count() == 4

    def test_the_document_is_html_not_a_fake_pdf(self, sync, db_session, shriramfin):
        sync.sync()
        document = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()
        assert document.filename.endswith(".html")
        assert document.storage_key.endswith(".html")

    def test_provenance_is_stored_on_the_document(
        self, sync, db_session, storage, shriramfin,
    ):
        sync.sync()
        _drain(db_session, storage)
        document = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()
        metadata = document.doc_metadata or {}

        assert document.status == DocumentStatus.COMPLETED.value
        assert document.chunk_count > 0
        assert metadata["source"] == "blogger"
        assert metadata["blogger_post_id"] == CORPUS[0].post_id
        assert metadata["blogger_url"].startswith("https://equitypilot.blogspot.com/")
        assert metadata["blogger_published"].endswith("Z")
        assert metadata["blogger_updated"].endswith("Z")
        assert "Shriram Finance" in metadata["blogger_labels"]
        assert metadata["blogger_mapped_ticker"] == "SHRIRAMFIN"
        assert metadata["ingested_by"] == "blogger-sync"

    def test_the_title_comes_from_the_post_not_the_filename(
        self, sync, db_session, storage, shriramfin,
    ):
        sync.sync()
        _drain(db_session, storage)
        document = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()
        assert document.title == CORPUS[0].title

    def test_an_unmapped_post_is_skipped_and_creates_nothing(
        self, sync, db_session, shriramfin,
    ):
        result = sync.sync()

        skipped = [o for o in result.outcomes if o.action == "skipped"]
        assert len(skipped) == 1
        assert skipped[0].post_id == CORPUS[-1].post_id
        assert skipped[0].reason
        assert db_session.query(Document).filter(
            Document.filename == stable_filename(CORPUS[-1].post_id)
        ).count() == 0

    def test_a_post_with_no_content_is_skipped(self, db_session, storage, shriramfin):
        # Blogger serves an empty body for a draft or an unpublished post.
        # There is nothing to index, and a document made from nothing would be
        # an empty page the chatbot could cite.
        payload = feed([
            entry("5555", "Empty post", content="", labels=("Shriram Finance",)),
        ]).encode()
        service = BloggerSyncService(
            db_session,
            feed=BloggerFeedClient(url=FEED_URL, fetcher=lambda url: payload),
            storage=storage,
        )
        result = service.sync()
        assert result.skipped == 1
        assert result.ingested == 0
        assert db_session.query(Document).count() == 0

    def test_the_budget_bounds_the_run(self, db_session, storage, shriramfin):
        service = BloggerSyncService(
            db_session, feed=client_for(FakeBloggerServer()), storage=storage,
        )
        result = service.sync(max_posts=2)
        assert result.fetched == 2
        assert db_session.query(Document).count() == 2


# ---------------------------------------------------------------------------
# Repeating
# ---------------------------------------------------------------------------
class TestSyncIsRepeatable:
    def test_a_second_run_writes_nothing(self, sync, db_session, shriramfin):
        first = sync.sync()
        jobs_after_first = db_session.query(DocumentJob).count()
        documents_after_first = db_session.query(Document).count()

        second = sync.sync()

        assert second.ingested == 0
        assert second.new_versions == 0
        assert second.unchanged == first.ingested
        assert second.changed == 0
        assert db_session.query(Document).count() == documents_after_first
        assert db_session.query(DocumentJob).count() == jobs_after_first

    def test_an_edited_post_becomes_a_new_version(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        filename = stable_filename(CORPUS[0].post_id)
        original = db_session.query(Document).filter(
            Document.filename == filename
        ).one()

        # The author edits the post: new body, new `updated` stamp.
        edited = CORPUS[0].text + "\n\nA correction: the ratio is 2.6%."
        server.posts = (
            FakePost(
                post_id=CORPUS[0].post_id, title=CORPUS[0].title,
                labels=CORPUS[0].labels, text=edited,
                published=CORPUS[0].published,
                updated=datetime.now(timezone.utc),
            ),
            *CORPUS[1:],
        )
        result = service.sync()

        assert result.new_versions == 1
        current = db_session.query(Document).filter(
            Document.filename == filename, Document.superseded_by.is_(None),
        ).one()
        assert current.id != original.id
        assert current.version == original.version + 1
        # The previous version is retired, not erased.
        db_session.refresh(original)
        assert original.superseded_by == current.id
        assert db_session.get(Document, original.id) is not None

    def test_a_new_timestamp_with_identical_content_is_not_a_new_version(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        before = db_session.query(Document).count()

        server.edits[CORPUS[0].post_id] = datetime.now(timezone.utc) + timedelta(days=1)
        result = service.sync()

        assert result.new_versions == 0
        assert result.ingested == 0
        assert result.unchanged + result.duplicates == result.fetched - result.skipped
        assert db_session.query(Document).count() == before

    def test_force_lets_the_content_hash_decide(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        before = db_session.query(Document).count()

        result = service.sync(force=True)

        # The fast path is off, so every post reaches `accept()` — and the
        # content hash, the second gate, still refuses to duplicate them.
        assert result.unchanged == 0
        assert result.duplicates == 4
        assert db_session.query(Document).count() == before

    def test_a_failed_document_is_requeued_not_re_ingested(
        self, sync, db_session, shriramfin,
    ):
        sync.sync()
        document = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()
        document.status = DocumentStatus.FAILED.value
        document.error = "the embedding provider was unreachable"
        db_session.commit()
        db_session.query(DocumentJob).delete()
        db_session.commit()

        documents_before = db_session.query(Document).count()
        result = sync.sync()

        requeued = [o for o in result.outcomes if o.action == "requeued"]
        assert len(requeued) == 1
        assert requeued[0].document_id == document.id
        assert requeued[0].job_id is not None
        # Re-indexed from its own stored source: no second document for the
        # same post, and exactly one new job for the one that had failed.
        assert db_session.query(Document).count() == documents_before
        assert db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).count() == 1
        assert db_session.query(DocumentJob).count() == 1

    def test_a_failed_document_with_missing_source_is_recovered_from_feed(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        """The production state: the row is FAILED and its object is gone.

        The shared volume lost the object the row's `storage_key` names, and
        the pipeline exhausted its retries, leaving the row FAILED. The feed
        is the authoritative source for a Blogger post, so the sync must not
        report a failure it cannot fix: it re-renders the post and hands the
        bytes to `accept()`, which writes them back to storage, repoints the
        row, and re-queues it for the existing worker. The platform keeps
        each company's bytes once, so unchanged content repairs the row in
        place rather than making a second document.
        """
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        broken = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()
        job = db_session.query(DocumentJob).filter(
            DocumentJob.document_id == broken.id
        ).one()

        # The object fell out of the shared volume; the job exhausted its
        # retries and the row is FAILED. The other posts' jobs are cleared so
        # this test counts exactly the recovered document's work.
        assert storage.delete(broken.storage_key) is True
        job.status = "failed"
        job.error = "stored object unreadable"
        broken.status = DocumentStatus.FAILED.value
        broken.error = "stored object unreadable"
        db_session.commit()
        db_session.query(DocumentJob).filter(
            DocumentJob.document_id != broken.id
        ).delete(synchronize_session=False)
        db_session.commit()
        documents_before = db_session.query(Document).count()
        jobs_before = db_session.query(DocumentJob).count()

        # A dry run reports the recovery without writing anything.
        dry = service.sync(dry_run=True)
        assert dry.failed == 0 and dry.changed == 0
        assert db_session.query(Document).count() == documents_before
        would = [o for o in dry.outcomes if o.document_id == broken.id]
        assert len(would) == 1
        assert would[0].action == "would_ingest"
        assert "recovered from the feed" in would[0].reason

        result = service.sync()

        # The broken post is the one that changed; nothing failed.
        assert result.failed == 0
        changed = [o for o in result.outcomes if o.action in CHANGED_ACTIONS]
        assert len(changed) == 1
        assert changed[0].post_id == CORPUS[0].post_id
        assert changed[0].action == "requeued"
        assert changed[0].document_id == broken.id
        assert changed[0].job_id == job.id
        assert "restored" in changed[0].reason

        # Same row, repaired: unchanged content is restored in place, not
        # duplicated, and the old state is not erased.
        assert db_session.query(Document).count() == documents_before
        db_session.refresh(broken)
        assert broken.status == DocumentStatus.QUEUED.value
        assert broken.error is None
        assert broken.superseded_by is None
        assert broken.version == 1
        # The source is back in storage, and it is the feed's current content.
        assert broken.storage_key
        assert storage.exists(broken.storage_key)
        restored = storage.read(broken.storage_key).decode("utf-8")
        assert f"<title>{CORPUS[0].title}</title>" in restored
        assert "non-banking finance" in restored
        assert 'name="blogger_post_id"' in restored

        # The job was re-scheduled through the normal mechanism — reset, not
        # deleted and not duplicated.
        assert db_session.query(DocumentJob).count() == jobs_before
        db_session.refresh(job)
        assert job.status == "queued"
        assert job.attempts == 0
        assert job.error is None

        # The existing worker picks it up and completes it. The worker closes
        # its session after each job, so the row is read back rather than
        # refreshed.
        _drain(db_session, storage)
        broken = db_session.get(Document, broken.id)
        assert broken.status == DocumentStatus.COMPLETED.value
        assert broken.chunk_count > 0

        # And the next sync is quiet: the post is stored and unchanged.
        again = service.sync()
        assert again.failed == 0
        assert again.changed == 0
        assert again.unchanged >= 1

    def test_a_failed_document_with_missing_source_and_edited_feed_becomes_a_new_version(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        """The same incident, but the author edited the post after ingestion.

        The feed now serves different content, so `accept()` cannot repair the
        broken row in place — the platform keeps each company's bytes once —
        and the recovery is the normal versioning path: a new document and a
        new job, with the broken row retired rather than deleted.
        """
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        broken = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()

        # The author edits the post, then the volume loses the stored object
        # and the pipeline leaves the row FAILED.
        edited = CORPUS[0].text + "\n\nA correction: the ratio is 2.6%."
        server.posts = (
            FakePost(
                post_id=CORPUS[0].post_id, title=CORPUS[0].title,
                labels=CORPUS[0].labels, text=edited,
                published=CORPUS[0].published,
                updated=datetime.now(timezone.utc),
            ),
            *CORPUS[1:],
        )
        assert storage.delete(broken.storage_key) is True
        broken.status = DocumentStatus.FAILED.value
        broken.error = "stored object unreadable"
        db_session.commit()
        db_session.query(DocumentJob).delete()
        db_session.commit()
        documents_before = db_session.query(Document).count()
        jobs_before = db_session.query(DocumentJob).count()

        result = service.sync()

        # Exactly one recovered outcome, through the normal versioning path.
        assert result.failed == 0
        recovered = [
            o for o in result.outcomes
            if o.action in ("ingested", "new_version")
        ]
        assert len(recovered) == 1
        assert recovered[0].action == "new_version"
        assert recovered[0].post_id == CORPUS[0].post_id
        assert recovered[0].document_id != broken.id
        assert recovered[0].version == broken.version + 1

        # One new document and one new job, on the existing queue.
        assert db_session.query(Document).count() == documents_before + 1
        assert db_session.query(DocumentJob).count() == jobs_before + 1

        current = db_session.get(Document, recovered[0].document_id)
        assert current.superseded_by is None
        assert current.version == broken.version + 1
        # The new current document has a valid storage_key, and the object is
        # really there.
        assert current.storage_key
        assert storage.exists(current.storage_key)
        assert storage.read(current.storage_key)
        assert current.status == DocumentStatus.QUEUED.value

        # The old document is preserved and retired, not erased.
        db_session.refresh(broken)
        assert broken.superseded_by == current.id
        assert db_session.get(Document, broken.id) is not None

        # The existing worker processes the recovered version. The worker
        # closes its session after each job, so the row is read back rather
        # than refreshed.
        _drain(db_session, storage)
        current = db_session.get(Document, current.id)
        assert current.status == DocumentStatus.COMPLETED.value
        assert current.chunk_count > 0

    def test_dry_run_writes_nothing(self, db_session, storage, server, shriramfin,
                                    monkeypatch):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        result = service.sync(dry_run=True)

        assert result.dry_run is True
        assert result.ingested == 0
        assert db_session.query(Document).count() == 0
        assert db_session.query(DocumentJob).count() == 0
        assert all(o.action == "would_ingest" for o in result.outcomes if o.ticker)
        # It still reports the mapping, which is the reason to run one.
        assert result.mapping.resolved == 4
        assert result.mapping.unresolved == 1

    def test_a_dry_run_over_an_unchanged_corpus_reports_no_change(
        self, sync, db_session, shriramfin,
    ):
        sync.sync()
        result = sync.sync(dry_run=True)
        assert result.ingested == 0
        assert not [o for o in result.outcomes if o.action == "would_ingest"]

    def test_nothing_is_deleted_when_the_feed_shrinks(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        """A post that vanishes from the feed keeps its document.

        Blogger pages by recency and an author can unpublish. Neither is
        evidence that the knowledge should be destroyed: a citation may already
        point at it, and the next page of the feed may simply not have been
        read.
        """
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        before = db_session.query(Document).count()

        server.posts = CORPUS[:2]
        service.sync()

        assert db_session.query(Document).count() == before

    def test_a_post_that_moves_company_supersedes_the_previous_row(
        self, db_session, storage, server, add_company, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        tcs = db_session.query(Company).filter(Company.ticker == "TCS").one()
        wipro = db_session.query(Company).filter(Company.ticker == "WIPRO").one()
        post = FakePost(
            post_id="6666", title="A review", labels=("TCS",),
            text="A review of the quarter.",
        )
        server.posts = (post,)
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        service.sync()
        original = db_session.query(Document).one()
        assert original.company_id == tcs.id

        # The author relabels the post. It is the same post — the same id, the
        # same filename — now about a different company.
        server.posts = (FakePost(
            post_id="6666", title="A review", labels=("WIPRO",),
            text="A review of the quarter.",
        ),)
        result = service.sync()

        assert result.moved_company == 1
        current = db_session.query(Document).filter(
            Document.superseded_by.is_(None)
        ).one()
        assert current.company_id == wipro.id
        db_session.refresh(original)
        assert original.superseded_by == current.id
        # Still there: a citation issued against it last week still resolves.
        assert db_session.get(Document, original.id) is not None


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
class TestSyncFailures:
    def test_a_disabled_sync_does_not_touch_the_feed(self, db_session, storage,
                                                     server, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        result = service.sync()
        assert result.error and "BLOGGER_ENABLED" in result.error
        assert server.requests == []

    def test_a_manual_run_can_override_the_switch(self, db_session, storage,
                                                  server, shriramfin, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        result = service.sync(allow_disabled=True)
        assert result.error is None
        assert result.ingested == 4

    def test_a_missing_feed_url_is_reported(self, db_session, storage, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_FEED_URL", "  ")
        result = BloggerSyncService(db_session, storage=storage).sync()
        assert result.error and "BLOGGER_FEED_URL" in result.error

    def test_an_unreachable_feed_leaves_the_corpus_intact(
        self, sync, db_session, server, shriramfin,
    ):
        sync.sync()
        before = db_session.query(Document).count()

        server.error = OSError("connection reset by peer")
        result = sync.sync()

        assert result.error and "could not fetch" in result.error
        assert result.ingested == 0
        # Nothing was deleted, nothing was marked failed: the corpus is exactly
        # as it was, and the next run retries.
        assert db_session.query(Document).count() == before
        assert db_session.query(Document).filter(
            Document.status == DocumentStatus.FAILED.value
        ).count() == 0

    def test_an_html_error_page_instead_of_a_feed_is_reported(
        self, sync, db_session, server, shriramfin,
    ):
        sync.sync()
        server.bad_body = "<html><body>Rate limited</body></html>"
        result = sync.sync()
        assert result.error
        assert result.ingested == 0

    def test_one_bad_post_does_not_stop_the_run(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        """A malformed entry costs one post, not the whole sync.

        Also exercises the rollback: a session left dirty by a failed flush
        fails every commit after it, which is how one bad entry used to be able
        to end a run.
        """
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        from app.services.blogger import sync as sync_module

        real_render = sync_module.render_document

        def flaky(post, **kwargs):
            if post.post_id == CORPUS[1].post_id:
                raise RuntimeError("simulated failure while rendering")
            return real_render(post, **kwargs)

        monkeypatch.setattr(sync_module, "render_document", flaky)
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )
        result = service.sync()

        assert result.failed == 1
        assert result.ingested == 3
        assert result.error is None
        failed = [o for o in result.outcomes if o.action == "failed"][0]
        assert failed.post_id == CORPUS[1].post_id
        assert "simulated failure" in failed.reason

    def test_the_result_summary_is_json_serialisable_and_bounded(
        self, sync, shriramfin,
    ):
        result = sync.sync()
        payload = json.dumps(result.as_dict())
        assert "SHRIRAMFIN" in payload
        assert len(result.as_dict()["outcomes"]) <= 50


# ---------------------------------------------------------------------------
# End to end: the point of the whole integration
# ---------------------------------------------------------------------------
class TestThePostBecomesKnowledge:
    def test_an_ingested_post_is_retrievable_by_the_existing_engine(
        self, sync, db_session, storage, shriramfin,
    ):
        sync.sync()
        _drain(db_session, storage)

        from app.services.documents.service import DocumentService

        answer = DocumentService(db_session).search(
            "commercial vehicle loans and asset quality",
            company_id=shriramfin.id, top_k=5,
        )
        assert answer.hits, "the post must be findable by the existing retrieval"
        text = " ".join(hit.text for hit in answer.hits).lower()
        assert "non-banking finance" in text or "stage-three" in text

    def test_the_stored_chunks_are_the_post_not_the_boilerplate(
        self, sync, db_session, storage, shriramfin,
    ):
        sync.sync()
        _drain(db_session, storage)
        document = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one()
        chunks = db_session.query(DocumentChunk).filter(
            DocumentChunk.document_id == document.id
        ).all()
        text = " ".join(chunk.text for chunk in chunks)

        assert "Shriram Finance" in text
        # The template's stylesheet and the HTML comment are gone: they would
        # otherwise be embedded, retrieved and quoted back to a reader.
        assert "font-family" not in text
        assert "This is a Blogger post" not in text

    def test_a_reindexed_document_keeps_its_provenance(
        self, sync, db_session, storage, shriramfin,
    ):
        """Re-processing must not wipe what the sync wrote.

        The parser emits its own metadata for the same row; a persist that
        overwrote rather than merged would drop the post's URL, labels and
        fingerprint, and the next sync would see a document it could not
        recognise as unchanged.
        """
        sync.sync()
        _drain(db_session, storage)
        document_id = db_session.query(Document).filter(
            Document.company_id == shriramfin.id
        ).one().id

        DocumentIngestionService(db_session, storage=storage).reprocess(document_id)
        _drain(db_session, storage)

        document = db_session.get(Document, document_id)
        metadata = document.doc_metadata or {}
        assert document.status == DocumentStatus.COMPLETED.value
        assert metadata["source"] == "blogger"
        assert metadata["blogger_post_id"] == CORPUS[0].post_id
        assert metadata["blogger_url"]
        assert metadata["blogger_content_fingerprint"]
        # And the next sync still recognises it as unchanged, which is what the
        # provenance is for.
        assert sync.sync().unchanged >= 1


# ---------------------------------------------------------------------------
# Platform wiring
# ---------------------------------------------------------------------------
class TestPlatformWiring:
    def test_the_job_kind_is_declared_everywhere_the_worker_looks(self):
        assert JobKind.BLOGGER_SYNC in JOB_LABELS
        assert JobKind.BLOGGER_SYNC in DEFAULT_PRIORITY
        assert JobKind.BLOGGER_SYNC in RETRY_POLICIES
        assert any(
            spec.kind is JobKind.BLOGGER_SYNC for spec in SCHEDULES
        ), "the sync must be scheduled by the existing scheduler"

    def test_the_handler_is_registered(self):
        from app.services.platform.jobs.handlers import HANDLERS, handler_for

        assert JobKind.BLOGGER_SYNC in HANDLERS
        assert handler_for(JobKind.BLOGGER_SYNC) is HANDLERS[JobKind.BLOGGER_SYNC]

    def test_the_handler_skips_when_the_feature_is_off(self, db_session, monkeypatch):
        from app.services.platform.jobs.handlers import handle_blogger_sync

        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        result = handle_blogger_sync(db_session, {})
        assert result["skipped"] is True
        assert "disabled" in result["reason"]

    def test_the_handler_skips_when_no_feed_is_configured(self, db_session, monkeypatch):
        from app.services.platform.jobs.handlers import handle_blogger_sync

        monkeypatch.setattr(settings, "BLOGGER_FEED_URL", "")
        result = handle_blogger_sync(db_session, {})
        assert result["skipped"] is True
        assert "feed" in result["reason"].lower()

    def test_the_handler_returns_the_run_summary(
        self, db_session, storage, server, shriramfin, monkeypatch,
    ):
        """The handler drives the same service the CLI does.

        Stubbed at the module the handler imports from, so what is exercised is
        the handler's own logic — the configuration gate, the summary it
        returns, the exception it raises — and not a second copy of the sync.
        """
        from app.services.platform.jobs import handlers

        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = BloggerSyncService(
            db_session, feed=client_for(server), storage=storage,
        )

        class _Stub:
            def __init__(self, db, **kwargs):
                pass

            def sync(self, **kwargs):
                return service.sync(**kwargs)

        monkeypatch.setattr("app.services.blogger.sync.BloggerSyncService", _Stub)
        result = handlers.handle_blogger_sync(db_session, {})

        assert result["ingested"] == 4
        assert result["skipped"] == 1
        assert result["error"] is None
        assert result["mapping"]["by_ticker"]["SHRIRAMFIN"] == 1

    def test_the_handler_raises_when_the_feed_is_unreachable(
        self, db_session, storage, server, monkeypatch,
    ):
        # Raised, not returned: the queue retries what a handler throws, and an
        # unreachable feed is exactly the case worth retrying.
        from app.services.platform.jobs import handlers

        server.error = OSError("timed out")

        class _Stub:
            def __init__(self, db, **kwargs):
                pass

            def sync(self, **kwargs):
                return BloggerSyncService(
                    db_session, feed=client_for(server), storage=storage,
                ).sync(**kwargs)

        monkeypatch.setattr("app.services.blogger.sync.BloggerSyncService", _Stub)
        # Raised so the queue's retry policy applies: an unreachable feed is
        # transient, and a job that swallowed it would never be retried.
        with pytest.raises(Exception, match="could not fetch"):
            handlers.handle_blogger_sync(db_session, {})


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
class TestCommandLine:
    @pytest.fixture()
    def fake_client_class(self, monkeypatch, server, storage):
        """Make the CLI's own client construction hit the fixture feed."""
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )

        def factory(*, url, timeout, **kwargs):
            return client_for(server)

        monkeypatch.setattr("app.services.blogger.sync.BloggerFeedClient", factory)
        return server

    def test_a_disabled_deployment_exits_two(self, capsys, monkeypatch):
        # Two rather than one: a cron job should be able to tell "the feature is
        # off here" from "the feature is on and broken", and alert only on the
        # second.
        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        assert main([]) == 2
        assert "BLOGGER_ENABLED" in capsys.readouterr().out

    def test_a_dry_run_reports_without_writing(
        self, capsys, db_session, fake_client_class, shriramfin,
    ):
        code = main(["--dry-run", "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert payload["ingested"] == 0
        assert db_session.query(Document).count() == 0

    def test_a_run_reports_what_it_did_in_prose(
        self, capsys, db_session, fake_client_class, shriramfin,
    ):
        code = main([])
        out = capsys.readouterr().out
        assert code == 0
        assert "ingested:" in out
        assert "needs attention: 1 skipped" in out
        assert "SHRIRAMFIN" in out

    def test_a_feed_failure_exits_one(self, capsys, db_session, fake_client_class):
        fake_client_class.error = OSError("connection refused")
        assert main(["--json"]) == 1
        assert "could not fetch" in json.loads(capsys.readouterr().out)["error"]

    def test_a_feed_failure_says_so_in_prose(self, capsys, db_session, fake_client_class):
        fake_client_class.error = OSError("connection refused")
        assert main([]) == 1
        assert "could not fetch" in capsys.readouterr().out

    def test_the_manual_override_runs_a_disabled_feature(
        self, capsys, db_session, fake_client_class, shriramfin, monkeypatch,
    ):
        # Connecting a new blog for the first time happens on a deployment
        # where the scheduled sync is still off, and the operator needs to see
        # what the feed maps to before switching it on.
        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        assert main(["--allow-disabled", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ingested"] == 4

    def test_arguments_override_configuration(
        self, capsys, db_session, fake_client_class, shriramfin,
    ):
        code = main(["--max-posts", "2", "--json"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["fetched"] == 2
