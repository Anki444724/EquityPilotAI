"""Deterministic mock financial statements — Phase 1.

The mock market provider exercises the price pipeline; this module exercises
the *financial* pipeline. `DATA_PROVIDER=mock` makes the financials sync job
ingest from here instead of screener.in, so the full 5,000-company loop —
universe → financials → quotes → history — runs offline and deterministically.

Same rules as every other ingestion path:
* values are a pure function of (ticker, fiscal_year) — identical on every
  run, which is what makes "repeated sync is a no-op" testable;
* rows carry `source='mock (synthetic)'`, so a mock fact is separable from a
  real fact by query for as long as it exists;
* the statements BALANCE by construction (assets = liabilities + equity),
  because the canonical engines derive ratios and ties from them and a
  nonsense balance sheet would fail validation for the wrong reason.

Scale realism, not factual realism: a ₹200cr–₹40,000cr revenue base with
plausible margins. The numbers are false in fact and shape-correct.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.ingest import _upsert_facts, _write_fact_version
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI
from app.models.company import Company
from app.models.financials import FinancialFactVersion

SOURCE_LABEL = "mock (synthetic)"

#: How many fiscal years a mock history covers (FY2016..FY2025 style).
DEFAULT_YEARS = 10


def _unit(ticker: str, salt: str) -> float:
    digest = hashlib.sha256(f"mockfin|{ticker}|{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


#: Absolute anchor year for the generated series. Every per-year figure is a
#: pure function of (ticker, fiscal_year) measured FROM THIS ANCHOR, so a
#: 5-year window and a 10-year window agree on every shared year — the
#: determinism contract the idempotency tests rely on.
_ANCHOR_FY = 2016


def _series_params(ticker: str):
    return (
        200.0 * (200.0 ** _unit(ticker, "rev")),    # revenue base
        0.03 + 0.15 * _unit(ticker, "growth"),      # growth/yr
        0.08 + 0.22 * _unit(ticker, "opm"),         # operating margin
        0.21 + 0.05 * _unit(ticker, "tax"),         # tax rate
        0.02 + 0.04 * _unit(ticker, "dep"),         # depreciation % of revenue
        0.005 + 0.02 * _unit(ticker, "int"),        # interest % of revenue
        float(10_000_000 + int(_unit(ticker, "shares") * 900_000_000)),
    )


def _fy_revenue(ticker: str, fy: int) -> float:
    base_rev, growth, *_rest = _series_params(ticker)
    wobble = 1.0 + (_unit(ticker, f"w{fy}") - 0.5) * 0.06
    return round(base_rev * (growth ** (fy - _ANCHOR_FY)) * wobble, 1)


def _fy_pat(ticker: str, fy: int) -> float:
    _base, _growth, opm, tax_rate, *_r = _series_params(ticker)
    return round(_fy_revenue(ticker, fy) * opm * (1 - tax_rate), 1)


def generate_mock_facts(ticker: str, years: int = DEFAULT_YEARS) -> dict[LI, dict[int, float]]:
    """Deterministic, balance-sheet-tying canonical facts for one ticker."""
    base_rev, growth, opm, tax_rate, dep_pct, int_pct, shares = _series_params(ticker)

    from datetime import date
    y = date.today().year
    this_year = y if date.today().month >= 4 else y - 1         # Indian FY

    fiscal_years = [this_year - i for i in range(years - 1, -1, -1)]

    out: dict[LI, dict[int, float]] = {}
    revenue: dict[int, float] = {}
    pat: dict[int, float] = {}
    dep: dict[int, float] = {}
    interest: dict[int, float] = {}
    for fy in fiscal_years:
        rev = _fy_revenue(ticker, fy)
        revenue[fy] = rev
        dep[fy] = round(rev * dep_pct, 1)
        interest[fy] = round(rev * int_pct, 1)
        pat[fy] = _fy_pat(ticker, fy)

    out[LI.REVENUE] = revenue
    out[LI.OTHER_INCOME] = {fy: round(revenue[fy] * 0.01, 1) for fy in fiscal_years}
    out[LI.DEPRECIATION] = dep
    out[LI.FINANCE_COSTS] = interest
    # PAT and EPS are DERIVED by the statement engines (PAT from the expense
    # chain, EPS = PAT / weighted shares), so — like the real ingestion — the
    # mock stores the reported inputs and lets the engines do the arithmetic.
    out[LI.TAX_EXPENSE] = {
        fy: round(rev * opm * tax_rate, 1) for fy, rev in revenue.items()
    }

    # ---- Balance sheet, built to tie: A = L + E -------------------------
    equity_capital = {fy: round(shares * 10.0 / 1e7, 1) for fy in fiscal_years}  # FV ₹10
    # Closed-form from the anchor year, so reserves for a given FY are the
    # same whatever window the series was generated over.
    reserves = {}
    for fy in fiscal_years:
        accumulated = base_rev * 1.2 * (1.02 ** (fy - _ANCHOR_FY))
        for prior in range(_ANCHOR_FY, fy + 1):
            accumulated += _fy_pat(ticker, prior)
        reserves[fy] = round(accumulated, 1)
    borrowings = {
        fy: round(revenue[fy] * (0.1 + 0.3 * _unit(ticker, "lev")), 1)
        for fy in fiscal_years
    }
    out[LI.EQUITY_SHARE_CAPITAL] = equity_capital
    out[LI.RESERVES_SURPLUS] = reserves
    out[LI.SHORT_TERM_BORROWINGS] = {
        fy: round(borrowings[fy] * 0.3, 1) for fy in fiscal_years
    }
    out[LI.LONG_TERM_BORROWINGS] = {
        fy: round(borrowings[fy] * 0.7, 1) for fy in fiscal_years
    }

    net_block = {
        fy: round(revenue[fy] * (0.4 + 0.4 * _unit(ticker, "assetturn")), 1)
        for fy in fiscal_years
    }
    out[LI.NET_BLOCK_PPE] = net_block
    out[LI.CWIP] = {fy: round(net_block[fy] * 0.08, 1) for fy in fiscal_years}
    out[LI.CASH_AND_BANK] = {
        fy: round(revenue[fy] * 0.05, 1) for fy in fiscal_years
    }
    out[LI.CURRENT_INVESTMENTS] = {
        fy: round(revenue[fy] * 0.04, 1) for fy in fiscal_years
    }
    out[LI.TRADE_RECEIVABLES] = {
        fy: round(revenue[fy] * (0.10 + 0.10 * _unit(ticker, "dso")), 1)
        for fy in fiscal_years
    }
    out[LI.INVENTORIES] = {
        fy: round(revenue[fy] * (0.05 + 0.10 * _unit(ticker, "dio")), 1)
        for fy in fiscal_years
    }
    out[LI.TRADE_PAYABLES] = {
        fy: round(revenue[fy] * (0.08 + 0.08 * _unit(ticker, "dpo")), 1)
        for fy in fiscal_years
    }
    # Balancing current-asset / current-liability lines so the sheet ties:
    equity = {fy: equity_capital[fy] + reserves[fy] for fy in fiscal_years}
    total_liab_equity = {
        fy: equity[fy] + borrowings[fy] + out[LI.TRADE_PAYABLES][fy]
        for fy in fiscal_years
    }
    known_assets = {
        fy: (net_block[fy] + out[LI.CWIP][fy] + out[LI.CASH_AND_BANK][fy]
             + out[LI.CURRENT_INVESTMENTS][fy] + out[LI.TRADE_RECEIVABLES][fy]
             + out[LI.INVENTORIES][fy])
        for fy in fiscal_years
    }
    out[LI.OTHER_CURRENT_ASSETS] = {
        fy: round(max(total_liab_equity[fy] - known_assets[fy], 1.0), 1)
        for fy in fiscal_years
    }
    out[LI.WEIGHTED_SHARES] = {fy: shares for fy in fiscal_years}

    # ---- Cash flow (simplified but consistent with the above) -----------
    out[LI.CHG_INVENTORIES_CF] = {
        fy: -out[LI.INVENTORIES][fy] * 0.1 for fy in fiscal_years
    }
    out[LI.CHG_RECEIVABLES_CF] = {
        fy: -out[LI.TRADE_RECEIVABLES][fy] * 0.1 for fy in fiscal_years
    }
    out[LI.CHG_PAYABLES_CF] = {
        fy: out[LI.TRADE_PAYABLES][fy] * 0.1 for fy in fiscal_years
    }
    out[LI.CAPEX] = {
        fy: -(dep[fy] * 1.2) for fy in fiscal_years
    }
    out[LI.DIRECT_TAXES_PAID] = {
        fy: -(pat[fy] / (1 - tax_rate) * tax_rate * 0.9) for fy in fiscal_years
    }

    return out


def upsert_mock_financials(
    db: Session, ticker: str, *, years: int = DEFAULT_YEARS,
) -> dict[str, Any]:
    """Upsert deterministic facts for one company. Idempotent by construction:
    identical values on every run ⇒ second run reports 0 inserted / 0 updated.
    """
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    if company is None:
        return {"ticker": ticker, "ok": False, "error": "company not found"}

    facts = generate_mock_facts(ticker, years=years)
    inserted, updated, unchanged = _upsert_facts(db, company.id, facts, SOURCE_LABEL)
    if inserted or updated:
        company.data_version = (company.data_version or 1) + 1
        next_ver = (
            db.execute(
                select(func.coalesce(func.max(FinancialFactVersion.version), 0))
                .where(FinancialFactVersion.company_id == company.id)
            ).scalar_one() + 1
        )
        db.add(FinancialFactVersion(
            company_id=company.id, version=next_ver, change_type="import",
            summary=f"Upserted mock financials ({inserted} new, {updated} changed)",
            snapshot={"facts": [], "quarterly": [], "shareholding": [], "actions": []},
        ))
    db.commit()
    return {
        "ticker": ticker, "ok": True,
        "inserted": inserted, "updated": updated, "unchanged": unchanged,
        "company_id": company.id,
    }
