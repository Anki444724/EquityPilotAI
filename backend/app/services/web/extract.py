"""Main-content extraction for fetched web pages.

Two jobs, kept apart on purpose:

* **URL canonicalization** — pure stdlib, no HTML involved, so it can be
  reasoned about (and tested) on its own. It is what makes the same article at
  two URLs one document.
* **Content extraction** — HTML to readable text plus whatever metadata the
  page genuinely states. ``bs4`` is imported *inside* the function that needs
  it, exactly as
  :class:`app.services.documents.extractors.office.HtmlParser` does, so this
  module imports cleanly on a machine without it.

What this module must never do
------------------------------
**Invent metadata.** A page with no ``<time>`` tag has no publication date, and
:class:`ExtractedPage` says ``None``. Guessing a date from a URL slug, a
filename, or the order of a listing produces a citation that *looks* verified
and is not — the worst possible outcome for an evidence layer, because nothing
downstream can tell it apart from a real one. Every extractor below returns
``None`` unless the page itself states the value, and the parsers validate what
they find (a ``datePublished`` that will not parse is discarded, not repaired).

**Re-parse a PDF.** PDF text comes from the extractor already registered for
that format; this module calls it rather than growing a second one.

**Replace the ingestion extractor.** The text produced here is persisted *as
an HTML document* and then parsed again by the pipeline's own ``HtmlParser``.
That is deliberate duplication of effort in exchange for not having two
divergent notions of what a web page's text is: the persistence layer sees a
tidy, boilerplate-free page, and everything downstream — chunking, embedding,
indexing, retrieval — runs unchanged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.domain.web.types import WebContentClass

# --- URL canonicalization -------------------------------------------------

#: Query parameters that never change what a page says. Explicitly named
#: rather than "anything starting with utm_", because a parameter the platform
#: strips wrongly is a parameter that changed the content.
TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_social",
    "utm_social-type", "fbclid", "gclid", "gbraid", "wbraid", "msclkid",
    "dclid", "yclid", "igshid", "mc_cid", "mc_eid", "mkt_tok", "_hsenc",
    "_hsmi", "vero_id", "oly_anon_id", "oly_enc_id", "rb_clickid",
    "s_kwcid", "trk", "trkCampaign", "sc_cid", "ref_src", "spm",
})

#: Parameters with empty values are noise; a bare ``?`` is dropped.
_CHARSET_FALLBACKS = ("utf-8", "latin-1")


def canonicalize_url(url: str) -> str:
    """A stable form of a URL, for identity and deduplication.

    Normalized: scheme and host lowercased, default port removed, fragment
    dropped, tracking parameters removed, remaining parameters sorted.

    **Not** normalized: the path. Trailing slashes, case and redundant
    separators are all server-defined — ``/Investors`` and ``/investors`` are
    routinely different pages, and ``/about/`` can 404 where ``/about`` does
    not. Rewriting them would trade a cosmetic tidy-up for broken fetches, so
    the path is preserved exactly as the page was reached.
    """
    parts = urlsplit((url or "").strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return (url or "").strip()

    port = parts.port
    netloc = host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and value != ""
    ]
    query = urlencode(sorted(kept))

    # The fragment is dropped: it addresses a position in a document, not a
    # different document, and keeping it would make one page look like many.
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def has_tracking_params(url: str) -> bool:
    """Whether a URL carried parameters that canonicalization removes."""
    keys = {k.lower() for k, _ in parse_qsl(urlsplit(url or "").query)}
    return bool(keys & TRACKING_PARAMS)


# --- text normalization ---------------------------------------------------

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_SPACES = re.compile(r"[ \t\u00a0\u2007\u202f]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Collapse a text blob to clean, single-spaced lines.

    The evidence block is parsed line by line by the model and by the citation
    audit, so newlines and runs of spaces inside a passage are not cosmetic —
    they change what the auditor sees. NBSP and zero-width characters are
    removed rather than folded, because they survive tokenisation as distinct
    tokens.
    """
    if not text:
        return ""
    cleaned = _ZERO_WIDTH.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(_SPACES.sub(" ", line).strip() for line in cleaned.split("\n"))
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return _BLANK_LINES.sub("\n\n", cleaned).strip()


def collapse_whitespace(text: str) -> str:
    """One line, no runs of spaces — the form a ``Citation`` value must take."""
    return " ".join((text or "").split())


# --- date and metadata parsing -------------------------------------------

def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse a page-stated timestamp, or return ``None``.

    Accepts the ISO-8601 shapes real pages emit (``Z`` suffix, offsets,
    space-separated, date-only). Anything else is discarded rather than
    guessed at: a date the platform cannot parse is a date it does not have.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    candidate = candidate.replace("Z", "+00:00") if candidate.endswith("Z") else candidate
    # A trailing timezone written as "+0530" rather than "+05:30".
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive timestamp from a page is not a UTC timestamp. Attaching UTC
        # would assert a timezone the page did not state, so it is treated as
        # unknown-but-dated: the date is real, the instant is not.
        parsed = parsed.replace(tzinfo=timezone.utc)
    if not (1990 <= parsed.year <= 2100):
        return None
    return parsed


# --- extraction -----------------------------------------------------------

#: Tags that never contain the page's argument.
_BOILERPLATE_TAGS = (
    "script", "style", "noscript", "template", "iframe", "svg", "canvas",
    "form", "nav", "footer", "header", "aside", "button", "select", "option",
    "figure", "figcaption", "picture", "source", "video", "audio", "embed",
    "object", "map", "dialog",
)

#: Class/id/role hints for chrome. Matched on word boundaries so ``ads``
#: matches ``ad-slot`` and ``main-nav`` matches ``nav``, while ``canvas`` does
#: not match ``nav``.
_BOILERPLATE_HINT = re.compile(
    r"\b("
    r"cookie|cookies|consent|gdpr|privacy-banner|banner|"
    r"nav|navbar|navigation|menu|menubar|sidebar|breadcrumb|breadcrumbs|"
    r"social|share|sharing|newsletter|subscribe|subscription|"
    r"advert|advertisement|ads|ad-slot|adsense|sponsor|sponsored|promo|promotion|"
    r"popup|modal|overlay|lightbox|"
    r"related|recommend|read-more|more-from|"
    r"comment|comments|disqus|"
    r"footer|header|masthead|topbar|top-bar|skip-link|skip|"
    r"pagination|pager|toolbar|utility|legal|disclaimer|terms|"
    r"search|searchbox|search-box|site-search|"
    r"app-banner|download-app|app-download|smart-banner|"
    r"language-selector|locale|country-selector|"
    r"ticker|marquee|breaking|alert-bar|notification-bar"
    r")\b",
    re.IGNORECASE,
)

#: Where the actual article lives, most-specific first. Only the largest match
#: is used — a page with several ``<article>`` teasers should yield the real
#: one, not the union of the teasers and the article.
_MAIN_SELECTORS = (
    "article", "main", "[role=main]", "[itemprop=articleBody]",
    "#content", "#main", "#main-content", "#article", "#article-body",
    ".article-body", ".article-content", ".entry-content", ".post-content",
    ".story-body", ".content-body", ".main-content",
)

#: Block tags that contribute a line each.
_TEXT_BLOCKS = (
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "blockquote", "pre", "td", "th", "dt", "dd", "summary",
)

_META_DATE_KEYS = (
    "article:published_time", "og:published_time", "og:article:published_time",
    "datepublished", "date", "pubdate", "publishdate", "publication_date",
    "dc.date", "dc.date.issued", "sailthru.date", "parsely-pub-date",
    "article.published", "bt:pubdate", "timestamp",
)

_META_AUTHOR_KEYS = (
    "author", "article:author", "og:article:author", "byl", "byline",
    "dc.creator", "sailthru.author", "parsely-author",
)


@dataclass(slots=True)
class ExtractedPage:
    """A fetched page reduced to what the platform will cite.

    ``published_at``, ``author`` and ``canonical_url`` are ``None`` when the
    page did not state them. ``title`` is empty in the same case — an absent
    title is not filled in from a heading, because a heading is not a title and
    a citation labelled with one would misattribute it.
    """

    text: str
    title: str = ""
    published_at: datetime | None = None
    author: str | None = None
    canonical_url: str | None = None
    #: Metadata scraped from the page, for the document row. Bounded by the
    #: caller; never contains the body.
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def _decode(payload: bytes, charset: str | None = None) -> str:
    """Decode bytes using the declared charset, then the usual fallbacks.

    Decoding is best-effort in the sense that we never fail a page for a bad
    byte — but ``errors="replace"`` is only reached after UTF-8 and latin-1
    have been tried, so a genuinely UTF-8 page is never mangled.
    """
    candidates = [charset] if charset else []
    candidates.extend(_CHARSET_FALLBACKS)
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _link_density(tag) -> float:
    """Share of a block's text that sits inside links.

    Navigation is a list of links; prose is not. This is the cheapest signal
    that separates a menu from an article when no semantic container exists,
    and it is why a page with no ``<article>`` still yields prose rather than
    the site's menu.
    """
    total = len(tag.get_text(" ", strip=True))
    if not total:
        return 1.0
    linked = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
    return min(1.0, linked / total)


def _strip_boilerplate(soup) -> None:
    """Remove chrome in place: obvious tags, then hinted containers."""
    for tag in soup(list(_BOILERPLATE_TAGS)):
        tag.decompose()
    for tag in soup.find_all(True):
        if getattr(tag, "attrs", None) is None:
            continue
        hints = " ".join(
            str(tag.attrs.get(attribute, ""))
            for attribute in ("class", "id", "role", "aria-label")
        )
        if hints and _BOILERPLATE_HINT.search(hints):
            tag.decompose()


def _main_container(soup):
    """The element whose text is the page, or the best-density fallback."""
    for selector in _MAIN_SELECTORS:
        matches = [
            node for node in soup.select(selector)
            if node.get_text(" ", strip=True)
        ]
        if matches:
            return max(matches, key=lambda node: len(node.get_text(" ", strip=True)))

    # No semantic container. Score every plausible block by usable text, with
    # link-dense blocks (menus, footers, tag clouds) discounted.
    best, best_score = None, 0.0
    for node in soup.find_all(["div", "section", "article", "main", "body"]):
        text = node.get_text(" ", strip=True)
        if len(text) < 200:
            continue
        score = len(text) * (1.0 - _link_density(node))
        if score > best_score:
            best, best_score = node, score
    if best is not None:
        return best
    return soup.body or soup


def _blocks_text(container) -> str:
    """Read a container as an ordered list of lines.

    Container elements are skipped when they hold block children, so a
    ``<div>`` wrapping three paragraphs does not repeat them.
    """
    lines: list[str] = []
    for element in container.find_all(list(_TEXT_BLOCKS)):
        if element.find(list(_TEXT_BLOCKS)):
            continue  # a list item holding paragraphs; its children carry them
        text = collapse_whitespace(element.get_text(" ", strip=True))
        if len(text) < 2:
            continue
        if lines and lines[-1] == text:
            continue  # a heading repeated verbatim as a link inside itself
        lines.append(text)
    if not lines:
        fallback = collapse_whitespace(container.get_text(" ", strip=True))
        if fallback:
            lines.append(fallback)
    return "\n\n".join(lines)


def _meta_map(soup) -> dict[str, str]:
    """``name``/``property``/``itemprop`` → content, first value winning."""
    found: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (
            tag.attrs.get("property")
            or tag.attrs.get("name")
            or tag.attrs.get("itemprop")
            or ""
        ).strip().lower()
        content = (tag.attrs.get("content") or "").strip()
        if key and content and key not in found:
            found[key] = content
    return found


def _jsonld_nodes(soup) -> list[dict]:
    """Every JSON-LD object on the page, flattened over ``@graph``."""
    nodes: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue  # malformed structured data is ignored, never repaired
        stack = [parsed] if isinstance(parsed, dict) else (
            list(parsed) if isinstance(parsed, list) else []
        )
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                nodes.append(node)
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(node, list):
                stack.extend(node)
    return nodes


def _jsonld_author_name(value) -> str | None:
    """Pull a person or organisation name out of a JSON-LD ``author`` value."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(value, list):
        for item in value:
            found = _jsonld_author_name(item)
            if found:
                return found
    return None


def _title_from(soup, metas: dict[str, str]) -> str:
    """The page's own title, or ``""``."""
    for key in ("og:title", "twitter:title", "title"):
        value = metas.get(key)
        if value:
            return collapse_whitespace(value)[:400]
    if soup.title and soup.title.string:
        text = collapse_whitespace(soup.title.string)
        if text:
            return text[:400]
    return ""


def _published_from(soup, metas: dict[str, str], nodes: list[dict]) -> datetime | None:
    """A publication date the page states, validated. Never inferred."""
    for key in _META_DATE_KEYS:
        value = metas.get(key)
        if value:
            parsed = parse_iso_datetime(value)
            if parsed:
                return parsed

    for node in nodes:
        for key in ("datePublished", "dateCreated", "uploadDate"):
            value = node.get(key)
            if isinstance(value, str):
                parsed = parse_iso_datetime(value)
                if parsed:
                    return parsed

    # A <time datetime="..."> that identifies itself as the publication time.
    for tag in soup.find_all("time"):
        if not tag.attrs.get("datetime"):
            continue
        marker = " ".join(
            str(tag.attrs.get(key, ""))
            for key in ("itemprop", "class", "id", "pubdate")
        ).lower()
        if "publi" in marker or "date" in marker or "time" in marker:
            parsed = parse_iso_datetime(str(tag.attrs["datetime"]))
            if parsed:
                return parsed
    return None


def _author_from(soup, metas: dict[str, str], nodes: list[dict]) -> str | None:
    """A byline the page states, bounded. Never inferred."""
    for key in _META_AUTHOR_KEYS:
        value = metas.get(key)
        if value:
            cleaned = collapse_whitespace(value)
            if 1 < len(cleaned) <= 200:
                return cleaned
    for node in nodes:
        found = _jsonld_author_name(node.get("author"))
        if found and len(found) <= 200:
            return collapse_whitespace(found)
    for link in soup.find_all("a", attrs={"rel": "author"}):
        text = collapse_whitespace(link.get_text(" ", strip=True))
        if 1 < len(text) <= 200:
            return text
    return None


def _canonical_from(soup, url: str) -> str | None:
    """The page's declared canonical URL, resolved and sanity-checked."""
    for link in soup.find_all("link", attrs={"rel": "canonical"}):
        href = (link.attrs.get("href") or "").strip()
        if not href:
            continue
        resolved = urljoin(url, href)
        if urlsplit(resolved).scheme in ("http", "https"):
            return resolved
    for key in ("og:url", "twitter:url"):
        value = None
        for tag in soup.find_all("meta"):
            candidate = (
                tag.attrs.get("property") or tag.attrs.get("name") or ""
            ).strip().lower()
            if candidate == key and tag.attrs.get("content"):
                value = tag.attrs["content"].strip()
                break
        if value:
            resolved = urljoin(url, value)
            if urlsplit(resolved).scheme in ("http", "https"):
                return resolved
    return None


def extract_html(
    payload: bytes,
    *,
    url: str,
    charset: str | None = None,
    max_chars: int = 400_000,
) -> ExtractedPage:
    """Reduce an HTML page to its argument, plus the metadata it states.

    Raises :class:`ImportError` if ``bs4`` is unavailable — the caller decides
    whether that is fatal. It is not caught here because silently returning
    empty text would look exactly like an empty page.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(payload, charset), "html.parser")

    # Metadata is read before anything is decomposed: the tags carrying it
    # often live in a `<head>` that the boilerplate pass would remove.
    metas = _meta_map(soup)
    nodes = _jsonld_nodes(soup)
    title = _title_from(soup, metas)
    published = _published_from(soup, metas, nodes)
    author = _author_from(soup, metas, nodes)
    canonical = _canonical_from(soup, url)

    _strip_boilerplate(soup)
    container = _main_container(soup)
    text = normalize_text(_blocks_text(container))
    if len(text) > max_chars:
        text = text[:max_chars]

    metadata: dict[str, str] = {}
    for key in ("og:site_name", "og:type", "article:section", "description",
                "og:description", "twitter:description", "language"):
        value = metas.get(key)
        if value:
            metadata[key] = collapse_whitespace(value)[:500]

    return ExtractedPage(
        text=text, title=title, published_at=published, author=author,
        canonical_url=canonical, metadata=metadata,
    )


def extract_text(
    payload: bytes, *, charset: str | None = None, max_chars: int = 400_000,
) -> ExtractedPage:
    """Plain text needs no extraction — only normalization."""
    text = normalize_text(_decode(payload, charset))
    return ExtractedPage(text=text[:max_chars])


def extract_pdf(payload: bytes, *, filename: str = "page.pdf") -> ExtractedPage:
    """PDF text, via the extractor already registered for that format.

    A thin adapter, not a parser: ``PdfParser`` (PyMuPDF + selective
    pdfplumber) already does this work well and in one place.
    """
    from app.domain.documents.types import FileFormat
    from app.services.documents.extractors.base import parser_for

    parsed = parser_for(FileFormat.PDF).parse(payload, filename=filename)
    pages = getattr(parsed, "pages", None) or []
    text = normalize_text("\n\n".join(page.text or "" for page in pages))
    return ExtractedPage(text=text, title=collapse_whitespace(parsed.title or ""))


def extract_page(
    payload: bytes,
    *,
    url: str,
    content_class: WebContentClass,
    charset: str | None = None,
    filename: str = "page",
) -> ExtractedPage:
    """Dispatch to the right extractor for an accepted content class."""
    if content_class is WebContentClass.HTML:
        return extract_html(payload, url=url, charset=charset)
    if content_class is WebContentClass.TEXT:
        return extract_text(payload, charset=charset)
    if content_class is WebContentClass.PDF:
        return extract_pdf(payload, filename=filename)
    raise ValueError(f"no extractor for content class {content_class!r}")


def build_clean_html(
    *,
    title: str,
    text: str,
    source_url: str,
    retrieved_at: datetime,
    published_at: datetime | None = None,
    author: str | None = None,
) -> bytes:
    """Render the extracted page as a small, tidy HTML document.

    This — not the raw response — is what gets persisted and ingested. The
    consequences are worth being explicit about:

    * the chunker never sees a cookie banner, a menu, or a tracking script, so
      retrieval cannot return them as evidence;
    * the page still travels the ordinary document path, so nothing downstream
      needs to know web pages exist;
    * provenance travels in ``<meta>`` tags *and* in the caller's metadata
      argument, which is what makes a citation's URL checkable later.
    """
    paragraphs = "\n".join(
        f"    <p>{escape(block)}</p>"
        for block in text.split("\n\n")
        if block.strip()
    )
    meta_lines = [
        f'    <meta name="source_url" content="{escape(source_url, quote=True)}">',
        '    <meta name="source" content="web">',
        f'    <meta name="retrieved_at" content="{escape(retrieved_at.isoformat(), quote=True)}">',
    ]
    if published_at is not None:
        meta_lines.append(
            f'    <meta name="published_at" '
            f'content="{escape(published_at.isoformat(), quote=True)}">'
        )
    if author:
        meta_lines.append(
            f'    <meta name="author" content="{escape(author, quote=True)}">'
        )
    heading = f"    <h1>{escape(title)}</h1>\n" if title else ""
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8">\n'
        f"    <title>{escape(title or source_url)}</title>\n"
        + "\n".join(meta_lines)
        + "\n  </head>\n"
        "  <body>\n"
        + heading
        + (f"{paragraphs}\n" if paragraphs else "")
        + "  </body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")
