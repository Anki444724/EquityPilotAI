"""Deterministic offline provider.

No live API key is available in this environment, so without this the AI layer
could not be demonstrated or tested end to end. It is **not** a stub that
returns lorem ipsum: it composes genuine prose from the grounded context the
platform assembled, obeying the same citation and hedging rules the real
system prompt imposes.

That distinction matters. It means the grounding, citation, guardrail and
memory machinery is exercised for real — only the language model is simulated.
When a key is configured the router prefers the live provider automatically and
this becomes dormant.

Every sentence it emits is derived from a supplied `[citation]` marker, so it
is structurally incapable of fabricating a number — which is the property the
brief cares most about.
"""
from __future__ import annotations

import re

from app.domain.ai.types import (
    CompletionRequest, CompletionResponse, Role, TokenUsage,
)
from app.services.ai.providers.base import LLMProvider, ProviderConfig

NAME = "Offline"

DEFAULTS = ProviderConfig(
    name=NAME,
    endpoint="local://offline",
    auth_header="",
    payload_shape="offline",
    response_path="",
    default_model="offline-analyst-v1",
    api_key="offline",          # always "configured"
    input_cost_per_m=0.0,
    output_cost_per_m=0.0,
)

#: Roughly four characters per token — good enough for accounting in a mock.
CHARS_PER_TOKEN = 4

_EVIDENCE = re.compile(r"^\[([a-z0-9_.]+)\]\s+(.+?):\s+(.+?)\s+—\s+source:\s+(.+)$",
                       re.MULTILINE)
_TASK = re.compile(r"^TASK:\s*(.+)$", re.MULTILINE)
#: The analyst's own words, as `prompt_builder` labels them.
#:
#: The whole user message is task + evidence block + question + style note, so
#: relevance-matching against it compares the question to sixty evidence
#: labels and always finds an overlap. Only the question itself is a fair
#: basis for deciding whether the evidence bears on what was asked.
_QUESTION = re.compile(r"^ANALYST QUESTION:\s*(.+)$", re.MULTILINE)


#: Fiscal years the platform could hold. Anything beyond the forecast horizon
#: is not a question about missing data — it is a question about the future.
_FUTURE_YEAR = re.compile(r"\bFY\s?(20[3-9]\d)\b|\b(20[3-9]\d)\b", re.IGNORECASE)
#: A specific calendar date. The platform retains a price *series*, not a
#: quotable close for an arbitrary past day.
_SPECIFIC_DATE = re.compile(
    r"\b\d{1,2}\s+(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b", re.IGNORECASE,
)
#: Dimensions the canonical 54 line items do not carry at all.
_UNHELD_DIMENSIONS = (
    "market share", "geography", "europe", "america", "region",
    "segment split", "headcount", "employees",
)
#: Vocabulary indicating the question is about an uploaded document rather
#: than a computed figure. Used only to choose the right refusal wording.
_DOCUMENT_TERMS = (
    "annual report", "uploaded", "document", "filing", "chairman",
    "chairperson", "director", "management discussion", "md&a", "auditor",
    "transcript", "conference call", "letter to shareholders", "statement",
    "disclosed", "notes to accounts", "governance",
)


def _has_document_evidence(evidence) -> bool:
    """Did retrieval put any document passage in front of the model?"""
    return any("doc_" in str(item[0]) for item in evidence)


#: Companies outside the coverage universe, named in a comparison.
_OFF_UNIVERSE = ("tesla", "apple", "amazon", "google", "microsoft", "nvidia")


def _out_of_scope(question: str, evidence_labels: str = "") -> bool:
    """True when the question asks for a scope the platform does not hold.

    `_UNHELD_DIMENSIONS` was written when the platform held nothing but the 54
    canonical line items, and headcount genuinely was unavailable. Document
    ingestion changed that: an uploaded annual report contributes headcount,
    attrition, principal risks and similar disclosures as real, cited
    evidence. Refusing on a fixed keyword list then denies a question the
    platform can now answer from a document the user uploaded for exactly
    that purpose.

    So the list is treated as a default rather than a verdict: a dimension is
    only out of scope when nothing in the evidence actually covers it. The
    genuinely unheld scopes — a future year, an off-universe peer, a specific
    past date — remain unconditional, because no amount of evidence about
    *this* company answers a question about Tesla.
    """
    lowered = question.lower()
    if _FUTURE_YEAR.search(question):
        return True
    if _SPECIFIC_DATE.search(question):
        return True
    if any(name in lowered for name in _OFF_UNIVERSE):
        return True
    labels = evidence_labels.lower()
    for term in _UNHELD_DIMENSIONS:
        if term in lowered and term not in labels:
            return True
    return False


class OfflineProvider(LLMProvider):
    """Composes a grounded answer from the evidence block in the prompt."""

    def build_payload(self, request: CompletionRequest, model: str) -> dict:
        return {}

    def extract_content(self, body: dict) -> str:
        return ""

    def extract_usage(self, body: dict) -> TokenUsage:
        return TokenUsage()

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        prompt = "\n".join(m.content for m in request.messages)
        user = next(
            (m.content for m in reversed(request.messages) if m.role is Role.USER), ""
        )
        evidence = _EVIDENCE.findall(prompt)
        task_match = _TASK.search(prompt)
        task = task_match.group(1).strip() if task_match else "this question"

        # Only the analyst's own question, not the whole assembled prompt.
        asked = _QUESTION.search(prompt)
        question = asked.group(1).strip() if asked else ""

        content = self._compose(task, question, evidence)
        prompt_tokens = max(1, len(prompt) // CHARS_PER_TOKEN)
        completion_tokens = max(1, len(content) // CHARS_PER_TOKEN)

        return CompletionResponse(
            content=content, provider=self.name,
            model=request.model or self.config.default_model,
            usage=TokenUsage(prompt_tokens, completion_tokens),
            latency_ms=1.0, cost_usd=0.0,
        )

    # ------------------------------------------------------------ composition
    @staticmethod
    def _compose(task: str, question: str, evidence: list[tuple[str, str, str, str]]) -> str:
        if not evidence:
            return (
                "**Unavailable.** The platform holds no figures that bear on this "
                "question, so there is nothing to analyse. Import the relevant "
                "filings and the analysis can be rerun."
            )

        # Prefer evidence whose label overlaps the question, so the answer
        # responds to what was actually asked.
        words = {w for w in re.findall(r"[a-z]{4,}", question.lower())}
        def overlap(item: tuple[str, str, str, str]) -> int:
            # Label *and* value. For a computed figure the label carries the
            # meaning ("Revenue (FY26)") and the value is a number, so
            # matching the label alone was right. For a retrieved passage it
            # is the reverse: the label is "Report p.3" and every meaningful
            # word — chairman, dividend, attrition — is in the value. Matching
            # the label only scored those at zero and refused a question the
            # passage answered verbatim.
            return sum(1 for w in words if w in f"{item[1]} {item[2]}".lower())

        ranked = sorted(evidence, key=lambda item: -overlap(item))

        # Say so when nothing on hand bears on the question.
        #
        # The audit asked eight deliberately unanswerable questions —
        # headcount, a 2031 revenue figure, an earnings-call quote — and got
        # eight confident-looking answers built from whatever evidence
        # happened to rank first. Nothing was fabricated (every figure was
        # real and cited, and the citation audit passed at 100%), but
        # answering "how many employees?" with cash flow from operations is a
        # non-answer dressed as an answer, and a reader skimming the first
        # line would not notice.
        #
        # A free-text question with no overlapping evidence gets a refusal.
        # Fixed capabilities are exempt: `bear_case` legitimately shares no
        # vocabulary with a balance-sheet label, and the caller chose it
        # deliberately.
        # A shared word is not the same as available evidence.
        #
        # "What was the revenue in FY2031?" overlaps the label "Revenue
        # (FY26)" on one word and would otherwise be answered with a figure
        # from the wrong year. The same for "market share in Europe" against
        # "Market capitalisation", and "Tesla's gross margin" against "EBITDA
        # margin". The overlap test catches a question about a topic the
        # platform does not hold; these are questions about a *scope* it does
        # not hold — a future year, a geography, an off-universe peer, a
        # specific past date — and they need naming explicitly.
        # Pass the evidence labels so a dimension the platform *does* now hold
        # — headcount from an uploaded annual report, for instance — is not
        # refused on the strength of a stale keyword list.
        out_of_scope = _out_of_scope(
            question, " ".join(" ".join(str(f) for f in item) for item in evidence),
        )

        if question.strip() and words and (overlap(ranked[0]) == 0 or out_of_scope):
            # Distinguish "nothing was retrieved from the uploaded documents"
            # from "the platform holds no figure". A question about a
            # chairman's statement is answerable in principle — the answer
            # lives in an uploaded report — so telling the user the platform
            # only has financials is actively misleading when the real cause
            # is that retrieval returned nothing.
            asked_of_documents = any(
                term in question.lower() for term in _DOCUMENT_TERMS
            )
            if asked_of_documents and not _has_document_evidence(evidence):
                return (
                    "**No evidence found in uploaded documents.**\n\n"
                    "Nothing in the documents ingested for this company "
                    "matches that question. Either the passage is not in any "
                    "uploaded file, or no document covering it has been "
                    "ingested yet.\n\n"
                    "_No figures are offered above because none would be "
                    "supported._"
                )
            return (
                "**Insufficient evidence.** The platform holds no figure that "
                "bears on this question, so there is nothing to cite and no "
                "answer to give.\n\n"
                "The evidence available for this company covers reported "
                "financials, forecast output, valuation and scoring, together "
                "with whatever has been extracted from uploaded documents. "
                "Anything outside that — market share, prices on a specific "
                "past date, an off-universe peer — is not in the platform's "
                "data and will not be inferred.\n\n"
                "_No figures are offered above because none would be "
                "supported._"
            )

        headline, supporting = ranked[0], ranked[1:5]

        lines = [
            f"**{task}**",
            "",
            f"The platform's figures put {headline[1].lower()} at "
            f"{headline[2]} [{headline[0]}]. That is the anchor for the "
            "assessment below.",
        ]

        if supporting:
            lines += ["", "Supporting evidence:", ""]
            lines += [
                f"- {label} of {value} [{key}], per {source}."
                for key, label, value, source in supporting
            ]

        lines += [
            "",
            "**Interpretation.** Read together, these figures suggest the "
            "position is best judged on the balance between the strongest and "
            "weakest measures above rather than on any single metric. This is "
            "the platform's reading of its own outputs, not a forecast of "
            "market behaviour.",
            "",
            "**Caveat.** This is analysis, not investment advice, and it rests "
            "only on the evidence cited. Figures the platform does not hold are "
            "absent from the reasoning entirely.",
        ]
        return "\n".join(lines)
