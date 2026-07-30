"""Calibrate forecast assumptions from reported history.

Default assumptions are *derived*, never hard-coded. A company that has grown
at 14% with a 22% EBITDA margin and 55 receivable days starts there — not at
some generic 10%/18%/45 that happens to be written in the source.

Where history cannot support a driver (no debt, so no observable interest rate)
the calibrator falls back to a documented constant and marks the provenance as
``DEFAULT`` so the UI can show which assumptions are grounded and which are not.
"""
from __future__ import annotations

from app.domain.calc import DAYS_IN_YEAR, cagr, safe_div
from app.domain.financials.statements import (
    BalanceSheet, CashFlowStatement, IncomeStatement,
)
from app.domain.forecast.assumptions import (
    Driver, ForecastAssumptions, Provenance, RevenueMethod, Scenario,
)
from app.domain.forecast.engine import ForecastBase

# Fallbacks used only when history cannot support a driver. Each is a
# conservative long-run Indian-market norm, and every one is overridable.
FALLBACK = {
    "revenue_growth": 0.10,
    "terminal_revenue_growth": 0.05,
    "ebitda_margin": 0.15,
    "capex_pct_revenue": 0.05,
    "depreciation_rate": 0.11,
    "inventory_days": 60.0,
    "receivable_days": 45.0,
    "payable_days": 40.0,
    "other_ca_pct_revenue": 0.04,
    "other_cl_pct_revenue": 0.05,
    "interest_rate": 0.085,
    "cash_yield": 0.04,
    "effective_tax_rate": 0.25,
    "dividend_payout": 0.20,
    "wacc": 0.115,
    "terminal_growth": 0.05,
    "exit_ev_ebitda": 12.0,
    "target_pe": 20.0,
}

#: Number of trailing years used for averaged drivers.
LOOKBACK = 3


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _driver(value: float | None, key: str, source: Provenance, note: str | None = None) -> Driver:
    """Build a driver, falling back to a documented constant when needed."""
    if value is None:
        return Driver(value=FALLBACK[key], source=Provenance.DEFAULT,
                      note="No usable history; platform default applied.")
    return Driver(value=value, source=source, note=note)


class AssumptionCalibrator:
    """Derives a base-case assumption set from reported financials."""

    def __init__(
        self,
        incomes: list[IncomeStatement],
        balances: list[BalanceSheet],
        cash_flows: list[CashFlowStatement],
    ) -> None:
        self.incomes = incomes
        self.balances = balances
        self.cash_flows = cash_flows
        self.n = len(incomes)

    # ------------------------------------------------------------- helpers
    def _avg(self, i: int, attr: str) -> float | None:
        closing = getattr(self.balances[i], attr, None)
        opening = getattr(self.balances[i - 1], attr, None) if i > 0 else None
        if closing is None:
            return opening
        return closing if opening is None else (closing + opening) / 2

    def _recent(self) -> range:
        return range(max(0, self.n - LOOKBACK), self.n)

    # ------------------------------------------------------------- drivers
    def revenue_growth(self) -> float | None:
        if self.n < 2:
            return None
        window = min(5, self.n - 1)
        first = self.incomes[self.n - 1 - window].total_revenue
        last = self.incomes[-1].total_revenue
        return cagr(first, last, window)

    def ebitda_margin(self) -> float | None:
        return _mean([self.incomes[i].ebitda_margin for i in self._recent()])

    def tax_rate(self) -> float | None:
        rates = [
            r for i in self._recent()
            if (r := self.incomes[i].effective_tax_rate) is not None and 0 < r < 0.60
        ]
        return sum(rates) / len(rates) if rates else None

    def capex_pct(self) -> float | None:
        return _mean([
            safe_div(abs(self.cash_flows[i].capex), self.incomes[i].total_revenue)
            for i in self._recent()
        ])

    def depreciation_rate(self) -> float | None:
        """D&A over the opening net block — the rate the engine actually uses."""
        rates = []
        for i in self._recent():
            if i == 0:
                continue
            rate = safe_div(self.incomes[i].depreciation, self.balances[i - 1].net_block_ppe)
            if rate is not None and 0 < rate < 0.60:
                rates.append(rate)
        return sum(rates) / len(rates) if rates else None

    def cycle_days(self) -> tuple[float | None, float | None, float | None]:
        dio, dso, dpo = [], [], []
        for i in self._recent():
            inc = self.incomes[i]
            if (v := safe_div(self._avg(i, "inventories"), inc.total_cogs)) is not None:
                dio.append(v * DAYS_IN_YEAR)
            if (v := safe_div(self._avg(i, "trade_receivables"), inc.total_revenue)) is not None:
                dso.append(v * DAYS_IN_YEAR)
            if (v := safe_div(self._avg(i, "trade_payables"), inc.total_cogs)) is not None:
                dpo.append(v * DAYS_IN_YEAR)
        return (
            sum(dio) / len(dio) if dio else None,
            sum(dso) / len(dso) if dso else None,
            sum(dpo) / len(dpo) if dpo else None,
        )

    def other_wc_pcts(self) -> tuple[float | None, float | None]:
        ca = _mean([
            safe_div(self.balances[i].other_current_assets, self.incomes[i].total_revenue)
            for i in self._recent()
        ])
        cl = _mean([
            safe_div(
                self.balances[i].other_current_liabilities + self.balances[i].short_term_provisions,
                self.incomes[i].total_revenue,
            )
            for i in self._recent()
        ])
        return ca, cl

    def interest_rate(self) -> float | None:
        rates = []
        for i in self._recent():
            rate = safe_div(self.incomes[i].finance_costs, self._avg(i, "gross_debt"))
            if rate is not None and 0 < rate < 0.30:
                rates.append(rate)
        return sum(rates) / len(rates) if rates else None

    def dividend_payout(self) -> float | None:
        ratios = [
            r for i in self._recent()
            if (r := safe_div(self.incomes[i].dividend_paid, self.incomes[i].pat)) is not None
            and 0 <= r <= 1.0
        ]
        return sum(ratios) / len(ratios) if ratios else None

    def other_income_pct(self) -> float | None:
        return _mean([
            safe_div(self.incomes[i].other_income, self.incomes[i].total_revenue)
            for i in self._recent()
        ])

    # ---------------------------------------------------------------- build
    def base_position(self) -> ForecastBase:
        """The last reported position the forecast starts from."""
        inc, bal = self.incomes[-1], self.balances[-1]
        # Operating working capital, consistent with the Module 2 definition.
        nwc = (
            bal.inventories + bal.trade_receivables + bal.other_current_assets
            - bal.trade_payables - bal.other_current_liabilities - bal.short_term_provisions
        )
        return ForecastBase(
            fiscal_year=inc.fiscal_year,
            revenue=inc.total_revenue,
            ebitda=inc.ebitda,
            net_block=bal.net_block_ppe,
            net_working_capital=nwc,
            gross_debt=bal.gross_debt,
            cash=bal.cash_and_bank + bal.current_investments,
            shares_outstanding=inc.weighted_shares,
            equity=bal.shareholders_equity,
            invested_capital=bal.invested_capital,
        )

    def calibrate(
        self,
        years: int = 5,
        method: RevenueMethod = RevenueMethod.CAGR,
    ) -> ForecastAssumptions:
        """Produce a history-grounded base-case assumption set."""
        H = Provenance.HISTORICAL
        dio, dso, dpo = self.cycle_days()
        other_ca, other_cl = self.other_wc_pcts()
        growth = self.revenue_growth()

        # Split observed growth into volume and price so the volume/price
        # method starts somewhere defensible rather than at a guess.
        vol = price = None
        if growth is not None:
            price = min(0.05, max(0.0, growth * 0.35))
            vol = (1 + growth) / (1 + price) - 1

        return ForecastAssumptions(
            years=years,
            scenario=Scenario.BASE,
            revenue_method=method,
            revenue_growth=_driver(growth, "revenue_growth", H,
                                   f"{min(5, self.n - 1)}-year historical CAGR"),
            terminal_revenue_growth=Driver(
                value=FALLBACK["terminal_revenue_growth"], source=Provenance.DEFAULT,
                note="Long-run nominal GDP proxy."),
            growth_fade=Driver(value=0.5, source=Provenance.DEFAULT,
                               note="Half-fade toward the long-run rate."),
            volume_growth=_driver(vol, "revenue_growth", H, "Implied from historical growth"),
            price_growth=_driver(price, "revenue_growth", H, "Assumed realisation component"),
            organic_growth=_driver(growth, "revenue_growth", H, "All historical growth treated as organic"),
            acquisition_growth=Driver(value=0.0, source=Provenance.DEFAULT),
            ebitda_margin=_driver(self.ebitda_margin(), "ebitda_margin", H,
                                  f"{LOOKBACK}-year average"),
            margin_expansion=Driver(value=0.0, source=Provenance.DEFAULT,
                                    note="Flat margins unless an analyst assumes otherwise."),
            other_income_pct_revenue=_driver(self.other_income_pct(), "other_ca_pct_revenue", H),
            capex_pct_revenue=_driver(self.capex_pct(), "capex_pct_revenue", H,
                                      f"{LOOKBACK}-year average"),
            depreciation_rate=_driver(self.depreciation_rate(), "depreciation_rate", H,
                                      "D&A over opening net block"),
            inventory_days=_driver(dio, "inventory_days", H, "On COGS"),
            receivable_days=_driver(dso, "receivable_days", H, "On revenue"),
            payable_days=_driver(dpo, "payable_days", H, "On COGS"),
            other_ca_pct_revenue=_driver(other_ca, "other_ca_pct_revenue", H),
            other_cl_pct_revenue=_driver(other_cl, "other_cl_pct_revenue", H),
            interest_rate=_driver(self.interest_rate(), "interest_rate", H,
                                  "Finance costs over average gross debt"),
            effective_tax_rate=_driver(self.tax_rate(), "effective_tax_rate", H,
                                       f"{LOOKBACK}-year average"),
            dividend_payout=_driver(self.dividend_payout(), "dividend_payout", H),
        )
