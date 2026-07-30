"""Derived financial statements.

Direct translation of workbook sheets `06 Historical IS`, `07 Historical BS`
and `08 Historical CF`. Every subtotal below carries the originating cell
reference so the mapping back to the specification stays auditable.

Pure functions of :class:`CanonicalFinancials`. No I/O, no framework.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div as _safe_div

from .canonical import CanonicalFinancials
from .line_items import LineItem as LI


@dataclass(frozen=True, slots=True)
class IncomeStatement:
    """`06 Historical IS` for one fiscal year."""

    fiscal_year: int

    revenue_operations: float
    other_operating_income: float
    total_revenue: float                # r11  =SUM(L9:L10)

    raw_materials: float
    purchase_stock_in_trade: float
    change_inventories: float
    total_cogs: float                   # r16  =SUM(L13:L15)
    gross_profit: float                 # r17  =L11-L16

    employee_benefit: float
    other_expenses: float
    total_opex: float                   # r21  =SUM(L19:L20)
    ebitda: float                       # r22  =L17-L21

    depreciation: float
    ebit: float                         # r25  =L22-L24
    other_income: float
    finance_costs: float
    pbt_before_exceptional: float       # r28  =L25+L26-L27
    exceptional_items: float
    pbt: float                          # r30  =L28+L29
    tax_expense: float
    pat_before_minority: float          # r32  =L30-L31
    minority_interest: float
    pat: float                          # r34  =L32-L33
    oci: float
    total_comprehensive_income: float   # r36  =L34+L35

    dividend_paid: float
    weighted_shares: float
    dividend_per_share: float | None    # r39  =IFERROR(L38/L43,0)
    eps_basic: float | None             # r41  =IFERROR(L34/L43,0)
    eps_diluted: float | None           # r42  =IFERROR(L34/(L43*1.005),0)

    gross_margin: float | None          # r46  =IFERROR(L17/L11,0)
    ebitda_margin: float | None         # r47  =IFERROR(L22/L11,0)
    ebit_margin: float | None
    pat_margin: float | None
    effective_tax_rate: float | None

    @property
    def has_data(self) -> bool:
        return self.total_revenue != 0


#: Diluted-share convention from `06 Historical IS!L42` — the workbook applies
#: a flat 0.5% dilution uplift to the weighted average share count.
DILUTION_FACTOR = 1.005


def build_income_statement(fin: CanonicalFinancials, year: int) -> IncomeStatement:
    """Compute `06 Historical IS` for one year."""
    g = fin.at

    revenue = g(LI.REVENUE, year)
    other_op = g(LI.OTHER_OPERATING_INCOME, year)
    total_revenue = revenue + other_op

    rm = g(LI.RAW_MATERIALS, year)
    purch = g(LI.PURCHASE_STOCK_IN_TRADE, year)
    chg_inv = g(LI.CHANGE_INVENTORIES, year)
    total_cogs = rm + purch + chg_inv
    gross_profit = total_revenue - total_cogs

    emp = g(LI.EMPLOYEE_BENEFIT, year)
    other_exp = g(LI.OTHER_EXPENSES, year)
    total_opex = emp + other_exp
    ebitda = gross_profit - total_opex

    dep = g(LI.DEPRECIATION, year)
    ebit = ebitda - dep

    other_inc = g(LI.OTHER_INCOME, year)
    fin_cost = g(LI.FINANCE_COSTS, year)
    pbt_before_exc = ebit + other_inc - fin_cost

    exceptional = g(LI.EXCEPTIONAL_ITEMS, year)
    pbt = pbt_before_exc + exceptional

    tax = g(LI.TAX_EXPENSE, year)
    pat_before_mi = pbt - tax
    mi = g(LI.MINORITY_INTEREST, year)
    pat = pat_before_mi - mi

    oci = g(LI.OCI, year)
    tci = pat + oci

    dividend = g(LI.DIVIDEND_PAID, year)
    shares = g(LI.WEIGHTED_SHARES, year)

    return IncomeStatement(
        fiscal_year=year,
        revenue_operations=revenue,
        other_operating_income=other_op,
        total_revenue=total_revenue,
        raw_materials=rm,
        purchase_stock_in_trade=purch,
        change_inventories=chg_inv,
        total_cogs=total_cogs,
        gross_profit=gross_profit,
        employee_benefit=emp,
        other_expenses=other_exp,
        total_opex=total_opex,
        ebitda=ebitda,
        depreciation=dep,
        ebit=ebit,
        other_income=other_inc,
        finance_costs=fin_cost,
        pbt_before_exceptional=pbt_before_exc,
        exceptional_items=exceptional,
        pbt=pbt,
        tax_expense=tax,
        pat_before_minority=pat_before_mi,
        minority_interest=mi,
        pat=pat,
        oci=oci,
        total_comprehensive_income=tci,
        dividend_paid=dividend,
        weighted_shares=shares,
        dividend_per_share=_safe_div(dividend, shares),
        eps_basic=_safe_div(pat, shares),
        eps_diluted=_safe_div(pat, shares * DILUTION_FACTOR),
        gross_margin=_safe_div(gross_profit, total_revenue),
        ebitda_margin=_safe_div(ebitda, total_revenue),
        ebit_margin=_safe_div(ebit, total_revenue),
        pat_margin=_safe_div(pat, total_revenue),
        effective_tax_rate=_safe_div(tax, pbt),
    )


@dataclass(frozen=True, slots=True)
class BalanceSheet:
    """`07 Historical BS` for one fiscal year."""

    fiscal_year: int

    cash_and_bank: float
    current_investments: float
    trade_receivables: float
    inventories: float
    other_current_assets: float
    total_current_assets: float

    net_block_ppe: float
    cwip: float
    goodwill: float
    other_intangibles: float
    lt_investments_associates: float
    other_nca: float
    deferred_tax_asset: float
    total_non_current_assets: float
    total_assets: float

    trade_payables: float
    short_term_borrowings: float
    current_maturities_ltd: float
    other_current_liabilities: float
    short_term_provisions: float
    total_current_liabilities: float

    long_term_borrowings: float
    deferred_tax_liability: float
    other_ncl: float
    total_non_current_liabilities: float
    total_liabilities: float

    equity_share_capital: float
    reserves_surplus: float
    shareholders_equity: float
    minority_interest: float
    total_equity: float

    total_equity_and_liabilities: float
    gross_debt: float
    net_debt: float
    capital_employed: float
    invested_capital: float
    net_working_capital: float

    balance_check: float

    @property
    def balances(self) -> bool:
        """Workbook balance test, tolerant to rounding (₹0.01 cr)."""
        return abs(self.balance_check) < 0.01


def build_balance_sheet(fin: CanonicalFinancials, year: int) -> BalanceSheet:
    """Compute `07 Historical BS` for one year."""
    g = fin.at

    cash = g(LI.CASH_AND_BANK, year)
    cur_inv = g(LI.CURRENT_INVESTMENTS, year)
    recv = g(LI.TRADE_RECEIVABLES, year)
    inv = g(LI.INVENTORIES, year)
    oca = g(LI.OTHER_CURRENT_ASSETS, year)
    tca = cash + cur_inv + recv + inv + oca

    ppe = g(LI.NET_BLOCK_PPE, year)
    cwip = g(LI.CWIP, year)
    gw = g(LI.GOODWILL, year)
    intang = g(LI.OTHER_INTANGIBLES, year)
    lt_inv = g(LI.LT_INVESTMENTS_ASSOCIATES, year)
    onca = g(LI.OTHER_NCA, year)
    dta = g(LI.DEFERRED_TAX_ASSET, year)
    tnca = ppe + cwip + gw + intang + lt_inv + onca + dta
    total_assets = tca + tnca

    pay = g(LI.TRADE_PAYABLES, year)
    std = g(LI.SHORT_TERM_BORROWINGS, year)
    cmltd = g(LI.CURRENT_MATURITIES_LTD, year)
    ocl = g(LI.OTHER_CURRENT_LIABILITIES, year)
    stp = g(LI.SHORT_TERM_PROVISIONS, year)
    tcl = pay + std + cmltd + ocl + stp

    ltd = g(LI.LONG_TERM_BORROWINGS, year)
    dtl = g(LI.DEFERRED_TAX_LIABILITY, year)
    oncl = g(LI.OTHER_NCL, year)
    tncl = ltd + dtl + oncl
    total_liabilities = tcl + tncl

    share_cap = g(LI.EQUITY_SHARE_CAPITAL, year)
    reserves = g(LI.RESERVES_SURPLUS, year)
    equity = share_cap + reserves
    mi = g(LI.MINORITY_INTEREST_BS, year)
    total_equity = equity + mi

    gross_debt = std + cmltd + ltd
    net_debt = gross_debt - cash - cur_inv
    capital_employed = total_equity + gross_debt
    invested_capital = capital_employed - cash - cur_inv
    nwc = tca - tcl

    return BalanceSheet(
        fiscal_year=year,
        cash_and_bank=cash,
        current_investments=cur_inv,
        trade_receivables=recv,
        inventories=inv,
        other_current_assets=oca,
        total_current_assets=tca,
        net_block_ppe=ppe,
        cwip=cwip,
        goodwill=gw,
        other_intangibles=intang,
        lt_investments_associates=lt_inv,
        other_nca=onca,
        deferred_tax_asset=dta,
        total_non_current_assets=tnca,
        total_assets=total_assets,
        trade_payables=pay,
        short_term_borrowings=std,
        current_maturities_ltd=cmltd,
        other_current_liabilities=ocl,
        short_term_provisions=stp,
        total_current_liabilities=tcl,
        long_term_borrowings=ltd,
        deferred_tax_liability=dtl,
        other_ncl=oncl,
        total_non_current_liabilities=tncl,
        total_liabilities=total_liabilities,
        equity_share_capital=share_cap,
        reserves_surplus=reserves,
        shareholders_equity=equity,
        minority_interest=mi,
        total_equity=total_equity,
        total_equity_and_liabilities=total_liabilities + total_equity,
        gross_debt=gross_debt,
        net_debt=net_debt,
        capital_employed=capital_employed,
        invested_capital=invested_capital,
        net_working_capital=nwc,
        balance_check=total_assets - (total_liabilities + total_equity),
    )


@dataclass(frozen=True, slots=True)
class CashFlowStatement:
    """`08 Historical CF` for one fiscal year."""

    fiscal_year: int

    pat: float
    depreciation: float
    finance_costs: float
    other_noncash_adj: float
    operating_profit_before_wc: float

    chg_inventories: float
    chg_receivables: float
    chg_payables: float
    other_wc_movement: float
    working_capital_change: float

    direct_taxes_paid: float
    cfo: float

    capex: float
    sale_fixed_assets: float
    purchase_sale_investments: float
    other_investing: float
    cfi: float

    equity_issued_buyback: float
    proceeds_borrowings: float
    repayment_borrowings: float
    dividend_paid: float
    interest_paid: float
    other_financing: float
    cff: float

    net_cash_flow: float
    opening_cash: float
    closing_cash: float

    free_cash_flow: float
    fcf_to_equity: float
    cfo_to_pat: float | None


def build_cash_flow(fin: CanonicalFinancials, year: int) -> CashFlowStatement:
    """Compute `08 Historical CF` for one year."""
    g = fin.at
    income = build_income_statement(fin, year)

    pat = income.pat
    dep = income.depreciation
    fin_cost = income.finance_costs
    other_nc = g(LI.OTHER_NONCASH_ADJ, year)
    opbwc = pat + dep + fin_cost + other_nc

    d_inv = g(LI.CHG_INVENTORIES_CF, year)
    d_recv = g(LI.CHG_RECEIVABLES_CF, year)
    d_pay = g(LI.CHG_PAYABLES_CF, year)
    d_other = g(LI.OTHER_WC_MOVEMENT, year)
    wc_change = d_inv + d_recv + d_pay + d_other

    taxes = g(LI.DIRECT_TAXES_PAID, year)
    cfo = opbwc + wc_change - taxes

    capex = g(LI.CAPEX, year)
    sale_fa = g(LI.SALE_FIXED_ASSETS, year)
    inv_flow = g(LI.PURCHASE_SALE_INVESTMENTS, year)
    other_inv = g(LI.OTHER_INVESTING, year)
    cfi = -capex + sale_fa + inv_flow + other_inv

    equity_flow = g(LI.EQUITY_ISSUED_BUYBACK, year)
    borrow = g(LI.PROCEEDS_BORROWINGS, year)
    repay = g(LI.REPAYMENT_BORROWINGS, year)
    dividend = income.dividend_paid
    other_fin = g(LI.OTHER_FINANCING, year)
    cff = equity_flow + borrow - repay - dividend - fin_cost + other_fin

    net_cf = cfo + cfi + cff
    opening = g(LI.OPENING_CASH, year)

    return CashFlowStatement(
        fiscal_year=year,
        pat=pat,
        depreciation=dep,
        finance_costs=fin_cost,
        other_noncash_adj=other_nc,
        operating_profit_before_wc=opbwc,
        chg_inventories=d_inv,
        chg_receivables=d_recv,
        chg_payables=d_pay,
        other_wc_movement=d_other,
        working_capital_change=wc_change,
        direct_taxes_paid=taxes,
        cfo=cfo,
        capex=capex,
        sale_fixed_assets=sale_fa,
        purchase_sale_investments=inv_flow,
        other_investing=other_inv,
        cfi=cfi,
        equity_issued_buyback=equity_flow,
        proceeds_borrowings=borrow,
        repayment_borrowings=repay,
        dividend_paid=dividend,
        interest_paid=fin_cost,
        other_financing=other_fin,
        cff=cff,
        net_cash_flow=net_cf,
        opening_cash=opening,
        closing_cash=opening + net_cf,
        free_cash_flow=cfo - capex,
        fcf_to_equity=cfo - capex + borrow - repay,
        cfo_to_pat=_safe_div(cfo, pat),
    )
