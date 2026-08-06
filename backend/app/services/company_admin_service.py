"""Admin company management — CRUD, bulk edit, import/export, merge, versions.

This is the heart of the Enterprise Admin Panel's company module. It layers on
top of the existing :class:`CompanyService` (which owns the read/research
paths) and adds the administrative operations: create, update, soft delete,
restore, permanent delete, a spreadsheet-style bulk editor, CSV/Excel import
and export, duplicate merging, and immutable version history with rollback.

Every mutation records a :class:`CompanyVersion` (an immutable snapshot of the
editable fields) and an audit event, so "every edit is logged" and rollback is
possible without a per-field audit table.

Soft deletion sets ``deleted_at`` on the company and records a recycle-bin
entry (``resource_type="company"``) so it appears in the admin recycle bin;
restore clears it and marks the bin entry restored; permanent delete removes
the row entirely.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyVersion
from app.schemas.company import (
    CompanyCreate, CompanyDetail, CompanyUpdate, CompanyVersionOut,
)
from app.services.platform.recycle_bin import RecycleBinService


class CompanyAdminError(Exception):
    """Raised when an admin company operation cannot be honoured."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_ticker(t: str) -> str:
    return (t or "").strip().upper()


def _normalise_isin(i: str) -> str | None:
    i = (i or "").strip().upper()
    return i or None


#: Editable fields on the Company model, used for snapshots and bulk editing.
_SNAPSHOT_FIELDS = [
    "name", "ticker", "exchange", "isin", "bse_code", "sector", "industry",
    "market_cap", "current_price", "shares_outstanding", "face_value",
    "listing_date", "website", "description", "ceo", "employees",
    "headquarters", "listing_status", "index_membership", "logo_url",
    "favicon_url",
]


def _snapshot(company: Company) -> dict[str, Any]:
    """Serialize a company's editable fields for version snapshots."""
    out: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        value = getattr(company, field, None)
        if isinstance(value, datetime):
            value = value.isoformat()
        out[field] = value
    return out


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        if before.get(field) != after.get(field):
            changes[field] = {"from": before.get(field), "to": after.get(field)}
    return changes


class CompanyAdminService:
    """Administrative operations on the company universe."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.recycle = RecycleBinService(db)

    # ==================================================================
    # Validation
    # ==================================================================
    def _check_unique(
        self, *, ticker: str | None = None, exchange: str | None = None,
        isin: str | None = None, bse_code: str | None = None,
        exclude_id: str | None = None,
    ) -> None:
        """Raise if a ticker/exchange or ISIN would duplicate an existing row."""
        if ticker:
            t = _normalise_ticker(ticker)
            stmt = select(Company).where(
                func.upper(Company.ticker) == t,
                Company.deleted_at.is_(None),
            )
            if exclude_id:
                stmt = stmt.where(Company.id != exclude_id)
            if self.db.execute(stmt).first() is not None:
                raise CompanyAdminError(
                    f"A company already exists with NSE symbol '{t}'."
                )
        if isin:
            n = _normalise_isin(isin)
            stmt = select(Company).where(
                Company.isin == n, Company.deleted_at.is_(None),
            )
            if exclude_id:
                stmt = stmt.where(Company.id != exclude_id)
            if self.db.execute(stmt).first() is not None:
                raise CompanyAdminError(
                    f"A company already exists with ISIN '{n}'."
                )
        if bse_code:
            stmt = select(Company).where(
                Company.bse_code == bse_code, Company.deleted_at.is_(None),
            )
            if exclude_id:
                stmt = stmt.where(Company.id != exclude_id)
            if self.db.execute(stmt).first() is not None:
                raise CompanyAdminError(
                    f"A company already exists with BSE code '{bse_code}'."
                )

    def _record_version(
        self, company: Company, *, actor_id: str | None, actor_email: str | None,
        changes: dict[str, Any], change_type: str, summary: str,
        snapshot: dict[str, Any] | None = None,
    ) -> CompanyVersion:
        next_version = (
            self.db.execute(
                select(func.coalesce(func.max(CompanyVersion.version), 0))
                .where(CompanyVersion.company_id == company.id)
            ).scalar_one()
            + 1
        )
        row = CompanyVersion(
            company_id=company.id, version=next_version,
            actor_id=actor_id, actor_email=actor_email,
            changes=changes or None,
            snapshot=snapshot if snapshot is not None else _snapshot(company),
            change_type=change_type, summary=summary, created_at=_utcnow(),
        )
        self.db.add(row)
        return row

    # ==================================================================
    # CRUD
    # ==================================================================
    def create(self, payload: CompanyCreate, *, actor_id=None, actor_email=None) -> Company:
        ticker = _normalise_ticker(payload.ticker)
        if not payload.name or not ticker:
            raise CompanyAdminError("name and ticker are required")
        self._check_unique(ticker=ticker, isin=payload.isin, bse_code=payload.bse_code)

        listing_date = None
        if payload.listing_date:
            try:
                listing_date = datetime.fromisoformat(
                    payload.listing_date.replace("Z", "+00:00")
                )
            except ValueError:
                raise CompanyAdminError("listing_date must be an ISO date")  # noqa: B904

        company = Company(
            id=str(uuid.uuid4()),
            name=payload.name.strip(),
            ticker=ticker,
            exchange=(payload.exchange or "NSE").upper(),
            isin=_normalise_isin(payload.isin),
            bse_code=payload.bse_code,
            sector=payload.sector,
            industry=payload.industry,
            market_cap=payload.market_cap,
            current_price=payload.current_price,
            shares_outstanding=payload.shares_outstanding,
            face_value=payload.face_value,
            listing_date=listing_date,
            website=payload.website,
            description=payload.description,
            ceo=payload.ceo,
            employees=payload.employees,
            headquarters=payload.headquarters,
            listing_status=payload.listing_status or "active",
            index_membership=payload.index_membership,
        )
        self.db.add(company)
        self.db.flush()
        self._record_version(
            company, actor_id=actor_id, actor_email=actor_email,
            changes={}, change_type="create",
            summary=f"Created company {company.ticker}",
        )
        return company

    def update(
        self, company_id: str, payload: CompanyUpdate, *, actor_id=None, actor_email=None,
    ) -> Company:
        company = self.db.get(Company, company_id)
        if company is None or company.deleted_at is not None:
            raise CompanyAdminError("company not found")

        # Capture pre-edit state for versioning and unique checks.
        before = _snapshot(company)

        new_ticker = _normalise_ticker(payload.ticker) if payload.ticker else company.ticker
        new_isin = _normalise_isin(payload.isin) if payload.isin is not None else company.isin
        new_bse = payload.bse_code if payload.bse_code is not None else company.bse_code
        new_exchange = (payload.exchange or company.exchange).upper()

        self._check_unique(
            ticker=new_ticker, exchange=new_exchange, isin=new_isin,
            bse_code=new_bse, exclude_id=company.id,
        )

        if payload.name is not None:
            company.name = payload.name.strip()
        if payload.ticker is not None:
            company.ticker = new_ticker
        if payload.exchange is not None:
            company.exchange = new_exchange
        if payload.isin is not None:
            company.isin = new_isin
        if payload.bse_code is not None:
            company.bse_code = payload.bse_code
        if payload.sector is not None:
            company.sector = payload.sector
        if payload.industry is not None:
            company.industry = payload.industry
        if payload.market_cap is not None:
            company.market_cap = payload.market_cap
        if payload.current_price is not None:
            company.current_price = payload.current_price
        if payload.shares_outstanding is not None:
            company.shares_outstanding = payload.shares_outstanding
        if payload.face_value is not None:
            company.face_value = payload.face_value
        if payload.listing_date is not None:
            try:
                company.listing_date = datetime.fromisoformat(
                    payload.listing_date.replace("Z", "+00:00")
                )
            except ValueError:
                raise CompanyAdminError("listing_date must be an ISO date")  # noqa: B904
        if payload.website is not None:
            company.website = payload.website
        if payload.description is not None:
            company.description = payload.description
        if payload.ceo is not None:
            company.ceo = payload.ceo
        if payload.employees is not None:
            company.employees = payload.employees
        if payload.headquarters is not None:
            company.headquarters = payload.headquarters
        if payload.listing_status is not None:
            company.listing_status = payload.listing_status
        if payload.index_membership is not None:
            company.index_membership = payload.index_membership
        if payload.logo_url is not None:
            company.logo_url = payload.logo_url
        if payload.favicon_url is not None:
            company.favicon_url = payload.favicon_url

        company.data_version += 1
        company.updated_at = _utcnow()
        after = _snapshot(company)
        changes = _diff(before, after)
        if changes:
            self._record_version(
                company, actor_id=actor_id, actor_email=actor_email,
                changes=changes, change_type="update",
                summary=f"Updated {len(changes)} field(s) for {company.ticker}",
                snapshot=after,
            )
        return company

    def get(self, company_id: str, *, include_deleted: bool = False) -> Company | None:
        company = self.db.get(Company, company_id)
        if company is None:
            return None
        if company.deleted_at is not None and not include_deleted:
            return None
        return company

    # ==================================================================
    # Soft delete / restore / permanent delete (recycle bin integration)
    # ==================================================================
    def soft_delete(self, company_id: str, *, actor_id=None, actor_email=None) -> Company:
        company = self.get(company_id)
        if company is None:
            raise CompanyAdminError("company not found")
        if company.deleted_at is not None:
            return company
        company.deleted_at = _utcnow()
        company.data_version += 1
        self.db.flush()
        self.recycle.soft_delete(
            resource_type="company", resource_id=company.id,
            display_name=f"{company.ticker} — {company.name}",
            payload=_snapshot(company),
            principal=None,
        )
        return company

    def restore(self, company_id: str, *, actor_id=None, actor_email=None) -> Company:
        company = self.db.get(Company, company_id)
        if company is None:
            raise CompanyAdminError("company not found")
        company.deleted_at = None
        company.data_version += 1
        self.db.flush()
        # Mark the matching recycle-bin entry restored.
        from app.models.recycle_bin import RecycleBin
        entry = self.db.execute(
            select(RecycleBin)
            .where(RecycleBin.resource_type == "company")
            .where(RecycleBin.resource_id == company.id)
            .where(RecycleBin.purged_at.is_(None))
            .order_by(RecycleBin.deleted_at.desc())
        ).scalars().first()
        if entry is not None and entry.is_active:
            self.recycle.restore(entry.id, principal=None)
        self._record_version(
            company, actor_id=actor_id, actor_email=actor_email,
            changes={}, change_type="restore",
            summary=f"Restored company {company.ticker}",
        )
        return company

    def permanent_delete(self, company_id: str, *, actor_id=None, actor_email=None) -> None:
        company = self.db.get(Company, company_id)
        if company is None:
            raise CompanyAdminError("company not found")
        # Hard delete the row (financial facts cascade).
        self.db.delete(company)
        self.db.flush()

    # ==================================================================
    # List / search / filters
    # ==================================================================
    def list_companies(
        self, *, page: int = 1, page_size: int = 25,
        search: str | None = None, sector: str | None = None,
        industry: str | None = None, exchange: str | None = None,
        listing_status: str | None = None, market_cap_min: float | None = None,
        market_cap_max: float | None = None, sort_by: str = "market_cap",
        order: str = "desc", include_deleted: bool = False,
    ) -> tuple[int, list[Company]]:
        """Paginated, filtered, sorted list. Index-backed for 10k+ rows."""
        stmt = select(Company)
        if not include_deleted:
            stmt = stmt.where(Company.deleted_at.is_(None))

        if search:
            pattern = f"%{search.strip().lower()}%"
            stmt = stmt.where(or_(
                func.lower(Company.name).like(pattern),
                func.lower(Company.ticker).like(pattern),
                func.lower(Company.isin).like(pattern),
                func.lower(Company.sector).like(pattern),
                func.lower(Company.industry).like(pattern),
            ))
        if sector:
            stmt = stmt.where(Company.sector == sector)
        if industry:
            stmt = stmt.where(Company.industry == industry)
        if exchange:
            stmt = stmt.where(func.upper(Company.exchange) == exchange.upper())
        if listing_status:
            stmt = stmt.where(Company.listing_status == listing_status)
        if market_cap_min is not None:
            stmt = stmt.where(Company.market_cap >= market_cap_min)
        if market_cap_max is not None:
            stmt = stmt.where(Company.market_cap <= market_cap_max)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar_one()

        column = {
            "name": Company.name, "ticker": Company.ticker,
            "sector": Company.sector, "industry": Company.industry,
            "market_cap": Company.market_cap, "listing_date": Company.listing_date,
            "created_at": Company.created_at,
        }.get(sort_by, Company.market_cap)
        order_by = column.asc() if order == "asc" else column.desc().nullslast()
        rows = (
            self.db.execute(
                stmt.order_by(order_by).offset((page - 1) * page_size).limit(page_size)
            ).scalars().all()
        )
        return total, list(rows)

    def sectors(self) -> list[str]:
        return [s for (s,) in self.db.execute(
            select(Company.sector).where(Company.sector.is_not(None))
            .distinct().order_by(Company.sector)).all() if s]

    def industries(self) -> list[str]:
        return [i for (i,) in self.db.execute(
            select(Company.industry).where(Company.industry.is_not(None))
            .distinct().order_by(Company.industry)).all() if i]

    # ==================================================================
    # Merge duplicates
    # ==================================================================
    def merge_duplicates(
        self, *, keep_id: str, delete_ids: list[str], actor_id=None, actor_email=None,
    ) -> tuple[Company, list[str]]:
        """Merge duplicate companies into one, reassigning financial facts."""
        keeper = self.get(keep_id)
        if keeper is None:
            raise CompanyAdminError(f"keeper company '{keep_id}' not found")

        merged: list[str] = []
        for dup_id in delete_ids:
            if dup_id == keep_id:
                continue
            dup = self.get(dup_id)
            if dup is None:
                raise CompanyAdminError(f"company '{dup_id}' not found")
            # Reassign financial facts to the keeper (facts cascade on delete,
            # so move them first).
            from app.models.company import FinancialFact
            self.db.execute(
                FinancialFact.__table__.update()
                .where(FinancialFact.company_id == dup.id)
                .values(company_id=keeper.id)
            )
            # Move the duplicate's version history under the keeper, re-sequencing
            # to avoid the (company_id, version) unique constraint.
            dup_versions = self.db.execute(
                select(CompanyVersion).where(CompanyVersion.company_id == dup.id)
            ).scalars().all()
            max_keeper = self.db.execute(
                select(func.coalesce(func.max(CompanyVersion.version), 0))
                .where(CompanyVersion.company_id == keeper.id)
            ).scalar_one()
            for i, v in enumerate(dup_versions):
                v.company_id = keeper.id
                v.version = max_keeper + 1 + i
            self.db.delete(dup)
            merged.append(dup_id)

        if merged:
            keeper.data_version += 1
            self._record_version(
                keeper, actor_id=actor_id, actor_email=actor_email,
                changes={}, change_type="merge",
                summary=f"Merged {len(merged)} duplicate(s) into {keeper.ticker}",
            )
        return keeper, merged

    # ==================================================================
    # Bulk editor
    # ==================================================================
    def bulk_edit(
        self, items: list[dict[str, Any]], *, actor_id=None, actor_email=None,
    ) -> tuple[int, int, list[dict[str, str]]]:
        """Apply spreadsheet-style edits in place. Matches by ticker or id."""
        updated = 0
        created = 0
        errors: list[dict[str, str]] = []
        for row in items:
            try:
                if not isinstance(row, dict):
                    raise CompanyAdminError("invalid row")
                tid = str(row.get("id") or "").strip() or None
                ticker = _normalise_ticker(str(row.get("ticker") or ""))
                company = None
                if tid:
                    company = self.get(tid)
                if company is None and ticker:
                    company = self.db.execute(
                        select(Company).where(
                            func.upper(Company.ticker) == ticker,
                            Company.deleted_at.is_(None),
                        )
                    ).scalars().first()
                if company is not None:
                    payload = CompanyUpdate(**{
                        k: v for k, v in row.items()
                        if k in CompanyUpdate.model_fields and v is not None
                    })
                    self.update(company.id, payload, actor_id=actor_id, actor_email=actor_email)
                    updated += 1
                elif row.get("name") and ticker:
                    company = self.create(
                        CompanyCreate(**{
                            k: v for k, v in row.items()
                            if k in CompanyCreate.model_fields and v is not None
                        }),
                        actor_id=actor_id, actor_email=actor_email,
                    )
                    created += 1
                else:
                    errors.append({"row": str(row), "error": "no match and no name/ticker"})
            except (CompanyAdminError, ValueError) as exc:
                errors.append({"row": str(row), "error": str(exc)})
        return updated, created, errors

    # ==================================================================
    # Import / export
    # ==================================================================
    def _parse_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Coerce a raw import row into a CompanyUpdate-shaped dict."""
        cleaned: dict[str, Any] = {}
        mapping = {
            "name": "name", "company name": "name", "company_name": "name",
            "ticker": "ticker", "nse symbol": "ticker", "symbol": "ticker",
            "nse": "ticker",
            "isin": "isin",
            "bse symbol": "bse_code", "bse": "bse_code", "bse_code": "bse_code",
            "exchange": "exchange",
            "sector": "sector", "industry": "industry",
            "market cap": "market_cap", "market_cap": "market_cap", "mcap": "market_cap",
            "face value": "face_value", "face_value": "face_value",
            "website": "website", "description": "description",
            "ceo": "ceo", "employees": "employees", "headquarters": "headquarters",
            "listing status": "listing_status", "listing_status": "listing_status",
            "listing date": "listing_date", "listing_date": "listing_date",
            "current price": "current_price", "current_price": "current_price",
            "shares outstanding": "shares_outstanding", "shares_outstanding": "shares_outstanding",
            "index membership": "index_membership", "index_membership": "index_membership",
        }
        for raw_key, value in row.items():
            key = mapping.get(str(raw_key).strip().lower())
            if key is None:
                continue
            cleaned[key] = value
        # Numeric coercion
        for num_field in ("market_cap", "face_value", "current_price", "shares_outstanding", "employees"):
            if num_field in cleaned and cleaned[num_field] not in (None, ""):
                try:
                    cleaned[num_field] = float(cleaned[num_field]) \
                        if num_field != "employees" else int(float(cleaned[num_field]))
                except (TypeError, ValueError):
                    cleaned[num_field] = None
        return cleaned

    def import_rows(
        self, rows: list[dict[str, Any]], *, actor_id=None, actor_email=None,
    ) -> dict[str, Any]:
        imported = updated = skipped = 0
        errors: list[dict[str, str]] = []
        for idx, raw in enumerate(rows, start=2):
            try:
                cleaned = self._parse_row(raw)
                ticker = _normalise_ticker(str(cleaned.get("ticker") or ""))
                if not cleaned.get("name") or not ticker:
                    skipped += 1
                    continue
                existing = self.db.execute(
                    select(Company).where(
                        func.upper(Company.ticker) == ticker,
                        Company.deleted_at.is_(None),
                    )
                ).scalars().first()
                if existing is not None:
                    self.update(
                        existing.id,
                        CompanyUpdate(**{
                            k: v for k, v in cleaned.items()
                            if k in CompanyUpdate.model_fields
                        }),
                        actor_id=actor_id, actor_email=actor_email,
                    )
                    updated += 1
                else:
                    self.create(
                        CompanyCreate(**{
                            k: v for k, v in cleaned.items()
                            if k in CompanyCreate.model_fields
                        }),
                        actor_id=actor_id, actor_email=actor_email,
                    )
                    imported += 1
            except (CompanyAdminError, ValueError) as exc:
                errors.append({"row": f"row {idx}", "error": str(exc)})
        return {"imported": imported, "updated": updated, "skipped": skipped, "errors": errors}

    def export_rows(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        stmt = select(Company).order_by(Company.ticker)
        if not include_deleted:
            stmt = stmt.where(Company.deleted_at.is_(None))
        companies = self.db.execute(stmt).scalars().all()
        return [_snapshot(c) for c in companies]

    # ==================================================================
    # Version history / rollback
    # ==================================================================
    def versions(self, company_id: str) -> list[CompanyVersionOut]:
        rows = self.db.execute(
            select(CompanyVersion)
            .where(CompanyVersion.company_id == company_id)
            .order_by(CompanyVersion.version.desc())
        ).scalars().all()
        return [CompanyVersionOut.model_validate(r) for r in rows]

    def rollback(
        self, company_id: str, version: int, *, actor_id=None, actor_email=None,
    ) -> Company:
        company = self.get(company_id)
        if company is None:
            raise CompanyAdminError("company not found")
        target = self.db.execute(
            select(CompanyVersion)
            .where(CompanyVersion.company_id == company_id)
            .where(CompanyVersion.version == version)
        ).scalars().first()
        if target is None or not target.snapshot:
            raise CompanyAdminError(f"version {version} has no snapshot to roll back to")

        before = _snapshot(company)
        snap = target.snapshot
        # Apply the snapshot fields back onto the company.
        for field in _SNAPSHOT_FIELDS:
            if field not in snap:
                continue
            value = snap[field]
            if field == "listing_date" and isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    value = None
            setattr(company, field, value)
        company.data_version += 1
        company.updated_at = _utcnow()
        self.db.flush()
        after = _snapshot(company)
        self._record_version(
            company, actor_id=actor_id, actor_email=actor_email,
            changes=_diff(before, after), change_type="rollback",
            summary=f"Rolled back {company.ticker} to version {version}",
            snapshot=after,
        )
        return company
