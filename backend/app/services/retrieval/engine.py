"""Hybrid retrieval over pgvector.

Replaces an engine that loaded every chunk for a company into Python on every
query and scored them in a loop. That was tractable at 8,758 chunks and is not
a design that survives growth: cost is linear in corpus size per query, and
the index was rebuilt from scratch each time.

Here the semantic search is an indexed `ORDER BY embedding <=> query` inside
Postgres, so the database does the work it is built for and returns 40 rows
instead of 11,477.

Four signals, fused by rank
---------------------------
* **Semantic** — pgvector cosine over bge-m3 embeddings.
* **Lexical** — Postgres full-text search, which is BM25-adjacent and, more
  importantly, already indexed. The previous engine's BM25 was recomputed in
  Python per query.
* **Metadata** — document type, section, fiscal year drawn from the question.
* **Temporal** — recency, applied as a signal rather than a filter so an
  older passage that answers the question is not discarded for being old.

See `domain/retrieval/types.py` for why fusion is rank-based.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import structlog
from sqlalchemy import text

from app.domain.retrieval.types import (
    CANDIDATE_POOL, RERANK_POOL, RetrievalResult, RetrievalSignal,
    confidence_of, reciprocal_rank_fusion,
)
from app.services.retrieval.rerank import RerankCandidate, build_reranker

log = structlog.get_logger(__name__)

#: Fiscal-year mentions: "FY26", "FY2026", "in 2026".
_YEAR = re.compile(r"\bfy\s?(\d{2,4})\b|\b(20\d{2})\b", re.IGNORECASE)

#: Document-type hints a question can carry.
_DOC_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "annual_report": ("annual report", "annual results", "10-k"),
    "quarterly_report": ("quarterly", "q1", "q2", "q3", "q4", "quarter"),
    "conference_call": ("call", "transcript", "management said", "guidance"),
    "investor_presentation": ("presentation", "deck", "slide"),
    "credit_rating": ("rating", "crisil", "icra", "care ratings"),
    "esg_report": ("esg", "sustainability", "brsr", "emissions"),
    "shareholding": ("shareholding", "promoter", "pledge", "fii", "dii"),
}



#: English stopwords, removed before building the lexical query.
#:
#: English-only and deliberately short. The text-search configuration is
#: 'simple' rather than 'english' so that Devanagari and transliterated
#: Hinglish survive tokenisation, which means stopword removal has to happen
#: here. An aggressive list would strip the content words of a Hinglish query
#: ("kya", "hai") and defeat the multilingual requirement.
_STOPWORDS = frozenset("""
a an the of and or in on at to for from by with is are was were be been
what which who whom whose how why when where does do did has have had this
that these those it its as if then than so such about into over under much
""".split())

_TERM = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)


def _content_terms(query: str) -> list[str]:
    """Content-bearing terms for the lexical query, deduplicated."""
    seen: list[str] = []
    for token in _TERM.findall((query or "").lower()):
        if token in _STOPWORDS or len(token) < 2:
            continue
        if token not in seen:
            seen.append(token)
    return seen[:24]      # a long question must not build a pathological query


@dataclass(slots=True)
class QueryIntent:
    """What the question implies beyond its words."""

    fiscal_year: int | None = None
    doc_types: list[str] = field(default_factory=list)
    wants_recent: bool = False

    @property
    def has_metadata(self) -> bool:
        return bool(self.fiscal_year or self.doc_types)


def parse_intent(query: str) -> QueryIntent:
    """Extract metadata and temporal hints from a natural-language question.

    Deliberately conservative. A hint the question did not intend becomes a
    filter that hides the answer, so only unambiguous signals are taken:
    an explicit year, a named document type, an explicit recency word.
    """
    intent = QueryIntent()
    lowered = (query or "").lower()

    match = _YEAR.search(lowered)
    if match:
        raw = match.group(1) or match.group(2)
        year = int(raw)
        # "FY26" means 2026. Two-digit years below 50 are this century.
        if year < 100:
            year += 2000
        if 1990 <= year <= 2100:
            intent.fiscal_year = year

    for doc_type, hints in _DOC_TYPE_HINTS.items():
        if any(hint in lowered for hint in hints):
            intent.doc_types.append(doc_type)

    intent.wants_recent = any(
        word in lowered
        for word in ("latest", "recent", "current", "now", "today", "this year")
    )
    return intent


class HybridRetrievalEngine:
    """Semantic + lexical + metadata + temporal, fused and reranked."""

    def __init__(
        self,
        db: Any,
        *,
        embedder: Any = None,
        reranker: Any = None,
    ) -> None:
        self.db = db
        if embedder is None:
            from app.services.retrieval.embeddings import (
                build_semantic_embedder,
            )
            embedder = build_semantic_embedder()
        self.embedder = embedder
        self.reranker = reranker if reranker is not None else build_reranker()

    @property
    def available(self) -> bool:
        """Whether semantic retrieval can run at all."""
        return self.embedder is not None

    # ---------------------------------------------------------- retrieval
    def retrieve(
        self,
        query: str,
        *,
        company_id: str | None = None,
        top_k: int = 10,
        document_ids: Sequence[int] | None = None,
        rerank: bool = True,
    ) -> list[RetrievalResult]:
        started = time.perf_counter()
        cleaned = " ".join((query or "").split())
        if not cleaned:
            return []

        intent = parse_intent(cleaned)
        rankings: dict[RetrievalSignal, list[int]] = {}
        raw_scores: dict[int, dict[str, float]] = {}

        semantic = self._semantic(cleaned, company_id, document_ids)
        if semantic:
            rankings[RetrievalSignal.SEMANTIC] = [c for c, _ in semantic]
            for chunk_id, score in semantic:
                raw_scores.setdefault(chunk_id, {})["semantic"] = score

        lexical = self._lexical(cleaned, company_id, document_ids)
        if lexical:
            rankings[RetrievalSignal.LEXICAL] = [c for c, _ in lexical]
            for chunk_id, score in lexical:
                raw_scores.setdefault(chunk_id, {})["lexical"] = score

        if intent.has_metadata:
            metadata = self._metadata(intent, company_id, document_ids)
            if metadata:
                rankings[RetrievalSignal.METADATA] = [c for c, _ in metadata]
                for chunk_id, score in metadata:
                    raw_scores.setdefault(chunk_id, {})["metadata"] = score

        if intent.wants_recent:
            temporal = self._temporal(company_id, document_ids)
            if temporal:
                rankings[RetrievalSignal.TEMPORAL] = [c for c, _ in temporal]
                for chunk_id, score in temporal:
                    raw_scores.setdefault(chunk_id, {})["temporal"] = score

        if not rankings:
            return []

        fused = reciprocal_rank_fusion(rankings)
        ordered = sorted(fused.items(), key=lambda kv: -kv[1][0])
        shortlist = [chunk_id for chunk_id, _ in ordered[:max(top_k, RERANK_POOL)]]
        if not shortlist:
            return []

        rows = self._hydrate(shortlist)
        results: list[RetrievalResult] = []
        best_fused = ordered[0][1][0] or 1.0

        for chunk_id in shortlist:
            row = rows.get(chunk_id)
            if row is None:
                continue
            score, provenance = fused[chunk_id]
            raw = raw_scores.get(chunk_id, {})
            results.append(RetrievalResult(
                chunk_id=chunk_id,
                document_id=row["document_id"],
                text=row["text"],
                page=row["page"] or 0,
                paragraph=row["paragraph"] or 0,
                section=row["section"] or "unknown",
                document_title=row["title"] or "",
                # Normalised against the best fused score so the figure reads
                # as "relative to the best match for THIS query", which is the
                # only claim a fused rank can honestly support.
                score=round(score / best_fused, 6),
                confidence=confidence_of(
                    semantic=raw.get("semantic", 0.0),
                    signal_count=len(provenance),
                ),
                signals=provenance,
                raw=raw,
                source=row["source"] or "",
                metadata={
                    "doc_type": row["doc_type"] or "",
                    "fiscal_year": str(row["fiscal_year"] or ""),
                },
            ))

        if rerank and self.reranker is not None and results:
            results = self._apply_rerank(cleaned, results)

        final = results[:top_k]
        log.debug("hybrid retrieval", query=cleaned[:60],
                  signals=list(rankings), candidates=len(fused),
                  returned=len(final),
                  ms=round((time.perf_counter() - started) * 1000, 1))
        return final

    # -------------------------------------------------------- 1. semantic
    def _semantic(
        self, query: str, company_id: str | None,
        document_ids: Sequence[int] | None,
    ) -> list[tuple[int, float]]:
        if self.embedder is None:
            return []
        try:
            vector = self.embedder.embed_one(query)
        except Exception:  # noqa: BLE001 — a provider outage degrades to lexical
            log.exception("query embedding failed")
            return []

        literal = "[" + ",".join(f"{v:.7f}" for v in vector) + "]"
        where = ["c.embedding_v2 IS NOT NULL"]
        params: dict[str, Any] = {"vec": literal, "limit": CANDIDATE_POOL}
        if company_id:
            where.append("d.company_id = :company_id")
            params["company_id"] = company_id
        if document_ids:
            where.append("c.document_id = ANY(:doc_ids)")
            params["doc_ids"] = list(document_ids)

        sql = text(f"""
            SELECT c.id, 1 - (c.embedding_v2 <=> CAST(:vec AS vector)) AS similarity
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(where)}
            ORDER BY c.embedding_v2 <=> CAST(:vec AS vector)
            LIMIT :limit
        """)
        try:
            return [(r[0], float(r[1])) for r in self.db.execute(sql, params)]
        except Exception:  # noqa: BLE001 — pgvector absent, e.g. on SQLite
            log.debug("semantic search unavailable")
            return []

    # --------------------------------------------------------- 2. lexical
    def _lexical(
        self, query: str, company_id: str | None,
        document_ids: Sequence[int] | None,
    ) -> list[tuple[int, float]]:
        # RETR-001. Postgres has no OR-by-default query parser.
        # `plainto_tsquery` AND-joins every term, and so does
        # `websearch_to_tsquery` for bare words — both turn "Who runs the
        # company?" into 'who' & 'runs' & 'the' & 'company', which matches
        # nothing, while 'director' alone matches four chunks.
        #
        # Measured: the first benchmark showed the lexical signal returning
        # zero rows for 6 of 8 natural-language probes and paraphrase MRR
        # collapsing to 0.06 against the legacy engine's 0.50. BM25 scores
        # terms independently; a conjunctive query is the wrong model for a
        # question.
        #
        # The query is therefore built explicitly as an OR of content terms,
        # with English stopwords removed here because the 'simple'
        # configuration deliberately does not strip them — 'simple' is used
        # so Hindi and Hinglish survive, and the trade is that stopword
        # removal becomes this layer's job.
        terms = _content_terms(query)
        if not terms:
            return []
        tsquery = "to_tsquery('simple', :q)"
        where = [f"c.text_search @@ {tsquery}"]
        params: dict[str, Any] = {
            "q": " | ".join(terms), "limit": CANDIDATE_POOL,
        }
        sql = text(f"""
            SELECT c.id, ts_rank_cd(c.text_search, {tsquery}) AS rank
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(where)}
            ORDER BY rank DESC
            LIMIT :limit
        """)
        try:
            return [(r[0], float(r[1])) for r in self.db.execute(sql, params)]
        except Exception:  # noqa: BLE001
            log.debug("lexical search unavailable")
            return []

    # -------------------------------------------------------- 3. metadata
    def _metadata(
        self, intent: QueryIntent, company_id: str | None,
        document_ids: Sequence[int] | None,
    ) -> list[tuple[int, float]]:
        where: list[str] = []
        params: dict[str, Any] = {"limit": CANDIDATE_POOL}
        if intent.fiscal_year:
            where.append("d.fiscal_year = :fy")
            params["fy"] = intent.fiscal_year
        if intent.doc_types:
            where.append("d.doc_type = ANY(:types)")
            params["types"] = intent.doc_types
        if not where:
            return []
        if company_id:
            where.append("d.company_id = :company_id")
            params["company_id"] = company_id
        if document_ids:
            where.append("c.document_id = ANY(:doc_ids)")
            params["doc_ids"] = list(document_ids)

        sql = text(f"""
            SELECT c.id
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(where)}
            ORDER BY c.id
            LIMIT :limit
        """)
        try:
            return [(r[0], 1.0) for r in self.db.execute(sql, params)]
        except Exception:  # noqa: BLE001
            return []

    # -------------------------------------------------------- 4. temporal
    def _temporal(
        self, company_id: str | None, document_ids: Sequence[int] | None,
    ) -> list[tuple[int, float]]:
        where = ["d.status = 'completed'"]
        params: dict[str, Any] = {"limit": CANDIDATE_POOL}
        if company_id:
            where.append("d.company_id = :company_id")
            params["company_id"] = company_id
        if document_ids:
            where.append("c.document_id = ANY(:doc_ids)")
            params["doc_ids"] = list(document_ids)

        sql = text(f"""
            SELECT c.id
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(d.fiscal_year, 0) DESC, d.created_at DESC, c.id
            LIMIT :limit
        """)
        try:
            return [(r[0], 1.0) for r in self.db.execute(sql, params)]
        except Exception:  # noqa: BLE001
            return []

    # -------------------------------------------------------- hydration
    def _hydrate(self, chunk_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        sql = text("""
            SELECT c.id, c.document_id, c.text, c.page, c.paragraph,
                   c.section, d.title, d.doc_type, d.fiscal_year, d.filename
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.id = ANY(:ids)
        """)
        rows = self.db.execute(sql, {"ids": list(chunk_ids)})
        return {
            r[0]: {
                "document_id": r[1], "text": r[2], "page": r[3],
                "paragraph": r[4], "section": r[5], "title": r[6],
                "doc_type": r[7], "fiscal_year": r[8], "source": r[9],
            }
            for r in rows
        }

    # --------------------------------------------------------- reranking
    def _apply_rerank(
        self, query: str, results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        pool = results[:RERANK_POOL]
        try:
            scores = self.reranker.rerank(
                query,
                [RerankCandidate(r.chunk_id, r.text) for r in pool],
            )
        except Exception:  # noqa: BLE001 — reranking must never lose the results
            log.exception("reranking failed; keeping fused order")
            return results

        by_chunk = {s.chunk_id: s.score for s in scores}
        for result in pool:
            result.rerank_score = by_chunk.get(result.chunk_id)
            result.confidence = confidence_of(
                semantic=result.raw.get("semantic", 0.0),
                signal_count=len(result.signals),
                rerank_score=result.rerank_score,
            )

        # Reranked candidates come first, in reranked order; anything beyond
        # the pool keeps its fused position rather than being discarded.
        pool.sort(key=lambda r: -(r.rerank_score or 0.0))
        return pool + results[RERANK_POOL:]
