"""Source routing for the AI layer.

A research analyst who asks "summarise the chairman's message from the
uploaded report" is making a claim about *provenance*, not just about topic.
Answering from computed financials instead is not a partial answer — it is the
wrong answer wearing the right clothes, and because every figure in it is real
and correctly cited, nothing downstream flags it.

The platform previously had no concept of a requested source: retrieval ran,
and if it returned nothing the full evidence context was used anyway. This
module makes the requested source explicit and enforceable.

Deliberately dependency-free — no SQLAlchemy, no FastAPI, no settings — so the
routing rules can be tested directly and cannot drift from the enforcement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.domain.ai.types import EvidenceKind


class SourceScope(StrEnum):
    """Which evidence the answer may be drawn from."""

    UPLOADED_DOCUMENTS_ONLY = "uploaded_documents_only"
    FINANCIAL_DATABASE_ONLY = "financial_database_only"
    MARKET_DATA_ONLY = "market_data_only"
    HYBRID = "hybrid"

    @property
    def is_restricted(self) -> bool:
        return self is not SourceScope.HYBRID


#: Which evidence kinds each scope admits. A scope is enforced by filtering
#: the context down to these kinds *before* the prompt is built, so restricted
#: evidence is never placed in front of the model — rather than asking the
#: model politely to ignore it, which is not a control.
SCOPE_KINDS: dict[SourceScope, frozenset[EvidenceKind]] = {
    SourceScope.UPLOADED_DOCUMENTS_ONLY: frozenset({EvidenceKind.DOCUMENT}),
    SourceScope.FINANCIAL_DATABASE_ONLY: frozenset({
        EvidenceKind.STATEMENT, EvidenceKind.RATIO,
        EvidenceKind.FORECAST, EvidenceKind.VALUATION, EvidenceKind.SCORING,
    }),
    SourceScope.MARKET_DATA_ONLY: frozenset({EvidenceKind.MARKET}),
    SourceScope.HYBRID: frozenset(EvidenceKind),
}

#: Default refusal per scope, used when the caller supplies no exact wording.
SCOPE_REFUSALS: dict[SourceScope, str] = {
    SourceScope.UPLOADED_DOCUMENTS_ONLY: (
        "No uploaded document matching this question is currently indexed."
    ),
    SourceScope.FINANCIAL_DATABASE_ONLY: (
        "No financial-database evidence bears on this question."
    ),
    SourceScope.MARKET_DATA_ONLY: (
        "No market data bears on this question."
    ),
    SourceScope.HYBRID: (
        "No evidence in the platform bears on this question."
    ),
}

# --- phrasing that restricts the source ---------------------------------
#
# Matched on the analyst's own question only, never the assembled prompt: the
# prompt contains the evidence block, and "uploaded document" appears there
# whenever a document is in context, which would restrict every question.
_DOCUMENT_ONLY = re.compile(
    r"\b(?:only|solely|exclusively)\b[^.?!]{0,40}?"
    r"\b(?:uploaded|attached|ingested)\b"
    r"|\b(?:uploaded|attached|ingested)\b[^.?!]{0,30}?\b(?:only|alone)\b"
    r"|\buse\s+only\s+(?:the\s+)?(?:uploaded|attached|ingested|document)"
    r"|\bfrom\s+the\s+uploaded\s+document[s]?\s+only\b",
    re.IGNORECASE,
)
_FINANCIALS_ONLY = re.compile(
    r"\b(?:only|solely|exclusively)\b[^.?!]{0,40}?"
    r"\b(?:financial\s+database|reported\s+financials|platform\s+financials|"
    r"financial\s+statements)\b",
    re.IGNORECASE,
)
_MARKET_ONLY = re.compile(
    r"\b(?:only|solely|exclusively)\b[^.?!]{0,40}?\bmarket\s+data\b",
    re.IGNORECASE,
)

#: "reply exactly: '...'" / "respond with exactly \"...\"". Captures the words
#: the caller demands verbatim, so an integration can rely on the string.
_EXACT_REPLY = re.compile(
    r"(?:reply|respond|answer|say|return)\s+(?:with\s+)?exactly[:,]?\s*"
    r"['\"\u2018\u201c]([^'\"\u2019\u201d]{3,200})['\"\u2019\u201d]",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class SourceDirective:
    """What the caller asked for, and how to refuse if it cannot be met."""

    scope: SourceScope = SourceScope.HYBRID
    #: Verbatim wording the caller demanded, if any.
    exact_refusal: str | None = None
    #: True when the scope was inferred from the question rather than passed
    #: explicitly by the API caller. Recorded for the audit trail.
    inferred: bool = False

    @property
    def refusal_text(self) -> str:
        return self.exact_refusal or SCOPE_REFUSALS[self.scope]

    def admits(self, kind: EvidenceKind) -> bool:
        return kind in SCOPE_KINDS[self.scope]


def parse_directive(question: str) -> SourceDirective:
    """Read any source restriction out of the analyst's question.

    Restriction is honoured whether it arrives as an API parameter or in
    natural language: a user typing "use ONLY uploaded documents" into a chat
    box has expressed the same requirement as a caller passing the enum, and
    honouring one but not the other is the bug this module fixes.
    """
    text = question or ""
    exact = _EXACT_REPLY.search(text)
    exact_refusal = exact.group(1).strip() if exact else None

    scope = SourceScope.HYBRID
    if _DOCUMENT_ONLY.search(text):
        scope = SourceScope.UPLOADED_DOCUMENTS_ONLY
    elif _FINANCIALS_ONLY.search(text):
        scope = SourceScope.FINANCIAL_DATABASE_ONLY
    elif _MARKET_ONLY.search(text):
        scope = SourceScope.MARKET_DATA_ONLY

    return SourceDirective(
        scope=scope,
        exact_refusal=exact_refusal,
        inferred=scope is not SourceScope.HYBRID,
    )
