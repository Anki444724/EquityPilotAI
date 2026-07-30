"""Semantic search and grounded answer composition.

The brief's requirement for search is unusually specific: return an **answer**,
**supporting paragraphs**, **page numbers** and a **confidence**. That is not a
search endpoint, it is a question-answering endpoint with an auditable trail.

The composer here is extractive, not generative. It assembles an answer out of
sentences that exist verbatim in the retrieved chunks, and it never writes a
sentence of its own beyond the framing. That is a deliberate architectural
choice and it is the same principle Module 6 was built on: *the platform
produces the evidence, the LLM explains it*. An extractive answer cannot
hallucinate a number, because every character of it came from a page.

Where an LLM is configured, Module 6's analyst consumes these chunks as
citations and writes the prose. The extractive answer remains the fallback and
the ground truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.documents.types import (
    SearchAnswer, SearchHit, SectionKind, normalise_whitespace,
)
from app.services.documents.pipeline.chunking import split_sentences
from app.services.documents.pipeline.embeddings import (
    EmbeddingProvider, stem, stem_tokens, tokenise,
)
from app.services.documents.pipeline.vector_store import ScoredRecord, VectorStore

#: Words carrying no discriminating power in a query over corporate filings.
_QUERY_STOPWORDS = frozenset(
    """a an the and or of for to in on at is are was were be been being what
    which who whom whose how why when where does do did can could should would
    tell me about please give show list any""".split()
)


@dataclass(frozen=True, slots=True)
class SearchConfig:
    top_k: int = 8
    #: Sentences included in the composed answer.
    answer_sentences: int = 4
    #: Below this the answer is withheld and the gap declared instead.
    min_confidence: float = 0.12
    #: Characters of each hit returned to the caller.
    snippet_chars: int = 600


DEFAULT_SEARCH_CONFIG = SearchConfig()


def query_terms(query: str) -> list[str]:
    """Content words of a query, stemmed. Shared by retrieval and scoring.

    Stemmed so coverage is measured in the same vocabulary the index uses;
    otherwise a query term that genuinely matched would be counted as absent.
    """
    return [
        stem(t) for t in tokenise(query)
        if t not in _QUERY_STOPWORDS and len(t) > 1
    ]


class DocumentSearch:
    """Hybrid retrieval plus extractive, fully-cited answer composition."""

    def __init__(
        self,
        store: VectorStore,
        embedder: EmbeddingProvider,
        config: SearchConfig | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config or DEFAULT_SEARCH_CONFIG

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        document_ids: list[int] | None = None,
        sections: list[SectionKind] | None = None,
    ) -> list[SearchHit]:
        cleaned = normalise_whitespace(query)
        if not cleaned:
            return []
        vector = self.embedder.embed_one(cleaned)
        results = self.store.search(
            vector, cleaned,
            top_k=top_k or self.config.top_k,
            document_ids=document_ids,
            sections=sections,
        )
        return [self._to_hit(item) for item in results]

    def answer(
        self,
        query: str,
        *,
        top_k: int | None = None,
        document_ids: list[int] | None = None,
        sections: list[SectionKind] | None = None,
    ) -> SearchAnswer:
        cleaned = normalise_whitespace(query)
        if not cleaned:
            return SearchAnswer(
                query=query, answer="", confidence=0.0,
                unavailable_reason="No query was supplied.",
            )

        hits = self.search(
            cleaned, top_k=top_k, document_ids=document_ids, sections=sections
        )
        if not hits:
            # Stated plainly. The brief's grounding rule applies to search
            # exactly as it applies to the analyst: no evidence, no answer.
            return SearchAnswer(
                query=cleaned, answer="", hits=[], confidence=0.0,
                unavailable_reason=(
                    "No indexed passage matches this query. The platform holds "
                    "no document evidence on this point and will not infer one."
                ),
            )

        confidence = self._confidence(hits, cleaned)
        if confidence < self.config.min_confidence:
            return SearchAnswer(
                query=cleaned, answer="", hits=hits, confidence=confidence,
                unavailable_reason=(
                    "Matches were found but none is a close enough match to "
                    "support an answer. The supporting passages are returned "
                    "for review rather than summarised."
                ),
            )

        return SearchAnswer(
            query=cleaned,
            answer=self._compose(cleaned, hits),
            hits=hits,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    def _to_hit(self, item: ScoredRecord) -> SearchHit:
        record = item.record
        return SearchHit(
            chunk_id=record.chunk_id,
            document_id=record.document_id,
            document_title=record.document_title,
            page=record.page,
            paragraph=record.paragraph,
            section=record.section,
            text=record.text[: self.config.snippet_chars],
            score=round(item.score, 6),
            lexical_score=round(item.lexical, 6),
            semantic_score=round(item.semantic, 6),
        )

    #: Query terms so common in filings that matching them is no evidence the
    #: question was answered. Every annual report contains "company" and
    #: "policy"; a hit on those alone means nothing.
    _LOW_VALUE_TERMS = frozenset(
        stem(t) for t in
        """company companies group business year years financial report annual
        total policy management board information data value values""".split()
    )

    def _confidence(self, hits: list[SearchHit], query: str) -> float:
        """Confidence in the *answer*, not in the retrieval ranking.

        Retrieval always returns its best guess; confidence is what stops that
        guess from being presented as an answer. Asked about a dividend policy
        by a corpus that never mentions dividends, an earlier version of this
        method returned 0.64 on a governance sentence that happened to contain
        "policy" — a confident answer to a question the documents cannot
        answer, which is precisely the failure the grounding rule exists to
        prevent.

        The fix is to weight coverage by term *informativeness*. Matching
        "dividend" is evidence; matching "policy" is not. Four signals:

        * the best hit's blended score;
        * **weighted term coverage**, where a term's weight falls to near zero
          if it is boilerplate vocabulary;
        * a hard gate: if no informative query term appears anywhere in the top
          hits, confidence collapses regardless of the other signals;
        * corroboration across independent pages.
        """
        if not hits:
            return 0.0
        terms = list(query_terms(query))
        if not terms:
            return 0.0

        weights = {
            term: 0.1 if term in self._LOW_VALUE_TERMS else 1.0 for term in terms
        }
        informative = {t for t, w in weights.items() if w > 0.5}

        top = hits[: min(3, len(hits))]
        covered: set[str] = set()
        for hit in top:
            covered |= set(stem_tokens(hit.text)) & set(terms)

        total_weight = sum(weights.values())
        covered_weight = sum(weights[t] for t in covered)
        coverage = covered_weight / total_weight if total_weight else 0.0

        pages = {(h.document_id, h.page) for h in top}
        corroboration = min(0.15, 0.05 * (len(pages) - 1))
        score = 0.5 * hits[0].score + 0.4 * coverage + corroboration

        # The gate. A query whose informative terms are entirely absent has not
        # been answered, however well the boilerplate matched.
        if informative and not (covered & informative):
            score = min(score, self.config.min_confidence * 0.5)
        return round(min(0.95, score), 4)

    def _compose(self, query: str, hits: list[SearchHit]) -> str:
        """Build an answer from verbatim sentences, each tagged with its page.

        Every sentence in the output is traceable to a page, which is what makes
        the answer auditable. Sentences are selected by term overlap with the
        query and deduplicated, then presented in hit order so the strongest
        evidence leads.
        """
        terms = set(query_terms(query))
        scored: list[tuple[float, str, SearchHit]] = []
        seen: set[str] = set()

        for rank, hit in enumerate(hits):
            for sentence in split_sentences(hit.text):
                if len(sentence) < 30:
                    continue
                normalised = sentence.lower()
                if normalised in seen:
                    continue
                seen.add(normalised)
                overlap = len(set(stem_tokens(sentence)) & terms)
                if overlap == 0:
                    continue
                # Rank decay keeps a weak hit's sentence from outranking a
                # strong hit's merely by repeating more query words.
                score = overlap * (1.0 / (1 + 0.35 * rank))
                scored.append((score, sentence, hit))

        if not scored:
            # Retrieval found chunks but no sentence contains a query term.
            # Quote the strongest passage rather than paraphrase it.
            best = hits[0]
            return (
                f"The closest supporting passage is on page {best.page} of "
                f"{best.document_title or 'the document'}:\n\n"
                f"\u201c{normalise_whitespace(best.text)[:400]}\u201d"
            )

        scored.sort(key=lambda item: -item[0])
        chosen = scored[: self.config.answer_sentences]
        chosen.sort(key=lambda item: (hits.index(item[2]), item[1]))

        lines: list[str] = []
        for _, sentence, hit in chosen:
            location = f"p.{hit.page}"
            if hit.section is not SectionKind.UNKNOWN:
                location += f", {hit.section.value.replace('_', ' ')}"
            lines.append(f"{sentence} [{location}]")
        return " ".join(lines)


# ---------------------------------------------------------------------------
# Citation framework
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DocumentCitation:
    """A document citation: document, page, section, paragraph.

    Exactly the four fields the brief requires. Constructed only from a real
    :class:`SearchHit`, so a citation cannot be assembled for a passage that
    was never retrieved.
    """

    document_id: int
    document_title: str
    page: int
    section: SectionKind
    paragraph: int
    quote: str
    chunk_id: int

    @property
    def marker(self) -> str:
        return f"[{self.document_title or 'doc'} p.{self.page}]"

    def render(self) -> str:
        parts = [self.document_title or f"document {self.document_id}", f"p.{self.page}"]
        if self.section is not SectionKind.UNKNOWN:
            parts.append(self.section.value.replace("_", " "))
        parts.append(f"¶{self.paragraph}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "document_title": self.document_title,
            "page": self.page,
            "section": self.section.value,
            "paragraph": self.paragraph,
            "chunk_id": self.chunk_id,
            "quote": self.quote,
            "reference": self.render(),
        }


def cite(hit: SearchHit, *, quote_chars: int = 320) -> DocumentCitation:
    """Build a citation from a retrieved hit. The only construction path."""
    return DocumentCitation(
        document_id=hit.document_id,
        document_title=hit.document_title,
        page=hit.page,
        section=hit.section,
        paragraph=hit.paragraph,
        quote=normalise_whitespace(hit.text)[:quote_chars],
        chunk_id=hit.chunk_id,
    )


def cite_all(hits: list[SearchHit], *, limit: int = 8) -> list[DocumentCitation]:
    return [cite(hit) for hit in hits[:limit]]


_PAGE_REFERENCE = re.compile(r"\[p\.(\d+)[^\]]*\]")


def verify_answer_citations(answer: str, citations: list[DocumentCitation]) -> dict:
    """Check that every page an answer names was actually retrieved.

    This is the document-side equivalent of Module 6's citation auditor. It
    exists because an answer that cites p.42 when nothing from p.42 was
    retrieved is fabricated evidence, and fabricated evidence is worse than no
    answer at all.
    """
    cited_pages = {int(m.group(1)) for m in _PAGE_REFERENCE.finditer(answer)}
    available = {c.page for c in citations}
    unsupported = sorted(cited_pages - available)
    return {
        "cited_pages": sorted(cited_pages),
        "available_pages": sorted(available),
        "unsupported_pages": unsupported,
        "verified": not unsupported,
        "coverage": round(len(cited_pages & available) / len(cited_pages), 4)
        if cited_pages else 0.0,
    }
