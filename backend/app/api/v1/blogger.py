"""The blog's public endpoints.

    POST /blogger/chat     grounded answer about a published ticker   (no auth)
    GET  /blogger/status   what the embeddable widget needs to render (no auth)
    POST /blogger/sync     queue a feed sync            (credential or permission)

Why a separate router rather than a flag on `/company/{ticker}/ai/chat`:

That endpoint is authenticated, and it stays exactly as it is. A blog post is
read by people with no account, so it needs an entry point that does not issue
or accept a session — and an entry point with no session has to answer a
different set of questions: which companies may be discussed at all, whose
conversation memory this is, what may appear in the response, and how often one
address may ask. Every one of those is a policy decision that would clutter the
authenticated path, where the signed-in user and their subscription already
answer most of them.

What is NOT separate is everything behind the endpoint. The question goes to the
same `ResearchAnalyst`, over the same retrieval, the same citation audit, the
same guardrails and the same Language Adapter that serve the platform's own
users. A Blogger post is an ordinary document in an ordinary corpus: it was
ingested by the ordinary pipeline, so it is retrieved by the ordinary engine and
cited like any other filing. There is no second AI here.

Security, since these are the only AI routes an anonymous caller reaches:

* The ticker must be in `BLOGGER_PUBLIC_TICKERS`. That list is the operator's
  explicit decision about which companies the blog discusses, and it is checked
  before anything is looked up, so an unlisted ticker cannot even provoke a
  database read.
* No provisioning. The authenticated path will fetch a US listing from a vendor
  and create the company on first request; an anonymous caller must never be
  able to make this platform perform outbound I/O or write rows by naming a
  ticker.
* The response schema is a smaller surface than the authenticated one: no
  provider, model, prompt, token or cost fields, no internal document ids.
  See :mod:`app.schemas.blogger` for the reasoning field by field.
* Rate limited per client address, below the anonymous ceiling the global
  middleware applies, because every request costs a provider call.
* Conversation memory is namespaced by client address, so one reader's
  `session_id` cannot continue another reader's conversation — the id is
  caller-chosen and therefore not an identity.
* Error text is generic and the detail goes to the log. A provider's own error
  message can carry a base URL, a model name or a fragment of a request; none of
  that belongs in a response an anonymous caller can read.
"""
from __future__ import annotations

import asyncio
import hmac
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import Principal, _client_ip, get_optional_user
from app.db.base import get_db
from app.domain.ai.sourcing import parse_directive
from app.domain.ai.types import Citation, NoProviderConfigured, ProviderError
from app.domain.language.types import (
    CANONICAL_LANGUAGE, Language, resolve as resolve_language,
)
from app.domain.platform.audit import AuditAction
from app.domain.platform.identity import Permission
from app.domain.platform.jobs import JobKind
from app.domain.platform.limits import RateScope
from app.models.document import Document
from app.schemas.blogger import (
    BloggerChatRequest, BloggerChatResponse, BloggerCitationOut,
    BloggerLanguageOut, BloggerStatusResponse, BloggerSyncRequest,
    BloggerSyncResponse, BloggerTickerOut,
)
from app.services.ai.analyst import AnalystResult
from app.services.ai.guardrails import DISCLOSURE
from app.services.ai.service import AIService
from app.services.analysis_service import AnalysisService
from app.services.blogger.document import FILENAME_PREFIX
from app.services.company_service import CompanyService
from app.services.platform import rate_limit
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.jobs.queue import JobQueue

router = APIRouter(prefix="/blogger", tags=["blogger"])
log = structlog.get_logger(__name__)

#: Header an operator's deployment tooling can present to trigger a sync.
SYNC_HEADER = "X-Blogger-Sync-Secret"

#: How much of a retrieved passage a public response quotes. The reader needs
#: enough to check the claim; the whole passage is not that, and a public
#: endpoint that returns ten full passages per answer is a convenient way to
#: download a corpus.
SNIPPET_CHARS = 400

#: Added when an answer had no document evidence behind it.
UNDOCUMENTED_NOTE = (
    "No indexed document supported this answer. It was produced from the "
    "platform's computed figures alone, so treat it as a starting point and "
    "check the primary filing before acting on it."
)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------
def _limit(rule: str, request: Request) -> None:
    """Consume one unit of a public endpoint's per-address budget."""
    decision = rate_limit.check(rule, _client_ip(request), scope=RateScope.IP)
    if decision.allowed:
        return
    # No address in the log line. Enforcement does not need it here — the
    # response says as much to the caller — and the audit trail, which is the
    # sanctioned place for request context, records it where it matters.
    log.warning("public blogger request rate limited", rule=rule)
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many requests. Please wait a moment before asking again.",
        headers=decision.headers(),
    )


def _public_language(requested: str | None, question: str) -> Language | None:
    """The response language for an anonymous caller.

    Mirrors the authenticated chat's resolution — an explicit request wins, and
    English short-circuits the Language Adapter entirely so the common case pays
    no translation cost — minus the stored user preference, which an anonymous
    caller does not have. Kept beside this endpoint rather than imported from
    the authenticated router: a private helper is not a shared contract, and
    reaching into another module's internals to save three lines would couple
    the public path to a file that has no reason to know it exists.
    """
    from app.domain.language.detect import choose_language

    explicit = resolve_language(requested)
    if explicit is not None:
        return None if explicit is CANONICAL_LANGUAGE else explicit
    language, _ = choose_language(question)
    return None if language is CANONICAL_LANGUAGE else language


def _document_details(db: Session, citations: list[Citation]) -> dict[int, tuple[str, str, str | None]]:
    """(title, type, url) for every document a citation names.

    One batched query for the whole answer. The URL comes from the document's
    stored provenance: a Blogger post carries the link it was fetched from, so
    a reader can go and check the passage against the post itself rather than
    take the model's summary of it on trust. Documents from any other source
    simply have no URL, and the field is omitted rather than invented.
    """
    ids = sorted({c.document_id for c in citations if c.document_id})
    if not ids:
        return {}
    try:
        rows = db.execute(
            select(Document.id, Document.title, Document.doc_type, Document.doc_metadata)
            .where(Document.id.in_(ids))
        ).all()
    except Exception:  # noqa: BLE001 — provenance must not cost the answer
        log.exception("could not load citation provenance")
        return {}

    details: dict[int, tuple[str, str, str | None]] = {}
    for doc_id, title, doc_type, metadata in rows:
        url = metadata.get("blogger_url") if isinstance(metadata, dict) else None
        details[doc_id] = (
            title or "",
            doc_type or "",
            url.strip() if isinstance(url, str) and url.strip() else None,
        )
    return details


def _public_citations(db: Session, citations: list[Citation]) -> list[BloggerCitationOut]:
    details = _document_details(db, citations)
    out: list[BloggerCitationOut] = []
    for citation in citations:
        title, doc_type, url = details.get(citation.document_id or 0, ("", "", None))
        snippet = " ".join((citation.snippet or "").split())
        out.append(BloggerCitationOut(
            label=citation.label,
            source=citation.source,
            kind=citation.kind.value,
            page=citation.page,
            snippet=snippet[:SNIPPET_CHARS] or None,
            url=url,
            document_title=title or None,
            document_type=doc_type or None,
            confidence=citation.confidence,
        ))
    return out


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@router.post("/chat", response_model=BloggerChatResponse,
             summary="Ask the blog's knowledge base about a published company")
async def blogger_chat(
    body: BloggerChatRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> BloggerChatResponse:
    """One grounded answer for a reader of the blog.

    The same analyst, retrieval, citation audit, guardrails and language
    handling as the platform's own chat — reached without an account, restricted
    to the companies the operator chose to publish, and answered from that
    company's indexed documents. When nothing in the corpus bears on the
    question the reader is told so, in their own language, rather than being
    given a plausible invention: the source router refuses fail-closed, exactly
    as it does for a signed-in user.
    """
    _limit("blogger.chat", request)

    if not settings.BLOGGER_ENABLED:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The blog chatbot is not enabled on this deployment.",
        )

    ticker = body.ticker.strip().upper()
    allowed = settings.blogger_public_tickers
    if ticker not in allowed:
        # Checked before any lookup: an unlisted ticker should not be able to
        # provoke a database read, and the message states the policy without
        # listing what IS allowed — /blogger/status is where a caller finds that.
        log.info("public chat refused a ticker", ticker=ticker[:24])
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{ticker} is not available through the blog chatbot.",
        )

    # `provision=False` is load-bearing here. The authenticated path will fetch
    # an unknown US listing from a vendor and write the company on first
    # request; an anonymous caller must not be able to trigger outbound I/O or
    # create rows by naming a ticker.
    #
    # In a thread, because this handler is `async def` and the lookup is
    # blocking database work: run on the loop it stalls every other reader's
    # request for as long as it takes, which on a cold company (financials
    # loaded from scratch) measured ~90 ms — long enough that twelve concurrent
    # readers each waited ~190 ms instead of being served together.
    analysis = await run_in_threadpool(
        AnalysisService.for_ticker, db, ticker, provision=False,
    )
    if analysis is None or analysis.company.deleted_at is not None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No company is recorded for {ticker}."
        )

    company = analysis.company
    service = AIService(db)

    # The session id is caller-chosen, so it is not an identity on its own:
    # without the address in the key, two readers who both sent "public" would
    # share one conversation and each would see the other's questions answered.
    session_key = f"blogger-public:{_client_ip(request)}:{body.session_id}"
    memory = service.memory(session_key)
    memory.set_company(company.id, company.ticker, company.name)

    analyst = service.analyst_for(analysis)
    budget = settings.blogger_chat_timeout_seconds
    started = time.monotonic()
    try:
        # The directive is parsed from the question, as it is for a signed-in
        # user: "only from the documents" typed by a reader is honoured, and if
        # that source has nothing the analyst declines instead of widening the
        # scope to find an answer.
        #
        # Bounded, because the provider chain behind this call is not: three
        # attempts at a sixty-second HTTP timeout per provider, across four
        # providers, is twelve minutes of patience. Retrieval, grounding and the
        # citation audit all finish in the first second — so without a budget
        # the reader's request hangs *after* the work is done, waiting on a
        # model, and the proxy in front gives up first. A 504 the reader can
        # retry beats an answer that arrives to a closed connection.
        result = await asyncio.wait_for(
            analyst.chat(
                body.question, memory,
                source=parse_directive(body.question),
                language=_public_language(body.language, body.question),
            ),
            timeout=budget,
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        # Logged with what the operator needs and nothing a reader should see:
        # how long it ran, against what budget, for which ticker.
        log.warning(
            "public chat exceeded its time budget",
            ticker=ticker, budget_seconds=budget,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "The answer took too long to generate. Please try again in a moment.",
        ) from exc
    except NoProviderConfigured as exc:
        log.warning("public chat has no AI provider configured", error=str(exc)[:200])
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The analysis service is not configured on this deployment.",
        ) from exc
    except ProviderError as exc:
        # Logged with the traceback, answered without it: a provider's own error
        # text can name a model, a base URL or part of a request.
        log.exception("public chat provider call failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The analysis service could not answer just now. Please try again.",
        ) from exc

    warnings = list(result.warnings)
    if not result.citations and result.provider != "source-router":
        # "source-router" means the analyst already declined for want of
        # evidence and said so in its own words; a second note would only
        # repeat it. Anything else with no citations was answered from computed
        # figures, and a reader of a research blog is entitled to know that.
        warnings.append(UNDOCUMENTED_NOTE)

    try:
        # Recorded like every other answer: a public AI that leaves no trace of
        # what it told people cannot be reviewed afterwards, and the usage rows
        # are how the operator learns what the blog costs. Off the event loop
        # for the same reason the lookup is: it is a blocking commit, and the
        # answer is already in hand.
        await run_in_threadpool(service.record, company.id, result, owner=None)
    except Exception:  # noqa: BLE001
        # Bookkeeping must not cost the reader an answer that has already been
        # paid for. Logged loudly; the usage roll-up reconciles the counters.
        log.exception("could not record a public chat answer", ticker=company.ticker)

    return BloggerChatResponse(
        ticker=company.ticker,
        company=company.name,
        answer=result.display_content or result.content,
        # "Grounded" here means what a reader of a research blog takes it to
        # mean: the answer rests on retrieved evidence. The audit's own verdict
        # is narrower — no uncited number, no invented key — and an answer that
        # declines for want of evidence satisfies it vacuously, citing nothing
        # and claiming nothing. Reporting that as grounded would be the one
        # genuinely misleading thing this response could say.
        grounded=result.is_supported and bool(result.citations),
        citations=await run_in_threadpool(
            _public_citations, db, result.citations,
        ),
        warnings=warnings,
        disclosure=(
            result.guardrails.disclosure if result.guardrails else DISCLOSURE
        ),
        language=_language_out(result),
        session_id=body.session_id,
        turn_count=memory.turn_count,
    )


def _language_out(result: AnalystResult) -> BloggerLanguageOut | None:
    """The reader-facing part of the Language Adapter's report.

    Built from the adapter's block with `.get` throughout: the block's shape is
    the adapter's business, and a public endpoint that answered the question
    correctly must not then fail with a 500 because a language report was
    missing a key. A missing field degrades to a blank label, not to a lost
    answer.
    """
    block = result.language
    if not block:
        return None
    detected = block.get("detected") or {}
    translation = block.get("translation") or {}
    return BloggerLanguageOut(
        language=str(block.get("language") or ""),
        label=str(block.get("label") or ""),
        native_label=str(block.get("native_label") or ""),
        script=str(block.get("script") or ""),
        bcp47=str(block.get("bcp47") or ""),
        resolved_from=str(block.get("resolved_from") or ""),
        is_mixed=bool(detected.get("is_mixed", False)),
        detected_confidence=float(detected.get("confidence") or 0.0),
        translated=bool(translation.get("translated", False)),
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@router.get("/status", response_model=BloggerStatusResponse,
            summary="What the blog widget may offer its readers")
def blogger_status(
    request: Request,
    db: Session = Depends(get_db),
) -> BloggerStatusResponse:
    """Everything the embedded widget needs before it renders a chat box.

    Public and unauthenticated by necessity — a blog reader has no session — and
    public in a harmless sense: what it returns is the list of companies the
    operator chose to publish, a count of indexed posts per company, and when
    the corpus last changed. When the feature is off it returns an empty list
    and a reason, so a disabled deployment advertises nothing and the widget
    hides itself instead of rendering a box that can only error.
    """
    _limit("blogger.chat", request)

    tickers = settings.blogger_public_tickers
    if not settings.BLOGGER_ENABLED:
        return BloggerStatusResponse(
            enabled=False,
            reason="The blog chatbot is not enabled on this deployment.",
            disclosure=DISCLOSURE,
        )
    if not tickers:
        return BloggerStatusResponse(
            enabled=False,
            reason="No company has been published for the blog chatbot yet.",
            disclosure=DISCLOSURE,
        )

    service = AIService(db)
    if not service.router.available:
        return BloggerStatusResponse(
            enabled=False,
            reason="The analysis service has no provider configured.",
            disclosure=DISCLOSURE,
        )

    # Counted by filename prefix rather than by a predicate over the JSON
    # metadata column: `doc_metadata` is JSON, and a query into it would have to
    # be written differently for SQLite and for Postgres — the two databases
    # this platform promises to run on. The prefix is the post's identity by
    # construction, so it is both portable and cheaper.
    rows = db.execute(
        select(
            Document.company_id,
            func.count(Document.id),
            func.max(Document.updated_at),
        )
        .where(
            Document.filename.like(f"{FILENAME_PREFIX}-%"),
            Document.superseded_by.is_(None),
        )
        .group_by(Document.company_id)
    ).all()
    counts = {company_id: (total, latest) for company_id, total, latest in rows}

    companies = CompanyService(db)
    published: list[BloggerTickerOut] = []
    last_synced = None
    for ticker in sorted(tickers):
        company = companies.get_by_ticker(ticker)
        if company is None or company.deleted_at is not None:
            # Configured but not present. Logged rather than surfaced: the
            # reader cannot do anything about it, and the operator can.
            log.warning("published blogger ticker has no company row",
                        ticker=ticker[:24])
            continue
        total, latest = counts.get(company.id, (0, None))
        if latest is not None and (last_synced is None or latest > last_synced):
            last_synced = latest
        published.append(BloggerTickerOut(
            ticker=company.ticker, name=company.name, posts=int(total or 0),
        ))

    return BloggerStatusResponse(
        enabled=bool(published),
        reason="" if published else (
            "No published company has a record in the platform yet."
        ),
        tickers=published,
        last_synced_at=last_synced.isoformat() if last_synced else None,
        disclosure=DISCLOSURE,
    )


# ---------------------------------------------------------------------------
# Sync trigger
# ---------------------------------------------------------------------------
def _sync_authorised(request: Request, principal: Principal | None) -> bool:
    """May this caller queue a sync?

    Two independent doors, either of which is sufficient:

    * a deployment credential in `X-Blogger-Sync-Secret`, for the operator's own
      tooling — a post-deploy hook on EC2, a cron job outside the container —
      which has no platform session and must not be given one;
    * a signed-in principal holding `DOCUMENT_UPLOAD`, which is the permission
      the rest of the platform uses for "may add documents to the corpus".

    Fail-closed in both directions. When no credential is configured the header
    is ignored entirely rather than compared: comparing against an empty string
    would let an empty header through, which is the kind of mistake that is
    invisible in a code review and total in production. And an unauthenticated
    caller with no credential gets a 403, not a queue.
    """
    configured = (settings.BLOGGER_SYNC_SECRET or "").strip()
    if configured:
        presented = (request.headers.get(SYNC_HEADER) or "").strip()
        if presented and hmac.compare_digest(presented, configured):
            return True
    if principal is not None and principal.can(Permission.DOCUMENT_UPLOAD):
        return True
    return False


@router.post("/sync", response_model=BloggerSyncResponse,
             summary="Queue a Blogger feed sync")
def blogger_sync(
    body: BloggerSyncRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal | None = Depends(get_optional_user),
) -> BloggerSyncResponse:
    """Queue one feed pass on the existing worker.

    Queued rather than run inline: a full pass reads a paged feed and enqueues a
    document job per changed post, which is far longer than an HTTP request
    should be held open — long enough that a proxy timeout would report a
    failure for work that actually succeeded. The worker's result lands on the
    job row, and `python -m app.services.blogger.sync` is the way to watch a run
    as it happens.

    Deduplicated by the queue, so a deploy hook that fires twice, or an operator
    who clicks twice, produces one sync.
    """
    _limit("blogger.sync", request)

    if not _sync_authorised(request, principal):
        # One message for every refusal. Whether a credential is configured at
        # all is a fact about the deployment, and this endpoint is reachable
        # without an account.
        log.warning("blogger sync trigger refused",
                    authenticated=principal is not None)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This deployment does not accept sync requests from this caller.",
        )

    feed_url = (settings.BLOGGER_FEED_URL or "").strip()
    if not feed_url:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No Blogger feed is configured, so there is nothing to sync.",
        )

    job = JobQueue(db).enqueue(
        JobKind.BLOGGER_SYNC,
        payload={
            "max_posts": body.max_posts,
            "force": body.force,
            "dry_run": body.dry_run,
        },
        resource_type="blogger_feed",
        resource_id=feed_url,
    )
    AuditService(db).record(
        AuditAction.JOB_ENQUEUED,
        principal=principal,
        resource_type="blogger_feed", resource_id=job.id,
        summary=(
            f"Blogger sync queued for {feed_url}"
            + (" (dry run)" if body.dry_run else "")
            + (" (forced)" if body.force else "")
        ),
        context=RequestContext(
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        ),
        metadata={
            "kind": JobKind.BLOGGER_SYNC.value,
            "max_posts": body.max_posts,
            "force": body.force,
            "dry_run": body.dry_run,
            "enabled": settings.BLOGGER_ENABLED,
        },
    )
    log.info("blogger sync queued", job_id=job.id, dry_run=body.dry_run,
             force=body.force, max_posts=body.max_posts)
    return BloggerSyncResponse(
        queued=True, job_id=job.id,
        message=(
            "The sync is queued and the worker will run it. Its result is "
            "recorded on the job."
        ),
    )
