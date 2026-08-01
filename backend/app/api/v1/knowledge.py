"""Knowledge Vault API: read the vault, its history, and its summaries."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.knowledge.vault import SummaryKind, VaultSection
from app.services.analysis_service import AnalysisService

router = APIRouter(tags=["knowledge"])


def _company(db: Session, ticker: str):
    analysis = AnalysisService.for_ticker(db, ticker, provision=False)
    if analysis is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown ticker {ticker}")
    return analysis.company


def _require_operator(user: CurrentUser) -> None:
    role = str(getattr(user, "role", "") or "").lower()
    if role not in ("admin", "super_admin", "tenant_admin", "operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")


@router.get("/company/{ticker}/knowledge", summary="The company's Knowledge Vault")
def read_vault(
    ticker: str,
    per_section: int = Query(default=20, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Current knowledge, section by section, each entry citable."""
    from app.services.knowledge.vault import KnowledgeVault

    company = _company(db, ticker)
    vault = KnowledgeVault(db)
    return {
        "ticker": company.ticker,
        "company": company.name,
        "sections": vault.read_vault(company.id, per_section=per_section),
        "stats": vault.stats(company.id).as_dict(),
    }


@router.get("/company/{ticker}/knowledge/{section}",
            summary="One vault section")
def read_section(
    ticker: str,
    section: str,
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    from app.services.knowledge.vault import KnowledgeVault

    try:
        wanted = VaultSection(section)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown section; expected one of {[s.value for s in VaultSection]}",
        ) from exc

    company = _company(db, ticker)
    vault = KnowledgeVault(db)
    entries = vault.read_section(company.id, wanted, limit=limit)
    return {
        "ticker": company.ticker, "section": wanted.value,
        "count": len(entries),
        "entries": [vault.render(e) for e in entries],
    }


@router.get("/company/{ticker}/knowledge/{section}/{key}/history",
            summary="Every version of one assertion")
def entry_history(
    ticker: str,
    section: str,
    key: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """What the platform believed, when, and on what evidence.

    The question the vault exists to answer. Nothing is ever overwritten, so
    every superseded version is still here with its original citation.
    """
    from app.services.knowledge.vault import KnowledgeVault

    try:
        wanted = VaultSection(section)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "unknown section") from exc

    company = _company(db, ticker)
    versions = KnowledgeVault(db).history(company.id, wanted, key)
    return {
        "ticker": company.ticker, "section": wanted.value, "key": key,
        "versions": len(versions), "history": versions,
    }


@router.get("/company/{ticker}/summaries", summary="Permanent AI summaries")
def read_summaries(
    ticker: str,
    kind: str | None = Query(default=None),
    limit: int = Query(default=40, le=200),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The memory an answer reads before touching a PDF."""
    from app.services.knowledge.summaries import SummaryService

    company = _company(db, ticker)
    kinds = None
    if kind:
        try:
            kinds = [SummaryKind(kind)]
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown kind; expected one of {[k.value for k in SummaryKind]}",
            ) from exc

    rows = SummaryService(db).for_company(company.id, kinds=kinds, limit=limit)
    return {
        "ticker": company.ticker,
        "count": len(rows),
        "summaries": [
            {
                "document_id": r.document_id, "kind": r.kind,
                "fiscal_year": r.fiscal_year, "quarter": r.quarter,
                "doc_type": r.doc_type, "words": r.word_count,
                "model": r.model, "is_fallback": r.is_fallback,
                "content": r.content,
            }
            for r in rows
        ],
    }


@router.get("/company/{ticker}/summaries/{kind}/timeline",
            summary="One summary kind across every period")
def summary_timeline(
    ticker: str,
    kind: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Temporal memory: how a dimension has changed, year by year.

    Answers "how has management guidance changed?" by reading short stored
    summaries rather than re-parsing a decade of annual reports.
    """
    from app.services.knowledge.summaries import SummaryService

    try:
        wanted = SummaryKind(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "unknown summary kind") from exc

    company = _company(db, ticker)
    entries = SummaryService(db).timeline(company.id, wanted)
    return {
        "ticker": company.ticker, "kind": wanted.value,
        "periods": len(entries), "timeline": entries,
    }


# ------------------------------------------------------------------ writes
@router.post("/company/{ticker}/knowledge/build",
             summary="Promote extracted facts into the vault")
def build_vault(
    ticker: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)
    from app.services.knowledge.ingest import KnowledgeIngestor

    company = _company(db, ticker)
    return KnowledgeIngestor(db).ingest_company(company.id).as_dict()


@router.post("/knowledge/build-all", summary="Promote facts for every company")
def build_all(
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)
    from app.services.knowledge.ingest import KnowledgeIngestor

    return KnowledgeIngestor(db).ingest_all(limit=limit)


@router.post("/knowledge/summarise", summary="Generate permanent summaries")
def summarise(
    limit: int = Query(default=2, le=10,
                       description="Documents per call; each costs 9 LLM calls"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate the nine summaries for documents that have none.

    Bounded hard: nine model calls per document, and Railway closes a request
    at five minutes. Two documents is roughly the most that fits.
    """
    _require_operator(user)
    from app.services.knowledge.summaries import SummaryService

    return SummaryService(db).run_batch(limit=limit).as_dict()


@router.get("/knowledge/stats", summary="Vault coverage across the platform")
def vault_stats(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.models.knowledge import DocumentSummary
    from app.services.knowledge.vault import KnowledgeVault

    payload = KnowledgeVault(db).stats().as_dict()
    payload["companies_with_knowledge"] = db.scalar(
        select(func.count(func.distinct(
            __import__("app.models.knowledge", fromlist=["x"])
            .KnowledgeEntry.company_id
        )))
    ) or 0
    payload["summaries"] = db.scalar(
        select(func.count()).select_from(DocumentSummary)
    ) or 0
    payload["companies_with_summaries"] = db.scalar(
        select(func.count(func.distinct(DocumentSummary.company_id)))
    ) or 0
    return payload
