"""Development seed data.

Generates a realistic NSE/BSE universe with full 54-item canonical financials
so every Module 1 screen renders against real, self-consistent data.

The generator uses the same balance-sheet-plug technique as the workbook's own
engine test, so seeded balance sheets tie in every year.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.base import Base, SessionLocal, engine
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI
from app.models.analysis import (  # noqa: F401  (registers Module 2 tables)
    CreditRating, DebtInstrument, ShareholdingSnapshot,
)
from app.models.forecast import (  # noqa: F401  (registers Module 3 tables)
    Forecast, ForecastAssumptionRecord,
)
from app.models.scoring import (  # noqa: F401  (registers Module 5 tables)
    ScoreSnapshot, ScoringWeightProfile,
)
from app.models.ai import (  # noqa: F401  (registers Module 6 tables)
    AIAnalysis, AIUsageRecord, PromptRecord,
)
from app.models.company import Company, FinancialFact

FISCAL_YEARS = tuple(range(2016, 2026))

# name, ticker, sector, industry, price, shares(cr), revenue base(₹cr), growth, margin
UNIVERSE = [
    ("Reliance Industries Ltd", "RELIANCE", "Oil & Gas", "Refining & Petrochemicals", 2945.0, 676.0, 9000, 0.11, 0.155),
    ("Tata Consultancy Services Ltd", "TCS", "IT Services", "IT Consulting", 3890.0, 362.0, 6200, 0.09, 0.26),
    ("HDFC Bank Ltd", "HDFCBANK", "Banking - Private", "Private Sector Bank", 1685.0, 760.0, 5400, 0.14, 0.29),
    ("Infosys Ltd", "INFY", "IT Services", "IT Consulting", 1620.0, 415.0, 3800, 0.10, 0.24),
    ("ICICI Bank Ltd", "ICICIBANK", "Banking - Private", "Private Sector Bank", 1210.0, 700.0, 4100, 0.15, 0.27),
    ("Hindustan Unilever Ltd", "HINDUNILVR", "FMCG", "Personal & Household Products", 2410.0, 235.0, 2900, 0.08, 0.23),
    ("Titan Company Ltd", "TITAN", "Consumer Durables", "Gems & Jewellery", 3320.0, 89.0, 3000, 0.19, 0.105),
    ("Larsen & Toubro Ltd", "LT", "Capital Goods", "Construction & Engineering", 3560.0, 137.0, 5100, 0.12, 0.115),
    ("Asian Paints Ltd", "ASIANPAINT", "Chemicals & Specialty", "Paints", 2280.0, 96.0, 1800, 0.13, 0.195),
    ("Maruti Suzuki India Ltd", "MARUTI", "Automobile & Ancillaries", "Passenger Vehicles", 11200.0, 31.0, 4600, 0.10, 0.115),
    ("Sun Pharmaceutical Industries Ltd", "SUNPHARMA", "Pharmaceuticals", "Generic Pharma", 1640.0, 240.0, 2400, 0.11, 0.225),
    ("Bharti Airtel Ltd", "BHARTIARTL", "Telecom", "Telecom Services", 1520.0, 570.0, 3900, 0.13, 0.32),
    ("UltraTech Cement Ltd", "ULTRACEMCO", "Cement", "Cement & Products", 10800.0, 29.0, 2700, 0.10, 0.185),
    ("Tata Steel Ltd", "TATASTEEL", "Metals & Mining", "Iron & Steel", 148.0, 1248.0, 3400, 0.07, 0.135),
    ("Nestle India Ltd", "NESTLEIND", "FMCG", "Packaged Foods", 2470.0, 96.0, 1200, 0.11, 0.225),
    ("Power Grid Corporation of India Ltd", "POWERGRID", "Power & Utilities", "Power Transmission", 322.0, 930.0, 1500, 0.08, 0.40),
    ("Bajaj Finance Ltd", "BAJFINANCE", "Banking - Private", "NBFC", 7150.0, 62.0, 2100, 0.21, 0.30),
    ("Wipro Ltd", "WIPRO", "IT Services", "IT Consulting", 545.0, 523.0, 2200, 0.07, 0.185),
    ("Grasim Industries Ltd", "GRASIM", "Chemicals & Specialty", "Diversified", 2560.0, 66.0, 2300, 0.09, 0.145),
    ("Cipla Ltd", "CIPLA", "Pharmaceuticals", "Generic Pharma", 1490.0, 81.0, 1400, 0.10, 0.205),
]


def _series(base: float, growth: float) -> list[float]:
    return [round(base * (1 + growth) ** i, 1) for i in range(len(FISCAL_YEARS))]


def _facts(scale: float, growth: float, margin: float, shares: float) -> dict[LI, list[float]]:
    """Self-consistent 54-item statements; reserves plug makes the BS tie."""
    rev = _series(scale, growth)
    d: dict[LI, list[float]] = {
        LI.REVENUE: rev,
        LI.OTHER_OPERATING_INCOME: [round(x * 0.012, 1) for x in rev],
        LI.RAW_MATERIALS: [round(x * (1 - margin - 0.19), 1) for x in rev],
        LI.PURCHASE_STOCK_IN_TRADE: [round(x * 0.05, 1) for x in rev],
        LI.CHANGE_INVENTORIES: [0.0] * len(rev),
        LI.EMPLOYEE_BENEFIT: [round(x * 0.09, 1) for x in rev],
        LI.OTHER_EXPENSES: [round(x * 0.10, 1) for x in rev],
        LI.DEPRECIATION: [round(x * 0.035, 1) for x in rev],
        LI.OTHER_INCOME: [round(x * 0.012, 1) for x in rev],
        LI.FINANCE_COSTS: [round(x * 0.008, 1) for x in rev],
        LI.EXCEPTIONAL_ITEMS: [0.0] * len(rev),
        LI.TAX_EXPENSE: [round(x * margin * 0.25, 1) for x in rev],
        LI.MINORITY_INTEREST: [round(x * 0.001, 1) for x in rev],
        LI.OCI: [0.0] * len(rev),
        LI.DIVIDEND_PAID: [round(x * margin * 0.18, 1) for x in rev],
        LI.WEIGHTED_SHARES: [shares] * len(rev),
        LI.CASH_AND_BANK: [round(x * 0.10, 1) for x in rev],
        LI.CURRENT_INVESTMENTS: [round(x * 0.05, 1) for x in rev],
        LI.TRADE_RECEIVABLES: [round(x * 0.12, 1) for x in rev],
        LI.INVENTORIES: [round(x * 0.14, 1) for x in rev],
        LI.OTHER_CURRENT_ASSETS: [round(x * 0.04, 1) for x in rev],
        LI.NET_BLOCK_PPE: [round(x * 0.31, 1) for x in rev],
        LI.CWIP: [round(x * 0.03, 1) for x in rev],
        LI.GOODWILL: [round(x * 0.01, 1) for x in rev],
        LI.OTHER_INTANGIBLES: [round(x * 0.008, 1) for x in rev],
        LI.LT_INVESTMENTS_ASSOCIATES: [round(x * 0.02, 1) for x in rev],
        LI.OTHER_NCA: [round(x * 0.014, 1) for x in rev],
        LI.DEFERRED_TAX_ASSET: [round(x * 0.004, 1) for x in rev],
        LI.TRADE_PAYABLES: [round(x * 0.10, 1) for x in rev],
        LI.SHORT_TERM_BORROWINGS: [round(x * 0.03, 1) for x in rev],
        LI.CURRENT_MATURITIES_LTD: [round(x * 0.011, 1) for x in rev],
        LI.OTHER_CURRENT_LIABILITIES: [round(x * 0.035, 1) for x in rev],
        LI.SHORT_TERM_PROVISIONS: [round(x * 0.014, 1) for x in rev],
        LI.LONG_TERM_BORROWINGS: [round(x * 0.07, 1) for x in rev],
        LI.DEFERRED_TAX_LIABILITY: [round(x * 0.017, 1) for x in rev],
        LI.OTHER_NCL: [round(x * 0.012, 1) for x in rev],
        LI.EQUITY_SHARE_CAPITAL: [shares] * len(rev),
        LI.MINORITY_INTEREST_BS: [round(x * 0.006, 1) for x in rev],
        LI.OTHER_NONCASH_ADJ: [round(x * 0.004, 1) for x in rev],
        LI.CHG_INVENTORIES_CF: [round(-x * 0.012, 1) for x in rev],
        LI.CHG_RECEIVABLES_CF: [round(-x * 0.010, 1) for x in rev],
        LI.CHG_PAYABLES_CF: [round(x * 0.009, 1) for x in rev],
        LI.OTHER_WC_MOVEMENT: [round(x * 0.002, 1) for x in rev],
        LI.DIRECT_TAXES_PAID: [round(x * margin * 0.24, 1) for x in rev],
        LI.CAPEX: [round(x * 0.055, 1) for x in rev],
        LI.SALE_FIXED_ASSETS: [round(x * 0.003, 1) for x in rev],
        LI.PURCHASE_SALE_INVESTMENTS: [round(-x * 0.012, 1) for x in rev],
        LI.OTHER_INVESTING: [round(x * 0.002, 1) for x in rev],
        LI.EQUITY_ISSUED_BUYBACK: [0.0] * len(rev),
        LI.PROCEEDS_BORROWINGS: [round(x * 0.030, 1) for x in rev],
        LI.REPAYMENT_BORROWINGS: [round(x * 0.024, 1) for x in rev],
        LI.OTHER_FINANCING: [round(-x * 0.002, 1) for x in rev],
        LI.OPENING_CASH: [round(x * 0.095, 1) for x in rev],
    }
    assets = [
        LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES, LI.INVENTORIES,
        LI.OTHER_CURRENT_ASSETS, LI.NET_BLOCK_PPE, LI.CWIP, LI.GOODWILL,
        LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES, LI.OTHER_NCA,
        LI.DEFERRED_TAX_ASSET,
    ]
    liabs = [
        LI.TRADE_PAYABLES, LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
        LI.OTHER_CURRENT_LIABILITIES, LI.SHORT_TERM_PROVISIONS,
        LI.LONG_TERM_BORROWINGS, LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL,
    ]
    d[LI.RESERVES_SURPLUS] = [
        round(
            sum(d[k][i] for k in assets)
            - sum(d[k][i] for k in liabs)
            - d[LI.EQUITY_SHARE_CAPITAL][i]
            - d[LI.MINORITY_INTEREST_BS][i],
            1,
        )
        for i in range(len(rev))
    ]
    return d


def seed(db: Session, *, reset: bool = True) -> dict[str, int]:
    if reset:
        db.execute(delete(FinancialFact))
        db.execute(delete(Company))
        db.commit()

    n_fact = 0
    for name, ticker, sector, industry, price, shares, scale, growth, margin in UNIVERSE:
        company = Company(
            id=str(uuid.uuid4()),
            name=name,
            ticker=ticker,
            exchange="NSE",
            isin=f"INE{abs(hash(ticker)) % 10**9:09d}",
            sector=sector,
            industry=industry,
            current_price=price,
            shares_outstanding=shares,
            market_cap=round(price * shares, 1),
            description=f"{name} is a listed Indian company operating in {industry}.",
            website=f"https://www.{ticker.lower()}.com",
            incorporated_year=1970 + (abs(hash(ticker)) % 45),
        )
        db.add(company)
        db.flush()

        for item, values in _facts(scale, growth, margin, shares).items():
            for year, value in zip(FISCAL_YEARS, values):
                db.add(
                    FinancialFact(
                        company_id=company.id,
                        fiscal_year=year,
                        line_item=item.value,
                        value=value,
                        precedence=int(Precedence.STORE),
                        source="seed",
                    )
                )
                n_fact += 1
    db.commit()
    return {"companies": len(UNIVERSE), "facts": n_fact}




# ---------------------------------------------------------------------------
# Module 2 — debt instruments and shareholding snapshots.
# These are separately disclosed facts, not derivable from the statements.
# ---------------------------------------------------------------------------

DEBT_TEMPLATE = [
    # instrument, security, rate_type, share of gross debt, rate, maturity offset
    ("Term loan — State Bank of India", "Secured", "Floating", 0.28, 0.0875, 3),
    ("Term loan — HDFC Bank", "Secured", "Floating", 0.18, 0.0910, 5),
    ("NCD Series I", "Secured", "Fixed", 0.20, 0.0825, 4),
    ("ECB / foreign-currency loan", "Secured", "Floating", 0.12, 0.0650, 6),
    ("Working-capital facility (CC/OD)", "Secured", "Floating", 0.10, 0.0950, 1),
    ("Lease liabilities (Ind AS 116)", "Secured", "Fixed", 0.07, 0.0800, 7),
    ("Commercial paper", "Unsecured", "Fixed", 0.05, 0.0725, 1),
]


def seed_module2(db: Session) -> dict[str, int]:
    """Seed debt schedules, credit ratings and shareholding history."""
    from datetime import date

    from app.domain.financials.statements import build_balance_sheet
    from app.services.company_service import CompanyService

    db.execute(delete(DebtInstrument))
    db.execute(delete(CreditRating))
    db.execute(delete(ShareholdingSnapshot))
    db.commit()

    svc = CompanyService(db)
    companies = db.execute(select(Company)).scalars().all()
    latest_fy = max(FISCAL_YEARS)
    n_debt = n_share = n_rating = 0

    for idx, company in enumerate(companies):
        fin = svc.load_financials(company.id)
        if not fin.has_data():
            continue

        # --- debt schedule reconciled to balance-sheet gross debt ----------
        bs = build_balance_sheet(fin, latest_fy)
        gross = bs.gross_debt
        allocated = 0.0
        for j, (name, sec, rate_type, share, rate, offset) in enumerate(DEBT_TEMPLATE):
            is_last = j == len(DEBT_TEMPLATE) - 1
            amount = round(gross - allocated, 2) if is_last else round(gross * share, 2)
            allocated += amount
            if amount <= 0:
                continue
            db.add(DebtInstrument(
                company_id=company.id, fiscal_year=latest_fy, instrument=name,
                lender=name.split("—")[-1].strip() if "—" in name else None,
                security=sec, rate_type=rate_type, amount=amount,
                interest_rate=rate, maturity_year=latest_fy + offset,
                currency="USD" if "foreign-currency" in name else "INR",
            ))
            n_debt += 1

        # --- credit rating -------------------------------------------------
        grade = ["AAA", "AA+", "AA", "AA-", "A+"][idx % 5]
        db.add(CreditRating(
            company_id=company.id, agency=["CRISIL", "ICRA", "CARE"][idx % 3],
            rating=grade, outlook=["Stable", "Positive", "Stable"][idx % 3],
            action_date=date(latest_fy, 6, 15), instrument_class="Long-term bank facilities",
        ))
        n_rating += 1

        # --- 12 quarters of shareholding -----------------------------------
        # Keep promoter + institutional + others under 100% so the retail
        # residual stays genuinely positive for every seeded company.
        base_promoter = 0.32 + (idx % 7) * 0.035      # 32%..53%
        base_fii = 0.16 - (idx % 5) * 0.020
        drift = 0.0015 if idx % 3 == 0 else -0.0011   # accumulation vs distribution
        pledge_base = 0.12 if idx % 6 == 0 else 0.0

        for q in range(12):
            fy = latest_fy - 2 + q // 4
            quarter = q % 4 + 1
            promoter = round(base_promoter + drift * q, 4)
            fii = round(base_fii + 0.0009 * q, 4)
            db.add(ShareholdingSnapshot(
                company_id=company.id, fiscal_year=fy, quarter=quarter,
                promoter_indian=round(promoter * 0.94, 4),
                promoter_foreign=round(promoter * 0.06, 4),
                fii_fpi=fii,
                mutual_funds=round(0.085 + 0.0007 * q, 4),
                insurance=0.035, banks_fis_aif=0.005, government=0.0,
                others_custodians=0.008,
                promoter_pledged=round(max(0.0, pledge_base - 0.004 * q), 4),
            ))
            n_share += 1

    db.commit()
    return {"debt_instruments": n_debt, "ratings": n_rating, "shareholding": n_share}




# ---------------------------------------------------------------------------
# Reference company with realistic, investment-grade-shaped financials.
#
# The synthetic universe above is deliberately crude (flat ratios, thin
# margins, cash-consuming capex) and exists to exercise the plumbing. It is
# NOT suitable for valuation: a business whose capex permanently exceeds its
# operating cash flow has no positive DCF value, and the data-quality gate
# correctly refuses to certify it.
#
# This entry provides one company whose economics are coherent — healthy
# margins, cash generation above capex, sensible leverage and share count — so
# the valuation engine can be validated end to end against plausible inputs.
# It is still marked source="reference_model", not "filing", so the quality
# gate still declines to call it investment-grade.
# ---------------------------------------------------------------------------

REFERENCE_COMPANY = {
    "name": "Bharat Consumer Products Ltd",
    "ticker": "BHARATCP",
    "sector": "FMCG",
    "industry": "Packaged Foods",
    "price": 268.0,   # ~19x FY25 EPS — a plausible FMCG rating
    "shares": 250.0,          # crore
    "revenue_base": 12000.0,  # ₹ crore, FY16
    "growth": 0.12,
    "ebitda_margin": 0.185,
}


def seed_reference_company(db: Session) -> dict[str, int]:
    """Insert one company with coherent, valuation-grade economics."""
    from app.domain.financials.line_items import LineItem as LI

    spec = REFERENCE_COMPANY
    existing = db.execute(
        select(Company).where(Company.ticker == spec["ticker"])
    ).scalar_one_or_none()
    if existing:
        db.execute(delete(FinancialFact).where(FinancialFact.company_id == existing.id))
        db.delete(existing)
        db.commit()

    shares = spec["shares"]
    company = Company(
        id=str(uuid.uuid4()),
        name=spec["name"], ticker=spec["ticker"], exchange="NSE",
        isin=f"INE{abs(hash(spec['ticker'])) % 10**9:09d}",
        sector=spec["sector"], industry=spec["industry"],
        current_price=spec["price"], shares_outstanding=shares,
        market_cap=round(spec["price"] * shares, 1),
        description=(
            "Reference company with coherent economics, used to validate the "
            "valuation engine. Not a real issuer."
        ),
        website="https://example.invalid", incorporated_year=1985,
    )
    db.add(company)
    db.flush()

    n = len(FISCAL_YEARS)
    g, margin = spec["growth"], spec["ebitda_margin"]
    rev = [round(spec["revenue_base"] * (1 + g) ** i, 1) for i in range(n)]

    d: dict[LI, list[float]] = {
        LI.REVENUE: rev,
        LI.OTHER_OPERATING_INCOME: [round(x * 0.008, 1) for x in rev],
        # cost stack sized so EBITDA margin lands on target
        LI.RAW_MATERIALS: [round(x * (1 - margin - 0.175), 1) for x in rev],
        LI.PURCHASE_STOCK_IN_TRADE: [round(x * 0.03, 1) for x in rev],
        LI.CHANGE_INVENTORIES: [0.0] * n,
        LI.EMPLOYEE_BENEFIT: [round(x * 0.075, 1) for x in rev],
        LI.OTHER_EXPENSES: [round(x * 0.098, 1) for x in rev],
        LI.DEPRECIATION: [round(x * 0.028, 1) for x in rev],
        LI.OTHER_INCOME: [round(x * 0.010, 1) for x in rev],
        LI.FINANCE_COSTS: [round(x * 0.007, 1) for x in rev],
        LI.EXCEPTIONAL_ITEMS: [0.0] * n,
        # ~25% effective rate on PBT
        LI.TAX_EXPENSE: [round(x * 0.0355, 1) for x in rev],
        LI.MINORITY_INTEREST: [round(x * 0.0008, 1) for x in rev],
        LI.OCI: [0.0] * n,
        LI.DIVIDEND_PAID: [round(x * 0.031, 1) for x in rev],
        LI.WEIGHTED_SHARES: [shares] * n,
        LI.CASH_AND_BANK: [round(x * 0.085, 1) for x in rev],
        LI.CURRENT_INVESTMENTS: [round(x * 0.075, 1) for x in rev],
        LI.TRADE_RECEIVABLES: [round(x * 0.062, 1) for x in rev],
        LI.INVENTORIES: [round(x * 0.088, 1) for x in rev],
        LI.OTHER_CURRENT_ASSETS: [round(x * 0.022, 1) for x in rev],
        LI.NET_BLOCK_PPE: [round(x * 0.235, 1) for x in rev],
        LI.CWIP: [round(x * 0.018, 1) for x in rev],
        LI.GOODWILL: [round(x * 0.030, 1) for x in rev],
        LI.OTHER_INTANGIBLES: [round(x * 0.012, 1) for x in rev],
        LI.LT_INVESTMENTS_ASSOCIATES: [round(x * 0.028, 1) for x in rev],
        LI.OTHER_NCA: [round(x * 0.011, 1) for x in rev],
        LI.DEFERRED_TAX_ASSET: [round(x * 0.003, 1) for x in rev],
        LI.TRADE_PAYABLES: [round(x * 0.072, 1) for x in rev],
        LI.SHORT_TERM_BORROWINGS: [round(x * 0.012, 1) for x in rev],
        LI.CURRENT_MATURITIES_LTD: [round(x * 0.008, 1) for x in rev],
        LI.OTHER_CURRENT_LIABILITIES: [round(x * 0.028, 1) for x in rev],
        LI.SHORT_TERM_PROVISIONS: [round(x * 0.010, 1) for x in rev],
        LI.LONG_TERM_BORROWINGS: [round(x * 0.045, 1) for x in rev],
        LI.DEFERRED_TAX_LIABILITY: [round(x * 0.012, 1) for x in rev],
        LI.OTHER_NCL: [round(x * 0.009, 1) for x in rev],
        LI.EQUITY_SHARE_CAPITAL: [shares] * n,
        LI.MINORITY_INTEREST_BS: [round(x * 0.004, 1) for x in rev],
        LI.OTHER_NONCASH_ADJ: [round(x * 0.003, 1) for x in rev],
        # modest working-capital drag, well below operating cash generation
        LI.CHG_INVENTORIES_CF: [round(-x * 0.006, 1) for x in rev],
        LI.CHG_RECEIVABLES_CF: [round(-x * 0.004, 1) for x in rev],
        LI.CHG_PAYABLES_CF: [round(x * 0.005, 1) for x in rev],
        LI.OTHER_WC_MOVEMENT: [round(x * 0.001, 1) for x in rev],
        LI.DIRECT_TAXES_PAID: [round(x * 0.034, 1) for x in rev],
        # capex comfortably below CFO — the key difference from the crude seed
        LI.CAPEX: [round(x * 0.035, 1) for x in rev],
        LI.SALE_FIXED_ASSETS: [round(x * 0.002, 1) for x in rev],
        LI.PURCHASE_SALE_INVESTMENTS: [round(-x * 0.008, 1) for x in rev],
        LI.OTHER_INVESTING: [round(x * 0.001, 1) for x in rev],
        LI.EQUITY_ISSUED_BUYBACK: [0.0] * n,
        LI.PROCEEDS_BORROWINGS: [round(x * 0.010, 1) for x in rev],
        LI.REPAYMENT_BORROWINGS: [round(x * 0.012, 1) for x in rev],
        LI.OTHER_FINANCING: [round(-x * 0.001, 1) for x in rev],
        LI.OPENING_CASH: [round(x * 0.080, 1) for x in rev],
    }

    assets = [
        LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES, LI.INVENTORIES,
        LI.OTHER_CURRENT_ASSETS, LI.NET_BLOCK_PPE, LI.CWIP, LI.GOODWILL,
        LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES, LI.OTHER_NCA,
        LI.DEFERRED_TAX_ASSET,
    ]
    liabs = [
        LI.TRADE_PAYABLES, LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
        LI.OTHER_CURRENT_LIABILITIES, LI.SHORT_TERM_PROVISIONS,
        LI.LONG_TERM_BORROWINGS, LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL,
    ]
    d[LI.RESERVES_SURPLUS] = [
        round(sum(d[k][i] for k in assets) - sum(d[k][i] for k in liabs)
              - d[LI.EQUITY_SHARE_CAPITAL][i] - d[LI.MINORITY_INTEREST_BS][i], 1)
        for i in range(n)
    ]

    count = 0
    for item, values in d.items():
        for year, value in zip(FISCAL_YEARS, values):
            db.add(FinancialFact(
                company_id=company.id, fiscal_year=year, line_item=item.value,
                value=value, precedence=int(Precedence.STORE), source="reference_model",
            ))
            count += 1
    db.commit()
    return {"companies": 1, "facts": count}


def main() -> None:  # pragma: no cover
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        stats = seed(db)
        m2 = seed_module2(db)
        ref = seed_reference_company(db)
        total = db.execute(select(Company)).scalars().all()
    print(f"seeded {stats['companies']} companies, {stats['facts']:,} facts")
    print(f"module 2: {m2['debt_instruments']} debt instruments, "
          f"{m2['ratings']} ratings, {m2['shareholding']} shareholding snapshots")
    print(f"reference company: {ref['companies']} ({ref['facts']} facts)")
    print(f"companies in db: {len(total)}")


if __name__ == "__main__":  # pragma: no cover
    main()


def seed_module8_data(db) -> dict:
    """Module 8 demo data. Imported lazily to keep the seed graph acyclic."""
    from app.db.seed_portfolio import seed_module8

    return seed_module8(db)
