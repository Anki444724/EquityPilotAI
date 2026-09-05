"""API contracts for the public Blogger chatbot.

A separate module rather than an extension of :mod:`app.schemas.ai`, and a
deliberately smaller surface. These schemas describe the one AI endpoint an
anonymous caller can reach, so every field here is a field published to the
open internet:

* no `provider`, `model`, `prompt_key` or `prompt_version` — that is the
  platform's own configuration, and naming the model behind a public answer
  invites a caller to negotiate with it;
* no `cost_usd`, `prompt_tokens` or `completion_tokens` — the economics of the
  deployment are not the reader's business, and a per-request cost figure is a
  ready-made estimate of how to exhaust a budget;
* no `document_id` or `chunk_id` — internal primary keys. They buy a public
  reader nothing and hand an enumerator a sequence to walk;
* no `data_quality` block — that score describes the platform's *financial*
  coverage of a company, which a document-grounded answer does not depend on.

What is kept is what a reader needs to judge the answer: the text, whether it
was grounded in retrieved evidence, the citations behind it with a link back to
the post, the warnings, the disclosure, and how the language was chosen.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BloggerChatRequest(BaseModel):
    """One question from the Blogger widget."""

    #: Must be in `BLOGGER_PUBLIC_TICKERS`. Validated against the configured
    #: allowlist in the endpoint, not by pattern: the pattern cannot know which
    #: companies the operator has chosen to publish.
    ticker: str = Field(
        min_length=1, max_length=24,
        description="NSE symbol of a company the blog is allowed to discuss.",
    )
    question: str = Field(min_length=1, max_length=2000)
    #: Free-form and caller-chosen, so a widget can keep a thread across
    #: messages. Scoped server-side by client address: one reader's id cannot
    #: continue another's conversation, whatever string is sent.
    session_id: str = Field(default="public", max_length=64)
    #: "auto" | "english" | "hindi" | "hinglish" | a BCP-47 tag. Defaults to
    #: detecting the language of the question, which is what a blog's readers
    #: expect: they type Hinglish and get Hinglish back.
    language: str = "auto"


class BloggerCitationOut(BaseModel):
    """One piece of evidence behind an answer.

    Carries the post's own URL when the evidence came from the blog, so a
    reader can check the claim against the source rather than take the model's
    word for it — the whole reason citations survive the trip to a public
    endpoint.
    """

    label: str = Field(description="What the evidence is, e.g. the post title.")
    source: str = ""
    kind: str = ""
    #: Page or section within the document. Blogger posts have no pages, so
    #: this is usually absent for them and present for filed reports.
    page: int | None = None
    snippet: str | None = Field(
        default=None,
        description="The passage the answer drew on, collapsed to one line.",
    )
    url: str | None = Field(
        default=None,
        description="Canonical link to the source, when the source has one.",
    )
    document_title: str | None = None
    document_type: str | None = None
    confidence: float | None = None


class BloggerLanguageOut(BaseModel):
    """How the answer's language was chosen.

    A reader-facing subset of the platform's own language block. The full one
    reports which provider translated the answer, what that cost and how long it
    took — deployment facts that a blog reader has no use for and no business
    seeing, so they are not carried over. What stays is what the widget can show:
    "answered in हिन्दी, detected from your question".
    """

    language: str
    label: str = ""
    native_label: str = ""
    script: str = ""
    bcp47: str = ""
    #: "requested" | "detected" | "preference".
    resolved_from: str = ""
    #: The question was written in more than one language — "ITC ka dividend
    #: yield kya hai?" — which is why the answer may mix them too.
    is_mixed: bool = False
    detected_confidence: float = 0.0
    translated: bool = False


class BloggerChatResponse(BaseModel):
    """The answer, and enough provenance to judge it."""

    ticker: str
    company: str
    #: What to display. Already in the requested or detected language, and
    #: already carrying any warning the guardrails prepended.
    answer: str
    #: True when the answer rests on retrieved evidence: the citation audit
    #: found it supported *and* there was something to cite. The audit's own
    #: verdict is narrower — no uncited number, no invented key — and an answer
    #: that declines for want of evidence satisfies it vacuously, so this field
    #: asks for evidence as well rather than report a refusal as grounded.
    #: False means the platform said so itself — and the answer text says it
    #: too, in the reader's language. A client should never have to infer
    #: honesty from the absence of citations.
    grounded: bool = False
    citations: list[BloggerCitationOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    disclosure: str = ""
    #: Present only when the answer went through the Language Adapter, i.e. a
    #: non-English language was requested or detected. Absent on the English
    #: path, which keeps the common case's payload small.
    language: BloggerLanguageOut | None = None
    session_id: str
    turn_count: int = 0


class BloggerTickerOut(BaseModel):
    """A company the public chatbot will discuss."""

    ticker: str
    name: str
    #: Documents indexed for this company from the blog. Zero means the widget
    #: can still ask — filings and other documents may answer — but it should
    #: not promise blog posts.
    posts: int = 0


class BloggerStatusResponse(BaseModel):
    """What the widget needs before it offers a chat box.

    Public by design: everything here is either already on the blog (the
    tickers the operator chose to publish) or a count of documents. Nothing
    describes the deployment.
    """

    #: False when the feature is switched off, or no provider is configured.
    #: The widget hides itself rather than showing a box that only errors.
    enabled: bool = False
    #: Human-readable reason when `enabled` is false. Never contains
    #: configuration values — "no AI provider is configured" is a statement
    #: about the system, not a leak from it.
    reason: str = ""
    tickers: list[BloggerTickerOut] = Field(default_factory=list)
    #: When the corpus last changed. Lets a reader see that the blog's answers
    #: come from something that is kept current.
    last_synced_at: str | None = None
    disclosure: str = ""


class BloggerSyncRequest(BaseModel):
    """A request to sync the feed now instead of waiting for the schedule."""

    max_posts: int | None = Field(default=None, ge=1, le=1000)
    #: Ignore the unchanged fast path and let the content hash decide. For a
    #: first run after changing how documents are rendered.
    force: bool = False
    #: Resolve and report without writing anything.
    dry_run: bool = False


class BloggerSyncResponse(BaseModel):
    """The sync was queued, not run.

    Queued rather than executed inline because a full pass fetches a paged feed
    and enqueues a document job per changed post; holding an HTTP request open
    for that is how a deployment ends up with a proxy timeout in front of a job
    that actually succeeded. The existing worker runs it, and its result lands
    on the job row.
    """

    queued: bool = False
    job_id: int | None = None
    message: str = ""
