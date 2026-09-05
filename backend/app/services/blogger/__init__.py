"""Blogger as a document source for the existing knowledge pipeline.

The point of this package is how little of it there is. A Blogger post is
fetched as Atom, rendered to an HTML document, and handed to
:class:`app.services.documents.ingestion.DocumentIngestionService` — the same
service an upload from the web UI and the filing collector both use. From that
call onwards the post is an ordinary `research_note` document: the existing
worker parses it, the existing chunker slices it, the existing embedder vectors
it, the existing hybrid retrieval finds it, and the existing analyst cites it.

There is deliberately no second index, no second embedding path and no second
retrieval engine here. A parallel RAG system for one blog would be a second
thing to keep correct, and the failure mode is not hypothetical: two indexes
disagree about what the platform knows, and the answer a user gets depends on
which one the question happened to reach.

Modules
-------
:mod:`~app.services.blogger.feed`
    Fetch and parse the Atom feed. No database, no settings beyond the feed URL
    and timeout it is handed.
:mod:`~app.services.blogger.mapping`
    Resolve a post to a company already in the database, or say honestly that
    it cannot be resolved.
:mod:`~app.services.blogger.document`
    Render a post into the HTML document that gets ingested, plus the stable
    filename and the provenance metadata that travel with it.
:mod:`~app.services.blogger.sync`
    Orchestration: what changed, what to ingest, what to leave alone. Also the
    `python -m app.services.blogger.sync` entry point.

Safety properties the sync is built around, each with a test:

* **Idempotent.** A post is re-ingested only when the feed says it changed, and
  even then the existing content-hash deduplication is what decides whether the
  result is a new version or nothing at all. Running it twice in a row writes
  nothing the second time.
* **Additive.** Nothing is ever deleted. A superseded version keeps its rows so
  a citation issued last month still resolves to the text it quoted.
* **Never guesses a company.** An unmapped post is logged and skipped rather
  than attached to whatever company happened to match a substring, which would
  put one company's analysis in front of another company's questions forever.
"""
from __future__ import annotations

from app.services.blogger.document import (
    BLOGGER_SOURCE, post_metadata, render_document, stable_filename,
)
from app.services.blogger.feed import (
    BloggerFeedClient, BloggerFeedError, BloggerPost,
)
from app.services.blogger.mapping import CompanyMapper, CompanyMatch
from app.services.blogger.sync import (
    BloggerSyncResult, BloggerSyncService, PostOutcome,
)

__all__ = [
    "BLOGGER_SOURCE",
    "BloggerFeedClient",
    "BloggerFeedError",
    "BloggerPost",
    "BloggerSyncResult",
    "BloggerSyncService",
    "CompanyMapper",
    "CompanyMatch",
    "PostOutcome",
    "post_metadata",
    "render_document",
    "stable_filename",
]
