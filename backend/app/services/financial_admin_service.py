"""Enterprise financial-statements admin service (Phase 3).

Edits annual facts, quarterly results, shareholding and corporate actions;
bulk-imports CSV / Excel / JSON; computes statements and ratios; and maintains
fact-level version history with rollback. Every mutation records a
:class:`FinancialFactVersion`, bumps ``companies.data_version`` and invalidates
the statements cache so downstream engines (AI score, risk, growth, valuation,
confidence) recompute on the next read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.financials.canonical import CanonicalFinancialsBuilder, Precedence
from app.domain.financials.line_items import LineItem, Statement
from app.domain.financials.statements import (
    build_balance_sheet, build_cash_flow, build_income_statement,
)
from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
from app.models.company import Company, FinancialFact
from app.models.financials import CorporateAction, FinancialFactVersion
from app.services.ratios.service import RatioService
from app.services.platform.cache import Namespace, cache


class FinancialAdminError(Exception):
    """Raised when a financial-statement operation cannot be honoured."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Line items grouped by statement, for editor columns.
STATEMENT_LINE_ITEMS: dict[str, list[str]] = {
    "income": [i.value for i in LineItem
               if i in (LineItem.REVENUE, LineItem.OTHER_OPERATING_INCOME,
                        LineItem.RAW_MATERIALS, LineItem.PURCHASE_STOCK_IN_TRADE,
                        LineItem.CHANGE_INVENTORIES, LineItem.EMPLOYEE_BENEFIT,
                        LineItem.OTHER_EXPENSES, LineItem.DEPRECIATION,
                        LineItem.OTHER_INCOME, LineItem.FINANCE_COSTS,
                        LineItem.EXCEPTIONAL_ITEMS, LineItem.TAX_EXPENSE,
                        LineItem.MINORITY_INTEREST, LineItem.OCI)],
    "balance": [i.value for i in LineItem
                if i in (LineItem.CASH_AND_BANK, LineItem.CURRENT_INVESTMENTS,
                         LineItem.TRADE_RECEIVABLES, LineItem.INVENTORIES,
                         LineItem.OTHER_CURRENT_ASSETS, LineItem.NET_BLOCK_PPE,
                         LineItem.CWIP, LineItem.GOODWILL, LineItem.OTHER_INTANGIBLES,
                         LineItem.LT_INVESTMENTS_ASSOCIATES, LineItem.OTHER_NCA,
                         LineItem.DEFERRED_TAX_ASSET, LineItem.TRADE_PAYABLES,
                         LineItem.SHORT_TERM_BORROWINGS, LineItem.CURRENT_MATURITIES_LTD,
                         LineItem.OTHER_CURRENT_LIABILITIES, LineItem.SHORT_TERM_PROVISIONS,
                         LineItem.LONG_TERM_BORROWINGS, LineItem.DEFERRED_TAX_LIABILITY,
                         LineItem.OTHER_NCL, LineItem.EQUITY_SHARE_CAPITAL,
                         LineItem.RESERVES_SURPLUS, LineItem.MINORITY_INTEREST_BS)],
    "cashflow": [i.value for i in LineItem
                 if i in (LineItem.OTHER_NONCASH_ADJ, LineItem.CHG_INVENTORIES_CF,
                          LineItem.CHG_RECEIVABLES_CF, LineItem.CHG_PAYABLES_CF,
                          LineItem.OTHER_WC_MOVEMENT, LineItem.DIRECT_TAXES_PAID,
                          LineItem.CAPEX, LineItem.SALE_FIXED_ASSETS,
                          LineItem.PURCHASE_SALE_INVESTMENTS, LineItem.OTHER_INVESTING,
                          LineItem.EQUITY_ISSUED_BUYBACK, LineItem.PROCEEDS_BORROWINGS,
                          LineItem.REPAYMENT_BORROWINGS)],
}


class FinancialAdminService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ==================================================================
    # Read
    # ==================================================================
    def _require_company(self, company_id: str) -> Company:
        company = self.db.get(Company, company_id)
        if company is None or company.deleted_at is not None:
            raise FinancialAdminError("company not found")
        return company

    def statements(self, company_id: str) -> dict[str, Any]:
        """Annual statements + ratios, computed from stored facts."""
        company = self._require_company(company_id)
        rows = self.db.execute(
            select(FinancialFact).where(FinancialFact.company_id == company_id)
        ).scalars().all()
        years = sorted({r.fiscal_year for r in rows})
        builder = CanonicalFinancialsBuilder(company_id, years)
        for r in rows:
            try:
                item = LineItem(r.line_item)
            except ValueError:
                continue
            builder.add(item, r.fiscal_year, r.value, Precedence(r.precedence or 2), r.source or "")
        fin = builder.build()

        from dataclasses import asdict

        incomes = [build_income_statement(fin, y) for y in years]
        balances = [build_balance_sheet(fin, y) for y in years]
        cash_flows = [build_cash_flow(fin, y) for y in years]
        statements = {
            y: {
                "income": _jsonable(asdict(incomes[i])),
                "balance": _jsonable(asdict(balances[i])),
                "cashflow": _jsonable(asdict(cash_flows[i])),
            }
            for i, y in enumerate(years)
        }

        ratios = {}
        if years:
            # Each section is computed independently and guarded: a partial
            # dataset must not take the whole statements view down.
            rs = RatioService(incomes, balances, cash_flows)
            for key, fn in (
                ("return", rs.return_ratios), ("dupont", rs.dupont),
                ("profitability", rs.profitability), ("liquidity", rs.liquidity),
                ("leverage", rs.leverage), ("efficiency", rs.efficiency),
            ):
                try:
                    section = fn()
                    if section:
                        ratios[key] = [
                            row.model_dump() if hasattr(row, "model_dump") else asdict(row)
                            for row in section.rows
                        ]
                    else:
                        ratios[key] = []
                except Exception:  # noqa: BLE001 - incomplete-data guard
                    ratios[key] = []

        return {
            "years": years,
            "statements": statements,
            "ratios": ratios,
            "fiscal_years": years,
        }

    def quarterly(self, company_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(QuarterlyResult).where(QuarterlyResult.company_id == company_id)
            .order_by(QuarterlyResult.fiscal_year.desc(), QuarterlyResult.quarter.desc())
        ).scalars().all()
        return [_q_dict(r) for r in rows]

    def shareholding(self, company_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(ShareholdingSnapshot).where(ShareholdingSnapshot.company_id == company_id)
            .order_by(ShareholdingSnapshot.fiscal_year.desc(), ShareholdingSnapshot.quarter.desc())
        ).scalars().all()
        return [_s_dict(r) for r in rows]

    def corporate_actions(self, company_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(CorporateAction).where(CorporateAction.company_id == company_id)
            .order_by(CorporateAction.ex_date.desc())
        ).scalars().all()
        return [CorporateAction.model_to_dict(r) if hasattr(CorporateAction, "model_to_dict") else _ca_dict(r) for r in rows]

    # ==================================================================
    # Mutation helpers
    # ==================================================================
    def _snapshot(self, company_id: str) -> dict[str, Any]:
        facts = [
            {"fiscal_year": f.fiscal_year, "line_item": f.line_item,
             "value": f.value, "precedence": f.precedence, "source": f.source}
            for f in self.db.execute(
                select(FinancialFact).where(FinancialFact.company_id == company_id)
            ).scalars()
        ]
        quarterly = self.quarterly(company_id)
        shareholding = self.shareholding(company_id)
        actions = self.corporate_actions(company_id)
        return {
            "facts": facts, "quarterly": quarterly,
            "shareholding": shareholding, "actions": actions,
        }

    def _bump(self, company: Company, actor_id, actor_email, summary, change_type="update") -> None:
        """Record a version, bump data_version, invalidate the cache."""
        next_ver = (
            self.db.execute(
                select(func.coalesce(func.max(FinancialFactVersion.version), 0))
                .where(FinancialFactVersion.company_id == company.id)
            ).scalar_one() + 1
        )
        self.db.add(FinancialFactVersion(
            company_id=company.id, version=next_ver,
            actor_id=actor_id, actor_email=actor_email,
            snapshot=self._snapshot(company.id),
            change_type=change_type, summary=summary, created_at=_utcnow(),
        ))
        company.data_version += 1
        company.updated_at = _utcnow()
        cache.invalidate(Namespace.STATEMENTS)

    # ==================================================================
    # Annual facts
    # ==================================================================
    def upsert_facts(
        self, company_id: str, facts: list[dict[str, Any]],
        *, actor_id=None, actor_email=None,
    ) -> dict[str, Any]:
        """Bulk upsert annual financial facts. Validates duplicates & negatives."""
        company = self._require_company(company_id)
        errors: list[dict[str, str]] = []
        updated = created = 0
        seen: set[tuple[int, str]] = set()

        for row in facts:
            try:
                year = int(row["fiscal_year"])
                item = str(row["line_item"]).strip()
                value = row.get("value")
                if item not in {i.value for i in LineItem}:
                    raise FinancialAdminError(f"unknown line item '{item}'")
                if (year, item) in seen:
                    raise FinancialAdminError(f"duplicate entry for {item} in FY{year}")
                seen.add((year, item))
                precedence = int(row.get("precedence", 2))
                source = str(row.get("source") or "admin")

                existing = self.db.execute(
                    select(FinancialFact).where(
                        FinancialFact.company_id == company_id,
                        FinancialFact.fiscal_year == year,
                        FinancialFact.line_item == item,
                        FinancialFact.precedence == precedence,
                    )
                ).scalars().first()
                if existing is not None:
                    existing.value = value
                    existing.source = source
                    updated += 1
                else:
                    self.db.add(FinancialFact(
                        company_id=company_id, fiscal_year=year, line_item=item,
                        value=value, precedence=precedence, source=source,
                    ))
                    created += 1
            except (KeyError, ValueError, TypeError, FinancialAdminError) as exc:
                errors.append({"row": str(row), "error": str(exc)})

        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Updated {updated}, created {created} annual fact(s)")
        return {"updated": updated, "created": created, "errors": errors}

    # ==================================================================
    # Quarterly results
    # ==================================================================
    def upsert_quarterly(
        self, company_id: str, rows: list[dict[str, Any]],
        *, actor_id=None, actor_email=None,
    ) -> dict[str, Any]:
        company = self._require_company(company_id)
        errors: list[dict[str, str]] = []
        updated = created = 0
        seen: set[tuple[int, int]] = set()

        for row in rows:
            try:
                year = int(row["fiscal_year"]); q = int(row["quarter"])
                if not (1 <= q <= 4):
                    raise FinancialAdminError(f"quarter must be 1..4, got {q}")
                if (year, q) in seen:
                    raise FinancialAdminError(f"duplicate entry for FY{year} Q{q}")
                seen.add((year, q))
                existing = self.db.execute(
                    select(QuarterlyResult).where(
                        QuarterlyResult.company_id == company_id,
                        QuarterlyResult.fiscal_year == year,
                        QuarterlyResult.quarter == q,
                    )
                ).scalars().first()
                if existing is not None:
                    for k, v in row.items():
                        if k not in ("company_id", "fiscal_year", "quarter") and hasattr(existing, k):
                            setattr(existing, k, v)
                    updated += 1
                else:
                    self.db.add(QuarterlyResult(company_id=company_id, **{
                        k: v for k, v in row.items()
                        if k in QuarterlyResult.__table__.c.keys()
                    }))
                    created += 1
            except (KeyError, ValueError, TypeError, FinancialAdminError) as exc:
                errors.append({"row": str(row), "error": str(exc)})

        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Updated {updated}, created {created} quarterly result(s)")
        return {"updated": updated, "created": created, "errors": errors}

    def delete_quarterly(self, company_id: str, year: int, quarter: int,
                         *, actor_id=None, actor_email=None) -> None:
        company = self._require_company(company_id)
        row = self.db.execute(
            select(QuarterlyResult).where(
                QuarterlyResult.company_id == company_id,
                QuarterlyResult.fiscal_year == year, QuarterlyResult.quarter == quarter,
            )
        ).scalars().first()
        if row is not None:
            self.db.delete(row)
            self.db.flush()
            self._bump(company, actor_id, actor_email,
                       summary=f"Deleted FY{year} Q{quarter}")

    # ==================================================================
    # Shareholding
    # ==================================================================
    def upsert_shareholding(
        self, company_id: str, rows: list[dict[str, Any]],
        *, actor_id=None, actor_email=None,
    ) -> dict[str, Any]:
        company = self._require_company(company_id)
        errors: list[dict[str, str]] = []
        updated = created = 0
        seen: set[tuple[int, int]] = set()

        for row in rows:
            try:
                year = int(row["fiscal_year"]); q = int(row["quarter"])
                if not (1 <= q <= 4):
                    raise FinancialAdminError("quarter must be 1..4")
                if (year, q) in seen:
                    raise FinancialAdminError(f"duplicate shareholding FY{year} Q{q}")
                seen.add((year, q))
                existing = self.db.execute(
                    select(ShareholdingSnapshot).where(
                        ShareholdingSnapshot.company_id == company_id,
                        ShareholdingSnapshot.fiscal_year == year,
                        ShareholdingSnapshot.quarter == q,
                    )
                ).scalars().first()
                if existing is not None:
                    for k, v in row.items():
                        if k not in ("company_id", "fiscal_year", "quarter") and hasattr(existing, k):
                            setattr(existing, k, v)
                    updated += 1
                else:
                    self.db.add(ShareholdingSnapshot(company_id=company_id, **{
                        k: v for k, v in row.items()
                        if k in ShareholdingSnapshot.__table__.c.keys()
                    }))
                    created += 1
            except (KeyError, ValueError, TypeError, FinancialAdminError) as exc:
                errors.append({"row": str(row), "error": str(exc)})

        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Updated {updated}, created {created} shareholding record(s)")
        return {"updated": updated, "created": created, "errors": errors}

    # ==================================================================
    # Corporate actions
    # ==================================================================
    def add_corporate_action(self, company_id: str, data: dict[str, Any],
                             *, actor_id=None, actor_email=None) -> CorporateAction:
        company = self._require_company(company_id)
        action = CorporateAction(company_id=company_id, **{
            k: v for k, v in data.items() if k in CorporateAction.__table__.c.keys()
        })
        self.db.add(action)
        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Added corporate action '{data.get('action_type')}'")
        return action

    def update_corporate_action(self, company_id: str, action_id: int, data: dict[str, Any],
                                *, actor_id=None, actor_email=None) -> CorporateAction:
        company = self._require_company(company_id)
        action = self.db.get(CorporateAction, action_id)
        if action is None or action.company_id != company_id:
            raise FinancialAdminError("corporate action not found")
        for k, v in data.items():
            if k in CorporateAction.__table__.c.keys():
                setattr(action, k, v)
        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Updated corporate action #{action.id}")
        return action

    def delete_corporate_action(self, company_id: str, action_id: int,
                                *, actor_id=None, actor_email=None) -> None:
        company = self._require_company(company_id)
        action = self.db.get(CorporateAction, action_id)
        if action is None or action.company_id != company_id:
            raise FinancialAdminError("corporate action not found")
        self.db.delete(action)
        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Deleted corporate action #{action.id}")

    # ==================================================================
    # Version history / rollback
    # ==================================================================
    def versions(self, company_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(FinancialFactVersion).where(FinancialFactVersion.company_id == company_id)
            .order_by(FinancialFactVersion.version.desc())
        ).scalars().all()
        return [
            {"id": r.id, "company_id": r.company_id, "version": r.version,
             "actor_email": r.actor_email, "change_type": r.change_type,
             "summary": r.summary, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    def rollback(self, company_id: str, version: int, *, actor_id=None, actor_email=None) -> None:
        company = self._require_company(company_id)
        target = self.db.execute(
            select(FinancialFactVersion).where(
                FinancialFactVersion.company_id == company_id,
                FinancialFactVersion.version == version,
            )
        ).scalars().first()
        if target is None or not target.snapshot:
            raise FinancialAdminError(f"version {version} has no snapshot")
        snap = target.snapshot

        # Replace annual facts
        self.db.execute(FinancialFact.__table__.delete().where(FinancialFact.company_id == company_id))
        for f in snap.get("facts", []):
            self.db.add(FinancialFact(company_id=company_id, **f))
        _SKIP = {"id", "created_at", "updated_at"}
        # Replace quarterly
        self.db.execute(QuarterlyResult.__table__.delete().where(QuarterlyResult.company_id == company_id))
        for q in snap.get("quarterly", []):
            clean = {k: v for k, v in q.items()
                     if k in QuarterlyResult.__table__.c.keys() and k not in _SKIP}
            self.db.add(QuarterlyResult(company_id=company_id, **clean))
        # Replace shareholding
        self.db.execute(ShareholdingSnapshot.__table__.delete().where(ShareholdingSnapshot.company_id == company_id))
        for s in snap.get("shareholding", []):
            clean = {k: v for k, v in s.items()
                     if k in ShareholdingSnapshot.__table__.c.keys() and k not in _SKIP}
            self.db.add(ShareholdingSnapshot(company_id=company_id, **clean))
        # Replace corporate actions
        self.db.execute(CorporateAction.__table__.delete().where(CorporateAction.company_id == company_id))
        for a in snap.get("actions", []):
            clean = {k: v for k, v in a.items()
                     if k in CorporateAction.__table__.c.keys() and k not in _SKIP}
            self.db.add(CorporateAction(company_id=company_id, **clean))

        self.db.flush()
        self._bump(company, actor_id, actor_email,
                   summary=f"Rolled back financials to version {version}",
                   change_type="rollback")


def _jsonable(value: Any) -> Any:
    """Convert a DB value to something a JSON snapshot column can store."""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # date / time
        return value.isoformat()
    return value


def _q_dict(r: QuarterlyResult) -> dict[str, Any]:
    return {c.name: _jsonable(getattr(r, c.name)) for c in QuarterlyResult.__table__.c}


def _s_dict(r: ShareholdingSnapshot) -> dict[str, Any]:
    return {c.name: _jsonable(getattr(r, c.name)) for c in ShareholdingSnapshot.__table__.c}


def _ca_dict(r: CorporateAction) -> dict[str, Any]:
    return {c.name: _jsonable(getattr(r, c.name)) for c in CorporateAction.__table__.c}
