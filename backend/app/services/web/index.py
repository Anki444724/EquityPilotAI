"""Self-owned web index — search over the web pages the platform already holds.

Part 3 Phase 4B. The planner names the ``WEB_RESEARCH`` route (Phase 4A)
and the planner-side ``WebQueryGenerator`` turns a plan into a handful of
short search strings. This module answers those strings from **local storage
only** (it does not import the planner: a query set is consumed by its
``texts`` shape, so the two sides stay independently testable):

* the corpus is every persisted ``Document`` whose ``doc_type`` is
  ``web_page`` — pages the web evidence crawl fetched, assessed and stored
  earlier. Filings, uploads, Blogger posts and every other document type are
  outside the index by construction (:meth:`SelfOwnedWebIndex._scope`);
* ranking reuses the platform's one retrieval engine
  (:class:`~app.services.retrieval.engine.HybridRetrievalEngine` — Postgres
  full-text search plus pgvector when the deployment has it) through the
  additive ``doc_types`` scope added for this phase, and falls back to the
  existing in-process store (:class:`InMemoryVectorStore`: BM25 plus the
  local hashing embedder) when the database is SQLite or the engine returns
  nothing. There is no second RAG engine, no second embedding model and no
  new table;
* authority and freshness come from the existing web quality layer
  (:func:`~app.services.web.quality.web_authority` over the
  ``WebSourceClass`` ordering) and the platform's single recency curve
  (:func:`~app.data.filings.base.recency_factor`).

What it deliberately does **not** do:

* fetch anything. No ``WebFetcher``, no crawler discovery, no HTTP client,
  no external search API and no LLM are imported or called. A query that
  the stored corpus cannot answer returns an empty result, honestly;
* rank stocks or companies. Only documents are ranked, and only among
  themselves;
* invent metadata. A page without a publish date reports ``None``; a page
  without a title reports ``None``. The platform's own "unknown date" recency
  factor is applied for ranking, but nothing is written into the result that
  the database does not hold;
* produce citations. The result is an explicit evidence candidate — the
  citation contract (Phase 4D) is a later, separate concern.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import select

from app.data.filings.base import recency_factor
from app.domain.documents.types import DocumentType, SectionKind
from app.domain.web.types import WebSourceClass
from app.models.document import Document, DocumentChunk
from app.services.documents.pipeline.embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
)
from app.services.documents.pipeline.vector_store import (
    InMemoryVectorStore,
    VectorRecord,
)
from app.services.web.extract import canonicalize_url
from app.services.web.quality import WEB_AUTHORITY, web_authority

log = logging.getLogger(__name__)

__all__ = [
    "AUTHORITY_WEIGHT",
    "FRESHNESS_WEIGHT",
    "INDEXED_STATUSES",
    "MULTI_QUERY_BONUS",
    "RELEVANCE_WEIGHT",
    "SEMANTIC_ONLY_FLOOR",
    "SelfOwnedWebIndex",
    "WEB_PAGE_DOC_TYPE",
    "WebEvidenceCandidate",
    "WebIndexLimits",
    "WebIndexSearchResult",
]

#: The only document type this index ever searches.
WEB_PAGE_DOC_TYPE: str = DocumentType.WEB_PAGE.value

#: Statuses a document must hold to be searchable. Mirrors
#: ``app.services.documents.service.INDEXED_STATUSES`` (asserted equal in the
#: tests) without importing the document service — the index reads rows, it
#: never processes them.
INDEXED_STATUSES: frozenset[str] = frozenset({"completed", "ready"})

#: Ranking blend. Relevance dominates because the question decides what is
#: useful; authority and freshness order otherwise comparable pages the way
#: the platform already orders web evidence elsewhere.
RELEVANCE_WEIGHT: float = 0.70
AUTHORITY_WEIGHT: float = 0.20
FRESHNESS_WEIGHT: float = 0.10
#: Added to relevance for every additional query variant that matched the
#: same document — a page hit by three phrasings of the question is more
#: likely on-topic than one hit by a single phrasing with the same top score.
MULTI_QUERY_BONUS: float = 0.05
#: A passage found by the semantic signal *alone* counts only when its
#: absolute similarity reaches this floor. Below it the passage is merely the
#: nearest neighbour in a corpus that may not contain the answer — which is
#: exactly the case a web search must report as "nothing found".
SEMANTIC_ONLY_FLOOR: float = 0.35

_AUTHORITY_CEILING: float = max(WEB_AUTHORITY.values())
_WHITESPACE = re.compile(r"\s+")
_ROUND = 6


# ---------------------------------------------------------------------------
# Limits and result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WebIndexLimits:
    """Hard bounds on one search. Every value is validated at construction."""

    #: Distinct query strings searched per call; extras are dropped in order.
    max_queries: int = 8
    #: Characters kept from each query string.
    max_query_chars: int = 200
    #: Passages requested from the engine per query.
    per_query: int = 10
    #: Candidates returned per call (after dedupe and ranking).
    max_results: int = 8
    #: Characters of chunk text kept in the snippet.
    snippet_chars: int = 400

    def __post_init__(self) -> None:
        for name in (
            "max_queries", "max_query_chars", "per_query", "max_results",
            "snippet_chars",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


@dataclass(frozen=True, slots=True)
class WebEvidenceCandidate:
    """One stored web page passage that answered at least one query.

    Every field is taken from the persisted document/chunk rows or computed
    from them; nothing is inferred. ``None`` means the store does not have it.
    """

    document_id: int
    chunk_id: int | None
    company_id: str | None
    source_url: str | None
    canonical_url: str | None
    title: str | None
    source_class: str | None
    published_at: datetime | None
    retrieved_at: datetime | None
    snippet: str
    #: Best per-query-normalised engine relevance in [0, 1].
    relevance: float
    #: ``web_authority(source_class, published_at)`` — the existing contract.
    authority: float
    #: ``recency_factor`` of the freshness basis, on the platform's curve.
    freshness: float
    #: Which date freshness was computed from: ``published_at``,
    #: ``retrieved_at`` or ``unknown``.
    freshness_basis: str
    #: Deterministic blend used for ordering.
    score: float
    matched_queries: tuple[str, ...] = ()
    #: Engine signals that produced the hit (``lexical``, ``semantic``,
    #: ``metadata``, ``temporal``).
    signals: tuple[str, ...] = ()
    page: int | None = None
    section: str | None = None
    #: Hash of the stored bytes — identity for dedupe, as ingestion uses it.
    content_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "company_id": self.company_id,
            "content_hash": self.content_hash,
            "source_url": self.source_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "source_class": self.source_class,
            "published_at": _iso(self.published_at),
            "retrieved_at": _iso(self.retrieved_at),
            "snippet": self.snippet,
            "relevance": self.relevance,
            "authority": self.authority,
            "freshness": self.freshness,
            "freshness_basis": self.freshness_basis,
            "score": self.score,
            "matched_queries": list(self.matched_queries),
            "signals": list(self.signals),
            "page": self.page,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class WebIndexSearchResult:
    """The outcome of one :meth:`SelfOwnedWebIndex.search` call."""

    queries: tuple[str, ...]
    company_id: str | None
    candidates: tuple[WebEvidenceCandidate, ...]
    #: Web pages in scope for this call (before any query was run).
    corpus_documents: int
    #: ``hybrid`` (the shared retrieval engine), ``in_memory`` (the local
    #: fallback store) or ``none`` (nothing was searched / nothing matched).
    engine: str
    #: Whether any returned candidate carried a semantic signal.
    semantic_used: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.candidates

    def as_dict(self) -> dict[str, Any]:
        return {
            "queries": list(self.queries),
            "company_id": self.company_id,
            "candidates": [c.as_dict() for c in self.candidates],
            "corpus_documents": self.corpus_documents,
            "engine": self.engine,
            "semantic_used": self.semantic_used,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Internal accumulation
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Hit:
    document_id: int
    chunk_id: int | None
    text: str
    page: int | None
    section: str | None
    relevance: float
    queries: list[str]
    signals: set[str]


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------
class SelfOwnedWebIndex:
    """Search the platform's stored web pages. Local storage only.

    ``engine`` may be any object exposing ``retrieve(query, *, company_id,
    top_k, doc_types, rerank)`` returning ``RetrievalResult``-shaped rows; by
    default the shared :class:`HybridRetrievalEngine` is built on first use
    when the session is bound to PostgreSQL. ``use_hybrid=False`` skips the
    shared engine entirely (the in-process store is then the only path).
    ``embedder`` supplies the query vector for the fallback store and must be
    a local provider; the default is the deterministic hashing embedder the
    document pipeline itself uses. ``clock`` supplies "today" for freshness.
    """

    def __init__(
        self,
        db: Any,
        *,
        engine: Any = None,
        use_hybrid: bool = True,
        embedder: EmbeddingProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        limits: WebIndexLimits | None = None,
    ) -> None:
        self.db = db
        self._engine = engine
        self._use_hybrid = use_hybrid
        self._embedder = embedder
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.limits = limits or WebIndexLimits()

    # ------------------------------------------------------------ public
    def search(
        self,
        queries: Any,
        *,
        company_id: str | None = None,
        limit: int | None = None,
    ) -> WebIndexSearchResult:
        """Rank stored web pages against ``queries``.

        ``queries`` may be a ``WebQuerySet``, a sequence of ``WebQuery``
        objects, a sequence of strings or a single string. ``company_id``
        restricts the corpus to that company's pages; ``limit`` caps the
        number of candidates (never above ``limits.max_results``).
        """
        texts = self._normalise_queries(queries)
        cap = self.limits.max_results if limit is None else max(1, min(limit, self.limits.max_results))
        notes: list[str] = []
        if not texts:
            return self._empty(texts, company_id, 0, ["no query supplied"])

        scope = self._scope(company_id)
        if not scope:
            return self._empty(
                texts, company_id, 0, ["no stored web pages in scope"],
            )
        allowed = set(scope)

        hits: dict[int | tuple[str, int], _Hit] = {}
        engine_used = "none"

        hybrid = self._hybrid_engine()
        if hybrid is not None:
            found = self._search_hybrid(hybrid, texts, company_id, allowed, hits)
            if found:
                engine_used = "hybrid"
            else:
                notes.append("shared retrieval engine returned no web passages")
        else:
            notes.append("shared retrieval engine not used on this database")

        if not hits:
            found = self._search_in_memory(texts, company_id, hits)
            if found:
                engine_used = "in_memory"
            else:
                notes.append("no stored web passage matched")

        if not hits:
            return self._empty(texts, company_id, len(scope), notes)

        candidates = self._assemble(hits.values(), allowed)
        candidates = self._dedupe(candidates)
        ranked = sorted(candidates, key=_rank_key)[:cap]
        return WebIndexSearchResult(
            queries=tuple(texts),
            company_id=company_id,
            candidates=tuple(ranked),
            corpus_documents=len(scope),
            engine=engine_used,
            semantic_used=any("semantic" in c.signals for c in ranked),
            notes=tuple(notes),
        )

    def corpus_size(self, company_id: str | None = None) -> int:
        """How many web pages are searchable (optionally for one company)."""
        return len(self._scope(company_id))

    # ------------------------------------------------------------- scope
    def _scope_query(self, company_id: str | None):
        query = select(Document.id).where(
            Document.doc_type == WEB_PAGE_DOC_TYPE,
            Document.status.in_(INDEXED_STATUSES),
            Document.superseded_by.is_(None),
        )
        if company_id is not None:
            query = query.where(Document.company_id == company_id)
        return query.order_by(Document.id)

    def _scope(self, company_id: str | None) -> list[int]:
        """Ids of every searchable web page, in a deterministic order."""
        try:
            rows = self.db.execute(self._scope_query(company_id)).all()
        except Exception:  # noqa: BLE001 — a scope failure is an empty index
            log.debug("web index scope query failed", exc_info=True)
            return []
        return [int(r[0]) for r in rows]

    # ----------------------------------------------------- hybrid engine
    def _hybrid_engine(self) -> Any:
        if not self._use_hybrid:
            return None
        if self._engine is not None:
            return self._engine
        dialect = _dialect_name(self.db)
        if dialect and dialect != "postgresql":
            # FTS/pgvector SQL cannot run here; do not issue doomed statements.
            return None
        try:
            from app.services.retrieval.engine import HybridRetrievalEngine

            self._engine = HybridRetrievalEngine(self.db)
        except Exception:  # noqa: BLE001 — the engine is optional
            log.debug("shared retrieval engine unavailable", exc_info=True)
            return None
        return self._engine

    def _search_hybrid(
        self,
        engine: Any,
        texts: Sequence[str],
        company_id: str | None,
        allowed: set[int],
        hits: dict,
    ) -> bool:
        found = False
        for query in texts:
            try:
                results = engine.retrieve(
                    query,
                    company_id=company_id,
                    top_k=self.limits.per_query,
                    doc_types=(WEB_PAGE_DOC_TYPE,),
                )
            except Exception:  # noqa: BLE001 — fall through to the local store
                log.debug("shared retrieval engine failed for %r", query, exc_info=True)
                results = []
            rows: list[tuple[Any, float, set[str]]] = []
            for result in results or []:
                document_id = _int_or_none(getattr(result, "document_id", None))
                if document_id is None or document_id not in allowed:
                    continue  # never let a non-web row through, whatever the engine did
                names = _signal_names(getattr(result, "signals", None))
                if not _hybrid_hit_counts(names, getattr(result, "raw", None)):
                    continue
                score = float(getattr(result, "score", 0.0) or 0.0)
                if score > 0:
                    rows.append((result, score, names))
            # The engine already reports each score relative to the best
            # match for the query; renormalising over the passages that
            # survived the gate keeps "1.0 = best accepted match".
            ceiling = max((score for _, score, _ in rows), default=0.0)
            for result, score, names in rows:
                metadata = getattr(result, "metadata", None) or {}
                section = getattr(result, "section", None)
                self._merge(
                    hits,
                    document_id=int(result.document_id),
                    chunk_id=_int_or_none(getattr(result, "chunk_id", None)),
                    text=str(getattr(result, "text", "") or ""),
                    page=_int_or_none(getattr(result, "page", None)),
                    section=_section_value(section) or _section_value(metadata.get("section")),
                    relevance=score / ceiling,
                    query=query,
                    signals=names or {"lexical"},
                )
                found = True
        return found

    # ------------------------------------------------- in-memory fallback
    def _embedder_or_default(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = HashingEmbeddingProvider()
        return self._embedder

    def _load_store(self, company_id: str | None) -> tuple[InMemoryVectorStore, bool]:
        """Load in-scope web page chunks into the existing in-process store.

        Vectors are reused only when the document was embedded in the same
        space as the query embedder; otherwise the chunk is lexical-only,
        because a cosine across two embedding spaces is a meaningless number.
        """
        store = InMemoryVectorStore()
        embedder = self._embedder_or_default()
        query = (
            select(DocumentChunk, Document)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(
                Document.doc_type == WEB_PAGE_DOC_TYPE,
                Document.status.in_(INDEXED_STATUSES),
                Document.superseded_by.is_(None),
            )
        )
        if company_id is not None:
            query = query.where(Document.company_id == company_id)
        query = query.order_by(DocumentChunk.document_id, DocumentChunk.id)
        try:
            rows = self.db.execute(query).all()
        except Exception:  # noqa: BLE001
            log.debug("web index chunk load failed", exc_info=True)
            return store, False

        records: list[VectorRecord] = []
        any_vector = False
        for chunk, document in rows:
            text_value = chunk.text or ""
            if not text_value.strip():
                continue
            vector: list[float] = []
            if (document.embedding_spec or "") == embedder.spec.key and chunk.embedding:
                vector = list(chunk.embedding)
                any_vector = True
            records.append(VectorRecord(
                chunk_id=int(chunk.id),
                document_id=int(document.id),
                text=text_value,
                page=int(chunk.page or 0),
                paragraph=int(chunk.paragraph or 0),
                section=_section_kind(chunk.section),
                document_title=document.title or document.filename or "",
                document_version=int(document.version or 1),
                vector=vector,
                metadata={"doc_type": document.doc_type or ""},
            ))
        if records:
            store.add(records, embedder.spec)
        return store, any_vector

    def _search_in_memory(
        self, texts: Sequence[str], company_id: str | None, hits: dict,
    ) -> bool:
        store, any_vector = self._load_store(company_id)
        if store.count() == 0:
            return False
        embedder = self._embedder_or_default()
        found = False
        for query in texts:
            vector = embedder.embed_one(query) if any_vector else []
            try:
                scored = store.search(vector, query, top_k=self.limits.per_query)
            except Exception:  # noqa: BLE001
                log.debug("in-memory web search failed for %r", query, exc_info=True)
                continue
            # The local hashing vectors are a character n-gram similarity,
            # not a semantic judgement: they may reorder passages the query
            # terms actually matched, never admit a passage they did not.
            accepted = [s for s in scored if s.score > 0 and s.lexical > 0]
            ceiling = max((s.score for s in accepted), default=0.0)
            for item in accepted:
                signals = {"lexical"}
                if item.semantic > 0:
                    signals.add("semantic")
                self._merge(
                    hits,
                    document_id=item.record.document_id,
                    chunk_id=item.record.chunk_id,
                    text=item.record.text,
                    page=item.record.page,
                    section=item.record.section.value,
                    relevance=item.score / ceiling,
                    query=query,
                    signals=signals or {"lexical"},
                )
                found = True
        return found

    # ------------------------------------------------------------ merge
    @staticmethod
    def _merge(
        hits: dict,
        *,
        document_id: int,
        chunk_id: int | None,
        text: str,
        page: int | None,
        section: str | None,
        relevance: float,
        query: str,
        signals: Iterable[str],
    ) -> None:
        key: int | tuple[str, int] = chunk_id if chunk_id is not None else ("doc", document_id)
        relevance = max(0.0, min(1.0, relevance))
        existing = hits.get(key)
        if existing is None:
            hits[key] = _Hit(
                document_id=document_id, chunk_id=chunk_id, text=text,
                page=page, section=section, relevance=relevance,
                queries=[query], signals=set(signals),
            )
            return
        existing.relevance = max(existing.relevance, relevance)
        if query not in existing.queries:
            existing.queries.append(query)
        existing.signals.update(signals)
        if not existing.text and text:
            existing.text = text

    # --------------------------------------------------------- assemble
    def _assemble(
        self, hits: Iterable[_Hit], allowed: set[int],
    ) -> list[WebEvidenceCandidate]:
        # One candidate per document: its best chunk, with every query and
        # signal that touched the document folded in.
        best: dict[int, _Hit] = {}
        extra_queries: dict[int, list[str]] = {}
        extra_signals: dict[int, set[str]] = {}
        for hit in sorted(hits, key=lambda h: (-h.relevance, h.document_id, h.chunk_id or 0)):
            if hit.document_id not in allowed:
                continue
            if hit.document_id not in best:
                best[hit.document_id] = hit
                extra_queries[hit.document_id] = list(hit.queries)
                extra_signals[hit.document_id] = set(hit.signals)
                continue
            for query in hit.queries:
                if query not in extra_queries[hit.document_id]:
                    extra_queries[hit.document_id].append(query)
            extra_signals[hit.document_id].update(hit.signals)

        documents = self._documents(best.keys())
        today = self._today()
        out: list[WebEvidenceCandidate] = []
        for document_id, hit in best.items():
            document = documents.get(document_id)
            if document is None:
                continue  # the row vanished between scope and hydrate; never guess
            web_meta = _web_metadata(document)
            source_url = document.source_url or _str_or_none(web_meta.get("source_url"))
            canonical = _str_or_none(web_meta.get("canonical_url")) or source_url
            canonical = _canonical(canonical)
            title = _str_or_none(document.title) or _str_or_none(web_meta.get("title"))
            source_class = _str_or_none(document.source_class) or _str_or_none(
                web_meta.get("source_class")
            )
            published_at = document.published_at or _parse_datetime(web_meta.get("published_at"))
            retrieved_at = document.retrieved_at or _parse_datetime(web_meta.get("retrieved_at"))

            authority = web_authority(_source_class(source_class), published_at=published_at)
            basis_date, basis = _freshness_basis(published_at, retrieved_at)
            freshness = recency_factor(basis_date, today=today)

            queries = tuple(extra_queries[document_id])
            relevance = min(1.0, hit.relevance + MULTI_QUERY_BONUS * (len(queries) - 1))
            score = (
                RELEVANCE_WEIGHT * relevance
                + AUTHORITY_WEIGHT * (authority / _AUTHORITY_CEILING if _AUTHORITY_CEILING else 0.0)
                + FRESHNESS_WEIGHT * freshness
            )
            out.append(WebEvidenceCandidate(
                document_id=document_id,
                chunk_id=hit.chunk_id,
                company_id=document.company_id,
                source_url=source_url,
                canonical_url=canonical,
                title=title,
                source_class=source_class,
                published_at=published_at,
                retrieved_at=retrieved_at,
                snippet=_snippet(hit.text, self.limits.snippet_chars),
                relevance=round(relevance, _ROUND),
                authority=round(authority, _ROUND),
                freshness=round(freshness, _ROUND),
                freshness_basis=basis,
                score=round(score, _ROUND),
                matched_queries=queries,
                signals=tuple(sorted(extra_signals[document_id])),
                page=hit.page,
                section=hit.section,
                content_hash=_str_or_none(document.content_hash),
            ))
        return out

    def _documents(self, ids: Iterable[int]) -> dict[int, Document]:
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        try:
            rows = self.db.execute(
                select(Document).where(Document.id.in_(wanted))
            ).scalars().all()
        except Exception:  # noqa: BLE001
            log.debug("web index hydrate failed", exc_info=True)
            return {}
        return {int(d.id): d for d in rows}

    # ----------------------------------------------------------- dedupe
    @staticmethod
    def _dedupe(candidates: list[WebEvidenceCandidate]) -> list[WebEvidenceCandidate]:
        """One candidate per page identity.

        Identity is the document row, the canonical URL and the stored
        bytes (``content_hash`` — the same page stored under two companies).
        Two snapshots of one URL collapse to the most recently retrieved
        copy, because that is the page as it currently reads; identical
        bytes collapse to the higher-ranked copy. Every tie ends on the
        lower document id, so the outcome never depends on input order.
        """
        latest_by_url: dict[str, WebEvidenceCandidate] = {}
        for candidate in candidates:
            url = candidate.canonical_url
            if not url:
                continue
            current = latest_by_url.get(url)
            if current is None or _snapshot_key(candidate) > _snapshot_key(current):
                latest_by_url[url] = candidate
        survivors = [
            c for c in candidates
            if not c.canonical_url or latest_by_url[c.canonical_url] is c
        ]
        kept: list[WebEvidenceCandidate] = []
        seen_docs: set[int] = set()
        seen_hashes: set[str] = set()
        for candidate in sorted(survivors, key=_rank_key):
            if candidate.document_id in seen_docs:
                continue
            if candidate.content_hash and candidate.content_hash in seen_hashes:
                continue
            seen_docs.add(candidate.document_id)
            if candidate.content_hash:
                seen_hashes.add(candidate.content_hash)
            kept.append(candidate)
        return kept

    # ----------------------------------------------------------- helpers
    def _normalise_queries(self, queries: Any) -> list[str]:
        texts: list[str] = []
        for raw in _iter_query_texts(queries):
            cleaned = _WHITESPACE.sub(" ", str(raw or "")).strip()
            if not cleaned:
                continue
            cleaned = cleaned[: self.limits.max_query_chars].strip()
            if cleaned and cleaned.casefold() not in {t.casefold() for t in texts}:
                texts.append(cleaned)
            if len(texts) >= self.limits.max_queries:
                break
        return texts

    def _today(self) -> date:
        try:
            now = self._clock()
        except Exception:  # noqa: BLE001
            now = datetime.now(timezone.utc)
        return now.date() if isinstance(now, datetime) else date.today()

    @staticmethod
    def _empty(
        texts: Sequence[str], company_id: str | None, corpus: int,
        notes: Sequence[str],
    ) -> WebIndexSearchResult:
        return WebIndexSearchResult(
            queries=tuple(texts),
            company_id=company_id,
            candidates=(),
            corpus_documents=corpus,
            engine="none",
            semantic_used=False,
            notes=tuple(notes),
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------
def _rank_key(candidate: WebEvidenceCandidate) -> tuple:
    retrieved = candidate.retrieved_at.timestamp() if candidate.retrieved_at else 0.0
    return (
        -candidate.score,
        -candidate.relevance,
        -candidate.authority,
        -retrieved,
        candidate.document_id,
        candidate.chunk_id if candidate.chunk_id is not None else -1,
    )


def _snapshot_key(candidate: WebEvidenceCandidate) -> tuple:
    retrieved = candidate.retrieved_at.timestamp() if candidate.retrieved_at else 0.0
    return (retrieved, candidate.score, -candidate.document_id)


def _signal_names(signals: Any) -> set[str]:
    if not signals:
        return set()
    if isinstance(signals, Mapping):
        return {str(k) for k in signals.keys()}
    try:
        return {str(s) for s in signals}
    except TypeError:
        return set()


def _hybrid_hit_counts(names: set[str], raw: Any) -> bool:
    """Whether an engine result is a match rather than a nearest neighbour."""
    if not names:
        return True  # an engine that reports no provenance is taken at its word
    if names & {"lexical", "metadata", "temporal"}:
        return True
    if "semantic" in names:
        similarity = 0.0
        if isinstance(raw, Mapping):
            try:
                similarity = float(raw.get("semantic", 0.0) or 0.0)
            except (TypeError, ValueError):
                similarity = 0.0
        return similarity >= SEMANTIC_ONLY_FLOOR
    return True


def _iter_query_texts(queries: Any) -> Iterable[str]:
    if queries is None:
        return []
    if isinstance(queries, str):
        return [queries]
    texts = getattr(queries, "texts", None)
    if texts is not None and not callable(texts):
        return list(texts)
    out: list[str] = []
    try:
        iterator = iter(queries)
    except TypeError:
        return out
    for item in iterator:
        if isinstance(item, str):
            out.append(item)
        else:
            text_value = getattr(item, "text", None)
            if isinstance(text_value, str):
                out.append(text_value)
    return out


def _dialect_name(db: Any) -> str:
    try:
        bind = db.get_bind()
    except Exception:  # noqa: BLE001 — duck-typed sessions have no bind
        return ""
    try:
        return str(bind.dialect.name or "")
    except Exception:  # noqa: BLE001
        return ""


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _section_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, SectionKind):
        return value.value
    return _str_or_none(getattr(value, "value", value))


def _section_kind(value: str | None) -> SectionKind:
    try:
        return SectionKind(value) if value else SectionKind.UNKNOWN
    except ValueError:
        return SectionKind.UNKNOWN


def _source_class(value: str | None) -> WebSourceClass:
    try:
        return WebSourceClass(value) if value else WebSourceClass.UNKNOWN
    except ValueError:
        return WebSourceClass.UNKNOWN


def _web_metadata(document: Document) -> Mapping[str, Any]:
    meta = document.doc_metadata or {}
    web = meta.get("web") if isinstance(meta, Mapping) else None
    return web if isinstance(web, Mapping) else {}


def _canonical(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return canonicalize_url(url) or url
    except Exception:  # noqa: BLE001 — an odd URL is still an identity
        return url


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _freshness_basis(
    published_at: datetime | None, retrieved_at: datetime | None,
) -> tuple[date | None, str]:
    if published_at is not None:
        return published_at.date(), "published_at"
    if retrieved_at is not None:
        return retrieved_at.date(), "retrieved_at"
    return None, "unknown"


def _snippet(text_value: str, limit: int) -> str:
    cleaned = _WHITESPACE.sub(" ", text_value or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
