"""Blogger sync: what changed in the feed, and what to do about it.

    Blogger feed → this service → DocumentIngestionService.accept()
                 → the existing document worker, chunker, embedder, vector
                   store, hybrid retrieval, knowledge vault and AI analyst.

The arrow after `accept()` is the point. This module decides *whether* a post
needs ingesting and *which company* it belongs to, and then hands it to the
pipeline the platform already runs for uploads and collected filings. It parses
nothing, chunks nothing, embeds nothing and retrieves nothing, and it starts no
worker of its own: the document rows it creates land in `document_jobs`, which
the existing document worker drains, and the sync itself runs as a
`JobKind.BLOGGER_SYNC` job in the existing platform worker on the existing
schedule.

Run by hand with::

    python -m app.services.blogger.sync                 # uses BLOGGER_* from the environment
    python -m app.services.blogger.sync --dry-run       # report what would change, write nothing
    python -m app.services.blogger.sync --force         # ignore the unchanged fast path

Inside a container that is the production configuration, so the database and
Redis it touches are the ones Docker injected::

    docker compose exec worker python -m app.services.blogger.sync

Safety, in the order it is enforced:

1. **Nothing is deleted.** Not a document, not a chunk, not a fact, not a
   company. A post that moves company supersedes its old row — the same
   mechanism a new version of an annual report uses — so a citation issued
   against the old row still resolves to the text it quoted.
2. **An unchanged post is not touched.** The feed's own `updated` timestamp and
   a content fingerprint are compared against what is stored; a match writes no
   row and enqueues no job. Behind that, the ingestion service's content-hash
   deduplication is the second gate, so even a fast path that misjudges costs a
   hash comparison and not a new version.
3. **A post with no confident company is skipped and logged**, never attached to
   whatever matched. See :mod:`app.services.blogger.mapping`.
4. **One bad post cannot stop the run.** Each post is handled inside its own
   guard, with a rollback before the next one, because a session left dirty by a
   failed flush would fail every subsequent commit and turn one malformed entry
   into a dead sync.
"""
from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.documents.types import DocumentStatus
from app.models.document import Document
from app.services.blogger.document import (
    DOCUMENT_TYPE, FILENAME_PREFIX, INGESTED_BY, post_metadata, render_document,
    stable_filename,
)
from app.services.blogger.feed import (
    BloggerFeedClient, BloggerFeedError, BloggerPost, iso_utc,
)
from app.services.blogger.mapping import CompanyMapper, CompanyMatch, MappingSummary

log = structlog.get_logger(__name__)

#: Outcomes recorded in a job result or a printed summary. A sync of a
#: hundred-post blog produces a hundred of these, and the queue stores its
#: result in a JSON column — so the detail is capped and the counts are not.
MAX_RECORDED_OUTCOMES = 50


@dataclass(slots=True)
class PostOutcome:
    """What happened to one post."""

    post_id: str
    title: str
    action: str
    url: str = ""
    reason: str = ""
    company_id: str | None = None
    ticker: str | None = None
    document_id: int | None = None
    version: int | None = None
    job_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "title": self.title[:160],
            "action": self.action,
            "url": self.url,
            "reason": self.reason or None,
            "ticker": self.ticker,
            "company_id": self.company_id,
            "document_id": self.document_id,
            "version": self.version,
            "job_id": self.job_id,
        }


#: Actions that mean the corpus grew or a document was re-run.
CHANGED_ACTIONS = frozenset({"ingested", "new_version", "requeued"})
#: Actions that mean nothing was written.
QUIET_ACTIONS = frozenset({"unchanged", "duplicate", "skipped", "would_ingest"})


@dataclass(slots=True)
class BloggerSyncResult:
    """One run's outcome. Returned by the service, stored by the job handler."""

    enabled: bool = False
    feed_url: str = ""
    dry_run: bool = False
    fetched: int = 0
    pages_fetched: int = 0
    ingested: int = 0
    new_versions: int = 0
    requeued: int = 0
    duplicates: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    moved_company: int = 0
    outcomes: list[PostOutcome] = field(default_factory=list)
    mapping: MappingSummary = field(default_factory=MappingSummary)
    duration_ms: float = 0.0
    #: Set when the run itself could not proceed — disabled, unconfigured, or
    #: the feed was unreadable. Per-post failures are counted, not fatal.
    error: str | None = None

    @property
    def ran(self) -> bool:
        return self.error is None

    @property
    def changed(self) -> int:
        return self.ingested + self.new_versions + self.requeued

    @property
    def is_clean(self) -> bool:
        """Nothing went wrong and nothing was left unresolved."""
        return self.error is None and self.failed == 0 and self.skipped == 0

    def record(self, outcome: PostOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.action == "ingested":
            self.ingested += 1
        elif outcome.action == "new_version":
            self.new_versions += 1
        elif outcome.action == "requeued":
            self.requeued += 1
        elif outcome.action == "duplicate":
            self.duplicates += 1
        elif outcome.action == "unchanged":
            self.unchanged += 1
        elif outcome.action == "skipped":
            self.skipped += 1
        elif outcome.action == "failed":
            self.failed += 1

    def as_dict(self, *, include_outcomes: bool = True) -> dict[str, Any]:
        """A JSON-safe summary.

        Outcomes are capped and only the interesting ones are kept in full:
        an operator reading a job result wants the failures and the posts that
        could not be mapped, not a hundred lines saying "unchanged".
        """
        payload: dict[str, Any] = {
            "enabled": self.enabled,
            "feed_url": self.feed_url,
            "dry_run": self.dry_run,
            "fetched": self.fetched,
            "pages_fetched": self.pages_fetched,
            "ingested": self.ingested,
            "new_versions": self.new_versions,
            "requeued": self.requeued,
            "duplicates": self.duplicates,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "moved_company": self.moved_company,
            "changed": self.changed,
            "duration_ms": round(self.duration_ms, 1),
            "mapping": self.mapping.as_dict(),
            "error": self.error,
        }
        if include_outcomes:
            notable = [
                outcome for outcome in self.outcomes
                if outcome.action not in ("unchanged", "duplicate")
            ]
            chosen = notable[:MAX_RECORDED_OUTCOMES]
            if not chosen:
                chosen = self.outcomes[:MAX_RECORDED_OUTCOMES]
            payload["outcomes"] = [outcome.as_dict() for outcome in chosen]
            payload["outcomes_truncated"] = len(notable) > len(chosen)
        return payload


class BloggerSyncService:
    """Fetches the feed and ingests what is new or changed.

    Every collaborator is injectable — the feed client, the company mapper, the
    ingestion service — so the whole run can be driven in a test against a
    fixture feed and an in-memory database with no network and no volume.
    """

    def __init__(
        self,
        db: Session,
        *,
        feed: BloggerFeedClient | None = None,
        mapper: CompanyMapper | None = None,
        ingestion: Any | None = None,
        storage: Any | None = None,
    ) -> None:
        self.db = db
        self._feed = feed
        self._mapper = mapper
        self._ingestion = ingestion
        self._storage = storage
        #: Filenames of the corpus's current Blogger documents, loaded once per
        #: run. One query for the whole sync rather than one per post, and the
        #: view is kept current as the run ingests, so a post cannot be
        #: compared against a snapshot that its own predecessor invalidated.
        self._known: dict[str, list[Document]] = {}
        self._known_loaded = False

    # ------------------------------------------------------------------
    # Collaborators, built on first use so that constructing the service
    # never fails on missing configuration
    # ------------------------------------------------------------------
    @property
    def feed_client(self) -> BloggerFeedClient:
        if self._feed is None:
            self._feed = BloggerFeedClient(
                url=settings.BLOGGER_FEED_URL,
                timeout=settings.BLOGGER_SYNC_TIMEOUT_SECONDS,
            )
        return self._feed

    @property
    def mapper(self) -> CompanyMapper:
        if self._mapper is None:
            self._mapper = CompanyMapper(
                self.db, default_ticker=settings.BLOGGER_DEFAULT_TICKER,
            )
        return self._mapper

    @property
    def ingestion(self):
        if self._ingestion is None:
            from app.services.documents.ingestion import DocumentIngestionService

            self._ingestion = DocumentIngestionService(self.db, storage=self._storage)
        return self._ingestion

    # ==================================================================
    # The run
    # ==================================================================
    def sync(
        self,
        *,
        max_posts: int | None = None,
        force: bool = False,
        dry_run: bool = False,
        allow_disabled: bool = False,
    ) -> BloggerSyncResult:
        """One complete pass over the feed.

        `force` ignores the unchanged fast path and lets the content hash
        decide, which is the way to re-check a corpus after changing how
        documents are rendered. `dry_run` resolves and reports without writing
        a single row. `allow_disabled` is the CLI's explicit override for a
        one-off run against a deployment where the scheduled sync is off.
        """
        started = time.perf_counter()
        feed_url = (settings.BLOGGER_FEED_URL or "").strip()
        enabled = bool(settings.BLOGGER_ENABLED)
        result = BloggerSyncResult(
            enabled=enabled, feed_url=feed_url, dry_run=dry_run,
        )

        def finish() -> BloggerSyncResult:
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        if not enabled and not allow_disabled:
            result.error = (
                "BLOGGER_ENABLED is false, so the sync did not run "
                "(pass allow_disabled for a one-off)"
            )
            log.info("blogger sync not run", reason="disabled")
            return finish()
        if not feed_url:
            result.error = "BLOGGER_FEED_URL is not configured"
            log.warning("blogger sync not run", reason="no feed url")
            return finish()

        budget = settings.BLOGGER_MAX_POSTS if max_posts is None else int(max_posts)
        try:
            client = self.feed_client
            posts = client.fetch_posts(max_posts=budget)
            result.pages_fetched = len(client.requests_made)
        except BloggerFeedError as exc:
            # The corpus is left exactly as it was. An unreachable feed is not
            # evidence that a post disappeared, and the next run retries.
            result.error = str(exc)
            log.error("blogger feed unreadable", error=str(exc)[:300])
            return finish()

        result.fetched = len(posts)
        self._load_known_documents()

        for post in posts:
            outcome = self._sync_one(post, force=force, dry_run=dry_run, result=result)
            result.record(outcome)

        log.info(
            "blogger sync complete",
            fetched=result.fetched, ingested=result.ingested,
            new_versions=result.new_versions, requeued=result.requeued,
            unchanged=result.unchanged, duplicates=result.duplicates,
            skipped=result.skipped, failed=result.failed,
            moved_company=result.moved_company, pages=result.pages_fetched,
            dry_run=dry_run, ms=round(result.duration_ms, 1),
        )
        return finish()

    # ------------------------------------------------------------------
    def _load_known_documents(self) -> None:
        """Index the current Blogger documents by filename, once per run.

        Matched on the filename prefix rather than on a JSON metadata query:
        `doc_metadata` is a JSON column, and a predicate over it would work
        differently on SQLite and on Postgres — the two databases this platform
        promises to run on. The filename is the post's identity by construction,
        so it is also the cheapest correct key.
        """
        rows = self.db.execute(
            select(Document)
            .where(Document.filename.like(f"{FILENAME_PREFIX}-%"))
            .order_by(Document.version.desc())
        ).scalars()
        known: dict[str, list[Document]] = {}
        for row in rows:
            if row.superseded_by is None:
                known.setdefault(row.filename, []).append(row)
        self._known = known
        self._known_loaded = True

    def _current_documents(self, filename: str) -> list[Document]:
        if not self._known_loaded:
            self._load_known_documents()
        return list(self._known.get(filename, ()))

    # ------------------------------------------------------------------
    def _sync_one(
        self,
        post: BloggerPost,
        *,
        force: bool,
        dry_run: bool,
        result: BloggerSyncResult,
    ) -> PostOutcome:
        """Decide and act on one post. Never raises."""
        base = {"post_id": post.post_id, "title": post.title, "url": post.url}
        try:
            return self._handle_post(post, base, force=force, dry_run=dry_run,
                                     result=result)
        except Exception as exc:  # noqa: BLE001 — one post must not stop the run
            # Rollback first: a failed flush leaves the session unusable, and
            # every commit after it would fail too, which is how one malformed
            # entry used to end the run.
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.exception(
                "blogger post failed", post_id=post.post_id,
                title=post.title[:120],
            )
            return PostOutcome(
                **base, action="failed",
                reason=f"{type(exc).__name__}: {exc}"[:500],
            )

    def _handle_post(
        self,
        post: BloggerPost,
        base: dict[str, Any],
        *,
        force: bool,
        dry_run: bool,
        result: BloggerSyncResult,
    ) -> PostOutcome:
        from app.services.documents.ingestion import IngestionError

        try:
            filename = stable_filename(post.post_id)
        except ValueError as exc:
            return PostOutcome(**base, action="failed", reason=str(exc))

        if not post.has_content:
            # Nothing to index. Recorded rather than silently dropped: a feed
            # that starts serving empty bodies is a change worth seeing.
            log.info("blogger post has no content", post_id=post.post_id)
            return PostOutcome(**base, action="skipped",
                               reason="the feed entry has no content")

        existing = self._current_documents(filename)
        match: CompanyMatch = self.mapper.resolve(title=post.title, labels=post.labels)
        result.mapping.add(match)

        if match.company is None:
            # Unresolved. Skipped, logged with enough detail to fix — the
            # labels actually present and the reason it failed — and left
            # unattached. Guessing here would put this analysis in front of
            # another company's questions permanently.
            log.info(
                "blogger post not mapped to a company",
                post_id=post.post_id, title=post.title[:160],
                url=post.url, labels=list(post.labels)[:20],
                reason=match.reason, considered=list(match.considered),
            )
            return PostOutcome(**base, action="skipped",
                               reason=match.reason or "no company matched")

        company = match.company
        outcome_base = {
            **base, "company_id": company.id, "ticker": company.ticker,
        }

        # --- is this post already stored, current, and unchanged? -------
        same_company = [row for row in existing if row.company_id == company.id]
        if not force:
            for row in same_company:
                if row.status == DocumentStatus.FAILED.value:
                    if self._stored_source_is_available(row):
                        # A previous run got as far as storing the bytes and
                        # then failed in the pipeline. The source is durable,
                        # so the right action is the platform's own re-index
                        # rather than a second document — and it is why
                        # keeping the original bytes was worth the storage.
                        if dry_run:
                            return PostOutcome(**outcome_base, action="would_ingest",
                                               document_id=row.id,
                                               reason="the stored version failed; it would be re-indexed")
                        job_id = self.ingestion.reprocess(row.id)
                        log.info("blogger document requeued", document_id=row.id,
                                 post_id=post.post_id, job_id=job_id)
                        return PostOutcome(**outcome_base, action="requeued",
                                           document_id=row.id, version=row.version,
                                           job_id=job_id,
                                           reason="the stored version had failed; re-indexed from its source")
                    # The stored bytes are gone: a legacy row whose object fell
                    # out of the shared volume, or one that predates source
                    # retention. `reprocess()` has nothing to read, so it is
                    # not called. The feed is the authoritative source for a
                    # Blogger post, so the recovery is a fresh render through
                    # `accept()`: the bytes return to storage, and the
                    # existing pipeline either re-indexes the same row
                    # (unchanged content) or supersedes it with a new version
                    # (edited post).
                    if dry_run:
                        return PostOutcome(**outcome_base, action="would_ingest",
                                           document_id=row.id,
                                           reason="the stored source is missing; it would be recovered from the feed")
                    log.warning(
                        "blogger stored source missing; recovering from the feed",
                        document_id=row.id, storage_key=row.storage_key,
                        post_id=post.post_id,
                    )
                    break

                unchanged_by = self._unchanged_reason(row, post)
                if unchanged_by:
                    return PostOutcome(**outcome_base, action="unchanged",
                                       document_id=row.id, version=row.version,
                                       reason=unchanged_by)

        metadata = post_metadata(
            post,
            company_ticker=company.ticker,
            company_name=company.name,
            mapped_by=match.method,
            matched_on=match.matched_on,
        )

        if dry_run:
            return PostOutcome(
                **outcome_base, action="would_ingest",
                document_id=same_company[0].id if same_company else None,
                reason=(
                    "new post; no document is stored for it"
                    if not existing else
                    "the stored document differs from what the feed now serves"
                ),
            )

        # --- hand it to the existing pipeline ---------------------------
        #
        # `accept()` is the whole integration. It stores the bytes, creates or
        # versions the row, detects a byte-identical duplicate by content hash,
        # and enqueues the job the existing document worker will claim. Nothing
        # below this line parses, chunks, embeds or indexes: that is all still
        # the pipeline's, and it runs at the same priority as every other
        # document in the queue.
        payload = render_document(post, metadata=metadata)
        try:
            accepted = self.ingestion.accept(
                company.id,
                payload,
                filename,
                doc_type=DOCUMENT_TYPE,
                uploaded_by=INGESTED_BY,
                metadata=metadata,
            )
        except IngestionError as exc:
            log.warning("blogger ingestion refused", post_id=post.post_id,
                        error=str(exc)[:300])
            return PostOutcome(**outcome_base, action="failed",
                               reason=f"ingestion refused: {exc}"[:500])

        document = accepted.document
        moved = self._supersede_other_companies(
            existing, document, company.id, post_id=post.post_id,
        )
        if moved:
            result.moved_company += moved
        self._remember(filename, document)

        if accepted.action == "duplicate":
            # Byte-identical to what is stored. The fast path above normally
            # catches this first; reaching here means the stored row predates
            # the metadata this sync writes, or `force` was used. Either way the
            # answer is the same: nothing to ingest, and the row's provenance
            # has just been refreshed by `accept()`.
            return PostOutcome(**outcome_base, action="duplicate",
                               document_id=document.id, version=document.version,
                               reason="the rendered post is byte-identical to the stored document")

        if accepted.action == "recovered":
            # The rendered post is the document's own content, and `accept()`
            # found that the row's stored object was missing: it wrote the
            # bytes back to storage, repointed the row at them and — for a
            # failed document — re-queued it for the existing worker. No
            # second document: the platform keeps each company's bytes once.
            log.info(
                "blogger document recovered from the feed", post_id=post.post_id,
                ticker=company.ticker, document_id=document.id,
                version=document.version, job_id=accepted.job_id,
                bytes=len(payload),
            )
            if accepted.job_id is not None:
                return PostOutcome(**outcome_base, action="requeued",
                                   document_id=document.id,
                                   version=document.version,
                                   job_id=accepted.job_id,
                                   reason="the stored source was missing; restored from the feed and re-indexed")
            return PostOutcome(**outcome_base, action="duplicate",
                               document_id=document.id, version=document.version,
                               reason="the stored source was missing; restored from the feed")

        action = "new_version" if accepted.action == "new_version" else "ingested"
        log.info(
            "blogger post ingested", post_id=post.post_id,
            ticker=company.ticker, document_id=document.id,
            version=document.version, action=accepted.action,
            job_id=accepted.job_id, superseded=accepted.superseded,
            mapped_by=match.method, matched_on=match.matched_on[:80],
            bytes=len(payload),
        )
        return PostOutcome(**outcome_base, action=action,
                           document_id=document.id, version=document.version,
                           job_id=accepted.job_id,
                           reason=(
                               f"superseded document {accepted.superseded}"
                               if accepted.superseded else
                               f"mapped by {match.method} on {match.matched_on[:80]!r}"
                           ))

    # ------------------------------------------------------------------
    @staticmethod
    def _unchanged_reason(row: Document, post: BloggerPost) -> str:
        """Why a stored document already represents this post, or "".

        Two independent signals, either of which is sufficient:

        * the **content fingerprint** — title, labels and body, hashed. This is
          the stronger one: it says the substance is the same whatever
          Blogger's timestamps claim.
        * the **feed timestamp** — `updated`, falling back to `published`. Cheap,
          and it is what an idle sync compares on every run without reading the
          post's body at all.

        A row with neither (ingested before this sync existed, or by a build
        that did not write them) is treated as changed and left to the content
        hash to decide, which is the honest order: unknown provenance should
        cost one comparison, not a skipped post.
        """
        metadata = row.doc_metadata or {}
        fingerprint = metadata.get("blogger_content_fingerprint")
        if fingerprint and fingerprint == post.fingerprint():
            return "the post's content fingerprint matches the stored document"

        stored_stamp = metadata.get("blogger_updated") or metadata.get("blogger_published")
        if stored_stamp and stored_stamp == iso_utc(post.effective_date):
            return "the feed timestamp matches the stored document"
        return ""

    def _stored_source_is_available(self, row: Document) -> bool:
        """Whether the pipeline can re-run `row` from its stored bytes.

        The same question `reprocess()` answers with an error when it is no:
        the key must be present, and the object it names must still be in the
        shared document storage. Legacy Blogger documents can fail this — the
        object fell out of the volume, or the row predates source retention —
        and for those the feed is the only source left, so the sync recovers
        by re-rendering the post through `accept()` rather than reporting a
        failure it cannot fix.
        """
        if not row.storage_key:
            return False
        from app.services.documents.storage import StorageError

        try:
            return self.ingestion.storage.exists(row.storage_key)
        except StorageError:
            return False

    def _supersede_other_companies(
        self,
        existing: Sequence[Document],
        document: Document,
        company_id: str,
        *,
        post_id: str = "",
    ) -> int:
        """Retire a post's row under a *different* company.

        Reached when a post's labels changed and it now maps somewhere else.
        The old row is superseded, not deleted: the platform's rule is that a
        superseded document leaves search but keeps resolving, because a
        citation issued against it last month points at text that still exists.
        Deleting would also remove the chunks, facts and graph edges a reader
        may be looking at right now.
        """
        moved = 0
        for row in existing:
            if row.company_id == company_id or row.superseded_by is not None:
                continue
            row.superseded_by = document.id
            moved += 1
            log.info(
                "blogger post moved company; previous document superseded",
                post_id=post_id, previous_document_id=row.id,
                previous_company_id=row.company_id, document_id=document.id,
                company_id=company_id,
            )
        if moved:
            self.db.commit()
        return moved

    def _remember(self, filename: str, document: Document) -> None:
        """Keep this run's view of the corpus current after a write."""
        if not self._known_loaded:
            return
        self._known[filename] = (
            [document] if document.superseded_by is None else []
        )


# ---------------------------------------------------------------------------
# Job handler entry point
# ---------------------------------------------------------------------------
def sync_now(db: Session, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one sync from a job payload. Called by the `BLOGGER_SYNC` handler.

    Raises :class:`BloggerFeedError` when the run could not proceed at all, so
    the queue's retry policy applies: at this point the only remaining causes are
    feed-side — unreachable, unreadable, or serving something that is not a feed
    — and all of them are transient. A swallowed one would mean a blog that
    quietly stopped updating until somebody noticed.

    Per-post failures are deliberately NOT raised. They are counted in the
    returned summary: one malformed entry is a data problem at the source, and
    retrying the whole feed would re-fetch and re-compare every post to fix one.
    """
    body = payload or {}
    result = BloggerSyncService(db).sync(
        max_posts=body.get("max_posts"),
        force=bool(body.get("force", False)),
        dry_run=bool(body.get("dry_run", False)),
    )
    if result.error:
        raise BloggerFeedError(result.error)
    return result.as_dict()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.blogger.sync",
        description=(
            "Fetch the Blogger feed and ingest new or changed posts into the "
            "existing document pipeline. Safe to run repeatedly: an unchanged "
            "post writes nothing, and nothing is ever deleted."
        ),
    )
    parser.add_argument(
        "--feed-url", default=None,
        help="override BLOGGER_FEED_URL for this run",
    )
    parser.add_argument(
        "--max-posts", type=int, default=None,
        help="override BLOGGER_MAX_POSTS for this run",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="override BLOGGER_SYNC_TIMEOUT_SECONDS for this run",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="ignore the unchanged fast path and let the content hash decide",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve and report what would change; write nothing",
    )
    parser.add_argument(
        "--allow-disabled", action="store_true",
        help="run even though BLOGGER_ENABLED is false (a one-off by hand)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print the machine-readable result instead of a summary",
    )
    return parser


def _print_summary(result: BloggerSyncResult) -> None:
    import json

    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))


def _print_human(result: BloggerSyncResult) -> None:
    """An operator-readable summary: what changed, and what needs a decision."""
    print(f"feed:            {result.feed_url or '(not configured)'}")
    print(f"enabled:         {result.enabled}")
    if result.dry_run:
        print("mode:            DRY RUN — nothing was written")
    print(
        f"fetched:         {result.fetched} posts "
        f"in {result.pages_fetched} feed request(s)"
    )
    print(
        f"ingested:        {result.ingested} new, {result.new_versions} new version(s), "
        f"{result.requeued} re-indexed"
    )
    print(
        f"left alone:      {result.unchanged} unchanged, {result.duplicates} identical, "
        f"{result.moved_company} moved company"
    )
    print(f"needs attention: {result.skipped} skipped, {result.failed} failed")
    print(f"took:            {result.duration_ms:,.0f} ms")

    mapping = result.mapping.as_dict()
    if mapping["by_ticker"]:
        tickers = ", ".join(
            f"{ticker}×{count}"
            for ticker, count in sorted(mapping["by_ticker"].items())
        )
        print(f"mapped to:       {tickers}")

    attention = [
        outcome for outcome in result.outcomes
        if outcome.action in ("skipped", "failed")
    ]
    for outcome in attention[:MAX_RECORDED_OUTCOMES]:
        print(f"  [{outcome.action}] {outcome.post_id} {outcome.title[:70]!r}")
        print(f"      {outcome.reason}")
    if len(attention) > MAX_RECORDED_OUTCOMES:
        print(f"  … and {len(attention) - MAX_RECORDED_OUTCOMES} more")

    if result.error:
        print(f"error:           {result.error}")


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m app.services.blogger.sync`.

    Exit codes: ``0`` clean, ``1`` the run had failures or could not read the
    feed, ``2`` it did not run at all because the feature is off or
    unconfigured. Distinguished so a cron job can alert on 1 and stay quiet on
    a deployment that simply has not turned the feature on.
    """
    args = _build_parser().parse_args(argv)

    from app.db.base import SessionLocal
    from app.services.platform.observability import configure_logging

    configure_logging()

    feed = None
    if args.feed_url or args.timeout:
        feed = BloggerFeedClient(
            url=(args.feed_url or settings.BLOGGER_FEED_URL),
            timeout=(args.timeout if args.timeout is not None
                     else settings.BLOGGER_SYNC_TIMEOUT_SECONDS),
        )

    db = SessionLocal()
    try:
        service = BloggerSyncService(db, feed=feed)
        result = service.sync(
            max_posts=args.max_posts,
            force=args.force,
            dry_run=args.dry_run,
            allow_disabled=args.allow_disabled,
        )
    except BloggerFeedError as exc:
        print(f"the feed could not be read: {exc}")
        return 1
    finally:
        db.close()

    if args.as_json:
        _print_summary(result)
    else:
        _print_human(result)

    if result.error and not result.enabled and not args.allow_disabled:
        return 2
    if result.error or result.failed:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
