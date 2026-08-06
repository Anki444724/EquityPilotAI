"""Portfolio engine — the single computation path.

Everything the API reports about a portfolio is produced here exactly once:
positions, valuation, allocations, risk, performance and rebalancing. The API
layer selects and serialises; it never computes, and neither does the frontend.

`PortfolioView` is deliberately a single object built by one pass. Assembling
positions in one endpoint and re-assembling them in another is how two screens
end up disagreeing about the same portfolio's weight in a holding — the
"single-resolution rule" that has governed every module since Module 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from app.domain.calc import safe_div
from app.domain.portfolio import allocation as alloc
from app.domain.portfolio import risk as risk_lib
from app.domain.portfolio.alerts import max_position_for_rating
from app.domain.portfolio.performance import (
    Attribution, CashFlow, ContributionRow, brinson_attribution,
    contribution_analysis, drawdown_series, money_weighted_return,
    rolling_returns, time_weighted_return, annualise,
)
from app.domain.portfolio.positions import PositionEngine, ReplayResult, enrich
from app.domain.portfolio.types import (
    Allocation, AllocationDimension, CashLedger, CostBasisMethod, Position,
    RealisedTrade, RebalanceAction, ReturnPoint, StyleBucket,
)

#: Drift beyond which a rebalancing trade is proposed. Below this the trading
#: cost of correcting the drift exceeds the benefit of correcting it.
REBALANCE_BAND = 0.02


@dataclass(slots=True)
class HoldingView:
    """A position enriched with everything the platform knows about it."""

    position: Position
    weight: float = 0.0
    target_weight: float | None = None
    score: float | None = None
    rating: str | None = None
    risk_score: float | None = None
    intrinsic_value: float | None = None
    target_price: float | None = None
    expected_cagr: float | None = None
    liquidity_days: float | None = None
    max_position_size: float = 0.04
    #: Provenance of the displayed price, from the shared LiveMarketService.
    price_source: str | None = None
    last_updated: str | None = None
    market_status: str | None = None

    @property
    def ticker(self) -> str:
        return self.position.ticker

    @property
    def upside(self) -> float | None:
        price = self.position.current_price
        if self.target_price is None or not price:
            return None
        return self.target_price / price - 1.0

    @property
    def drift(self) -> float | None:
        return None if self.target_weight is None else self.weight - self.target_weight

    @property
    def is_oversized(self) -> bool:
        return self.weight > self.max_position_size

    @property
    def contribution(self) -> float | None:
        """This holding's share of total portfolio P&L, in rupees."""
        return self.position.unrealised_pnl


@dataclass(slots=True)
class RebalanceTrade:
    """A proposed trade to close a drift."""

    ticker: str
    name: str
    action: RebalanceAction
    current_weight: float
    target_weight: float
    drift: float
    value_delta: float
    shares: float | None = None
    reason: str = ""


@dataclass(slots=True)
class PortfolioView:
    """One complete, internally consistent picture of a portfolio."""

    portfolio_id: int
    name: str
    benchmark: str
    as_of: date

    holdings: list[HoldingView] = field(default_factory=list)
    closed: list[Position] = field(default_factory=list)
    realised: list[RealisedTrade] = field(default_factory=list)
    cash: CashLedger = field(default_factory=CashLedger)

    market_value: float = 0.0
    cost_basis: float = 0.0
    #: Holdings whose price is unknown, so their value is genuinely not zero
    #: but unmeasurable. Reported rather than silently valued at nil.
    unpriced: list[str] = field(default_factory=list)

    allocations: dict[str, Allocation] = field(default_factory=dict)
    risk: risk_lib.RiskProfile | None = None
    attribution: Attribution | None = None
    contributions: list[ContributionRow] = field(default_factory=list)
    rebalance: list[RebalanceTrade] = field(default_factory=list)

    analytics_errors: dict[str, str] = field(default_factory=dict)
    #: Raw per-ticker analytics, retained so the alert engine reads the same
    #: numbers the view was built from rather than resolving them again.
    analytics: dict[str, dict] = field(default_factory=dict)

    twr: float | None = None
    twr_annualised: float | None = None
    mwr: float | None = None
    benchmark_return: float | None = None
    series: list[ReturnPoint] = field(default_factory=list)

    # -- headline figures ----------------------------------------------
    @property
    def total_value(self) -> float:
        return self.market_value + self.cash.balance

    @property
    def unrealised_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def realised_pnl(self) -> float:
        return sum(t.pnl for t in self.realised)

    @property
    def dividends(self) -> float:
        return sum(h.position.dividends for h in self.holdings) + sum(
            p.dividends for p in self.closed
        )

    @property
    def total_pnl(self) -> float:
        return self.unrealised_pnl + self.realised_pnl + self.dividends

    @property
    def total_return(self) -> float | None:
        return safe_div(self.unrealised_pnl, self.cost_basis)

    @property
    def position_count(self) -> int:
        return len(self.holdings)

    @property
    def weights(self) -> list[float]:
        return [h.weight for h in self.holdings]

    @property
    def cash_weight(self) -> float | None:
        return safe_div(self.cash.balance, self.total_value)

    @property
    def largest_position(self) -> HoldingView | None:
        return max(self.holdings, key=lambda h: h.weight, default=None)

    @property
    def active_return(self) -> float | None:
        if self.twr is None or self.benchmark_return is None:
            return None
        return self.twr - self.benchmark_return

    def portfolio_metrics(self) -> dict[str, float | None]:
        """The metric bag the portfolio-scoped alert rules read."""
        sector = self.allocations.get(AllocationDimension.SECTOR.value)
        weights = self.weights
        scored = [
            (h.weight, h.risk_score) for h in self.holdings if h.risk_score is not None
        ]
        risk_weight = sum(w for w, _ in scored)
        return {
            "portfolio_risk_score": (
                sum(w * s for w, s in scored) / risk_weight if risk_weight else None
            ),
            "diversification_score": (
                risk_lib.diversification_score(weights) if weights else None
            ),
            "largest_sector_weight": (
                sector.largest.weight if sector and sector.largest else None
            ),
            "top_5_concentration": (
                risk_lib.top_n_concentration(weights, 5) if weights else None
            ),
            "effective_positions": (
                risk_lib.effective_positions(weights) if weights else None
            ),
            "max_drawdown": self.risk.max_drawdown if self.risk else None,
            "cash_weight": self.cash_weight,
        }


class PortfolioEngine:
    """Builds a `PortfolioView`. Stateless; one instance may serve many books."""

    def __init__(self, cost_basis: CostBasisMethod = CostBasisMethod.FIFO) -> None:
        self.positions = PositionEngine(cost_basis)

    # ------------------------------------------------------------------
    def build(
        self,
        *,
        portfolio_id: int,
        name: str,
        benchmark: str,
        transactions: Sequence,
        prices: Mapping[str, float],
        company_meta: Mapping[str, dict] | None = None,
        analytics: Mapping[str, dict] | None = None,
        targets: Mapping[str, dict[str, float]] | None = None,
        snapshots: Sequence = (),
        benchmark_levels: Sequence[float] = (),
        style_inputs: Mapping[str, alloc.StyleInputs] | None = None,
        risk_free: float = 0.07,
        as_of: date | None = None,
        max_position_size: float = 0.10,
    ) -> PortfolioView:
        replay = self.positions.replay(transactions)
        enrich(list(replay.positions.values()), dict(prices), dict(company_meta or {}))

        view = PortfolioView(
            portfolio_id=portfolio_id, name=name, benchmark=benchmark,
            as_of=as_of or date.today(),
            closed=replay.closed_positions, realised=replay.realised,
            cash=replay.cash,
        )

        open_positions = replay.open_positions
        view.unpriced = sorted(
            p.ticker for p in open_positions if p.current_price is None
        )
        view.market_value = sum(
            p.market_value or 0.0 for p in open_positions
        )
        view.cost_basis = sum(p.cost for p in open_positions)

        view.holdings = self._holdings(
            open_positions, view.market_value, analytics or {},
            (targets or {}).get("position", {}), max_position_size,
        )
        view.allocations = self._allocations(
            open_positions, targets or {}, style_inputs or {},
        )
        view.contributions = contribution_analysis([
            (h.ticker, h.position.name or h.ticker, h.weight,
             h.position.unrealised_return or 0.0)
            for h in view.holdings
        ])
        view.rebalance = self._rebalance(view.holdings, view.market_value)
        view.analytics = dict(analytics or {})
        self._performance(view, snapshots, benchmark_levels, risk_free)
        return view

    # ------------------------------------------------------------------
    @staticmethod
    def _holdings(
        positions: Sequence[Position],
        market_value: float,
        analytics: Mapping[str, dict],
        position_targets: Mapping[str, float],
        policy_max: float,
    ) -> list[HoldingView]:
        out: list[HoldingView] = []
        for position in positions:
            details = analytics.get(position.ticker, {})
            rating = details.get("rating")
            # The tighter of the portfolio's blanket policy and the cap implied
            # by the holding's own institutional rating. A AAA name may still
            # be limited by house policy; a BB name is limited by its quality
            # whatever the policy allows.
            cap = min(policy_max, max_position_for_rating(rating))
            out.append(HoldingView(
                position=position,
                weight=round(safe_div(position.market_value, market_value) or 0.0, 6),
                target_weight=position_targets.get(position.ticker),
                score=details.get("score"),
                rating=rating,
                risk_score=details.get("risk_score"),
                intrinsic_value=details.get("intrinsic_value"),
                target_price=details.get("target_price"),
                expected_cagr=details.get("expected_cagr"),
                liquidity_days=details.get("liquidity_days"),
                max_position_size=cap,
            ))
        out.sort(key=lambda h: -h.weight)
        return out

    @staticmethod
    def _allocations(
        positions: Sequence[Position],
        targets: Mapping[str, dict[str, float]],
        style_inputs: Mapping[str, alloc.StyleInputs],
    ) -> dict[str, Allocation]:
        return {
            AllocationDimension.SECTOR.value: alloc.by_sector(
                positions, targets.get(AllocationDimension.SECTOR.value)),
            AllocationDimension.INDUSTRY.value: alloc.by_industry(
                positions, targets.get(AllocationDimension.INDUSTRY.value)),
            AllocationDimension.MARKET_CAP.value: alloc.by_market_cap(
                positions, targets.get(AllocationDimension.MARKET_CAP.value)),
            AllocationDimension.COUNTRY.value: alloc.by_country(
                positions, targets.get(AllocationDimension.COUNTRY.value)),
            AllocationDimension.STYLE.value: alloc.by_style(
                positions, dict(style_inputs),
                targets.get(AllocationDimension.STYLE.value)),
        }

    @staticmethod
    def _rebalance(
        holdings: Sequence[HoldingView], market_value: float
    ) -> list[RebalanceTrade]:
        """Propose trades where drift exceeds the band, or policy is breached.

        Two independent triggers. A target drift is a preference; an oversized
        position relative to its rating-implied cap is a policy breach, and is
        raised even when no target weight was ever set.
        """
        trades: list[RebalanceTrade] = []
        for holding in holdings:
            price = holding.position.current_price
            name = holding.position.name or holding.ticker

            if holding.is_oversized:
                delta = (holding.max_position_size - holding.weight) * market_value
                trades.append(RebalanceTrade(
                    ticker=holding.ticker, name=name, action=RebalanceAction.REDUCE,
                    current_weight=holding.weight,
                    target_weight=holding.max_position_size,
                    drift=holding.weight - holding.max_position_size,
                    value_delta=round(delta, 2),
                    shares=round(delta / price, 2) if price else None,
                    reason=(
                        f"Weight {holding.weight:.1%} exceeds the "
                        f"{holding.max_position_size:.1%} cap for a "
                        f"{holding.rating or 'unrated'} holding"
                    ),
                ))
                continue

            drift = holding.drift
            if drift is None or abs(drift) < REBALANCE_BAND:
                continue
            delta = -drift * market_value
            trades.append(RebalanceTrade(
                ticker=holding.ticker, name=name,
                action=RebalanceAction.REDUCE if drift > 0 else RebalanceAction.INCREASE,
                current_weight=holding.weight,
                target_weight=holding.target_weight or 0.0,
                drift=drift, value_delta=round(delta, 2),
                shares=round(delta / price, 2) if price else None,
                reason=(
                    f"Drift of {drift:+.1%} against a "
                    f"{(holding.target_weight or 0):.1%} target"
                ),
            ))
        trades.sort(key=lambda t: -abs(t.value_delta))
        return trades

    @staticmethod
    def _performance(
        view: PortfolioView,
        snapshots: Sequence,
        benchmark_levels: Sequence[float],
        risk_free: float,
    ) -> None:
        """Attach return and risk measures, where history supports them."""
        points = [
            ReturnPoint(s.as_of, s.market_value + s.cash, s.net_flow)
            for s in sorted(snapshots, key=lambda s: s.as_of)
        ]
        view.series = points
        if len(points) < 2:
            view.risk = risk_lib.build_risk_profile(
                [p.value for p in points], None, view.weights,
                risk_free=risk_free,
            )
            if view.risk.unavailable is not None:
                view.risk.unavailable.insert(
                    0,
                    "Return history needs at least two valuation snapshots; "
                    f"{len(points)} recorded",
                )
            return

        values = [p.value for p in points]
        view.twr = time_weighted_return(points)
        view.twr_annualised = annualise(
            view.twr, (points[-1].as_of - points[0].as_of).days
        )

        flows = [
            CashFlow(p.as_of, -p.net_flow) for p in points if p.net_flow
        ]
        # The terminal value is the notional liquidation that closes the IRR.
        flows.append(CashFlow(points[-1].as_of, points[-1].value))
        if points[0].net_flow == 0 and points[0].value:
            flows.insert(0, CashFlow(points[0].as_of, -points[0].value))
        view.mwr = money_weighted_return(flows)

        levels = list(benchmark_levels)
        if len(levels) >= 2 and levels[0]:
            view.benchmark_return = levels[-1] / levels[0] - 1.0

        illiquid = sum(
            1 for h in view.holdings
            if h.liquidity_days is not None and h.liquidity_days > 30
        )
        view.risk = risk_lib.build_risk_profile(
            values, levels or None, view.weights,
            risk_free=risk_free, illiquid_count=illiquid,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def sector_attribution(
        view: PortfolioView, benchmark_weights: Mapping[str, float],
        benchmark_returns: Mapping[str, float],
    ) -> Attribution:
        """Brinson attribution of the portfolio against benchmark sectors."""
        sector = view.allocations.get(AllocationDimension.SECTOR.value)
        if sector is None:
            return Attribution()

        returns_by_sector: dict[str, tuple[float, float]] = {}
        for holding in view.holdings:
            key = holding.position.sector or alloc.UNCLASSIFIED
            value = holding.position.market_value or 0.0
            ret = holding.position.unrealised_return or 0.0
            weighted, total = returns_by_sector.get(key, (0.0, 0.0))
            returns_by_sector[key] = (weighted + ret * value, total + value)

        segments = []
        for slice_ in sector.slices:
            weighted, total = returns_by_sector.get(slice_.key, (0.0, 0.0))
            segments.append((
                slice_.key, slice_.label, slice_.weight,
                benchmark_weights.get(slice_.key, 0.0),
                (weighted / total) if total else 0.0,
                benchmark_returns.get(slice_.key, 0.0),
            ))
        return brinson_attribution(segments)

    @staticmethod
    def rolling(view: PortfolioView, window: int) -> list[tuple[date, float]]:
        return rolling_returns(view.series, window)

    @staticmethod
    def underwater(view: PortfolioView) -> list[tuple[date, float]]:
        return drawdown_series(view.series)
