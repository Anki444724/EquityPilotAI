"""Scheduler dashboard — what the collection pipeline actually did.

Every figure is computed from the database at request time rather than from a
counter maintained by the crawler. A counter drifts the moment a crash
interrupts a pass, and the crawler has crashed; the tables are the only
account that survives.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.models.company import Company
from app.models.document import Document
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.models.knowledge import KnowledgeEntry
from app.models.platform import BackgroundJob


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerDashboard:
    """Read-only view of crawl coverage, retries, failures and memory."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def snapshot(self, *, window_hours: int = 24) -> dict[str, Any]:
        since = _utcnow() - timedelta(hours=window_hours)

        universe = self.db.scalar(
            select(func.count()).select_from(Company).where(
                Company.listing_status == "active",
                Company.exchange.in_(("NSE", "BSE", "NSE/BSE")),
            )
        ) or 0

        crawled = self.db.scalar(
            select(func.count()).select_from(CompanyCrawlState).where(
                CompanyCrawlState.last_crawled_at >= since,
            )
        ) or 0

        never = self.db.scalar(
            select(func.count()).select_from(CompanyCrawlState).where(
                CompanyCrawlState.last_crawled_at.is_(None),
            )
        ) or 0

        # "Remaining" is the honest complement of "crawled today": companies
        # in the universe that this window has not reached. Reported against
        # the universe rather than against the crawl-state table, because a
        # company with no state row has not been crawled either.
        remaining = max(universe - crawled, 0)

        failed = self.db.scalar(
            select(func.count()).select_from(CompanyCrawlState).where(
                CompanyCrawlState.last_status == "failed",
            )
        ) or 0

        # A filing that failed download but has attempts left is a pending
        # retry; one that has exhausted them is a failure. Distinguished
        # because the first needs patience and the second needs attention.
        pending_retries = self.db.scalar(
            select(func.count()).select_from(DiscoveredFiling).where(
                DiscoveredFiling.status == "failed",
                DiscoveredFiling.attempts < 3,
            )
        ) or 0
        exhausted = self.db.scalar(
            select(func.count()).select_from(DiscoveredFiling).where(
                DiscoveredFiling.status == "failed",
                DiscoveredFiling.attempts >= 3,
            )
        ) or 0

        ir_total = self.db.scalar(
            select(func.count()).select_from(CompanyCrawlState).where(
                CompanyCrawlState.ir_url.is_not(None),
            )
        ) or 0
        ir_today = self.db.scalar(
            select(func.count()).select_from(CompanyCrawlState).where(
                CompanyCrawlState.ir_url.is_not(None),
                CompanyCrawlState.ir_url_checked_at >= since,
            )
        ) or 0

        downloaded = self.db.scalar(
            select(func.count()).select_from(DiscoveredFiling).where(
                DiscoveredFiling.downloaded_at >= since,
            )
        ) or 0

        documents_today = self.db.scalar(
            select(func.count()).select_from(Document).where(
                Document.created_at >= since,
            )
        ) or 0

        memory_updates = self.db.scalar(
            select(func.count()).select_from(BackgroundJob).where(
                BackgroundJob.kind == "memory_enrichment",
                BackgroundJob.status == "succeeded",
                BackgroundJob.finished_at >= since,
            )
        ) or 0
        vault_writes = self.db.scalar(
            select(func.count()).select_from(KnowledgeEntry).where(
                KnowledgeEntry.created_at >= since,
            )
        ) or 0

        return {
            "window_hours": window_hours,
            "generated_at": _utcnow(),
            "coverage": {
                "universe": universe,
                "crawled_today": crawled,
                "remaining": remaining,
                "never_crawled": never,
                "coverage_pct": (
                    round(100.0 * crawled / universe, 2) if universe else 0.0
                ),
            },
            "retries": {
                "pending": pending_retries,
                "exhausted": exhausted,
            },
            "failures": {
                "companies_failed": failed,
                "top_errors": self._top_errors(),
            },
            "ir_urls": {
                "discovered_total": ir_total,
                "discovered_today": ir_today,
                "missing": max(universe - ir_total, 0),
                "by_method": self._ir_by_method(),
            },
            "documents": {
                "downloaded_today": downloaded,
                "ingested_today": documents_today,
                "pending_download": self.db.scalar(
                    select(func.count()).select_from(DiscoveredFiling).where(
                        DiscoveredFiling.status == "discovered",
                    )
                ) or 0,
            },
            "memory": {
                "enrichment_runs_today": memory_updates,
                "vault_entries_today": vault_writes,
            },
            "classification": self._classification(),
            "schedules": self._schedules(),
        }

    # ----------------------------------------------------------- breakdowns
    def _top_errors(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(
                func.substr(CompanyCrawlState.last_error, 1, 90).label("err"),
                func.count().label("n"),
            )
            .where(CompanyCrawlState.last_error.is_not(None))
            .group_by("err")
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
        return [{"error": r.err, "companies": r.n} for r in rows]

    def _ir_by_method(self) -> dict[str, int]:
        rows = self.db.execute(
            select(CompanyCrawlState.ir_url_method, func.count())
            .where(CompanyCrawlState.ir_url_method.is_not(None))
            .group_by(CompanyCrawlState.ir_url_method)
        ).all()
        return {method: count for method, count in rows}

    def _classification(self) -> dict[str, Any]:
        """How well the classifier is doing, which is a coverage figure too.

        A filing stored as `other` is retrievable but not routable, so the
        share of unclassified rows belongs on the same dashboard as the crawl.
        """
        rows = self.db.execute(
            select(DiscoveredFiling.doc_type, func.count())
            .group_by(DiscoveredFiling.doc_type)
            .order_by(func.count().desc())
        ).all()
        by_type = {(doc_type or "unclassified"): count for doc_type, count in rows}
        total = sum(by_type.values())
        vague = by_type.get("other", 0) + by_type.get("unclassified", 0)
        return {
            "total": total,
            "by_doc_type": by_type,
            "classified_pct": (
                round(100.0 * (total - vague) / total, 2) if total else 0.0
            ),
        }

    def _schedules(self) -> list[dict[str, Any]]:
        from app.models.platform import ScheduleState

        rows = self.db.execute(
            select(ScheduleState).order_by(ScheduleState.kind)
        ).scalars().all()
        return [
            {
                "kind": r.kind,
                "enabled": r.enabled,
                "every_seconds": r.every_seconds,
                "last_run_at": r.last_run_at,
                "next_run_at": r.next_run_at,
                "run_count": r.run_count,
                "last_status": r.last_status,
            }
            for r in rows
        ]
