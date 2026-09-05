"""A Blogger feed, built the way Blogger actually serves one.

Modelled on the live EquityPilot feed (`equitypilot.blogspot.com`), including
the details that are easy to get wrong and expensive to discover in production:

* entry ids are `tag:blogger.com,1999:blog-<BLOGID>.post-<POSTID>`, and the post
  id is the part after the last `post-`;
* an entry carries four links, of which only `rel="alternate"` is the post —
  the others are the feed, the edit URL (which needs an account) and the replies
  feed;
* labels are `category` elements in Blogger's own scheme, and an author who
  pastes a list into one label produces a term holding several newline-separated
  names. A literal newline in an XML attribute is normalised to a space by the
  parser, so a real one has to be written as `&#10;` — which is how the live feed
  carries it, and why the fixture does too;
* paging is `start-index` + `max-results`, advertised through
  `openSearch:totalResults` and a `rel="next"` link;
* post bodies begin with an HTML comment and a large `<style>` block, because
  that is what Blogger's own template emits.

Everything here is synthetic. No test reaches the network: the sandbox cannot,
and a suite that depends on a third-party blog being up is a suite that fails
for reasons nobody can fix.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import escape, quoteattr

BLOG_ID = "2807216376160982705"
FEED_URL = "https://equitypilot.blogspot.com/feeds/posts/default"
BLOG_ORIGIN = "https://equitypilot.blogspot.com"
AUTHOR = "GyaanGarage"

#: What Blogger's own post template puts in front of every body: a comment and
#: a stylesheet. The parser must strip both and keep the prose.
POST_PREAMBLE = """<!-- This is a Blogger post. -->
<style>
  .post-body { font-family: 'Helvetica Neue', Arial, sans-serif; }
  .separator a { margin-left: 1em; }
  @media (max-width: 600px) { .post-body { font-size: 15px; } }
</style>
"""


def _attr(value: str) -> str:
    """Quote an attribute value, preserving newlines as character references.

    Without the replacement, a multi-name label would arrive at the parser as
    one space-joined string, and the fixture would not be testing the behaviour
    the real feed has.
    """
    return quoteattr(value).replace("\n", "&#10;").replace("\r", "&#13;")


def entry_id(post_id: str, *, blog_id: str = BLOG_ID) -> str:
    return f"tag:blogger.com,1999:blog-{blog_id}.post-{post_id}"


def post_url(slug: str, *, year: int = 2026, month: int = 9) -> str:
    return f"{BLOG_ORIGIN}/{year}/{month:02d}/{slug}.html"


def iso(moment: datetime) -> str:
    """Blogger's timestamp format: ISO 8601 with the blog's own UTC offset."""
    return moment.astimezone(timezone(timedelta(hours=-7))).isoformat(
        timespec="milliseconds"
    )


def body(text: str = "The company reported steady growth.", *, preamble: bool = True) -> str:
    """A post body: the template's preamble plus one paragraph of prose."""
    prose = "\n".join(f"<p>{escape(paragraph)}</p>" for paragraph in text.split("\n\n"))
    return (POST_PREAMBLE if preamble else "") + f'<div class="post-body">{prose}</div>'


def entry(
    post_id: str,
    title: str,
    *,
    content: str | None = None,
    labels: Sequence[str] = (),
    published: datetime | None = None,
    updated: datetime | None = None,
    url: str | None = None,
    author: str = AUTHOR,
    blog_id: str = BLOG_ID,
    include_edit_link: bool = True,
) -> str:
    """One `<entry>`, with the link set a real Blogger entry carries."""
    published = published or datetime(2026, 9, 5, 8, 57, tzinfo=timezone.utc)
    updated = updated or published + timedelta(seconds=41)
    url = url or post_url(title.lower().replace(" ", "-")[:40])
    content = body() if content is None else content

    # The link set a real Blogger entry carries, in the order it carries it.
    # The second one is the trap: `rel="replies"` with an HTML type points at
    # Blogger's own comment page, so a parser that took "the first HTML link"
    # would cite a dead end instead of the post.
    links = [
        f'<link rel="replies" type="application/atom+xml" '
        f'href="{escape(BLOG_ORIGIN)}/feeds/{post_id}/comments/default" '
        f'title="Post Comments"/>',
        '<link rel="replies" type="text/html" '
        'href="https://www.blogger.com/comment/fullpage/post/'
        f'{blog_id}/{post_id}" title="0 Comments"/>',
        f'<link rel="alternate" type="text/html" href="{escape(url)}" '
        f'title={_attr(title)}/>',
    ]
    if include_edit_link:
        # Needs an account. Must never be stored as a citation.
        links.append(
            f'<link rel="edit" type="application/atom+xml" '
            f'href="https://www.blogger.com/feeds/{blog_id}/posts/default/{post_id}"/>'
        )
        links.append(
            f'<link rel="self" type="application/atom+xml" '
            f'href="https://www.blogger.com/feeds/{blog_id}/posts/default/{post_id}"/>'
        )

    categories = "".join(
        f'<category scheme="http://www.blogger.com/atom/ns#" term={_attr(label)}/>'
        for label in labels
    )
    return f"""  <entry>
    <id>{escape(entry_id(post_id, blog_id=blog_id))}</id>
    <published>{iso(published)}</published>
    <updated>{iso(updated)}</updated>
    <title type="text">{escape(title)}</title>
    <content type="html">{escape(content)}</content>
    <author><name>{escape(author)}</name><uri>https://www.blogger.com/profile/1</uri></author>
    {"".join(links)}
    {categories}
  </entry>"""


def feed(
    entries: Iterable[str] = (),
    *,
    total: int | None = None,
    next_url: str | None = None,
    start_index: int = 1,
    items_per_page: int | None = None,
    blog_id: str = BLOG_ID,
    title: str = "EquityPilot",
    updated: datetime | None = None,
    root: str = "feed",
) -> str:
    """A complete feed document."""
    entries = list(entries)
    updated = updated or datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)
    paging = f"<openSearch:totalResults>{total}</openSearch:totalResults>" if total else ""
    if items_per_page:
        paging += f"<openSearch:itemsPerPage>{items_per_page}</openSearch:itemsPerPage>"
    paging += f"<openSearch:startIndex>{start_index}</openSearch:startIndex>"
    next_link = (
        f'<link rel="next" type="application/atom+xml" href="{escape(next_url)}"/>'
        if next_url else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="http://www.w3.org/2005/Atom"
        xmlns:openSearch="http://a9.com/-/spec/opensearchrss/1.0/"
        xmlns:thr="http://purl.org/syndication/thread/1.0">
  <id>tag:blogger.com,1999:blog-{blog_id}</id>
  <updated>{iso(updated)}</updated>
  <title type="text">{escape(title)}</title>
  <subtitle type="html">Stock analysis for Indian investors.</subtitle>
  <link rel="http://schemas.google.com/g/2005#feed" type="application/atom+xml"
        href="{escape(FEED_URL)}"/>
  <generator version="7.00" uri="https://www.blogger.com">Blogger</generator>
  <author><name>{escape(AUTHOR)}</name></author>
  {paging}
  {next_link}
{"".join(entries)}
</{root}>"""


# ---------------------------------------------------------------------------
# A corpus shaped like the real blog's
# ---------------------------------------------------------------------------
BASE_TIME = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class FakePost:
    """One post in the fake corpus, in the shape `entry()` wants."""

    post_id: str
    title: str
    labels: tuple[str, ...] = ()
    text: str = ""
    #: Markup to serve verbatim instead of building it from `text`, for the
    #: tests that care about the HTML itself rather than the prose in it.
    content: str | None = None
    slug: str = ""
    published: datetime = BASE_TIME
    updated: datetime | None = None

    @property
    def url(self) -> str:
        """The canonical URL Blogger serves for this entry.

        Derived from the slug the way Blogger derives it from a title, so a test
        can assert the URL a citation should carry without repeating the rule.
        """
        return post_url(self.slug or self.title.lower().replace(" ", "-")[:40])

    def xml(self) -> str:
        markup = (
            self.content if self.content is not None
            else body(self.text or f"{self.title} — analysis of the business.")
        )
        return entry(
            self.post_id,
            self.title,
            content=markup,
            labels=list(self.labels),
            published=self.published,
            updated=self.updated,
            url=self.url,
        )


#: Modelled on the labels the live feed actually uses: a mix of explicit NSE
#: tickers, company names (one of them accented), generic topic labels, and one
#: term holding several newline-separated labels.
CORPUS: tuple[FakePost, ...] = (
    FakePost(
        post_id="4850132895787140759",
        title="Shriram Finance Share Price Analysis",
        labels=(
            "Finance Stocks", "Fundamental Analysis", "NBFC", "Share Price",
            "Shriram Finance\nStock Analysis",
        ),
        text=(
            "Shriram Finance is one of India's largest non-banking finance "
            "companies, with a retail book concentrated in commercial vehicle "
            "loans.\n\nAsset quality improved through the year, with the gross "
            "stage-three ratio at 2.4% of loans."
        ),
        published=BASE_TIME + timedelta(days=4),
    ),
    FakePost(
        post_id="1111111111111111111",
        title="TCS Q1 Review",
        labels=("TCS", "Quarterly Results", "IT Sector"),
        text="Tata Consultancy Services reported a strong first quarter.",
        published=BASE_TIME + timedelta(days=3),
    ),
    FakePost(
        post_id="2222222222222222222",
        title="Nestlé India: Rural Demand",
        # Accented, exactly as the live feed carries it.
        labels=("Nestlé India", "FMCG", "NESTLEIND"),
        text="Nestlé India's rural distribution deepened through the year.",
        published=BASE_TIME + timedelta(days=2),
    ),
    FakePost(
        post_id="3333333333333333333",
        title="L&T Stock Analysis",
        # "L&T" is the label; the company row's ticker is "LT".
        labels=("L&T", "Infrastructure", "Capital Goods"),
        text="Larsen & Toubro's order book reached a record.",
        published=BASE_TIME + timedelta(days=1),
    ),
    FakePost(
        post_id="4444444444444444444",
        title="What NBFCs Expect From The Budget",
        # A topic label and nothing that names a company.
        labels=("Budget", "NBFC", "Policy"),
        text="The budget is expected to address liquidity for lenders.",
        published=BASE_TIME,
    ),
)


@dataclass(slots=True)
class FakeBloggerServer:
    """Serves a fixed corpus over the fake fetcher interface.

    Implements Blogger's paging contract — a `start-index` and `max-results`
    pair, a `totalResults` count and a `rel="next"` link while entries remain —
    so the client's paging is exercised against the semantics it will meet, not
    against a stub that returns everything at once and hides a broken loop.
    """

    posts: tuple[FakePost, ...] = CORPUS
    base_url: str = FEED_URL
    #: When set, the next call raises this instead of serving a feed.
    error: Exception | None = None
    #: When set, served instead of a feed document — an HTML error page, say.
    bad_body: str | None = None
    requests: list[str] = field(default_factory=list)
    #: Rewrites a post's `updated` stamp, so a test can change the feed without
    #: rebuilding the corpus: the way an author editing a post looks from here.
    edits: dict[str, datetime] = field(default_factory=dict)

    def served(self) -> list[FakePost]:
        """The corpus as it is currently served, with edits applied."""
        out: list[FakePost] = []
        for post in self.posts:
            stamp = self.edits.get(post.post_id)
            out.append(
                FakePost(
                    post_id=post.post_id, title=post.title, labels=post.labels,
                    text=post.text, content=post.content, slug=post.slug,
                    published=post.published, updated=stamp or post.updated,
                ) if stamp else post
            )
        return out

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        if self.error is not None:
            raise self.error
        if self.bad_body is not None:
            return self.bad_body.encode("utf-8")

        parts = urlsplit(url)
        query = {key.lower(): value for key, value in parse_qsl(parts.query)}
        start = int(query.get("start-index", "1"))
        size = int(query.get("max-results", "150"))

        served = self.served()
        window = served[start - 1:start - 1 + size]
        remaining = len(served) - (start - 1 + len(window))
        next_url = (
            f"{self.base_url}?start-index={start + len(window)}&max-results={size}"
            if remaining > 0 and window else None
        )
        return feed(
            (post.xml() for post in window),
            total=len(served),
            next_url=next_url,
            start_index=start,
            items_per_page=size,
        ).encode("utf-8")


def server(*posts: FakePost, **kwargs: Any) -> FakeBloggerServer:
    """A server over an explicit corpus, rather than the default one."""
    return FakeBloggerServer(posts=tuple(posts) if posts else CORPUS, **kwargs)


def client_for(srv: FakeBloggerServer, *, page_size: int = 150):
    """A `BloggerFeedClient` wired to a fake server."""
    from app.services.blogger.feed import BloggerFeedClient

    return BloggerFeedClient(url=srv.base_url, page_size=page_size, fetcher=srv)


def one_post(
    post_id: str = "9000000000000000001",
    title: str = "Shriram Finance Share Price Analysis",
    labels: Sequence[str] = ("Shriram Finance", "NBFC"),
    text: str = "Shriram Finance is a large retail NBFC.",
    **kwargs: Any,
) -> FakePost:
    """A single post, for the tests that only care about one."""
    return FakePost(
        post_id=post_id, title=title, labels=tuple(labels), text=text, **kwargs,
    )


def parsed_post(post: FakePost | None = None, **kwargs: Any):
    """A `BloggerPost` — fixture input rendered to a feed and parsed back.

    Goes through the real parser rather than constructing the dataclass
    directly, so a test of the fingerprint or the metadata is testing what the
    sync will actually hold, not a hand-built object that could disagree with
    it.
    """
    from app.services.blogger.feed import parse_feed

    fake = post or one_post(**kwargs)
    return parse_feed(feed([fake.xml()])).posts[0]
