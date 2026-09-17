"""Core AI types.

These are deliberately provider-agnostic. Nothing above the transport layer
knows whether a response came from OpenAI, Claude, Gemini or a local mock —
the same lesson the workbook taught in Module 4, where a "provider abstraction"
that still branched on provider name in the VBA was no abstraction at all.

The type that matters most here is :class:`Citation`. Every factual claim the
AI makes has to trace back to a number the *platform* computed, not to the
model's recollection. A response without citations is treated as unsupported.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

#: Characters permitted inside a citation-key body. The marker pattern the
#: citation engine enforces admits ``[a-z0-9_.]`` only, so a key is minted
#: from a reduced alphabet rather than assembled from a hostname that may
#: contain hyphens or unicode.
_KEY_UNSAFE = re.compile(r"[^a-z0-9]")


def mint_web_citation_key(url: str) -> str:
    """The one implementation of web citation-key minting.

    Single implementation on purpose: the key is what the model writes into a
    response and what the audit resolves back to evidence, so two minting
    rules would mean keys that parse for one path and are reported as invented
    for another. Derived from the canonical URL — stable when the same page is
    re-fetched, and different for two pages on the same host.
    """
    host = (urlsplit(url or "").hostname or "").lower()
    digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:8]
    return f"web_{_KEY_UNSAFE.sub('', host)[:24]}_{digest}"


class PayloadShape(StrEnum):
    """Wire format families. Providers speaking the same dialect share an adapter."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class EvidenceKind(StrEnum):
    """Where a cited number came from.

    The distinction is load-bearing: a reported figure and a model projection
    carry very different epistemic weight, and the UI renders them differently.
    """

    STATEMENT = "statement"        # reported financial statements
    RATIO = "ratio"                # computed ratio
    FORECAST = "forecast"          # projection — an estimate, not a fact
    VALUATION = "valuation"        # DCF / relative output
    SCORING = "scoring"            # institutional score
    DOCUMENT = "document"          # uploaded filing or transcript
    #: A page the platform fetched from a pinned host (Part 3 Phase 1). Its
    #: own kind rather than DOCUMENT because the provenance differs in a way
    #: that matters to a reader: a filing is something the company lodged, a
    #: fetched page is something the platform read. Ranking it under DOCUMENT
    #: keeps it out of every scope and prompt block that means "uploaded".
    WEB = "web"
    #: A durable assertion from the Knowledge Vault, or a stored AI summary.
    #: Distinct from DOCUMENT because it is knowledge the platform has already
    #: distilled and versioned rather than a raw passage — it is read first,
    #: and a reader should be able to tell the two apart.
    KNOWLEDGE = "knowledge"
    MARKET = "market"              # price, market cap


class ClaimType(StrEnum):
    """The guardrail taxonomy the brief requires.

    Every paragraph of AI output is classified so a reader can tell a reported
    fact from a model output from an opinion.
    """

    FACT = "fact"                  # reported, verifiable
    MODEL_OUTPUT = "model_output"  # platform-computed projection or valuation
    INTERPRETATION = "interpretation"  # AI reasoning over the above
    OPINION = "opinion"            # judgement; must be hedged


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class WebProvenance:
    """Where a fetched page came from, carried on the citation itself.

    A web citation has to be checkable by the reader, which means the URL and
    the retrieval time travel with it. Without them the citation is an
    assertion that a page once said something, with no way to verify it and no
    way to tell whether *this platform* fetched it or a model recalled it.

    Deliberately not a second citation type: it is a field on
    :class:`Citation`, so the audit funnel, the marker parser and the citation
    engine keep working on one shape (see the Part 3 audit, finding on
    ``DocumentCitation2``).

    ``published_at`` is ``None`` whenever the page did not genuinely state a
    date. It is never inferred from ``retrieved_at``: a page fetched today may
    be five years old, and conflating the two would let any page look current.
    """

    url: str
    retrieved_at: "datetime"
    title: str = ""
    published_at: "datetime | None" = None
    canonical_url: str = ""
    content_hash: str = ""

    def citation_key(self) -> str:
        """The citation key for this page, stable across re-fetch and re-ingest."""
        return mint_web_citation_key(self.canonical_url or self.url)

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "retrieved_at": (
                self.retrieved_at.isoformat() if self.retrieved_at else None
            ),
            "canonical_url": self.canonical_url,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class Citation:
    """A single piece of evidence backing a claim."""

    key: str                       # e.g. "revenue_fy25"
    label: str                     # human-readable
    kind: EvidenceKind
    value: float | str | None = None
    unit: str = ""
    source: str = ""               # "06 Historical IS", "DCF engine", "AR FY25 p.42"
    fiscal_year: int | None = None

    # --- retrieval provenance (document evidence only) ------------------
    # Populated when the citation came from a RAG passage rather than a
    # computed figure. A reader auditing a claim about prose needs to reach
    # the exact paragraph, which a source string alone does not permit: two
    # passages on the same page are indistinguishable without the chunk id.
    document_id: int | None = None
    chunk_id: int | None = None
    page: int | None = None
    #: Retrieval score, 0–1. Reported so a weakly-supported answer can be
    #: recognised as weakly supported rather than read with equal confidence.
    confidence: float | None = None
    #: The verbatim passage, kept separate from `value` so truncation for the
    #: prompt never silently shortens what the UI shows as the quotation.
    snippet: str | None = None

    # --- web provenance (EvidenceKind.WEB only) -------------------------
    #: Set when the evidence is a page the platform fetched. A field rather
    #: than a subclass so every existing consumer — the marker parser, the
    #: verifier, the report renderer — keeps seeing one citation shape.
    web: WebProvenance | None = None

    @property
    def marker(self) -> str:
        """Inline citation marker the model is told to use."""
        return f"[{self.key}]"

    def render(self) -> str:
        """One evidence line for the prompt context.

        Percentages are stored as fractions throughout the platform but must be
        presented to the model in percentage points. Handing an LLM "0.15 %"
        when the figure is 15% is an invitation to misread it by two orders of
        magnitude, and the model has no way to detect the error.
        """
        if self.value is None:
            value = "unavailable"
        elif isinstance(self.value, float):
            value = (
                f"{self.value * 100:,.2f}" if self.unit == "%"
                else f"{self.value:,.2f}"
            )
        else:
            value = str(self.value)
        unit = f" {self.unit}" if self.unit else ""
        year = f" (FY{str(self.fiscal_year)[-2:]})" if self.fiscal_year else ""
        return f"[{self.key}] {self.label}{year}: {value}{unit} — source: {self.source}"


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """A provider-agnostic completion request."""

    messages: list[Message]
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2000
    stream: bool = False
    #: Optional JSON-schema hint for providers that support structured output.
    response_format: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """What every provider returns, whatever its wire format."""

    content: str
    provider: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    finish_reason: str = "stop"
    #: Set when a fallback provider served the request.
    fell_back_from: str | None = None
    cached: bool = False


class ProviderError(RuntimeError):
    """Transport or provider-side failure."""

    def __init__(self, message: str, *, provider: str = "", status: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable


class RateLimitError(ProviderError):
    """A 429. Two quite different situations share this status code.

    *Throttling* — too many requests this minute — clears on its own, and
    retrying after the advertised delay is the right response.

    *Quota exhaustion* — the daily or monthly allowance is spent — does not
    clear for hours. Retrying it burns the caller's latency to reach the same
    answer, so the router falls straight through to the next provider.

    `quota_exhausted` distinguishes them. It is set from the provider's own
    error body rather than guessed, because only the provider knows which of
    the two it meant.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        retry_after: float = 1.0,
        quota_exhausted: bool = False,
    ) -> None:
        super().__init__(message, provider=provider, status=429, retryable=True)
        self.retry_after = retry_after
        self.quota_exhausted = quota_exhausted


class NoProviderConfigured(ProviderError):
    """No provider has an API key. The platform still works; the AI does not."""
