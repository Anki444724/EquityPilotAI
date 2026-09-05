"""Render a Blogger post into the document the platform ingests.

Three decisions live here, and each one exists because the alternative breaks
something further down the pipeline.

**The format is HTML.** A blog post's source *is* HTML — the tables, the
headings and the section structure that the extractor and the section detector
read are all in it. Rendering the same post to text would discard the tables
the platform's table extractor recovers, and dressing it up as a PDF would send
it through an OCR-and-layout path built for scanned filings. Neither is what
the post is.

**The filename is derived from the post id, not the title.** The ingestion
service finds a document's predecessor by `(company, filename)` and its
duplicates by content hash, so the filename is the post's identity across
syncs. A title-derived name changes when the author edits the title — which
would orphan every chunk, fact and citation already stored under the old name
and start a second document for the same post.

**The provenance is written into the document itself**, as `<meta>` tags in the
head, as well as being passed to `accept()`. The stored bytes are the only
thing guaranteed to survive: a database restored without a row, a re-index run
years later, a migration that rebuilds documents from storage — all of them can
recover the post id, the canonical URL and the label list from the artifact
rather than depending on state that lived somewhere else. The HTML parser reads
those tags back into `ParsedDocument.metadata`, which `_persist` merges into
`Document.doc_metadata`.

The rendering is **deterministic**: no timestamp of the moment of ingestion, no
run id, nothing that differs between two syncs of an unchanged post. Two syncs
of the same feed produce byte-identical documents, which is what lets the
existing content-hash deduplication — rather than a second mechanism invented
here — decide that nothing has changed.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any

from app.domain.documents.types import DocumentType, FileFormat
from app.services.blogger.feed import BloggerPost, iso_utc

#: Value of the `source` metadata key. Short and lower-case because it is
#: compared against, filtered on, and read by humans in a processing log.
BLOGGER_SOURCE = "blogger"

#: Written to `Document.uploaded_by`, mirroring the filing collector's
#: `"filing-collector"` so an operator can tell an automated ingest from a
#: human upload by the same column they already use.
INGESTED_BY = "blogger-sync"

#: A Blogger post is a research note: analysis written by the operator about a
#: company, not a filing the company produced. It is the closest of the
#: platform's existing document classes, and using an existing class matters —
#: a new one would need a place in the classifier, the register, the citation
#: labels and the scoring evidence routes to be treated as anything but OTHER.
DOCUMENT_TYPE = DocumentType.RESEARCH_NOTE

#: The extension is load-bearing: `DocumentParser.format_for` resolves the
#: parser from it, and `.html` is what selects the HTML path.
FILE_EXTENSION = ".html"

FILENAME_PREFIX = "blogger"

#: Post ids are digits, but the filename is built from an untrusted string, so
#: it is reduced to a safe alphabet rather than assumed to be well formed.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: A filename longer than this is truncated. The column is 400 characters and
#: the storage key embeds the name, so an unbounded title-derived suffix would
#: eventually collide with a database limit — the id alone cannot, but the
#: guard costs nothing.
MAX_FILENAME_CHARS = 80


def stable_filename(post_id: str) -> str:
    """`blogger-<post id>.html` — the same post's name on every sync.

    The post id is the identity because it is the one thing Blogger guarantees
    to keep for the life of a post: titles are edited, URLs change when a post
    is moved to a new date path, and labels are added and removed. An empty or
    unusable id raises rather than falling back to a hash of the title, because
    a filename that can drift is precisely the failure this function exists to
    prevent.
    """
    safe = _UNSAFE.sub("", (post_id or "").strip())
    if not safe:
        raise ValueError("a Blogger post id is required to name its document")
    stem = f"{FILENAME_PREFIX}-{safe}"[:MAX_FILENAME_CHARS]
    return f"{stem}{FILE_EXTENSION}"


def post_metadata(
    post: BloggerPost,
    *,
    company_ticker: str | None = None,
    company_name: str | None = None,
    mapped_by: str | None = None,
    matched_on: str | None = None,
) -> dict[str, Any]:
    """The provenance recorded on the document row and inside the HTML head.

    Values are strings except the label list, which stays a list: it is stored
    in a JSON column, and a comma-joined string would have to be split again by
    every reader that wants to filter on a label — including the ones that
    would then have to decide what to do about a label that contains a comma,
    which real Blogger labels do.

    There is deliberately no ingestion timestamp here. Metadata that changes on
    every run makes every run look like a change, and the point of the sync is
    that an unchanged blog writes nothing.
    """
    metadata: dict[str, Any] = {
        "source": BLOGGER_SOURCE,
        "blogger_post_id": post.post_id,
        "blogger_blog_id": post.blog_id,
        "blogger_entry_id": post.entry_id,
        "blogger_url": post.url,
        "blogger_title": post.title,
        "blogger_author": post.author,
        "blogger_published": iso_utc(post.published),
        "blogger_updated": iso_utc(post.updated),
        "blogger_labels": list(post.labels),
        #: Hash of title + labels + body. The answer to "did the substance
        #: change?" that does not depend on trusting Blogger's own timestamp.
        "blogger_content_fingerprint": post.fingerprint(),
        "ingested_by": INGESTED_BY,
    }
    if company_ticker:
        metadata["blogger_mapped_ticker"] = company_ticker
    if company_name:
        metadata["blogger_mapped_company"] = company_name
    if mapped_by:
        # Which rule attached this post to this company, and the exact label or
        # phrase that decided it. Without both, "why is this analysis under
        # BEL?" is an archaeology exercise across the feed's history.
        metadata["blogger_mapped_by"] = mapped_by
    if matched_on:
        metadata["blogger_matched_on"] = matched_on
    return metadata


def _meta_tag(key: str, value: Any) -> str | None:
    """One `<meta name=… content=…>` tag, or None for a value worth skipping."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        # Sorted keys and compact separators: the same list must serialise to
        # the same bytes on every sync, or the content hash changes and an
        # untouched post looks edited.
        text = json.dumps(
            list(value) if isinstance(value, tuple) else value,
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
    elif isinstance(value, datetime):
        text = iso_utc(value)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if not text.strip():
        return None
    # `quote=True` escapes the double quote, so a label containing one cannot
    # terminate the attribute early and turn the head into markup.
    return f'<meta name="{html.escape(key, quote=True)}" content="{html.escape(text, quote=True)}">'


def render_document(
    post: BloggerPost,
    *,
    metadata: dict[str, Any] | None = None,
    **metadata_extra: Any,
) -> bytes:
    """The HTML document to hand to `DocumentIngestionService.accept()`.

    `metadata` lets a caller that has already built the provenance dict — the
    sync does, because it passes the same dict to `accept()` — render the
    document from it instead of deriving a second copy that could drift from
    the one stored on the row.

    A complete document rather than the post's fragment, for two reasons: the
    HTML parser takes its title from `<title>`, and the provenance tags have to
    live in a head to be ordinary HTML rather than something a strict parser
    would relocate into the body.

    The post's own markup is inserted verbatim. It is not sanitised here
    because it is never served to a browser by this platform: it is stored as
    the document's source bytes and read by BeautifulSoup, which discards
    `<script>`, `<style>` and `<noscript>` before extracting text. Sanitising
    it would change the stored artifact and therefore its hash, and would
    remove exactly the tables and headings the extractor reads.
    """
    if metadata is None:
        metadata = post_metadata(post, **metadata_extra)

    head: list[str] = ['<meta charset="utf-8">']
    title = post.title.strip() or f"Blogger post {post.post_id}"
    head.append(f"<title>{html.escape(title)}</title>")
    for key, value in metadata.items():
        tag = _meta_tag(key, value)
        if tag is not None:
            head.append(tag)

    document = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n" + "\n".join(head) + "\n</head>\n"
        "<body>\n"
        "<article>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"{post.content_html.strip()}\n"
        "</article>\n"
        "</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")


def document_format() -> FileFormat:
    """The format a rendered post is ingested as. Exposed for the API surface."""
    return FileFormat.HTML
