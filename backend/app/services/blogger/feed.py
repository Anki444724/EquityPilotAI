"""Fetch and parse a Blogger Atom feed.

Blogger publishes `https://<blog>.blogspot.com/feeds/posts/default` as Atom 1.0
with the OpenSearch paging extensions. Three properties of that feed drive the
design here:

* **It pages.** The default response carries 25 entries and advertises the rest
  through `openSearch:totalResults` and a `rel="next"` link, so a blog with 32
  posts is two requests and a blog with 300 is a dozen. `max-results` is
  honoured up to a ceiling Blogger imposes itself, which is why the client asks
  for a page at a time rather than one very large page that silently comes back
  short.
* **`updated` moves only on an edit.** That timestamp is the cheapest possible
  change detector, and using it means an idle sync costs one HTTP request and no
  database writes at all.
* **The content is HTML, escaped inside the entry.** It arrives as the post's
  real markup — `<style>` blocks, tables, inline charts and all — which is
  exactly what the platform's HTML parser already handles. It is not converted
  to text here and it is not dressed up as a PDF: the source format of a blog
  post is HTML, and pretending otherwise would throw away the tables the
  extractor recovers.

XML is parsed with the standard library and matched on *local* names rather than
namespace URIs. Blogger has historically served the same feed under more than
one namespace set (Atom, the OpenSearch and Google Data extensions, and a
`blogger:` namespace for its own fields), and a parser keyed on exact URIs
stops finding entries the day one of them changes — silently, with an empty
list, which reads as "the blog has no posts".

No database and no settings are touched here. The client is handed a URL and a
timeout; the caller decides what to do with the result.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)

#: Page size requested per feed call. Blogger caps `max-results` well below the
#: 500 some of its own documentation mentions, and a short page costs one more
#: request while an over-large one risks a truncated response. 150 is inside
#: every observed limit and keeps a single page's HTML in the low megabytes.
DEFAULT_PAGE_SIZE = 150

#: Hard ceiling on pages per sync, so a feed that keeps advertising a `next`
#: link without advancing cannot turn into an unbounded loop.
MAX_PAGES = 50

#: A feed response larger than this is treated as a source error rather than
#: parsed. 32 posts of rich HTML run to a few megabytes; this is the guard
#: against being handed something that is not the blog.
MAX_FEED_BYTES = 64 * 1024 * 1024

#: Identifies the platform to the feed host. A bare default client string is
#: the fastest way to be mistaken for a scraper and throttled.
USER_AGENT = "EquityPilotAI-BloggerSync/1.0 (+document ingestion)"

#: `tag:blogger.com,1999:blog-<blog id>.post-<post id>`
_ENTRY_ID = re.compile(r"blog-(\d+)\.post-(\d+)")

#: Blogger timestamps are RFC 3339 with milliseconds and a numeric offset
#: (`2026-09-05T05:35:06.786-07:00`). `fromisoformat` handles that from Python
#: 3.11 onwards, including a trailing `Z`; the fallbacks are for the two other
#: shapes the platform has been served.
_FALLBACK_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S",
)


class BloggerFeedError(Exception):
    """The feed could not be fetched or could not be read.

    Raised rather than returned as an empty list: "the blog is unreachable" and
    "the blog has no posts" must not look the same to the caller, because only
    one of them is a reason to keep whatever is already stored.
    """


@dataclass(frozen=True, slots=True)
class BloggerPost:
    """One entry of the feed, reduced to what ingestion needs.

    Immutable and free of any database or HTTP reference, so a parsed post can
    be asserted on directly in a test without a fixture blog.
    """

    #: The numeric post id from the entry's Atom id. Stable for the life of the
    #: post and unique within the blog, which is what makes it usable as the
    #: document's identity across syncs.
    post_id: str
    title: str
    content_html: str
    url: str = ""
    published: datetime | None = None
    updated: datetime | None = None
    labels: tuple[str, ...] = ()
    author: str = ""
    blog_id: str = ""
    #: The raw Atom id, kept for logs: it names the blog as well as the post.
    entry_id: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.content_html.strip())

    @property
    def effective_date(self) -> datetime | None:
        """The timestamp that represents "this version of the post"."""
        return self.updated or self.published

    def fingerprint(self) -> str:
        """Hash of the post's *substance*: title, labels and body.

        Deliberately excludes `updated`. The feed's timestamp says when Blogger
        last wrote the entry; this says whether what it wrote differs. The two
        normally agree, and when they do not — a re-publish that touched
        nothing — the fingerprint is the one that should decide whether the
        platform re-ingests, because re-ingesting an identical body creates a
        new document version that supersedes its predecessor for no reason.
        """
        canonical = "\n".join((
            self.title.strip().lower(),
            "\n".join(self.labels),
            _canonical_html(self.content_html),
        ))
        return hashlib.sha256(
            canonical.encode("utf-8"), usedforsecurity=False,
        ).hexdigest()


#: Whitespace runs, collapsed. Blogger's editor emits a different amount of it
#: depending on which of its own editors the author used, and none of it is
#: content.
_WHITESPACE = re.compile(r"\s+")


#: Whitespace sitting between two tags. Blogger's editors disagree about it —
#: the compose view emits none, the HTML view indents every element — and it is
#: never part of what a post says.
_BETWEEN_TAGS = re.compile(r">\s+<")


def _canonical_html(html: str) -> str:
    """Whitespace-normalised HTML, for fingerprinting only.

    Not a sanitiser and not a parser: the bytes that get ingested are the
    post's real markup, untouched. This exists so that a re-save which only
    reflows indentation does not look like an edit — because if it did, one
    author's tidying afternoon would supersede every document in the corpus,
    and each supersession re-embeds a post whose words never changed.

    Two steps, and the order matters: runs are collapsed first, so that the
    between-tag rule sees one space rather than an arbitrary run, and only then
    is whitespace between tags removed. Whitespace *inside* text survives as a
    single space, so "revenue grew" and "revenue  grew" fingerprint alike while
    "revenue grew" and "revenue fell" do not.
    """
    collapsed = _WHITESPACE.sub(" ", (html or "")).strip()
    return _BETWEEN_TAGS.sub("><", collapsed).lower()


# ---------------------------------------------------------------------------
# XML helpers — local-name matching, see the module docstring
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    """`{http://www.w3.org/2005/Atom}entry` → `entry`."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _parse_datetime(value: str) -> datetime | None:
    """Parse a feed timestamp into an aware UTC datetime, or None.

    Returns None rather than raising: one malformed timestamp in one entry is
    not a reason to lose the other thirty-one posts, and a post with no date is
    still perfectly ingestable. Everything is normalised to UTC so that a value
    stored during one sync compares equal to the same instant served with a
    different offset during the next.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(candidate)
        except ValueError:
            parsed = None
        if parsed is not None:
            return _as_utc(parsed)
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return _as_utc(datetime.strptime(candidate, fmt))
        except ValueError:
            continue
    log.debug("unparseable feed timestamp", value=raw[:40])
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # A feed that omits the offset is publishing Blogger's own local time.
        # Assuming UTC is the honest default: guessing an offset would make the
        # stored timestamp wrong by hours and the change detector with it.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime | None) -> str:
    """The one serialisation used for stored Blogger timestamps.

    Both the writer (document metadata) and the reader (the change detector)
    go through here, so a comparison between them is a comparison between two
    strings produced by the same function from the same instant — which is the
    only reason string equality is safe to rely on for "has this changed".
    """
    if value is None:
        return ""
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _labels(entry: ET.Element) -> tuple[str, ...]:
    """Category terms, in feed order, deduplicated.

    Two cleanups the real feed requires:

    * A label is free text, and authors paste multi-line lists into one. The
      live EquityPilot feed carries a single term holding eight newline-separated
      labels, which is useless as a mapping signal until it is split.
    * Blogger repeats a term when a post is labelled through more than one of
      its own surfaces.
    """
    found: list[str] = []
    seen: set[str] = set()
    for category in _children(entry, "category"):
        term = (category.get("term") or "").strip()
        if not term:
            continue
        for part in re.split(r"[\n\r]+", term):
            label = part.strip()
            # Case-insensitive dedup, but the author's own casing is kept: it
            # is what a human reads in the processing log.
            key = label.casefold()
            if label and key not in seen:
                seen.add(key)
                found.append(label)
    return tuple(found)


def _post_url(entry: ET.Element) -> str:
    """The canonical HTML link for the entry.

    A Blogger entry carries four links: the feed itself, the edit URL (which
    needs an account and must never be stored as a citation), the replies feed
    and the post. Only `rel="alternate"` with an HTML type is the post; falling
    back to "the first http link that is not a feed" keeps a citation working if
    the type attribute is missing, without ever picking the edit URL.
    """
    fallback = ""
    for link in _children(entry, "link"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = (link.get("rel") or "").strip().lower()
        link_type = (link.get("type") or "").strip().lower()
        if rel == "alternate" and ("html" in link_type or not link_type):
            return href
        if rel in ("edit", "replies", "self") or "/feeds/" in href:
            continue
        fallback = fallback or href
    return fallback


@dataclass(frozen=True, slots=True)
class FeedPage:
    """One page of the feed, with the paging metadata Blogger advertises."""

    posts: tuple[BloggerPost, ...]
    total_results: int | None = None
    start_index: int | None = None
    next_url: str | None = None
    blog_id: str = ""
    blog_title: str = ""
    feed_updated: datetime | None = None


def parse_feed(payload: bytes | str) -> FeedPage:
    """Parse one feed response into posts and paging metadata.

    Tolerant by construction. An entry that cannot yield an id is skipped with
    a log line rather than aborting the page: one malformed entry among thirty
    is a data problem at the source, and losing the other twenty-nine posts
    would be a bigger one here.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not payload.strip():
        raise BloggerFeedError("the feed response was empty")

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        # The message is included, the payload is not: a feed response can
        # carry an author's entire post, and a parse error that logs it would
        # put the blog's content into the platform's log stream.
        raise BloggerFeedError(f"the feed is not valid XML: {exc}") from exc

    if _local(root.tag) != "feed":
        # Refused rather than half-supported. Blogger serves RSS on the same
        # path with `?alt=rss`, where entries live under `channel/item` with
        # different element names again; reading that shape badly would produce
        # a sync that reports success and ingests nothing, which is worse than
        # an error naming the fix.
        raise BloggerFeedError(
            f"the feed's root element is '{_local(root.tag)}', not an Atom "
            "feed — if the URL ends in alt=rss, use the Atom feed instead"
        )

    entries = _children(root, "entry")

    total = _opensearch(root, "totalresults")
    start = _opensearch(root, "startindex")

    posts: list[BloggerPost] = []
    for entry in entries:
        post = _parse_entry(entry)
        if post is None:
            continue
        posts.append(post)

    next_url = None
    for link in _children(root, "link"):
        if (link.get("rel") or "").strip().lower() == "next":
            next_url = (link.get("href") or "").strip() or None
            break

    blog_title = _text(_child(root, "title"))
    author = _child(root, "author")
    if author is not None:
        # The blog's own title and the author's name are separate elements;
        # a text extraction that concatenates them reads as one string, which
        # is what made the first inspection of this feed confusing.
        name = _text(_child(author, "name"))
        if name and blog_title and not blog_title.endswith(name):
            blog_title = f"{blog_title} ({name})"

    return FeedPage(
        posts=tuple(posts),
        total_results=int(total) if total and total.isdigit() else None,
        start_index=int(start) if start and start.isdigit() else None,
        next_url=next_url,
        blog_id=_blog_id_from(_text(_child(root, "id"))),
        blog_title=blog_title,
        feed_updated=_parse_datetime(_text(_child(root, "updated"))),
    )


def _opensearch(root: ET.Element, name: str) -> str:
    """Read an `openSearch:*` element by local name.

    Compared case-insensitively because the real element names are camelCase —
    `totalResults`, `startIndex`, `itemsPerPage` — and a caller should not have
    to remember which letter Blogger capitalised for a value that is only ever
    read as a number.
    """
    wanted = name.lower()
    for child in root:
        if _local(child.tag).lower() == wanted:
            return _text(child)
    return ""


def _blog_id_from(entry_id: str) -> str:
    match = re.search(r"blog-(\d+)", entry_id or "")
    return match.group(1) if match else ""


def _parse_entry(entry: ET.Element) -> BloggerPost | None:
    raw_id = _text(_child(entry, "id"))
    match = _ENTRY_ID.search(raw_id)
    if match:
        blog_id, post_id = match.group(1), match.group(2)
    elif raw_id:
        # Not a Blogger post id — a comment entry, or a feed from a platform
        # that imitates Blogger's shape. Derive a stable identity from the id
        # itself so the post is still recognisable across syncs, and say in
        # the log that the id was not the documented one.
        blog_id = ""
        post_id = "x" + hashlib.sha1(
            raw_id.encode("utf-8"), usedforsecurity=False,
        ).hexdigest()[:19]
        log.info("feed entry id was not a Blogger post id", entry_id=raw_id[:120])
    else:
        log.warning("feed entry has no id; skipped")
        return None

    content = _child(entry, "content")
    body = _text(content)
    if not body:
        # Some feed configurations publish a summary instead of the body. A
        # summary is a truncated post, so it is used only when there is nothing
        # else, and the log records that the full text was not available.
        summary = _text(_child(entry, "summary"))
        if summary:
            log.info("feed entry carried a summary rather than content",
                     post_id=post_id)
        body = summary

    author_element = _child(entry, "author")
    author = _text(_child(author_element, "name")) if author_element is not None else ""

    return BloggerPost(
        post_id=post_id,
        title=_text(_child(entry, "title")),
        content_html=body,
        url=_post_url(entry),
        published=_parse_datetime(_text(_child(entry, "published"))),
        updated=_parse_datetime(_text(_child(entry, "updated"))),
        labels=_labels(entry),
        author=author,
        blog_id=blog_id,
        entry_id=raw_id,
    )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------
#: Injectable transport. Takes a URL, returns the response body. Tests hand in
#: a function over fixture bytes; production uses httpx.
Fetcher = Callable[[str], bytes]


def _http_fetch(url: str, *, timeout: float) -> bytes:
    """One GET, with the platform's own user agent and redirect following.

    httpx rather than a second HTTP dependency: it is already required by the
    market-data providers, the broker client and the AI provider router.
    """
    import httpx

    try:
        response = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Includes timeouts, connection failures and 4xx/5xx alike. The URL is
        # in the message because "which feed" is the first question, and it
        # carries no credential — a Blogger feed is public by definition.
        raise BloggerFeedError(f"could not fetch the feed {url}: {exc}") from exc

    payload = response.content
    if len(payload) > MAX_FEED_BYTES:
        raise BloggerFeedError(
            f"the feed response was {len(payload):,} bytes, above the "
            f"{MAX_FEED_BYTES:,} byte ceiling"
        )
    return payload


@dataclass(slots=True)
class BloggerFeedClient:
    """Paged reads of one Blogger feed.

    Constructed with a URL and a timeout, or with an explicit `fetcher` for a
    test. It holds no state between calls: the feed is the source of truth for
    what exists, and the database is the source of truth for what the platform
    already has, so there is nothing for the client to remember.
    """

    url: str
    timeout: float = 30.0
    page_size: int = DEFAULT_PAGE_SIZE
    fetcher: Fetcher | None = None
    #: Every URL actually requested, in order. Recorded because "the sync saw
    #: 32 posts" is not verifiable without knowing how many pages it read, and
    #: because a feed that advertises a `next` link pointing at itself is only
    #: visible in this list.
    requests_made: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (self.url or "").strip():
            raise BloggerFeedError("no feed URL is configured")
        self.url = self.url.strip()
        if self.page_size < 1:
            self.page_size = DEFAULT_PAGE_SIZE

    # ------------------------------------------------------------------
    def _get(self, url: str) -> bytes:
        self.requests_made.append(url)
        if self.fetcher is not None:
            try:
                payload = self.fetcher(url)
            except BloggerFeedError:
                raise
            except Exception as exc:  # noqa: BLE001 — one contract for callers
                # Wrapped so that an injected fetcher behaves like the real
                # transport: whatever goes wrong on the wire, the caller sees a
                # BloggerFeedError and can treat it as "the feed is unavailable"
                # rather than having to catch every exception a test double
                # might choose to raise.
                raise BloggerFeedError(f"could not fetch the feed {url}: {exc}") from exc
            if len(payload) > MAX_FEED_BYTES:
                raise BloggerFeedError(
                    f"the feed response was {len(payload):,} bytes, above the "
                    f"{MAX_FEED_BYTES:,} byte ceiling"
                )
            return payload
        return _http_fetch(url, timeout=self.timeout)

    def page_url(self, *, start_index: int, max_results: int) -> str:
        """The feed URL for one page.

        Built by replacing the paging parameters rather than appending them:
        Blogger's own `next` link already carries a `start-index`, and a URL
        with two of them is interpreted by the feed in a way that depends on
        the order they appear in.
        """
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(self.url)
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in {"start-index", "max-results"}
        ]
        query.append(("start-index", str(start_index)))
        query.append(("max-results", str(max_results)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def read_page(self, *, start_index: int = 1, max_results: int | None = None) -> FeedPage:
        """One page, for a caller that wants to drive the paging itself."""
        size = max_results or self.page_size
        return parse_feed(self._get(self.page_url(start_index=start_index, max_results=size)))

    def fetch_posts(self, *, max_posts: int) -> list[BloggerPost]:
        """Up to `max_posts` entries, newest first, following the feed's paging.

        Stops on any of: the budget is spent, the feed stops returning entries,
        the feed says there are no more, or the page ceiling is reached. The
        ceiling and the repeated-entry guard are both there for the same reason
        — a `next` link that points back at the page just read is a real
        Blogger behaviour on some feed configurations, and without a guard it
        is an infinite loop in a scheduled job that nobody is watching.
        """
        if max_posts <= 0:
            return []

        collected: list[BloggerPost] = []
        seen_ids: set[str] = set()
        start_index = 1
        pages = 0

        while len(collected) < max_posts and pages < MAX_PAGES:
            pages += 1
            remaining = max_posts - len(collected)
            size = min(self.page_size, remaining)
            page = self.read_page(start_index=start_index, max_results=size)

            if not page.posts:
                break

            fresh = [post for post in page.posts if post.post_id not in seen_ids]
            if not fresh:
                # The feed served the same entries again. Logged as a warning
                # because it means the paging did not advance, and stopping is
                # the only safe response: continuing would either loop forever
                # or re-ingest what is already stored.
                log.warning(
                    "feed paging did not advance", start_index=start_index,
                    entries=len(page.posts), collected=len(collected),
                )
                break

            for post in fresh:
                seen_ids.add(post.post_id)
                collected.append(post)

            start_index += len(page.posts)
            if page.total_results is not None and start_index > page.total_results:
                break
            if len(page.posts) < size:
                # A short page is the feed saying it has no more, whatever the
                # advertised total claims.
                break

        if len(collected) > max_posts:
            # A page is read whole, so the last one can overshoot the budget —
            # and Blogger serves `max-results` as a hint, not a guarantee.
            # Trimming keeps the contract exact: the newest `max_posts` posts,
            # which is what the budget is for.
            collected = collected[:max_posts]

        log.info(
            "blogger feed read", url=self.url, posts=len(collected),
            pages=pages, requested=max_posts,
        )
        return collected


def posts_from_payload(payload: bytes | str) -> list[BloggerPost]:
    """Parse a single feed response. For tests and one-off inspection.

    Production goes through :meth:`BloggerFeedClient.fetch_posts`, which pages;
    this exists so a saved response can be examined without a client.
    """
    return list(parse_feed(payload).posts)
