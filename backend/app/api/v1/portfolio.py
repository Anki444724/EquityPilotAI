"""Portfolio, watchlist, alert and analytics endpoints.

    POST   /portfolios                          create
    GET    /portfolios                          list
    GET    /portfolios/capabilities             engine self-description
    GET    /portfolios/alerts                   alerts across every portfolio
    GET    /portfolios/{id}                     full resolved view
    PATCH  /portfolios/{id}                     update policy
    DELETE /portfolios/{id}                     delete
    GET    /portfolios/{id}/holdings            positions only
    POST   /portfolios/{id}/transactions        record a transaction
    GET    /portfolios/{id}/transactions        the ledger
    DELETE /portfolios/{id}/transactions/{txn}  remove a transaction
    GET    /portfolios/{id}/performance         TWR, MWR, rolling, drawdown
    GET    /portfolios/{id}/attribution         Brinson decomposition
    GET    /portfolios/{id}/allocation          one or all dimensions
    GET    /portfolios/{id}/risk                the risk profile
    GET    /portfolios/{id}/alerts              evaluate now
    POST   /portfolios/{id}/alerts/rules        override or define a rule
    POST   /portfolios/{id}/snapshots           freeze today's valuation
    GET    /portfolios/{id}/snapshots           the valuation history
    PUT    /portfolios/{id}/targets             set a target weight
    GET    /portfolios/{id}/commentary          AI portfolio commentary
    POST   /alerts/{id}/acknowledge             acknowledge an alert
    POST   /watchlists · GET /watchlists · …    watchlist CRUD

Literal paths precede `/{portfolio_id}` — FastAPI matches in declaration order,
so `/portfolios/capabilities` would otherwise route to the detail handler and
fail on the integer coercion.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.portfolio.alerts import ALL_RULES, AlertEngine, RATING_MAX_POSITION
from app.domain.portfolio.types import (
    AlertCategory, AlertSeverity, AllocationDimension, CostBasisMethod,
    PortfolioError, TransactionType,
)
from app.models.portfolio import AlertRuleOverride, Watchlist
from app.schemas.portfolio import (
    AlertEvaluationOut, AlertEventOut, AlertOverrideIn, AlertRuleOut,
    AlertSummaryOut, AllocationOut, AllocationSliceOut, AttributionOut,
    AttributionRowOut, CapabilitiesOut, CashOut, CommentaryCitationOut,
    CommentaryOut, CommentarySectionOut, HoldingOut, PerformanceOut,
    PortfolioCreate, PortfolioOut, PortfolioSummaryOut, PortfolioUpdate,
    PortfolioViewOut, RealisedTradeOut, RebalanceTradeOut, RiskOut,
    SeriesPoint, SnapshotOut, TargetIn, TargetOut, TransactionCreate,
    TransactionOut, WatchlistCreate, WatchlistEntryCreate, WatchlistOut,
    WatchlistRowOut, WatchlistUpdate,
)
from app.services.portfolio.commentary import CommentaryEngine
from app.services.portfolio.engine import PortfolioEngine, PortfolioView
from app.services.portfolio.service import PortfolioService

router = APIRouter(tags=["portfolio"])


def _service(db: Session = Depends(get_db)) -> PortfolioService:
    return PortfolioService(db)


def _owned(service: PortfolioService, portfolio_id: int, user: CurrentUser):
    portfolio = service.get_portfolio(portfolio_id)
    if portfolio is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "portfolio not found")
    if portfolio.owner_id != user.id:
        # 404 rather than 403: revealing that a portfolio exists but belongs to
        # someone else is itself a disclosure.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "portfolio not found")
    return portfolio


# ---------------------------------------------------------------------------
# Serialisation — one place, so no two endpoints can disagree
# ---------------------------------------------------------------------------
def _holding_out(holding) -> HoldingOut:
    position = holding.position
    return HoldingOut(
        ticker=holding.ticker, company_id=position.company_id,
        name=position.name or holding.ticker,
        sector=position.sector, industry=position.industry,
        quantity=position.quantity, average_cost=position.average_cost,
        cost=position.cost, current_price=position.current_price,
        market_value=position.market_value,
        unrealised_pnl=position.unrealised_pnl,
        unrealised_return=position.unrealised_return,
        realised_pnl=position.realised_pnl, dividends=position.dividends,
        total_pnl=position.total_pnl, weight=holding.weight,
        target_weight=holding.target_weight, drift=holding.drift,
        max_position_size=holding.max_position_size,
        is_oversized=holding.is_oversized, score=holding.score,
        rating=holding.rating, risk_score=holding.risk_score,
        intrinsic_value=holding.intrinsic_value,
        target_price=holding.target_price, upside=holding.upside,
        expected_cagr=holding.expected_cagr,
        liquidity_days=holding.liquidity_days,
        holding_days=position.holding_days, first_bought=position.first_bought,
    )


def _allocation_out(allocation) -> AllocationOut:
    return AllocationOut(
        dimension=allocation.dimension.value,
        slices=[
            AllocationSliceOut(
                key=s.key, label=s.label, market_value=s.market_value,
                weight=s.weight, position_count=s.position_count,
                target_weight=s.target_weight, drift=s.drift,
                unrealised_pnl=s.unrealised_pnl,
            )
            for s in allocation.slices
        ],
        unclassified_value=allocation.unclassified_value,
        herfindahl=round(allocation.herfindahl, 6),
        effective_count=round(allocation.effective_count, 4),
    )


def _risk_out(view: PortfolioView) -> RiskOut:
    profile = view.risk
    if profile is None:
        return RiskOut(observations=0, unavailable=["No risk profile computed"])
    return RiskOut(**{
        field: getattr(profile, field)
        for field in RiskOut.model_fields
        if hasattr(profile, field)
    })


def _performance_out(view: PortfolioView, service: PortfolioService) -> PerformanceOut:
    engine = PortfolioEngine()
    window = min(90, max(2, len(view.series) // 3))
    return PerformanceOut(
        twr=view.twr, twr_annualised=view.twr_annualised, mwr=view.mwr,
        benchmark_return=view.benchmark_return,
        active_return=view.active_return,
        series=[
            SeriesPoint(as_of=p.as_of, value=p.value, net_flow=p.net_flow)
            for p in view.series
        ],
        rolling=[
            {"as_of": when.isoformat(), "value": value}
            for when, value in engine.rolling(view, window)
        ],
        underwater=[
            {"as_of": when.isoformat(), "value": value}
            for when, value in engine.underwater(view)
        ],
        contributions=[
            {
                "ticker": row.ticker, "name": row.name, "weight": row.weight,
                "position_return": row.position_return,
                "contribution": row.contribution,
            }
            for row in view.contributions
        ],
    )


def _view_out(view: PortfolioView, service: PortfolioService) -> PortfolioViewOut:
    return PortfolioViewOut(
        summary=PortfolioSummaryOut(
            portfolio_id=view.portfolio_id, name=view.name,
            benchmark=view.benchmark, as_of=view.as_of,
            market_value=view.market_value, cost_basis=view.cost_basis,
            cash=view.cash.balance, total_value=view.total_value,
            unrealised_pnl=view.unrealised_pnl, realised_pnl=view.realised_pnl,
            dividends=view.dividends, total_pnl=view.total_pnl,
            total_return=view.total_return, position_count=view.position_count,
            cash_weight=view.cash_weight, unpriced=view.unpriced,
            analytics_errors=view.analytics_errors,
        ),
        holdings=[_holding_out(h) for h in view.holdings],
        cash=CashOut(
            balance=view.cash.balance, deposits=view.cash.deposits,
            withdrawals=view.cash.withdrawals, buys=view.cash.buys,
            sells=view.cash.sells, dividends=view.cash.dividends,
            fees=view.cash.fees, taxes=view.cash.taxes,
            interest=view.cash.interest, net_invested=view.cash.net_invested,
        ),
        allocations={k: _allocation_out(a) for k, a in view.allocations.items()},
        risk=_risk_out(view),
        performance=_performance_out(view, service),
        rebalance=[
            RebalanceTradeOut(
                ticker=t.ticker, name=t.name, action=t.action.value,
                current_weight=t.current_weight, target_weight=t.target_weight,
                drift=t.drift, value_delta=t.value_delta, shares=t.shares,
                reason=t.reason,
            )
            for t in view.rebalance
        ],
        realised=[
            RealisedTradeOut(
                ticker=t.ticker, sell_date=t.sell_date, buy_date=t.buy_date,
                quantity=t.quantity, cost_per_unit=t.cost_per_unit,
                sale_per_unit=t.sale_per_unit, cost=t.cost,
                proceeds=t.proceeds, pnl=t.pnl, return_pct=t.return_pct,
                holding_days=t.holding_days, is_long_term=t.is_long_term,
            )
            for t in view.realised
        ],
        metrics=view.portfolio_metrics(),
    )


# ===========================================================================
# Portfolios
# ===========================================================================
@router.post(
    "/portfolios", response_model=PortfolioOut,
    status_code=status.HTTP_201_CREATED,
)
def create_portfolio(
    payload: PortfolioCreate,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> PortfolioOut:
    try:
        portfolio = service.create_portfolio(
            user.id, payload.name,
            **payload.model_dump(exclude={"name"}, exclude_none=True),
        )
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return PortfolioOut.model_validate(portfolio)


@router.get("/portfolios", response_model=list[PortfolioOut])
def list_portfolios(
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[PortfolioOut]:
    return [
        PortfolioOut.model_validate(p) for p in service.list_portfolios(user.id)
    ]


@router.get("/portfolios/capabilities", response_model=CapabilitiesOut)
def capabilities(
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> CapabilitiesOut:
    """Engine self-description, derived from the registries."""
    return CapabilitiesOut(
        transaction_types=[t.value for t in TransactionType],
        allocation_dimensions=[d.value for d in AllocationDimension],
        alert_categories=[c.value for c in AlertCategory],
        alert_severities=[s.value for s in AlertSeverity],
        cost_basis_methods=[m.value for m in CostBasisMethod],
        rules=[
            AlertRuleOut(
                key=r.key, label=r.label, condition=r.condition,
                metric=r.metric, comparator=r.comparator.value,
                threshold=(
                    None if isinstance(r.threshold, frozenset)
                    else r.threshold
                ),
                severity=r.severity.value, category=r.category.value,
                action=r.action, scope=r.scope, enabled=r.enabled,
            )
            for r in ALL_RULES
        ],
        rating_position_limits=RATING_MAX_POSITION,
        cache=service.cache_stats(),
    )


@router.get("/portfolios/alerts", response_model=list[AlertEventOut])
def all_alerts(
    alert_status: str | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[AlertEventOut]:
    """Stored alerts across every portfolio this user owns."""
    return [
        AlertEventOut.model_validate(a)
        for a in service.alerts(
            owner_id=user.id, status=alert_status, severity=severity
        )
    ]


@router.get("/portfolios/{portfolio_id}", response_model=PortfolioViewOut)
def get_portfolio(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> PortfolioViewOut:
    _owned(service, portfolio_id, user)
    return _view_out(service.view(portfolio_id), service)


@router.patch("/portfolios/{portfolio_id}", response_model=PortfolioOut)
def update_portfolio(
    portfolio_id: int,
    payload: PortfolioUpdate,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> PortfolioOut:
    _owned(service, portfolio_id, user)
    portfolio = service.update_portfolio(
        portfolio_id, **payload.model_dump(exclude_none=True)
    )
    return PortfolioOut.model_validate(portfolio)


@router.delete(
    "/portfolios/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_portfolio(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    _owned(service, portfolio_id, user)
    service.delete_portfolio(portfolio_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/portfolios/{portfolio_id}/holdings", response_model=list[HoldingOut])
def holdings(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[HoldingOut]:
    _owned(service, portfolio_id, user)
    return [_holding_out(h) for h in service.view(portfolio_id).holdings]


# ===========================================================================
# Transactions
# ===========================================================================
@router.post(
    "/portfolios/{portfolio_id}/transactions", response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
)
def add_transaction(
    portfolio_id: int,
    payload: TransactionCreate,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> TransactionOut:
    _owned(service, portfolio_id, user)
    try:
        txn = service.add_transaction(
            portfolio_id, **{**payload.model_dump(), "txn_type": payload.txn_type.value}
        )
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return TransactionOut.model_validate(txn)


@router.get(
    "/portfolios/{portfolio_id}/transactions", response_model=list[TransactionOut]
)
def list_transactions(
    portfolio_id: int,
    ticker: str | None = Query(default=None),
    txn_type: TransactionType | None = Query(default=None),
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[TransactionOut]:
    _owned(service, portfolio_id, user)
    return [
        TransactionOut.model_validate(t)
        for t in service.transactions(
            portfolio_id, ticker=ticker,
            txn_type=txn_type.value if txn_type else None,
        )
    ]


@router.delete(
    "/portfolios/{portfolio_id}/transactions/{txn_id}",
    status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
)
def delete_transaction(
    portfolio_id: int,
    txn_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    _owned(service, portfolio_id, user)
    try:
        service.delete_transaction(portfolio_id, txn_id)
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Analytics
# ===========================================================================
@router.get("/portfolios/{portfolio_id}/performance", response_model=PerformanceOut)
def performance(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> PerformanceOut:
    _owned(service, portfolio_id, user)
    return _performance_out(service.view(portfolio_id), service)


@router.get("/portfolios/{portfolio_id}/risk", response_model=RiskOut)
def risk(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> RiskOut:
    _owned(service, portfolio_id, user)
    return _risk_out(service.view(portfolio_id))


@router.get("/portfolios/{portfolio_id}/allocation")
def allocation(
    portfolio_id: int,
    dimension: AllocationDimension | None = Query(default=None),
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    _owned(service, portfolio_id, user)
    allocations = service.view(portfolio_id).allocations
    if dimension is not None:
        found = allocations.get(dimension.value)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "dimension not found")
        return _allocation_out(found)
    return {k: _allocation_out(a) for k, a in allocations.items()}


@router.get("/portfolios/{portfolio_id}/attribution", response_model=AttributionOut)
def attribution(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> AttributionOut:
    """Brinson-Fachler attribution against equal-weighted sector benchmarks.

    With no external benchmark constituent data, the benchmark is modelled as
    an equal weight across the sectors the portfolio actually holds, each
    earning the portfolio's own average return. That makes *allocation* the
    meaningful term and selection near zero, and it is stated here rather than
    presented as a market comparison it is not.
    """
    _owned(service, portfolio_id, user)
    view = service.view(portfolio_id)
    sector = view.allocations.get(AllocationDimension.SECTOR.value)
    if sector is None or not sector.slices:
        return AttributionOut(
            rows=[], portfolio_return=0.0, benchmark_return=0.0,
            active_return=0.0, total_allocation=0.0, total_selection=0.0,
            total_interaction=0.0, residual=0.0,
        )

    equal = 1.0 / len(sector.slices)
    average = view.total_return or 0.0
    result = PortfolioEngine.sector_attribution(
        view,
        {s.key: equal for s in sector.slices},
        {s.key: average for s in sector.slices},
    )
    return AttributionOut(
        rows=[
            AttributionRowOut(
                key=r.key, label=r.label,
                portfolio_weight=r.portfolio_weight,
                benchmark_weight=r.benchmark_weight,
                active_weight=r.active_weight,
                portfolio_return=r.portfolio_return,
                benchmark_return=r.benchmark_return,
                allocation=r.allocation, selection=r.selection,
                interaction=r.interaction, total=r.total,
            )
            for r in result.rows
        ],
        portfolio_return=result.portfolio_return,
        benchmark_return=result.benchmark_return,
        active_return=result.active_return,
        total_allocation=result.total_allocation,
        total_selection=result.total_selection,
        total_interaction=result.total_interaction,
        residual=result.residual,
    )


# ===========================================================================
# Alerts
# ===========================================================================
@router.get("/portfolios/{portfolio_id}/alerts", response_model=AlertSummaryOut)
def evaluate_alerts(
    portfolio_id: int,
    triggered_only: bool = Query(default=False),
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> AlertSummaryOut:
    _owned(service, portfolio_id, user)
    evaluations = service.evaluate_alerts(portfolio_id)
    counts = AlertEngine.summarise(evaluations)
    if triggered_only:
        evaluations = [e for e in evaluations if e.is_triggered]
    return AlertSummaryOut(
        counts=counts,
        evaluations=[
            AlertEvaluationOut(
                key=e.key, label=e.label, category=e.category.value,
                severity=e.severity.value, status=e.status.value,
                condition=e.condition, action=e.action, observed=e.observed,
                threshold=(
                    None if isinstance(e.threshold, (set, frozenset))
                    else e.threshold
                ),
                ticker=e.ticker, company_id=e.company_id, detail=e.detail,
            )
            for e in evaluations
        ],
    )


@router.post("/portfolios/{portfolio_id}/alerts/rules", response_model=AlertRuleOut)
def override_rule(
    portfolio_id: int,
    payload: AlertOverrideIn,
    db: Session = Depends(get_db),
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> AlertRuleOut:
    """Change a built-in rule's threshold or severity, or define a new one."""
    _owned(service, portfolio_id, user)
    from sqlalchemy import select

    existing = db.scalar(
        select(AlertRuleOverride).where(
            AlertRuleOverride.owner_id == user.id,
            AlertRuleOverride.portfolio_id == portfolio_id,
            AlertRuleOverride.rule_key == payload.rule_key,
        )
    )
    override = existing or AlertRuleOverride(
        owner_id=user.id, portfolio_id=portfolio_id, rule_key=payload.rule_key
    )
    for field, value in payload.model_dump(exclude={"rule_key"}).items():
        if value is not None:
            setattr(override, field, value.value if hasattr(value, "value") else value)
    if existing is None:
        db.add(override)
    db.commit()
    service.cache.invalidate(portfolio_id)

    resolved = next(
        (r for r in service.rules_for(user.id, portfolio_id)
         if r.key == payload.rule_key), None,
    )
    if resolved is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown rule")
    return AlertRuleOut(
        key=resolved.key, label=resolved.label, condition=resolved.condition,
        metric=resolved.metric, comparator=resolved.comparator.value,
        threshold=(
            None if isinstance(resolved.threshold, frozenset)
            else resolved.threshold
        ),
        severity=resolved.severity.value, category=resolved.category.value,
        action=resolved.action, scope=resolved.scope, enabled=resolved.enabled,
    )


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertEventOut)
def acknowledge(
    alert_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> AlertEventOut:
    try:
        return AlertEventOut.model_validate(service.acknowledge(alert_id))
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


# ===========================================================================
# Snapshots and targets
# ===========================================================================
@router.post(
    "/portfolios/{portfolio_id}/snapshots", response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def record_snapshot(
    portfolio_id: int,
    as_of: date | None = Query(default=None),
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> SnapshotOut:
    _owned(service, portfolio_id, user)
    return SnapshotOut.model_validate(service.record_snapshot(portfolio_id, as_of))


@router.get("/portfolios/{portfolio_id}/snapshots", response_model=list[SnapshotOut])
def list_snapshots(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[SnapshotOut]:
    _owned(service, portfolio_id, user)
    return [SnapshotOut.model_validate(s) for s in service.snapshots(portfolio_id)]


@router.put("/portfolios/{portfolio_id}/targets", response_model=TargetOut)
def set_target(
    portfolio_id: int,
    payload: TargetIn,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> TargetOut:
    _owned(service, portfolio_id, user)
    target = service.set_target(
        portfolio_id, payload.dimension.value, payload.bucket_key,
        payload.target_weight,
    )
    return TargetOut.model_validate(target)


# ===========================================================================
# AI commentary
# ===========================================================================
@router.get("/portfolios/{portfolio_id}/commentary", response_model=CommentaryOut)
def commentary(
    portfolio_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> CommentaryOut:
    """Grounded commentary. Every figure comes from the resolved view."""
    _owned(service, portfolio_id, user)
    view = service.view(portfolio_id)
    evaluations = service.evaluate_alerts(portfolio_id, persist=False)
    result = CommentaryEngine().generate(view, evaluations)
    return CommentaryOut(
        portfolio_id=result.portfolio_id, provider=result.provider,
        sections=[
            CommentarySectionOut(key=s.key, title=s.title, body=s.body)
            for s in result.sections
        ],
        citations=[
            CommentaryCitationOut(
                key=c.key, label=c.label, kind=c.kind.value,
                value=c.value, unit=c.unit, source=c.source,
            )
            for c in result.citations
        ],
        disclosure=result.disclosure,
    )


# ===========================================================================
# Watchlists
# ===========================================================================
@router.post(
    "/watchlists", response_model=WatchlistOut,
    status_code=status.HTTP_201_CREATED,
)
def create_watchlist(
    payload: WatchlistCreate,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> WatchlistOut:
    try:
        watchlist = service.create_watchlist(
            user.id, payload.name, description=payload.description
        )
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return WatchlistOut.model_validate(watchlist)


@router.get("/watchlists", response_model=list[WatchlistOut])
def list_watchlists(
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[WatchlistOut]:
    return [
        WatchlistOut.model_validate(w) for w in service.list_watchlists(user.id)
    ]


@router.get("/watchlists/{watchlist_id}", response_model=list[WatchlistRowOut])
def watchlist_rows(
    watchlist_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[WatchlistRowOut]:
    try:
        return [WatchlistRowOut(**row) for row in service.watchlist_view(watchlist_id)]
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.post(
    "/watchlists/{watchlist_id}/entries", response_model=WatchlistRowOut,
    status_code=status.HTTP_201_CREATED,
)
def add_watchlist_entry(
    watchlist_id: int,
    payload: WatchlistEntryCreate,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> WatchlistRowOut:
    try:
        entry = service.add_to_watchlist(
            watchlist_id, payload.ticker,
            **payload.model_dump(exclude={"ticker"}, exclude_none=True),
        )
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    row = next(
        r for r in service.watchlist_view(watchlist_id) if r["id"] == entry.id
    )
    return WatchlistRowOut(**row)


@router.delete(
    "/watchlists/{watchlist_id}/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
)
def remove_watchlist_entry(
    watchlist_id: int,
    entry_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    try:
        service.remove_from_watchlist(watchlist_id, entry_id)
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(\"/watchlists/{watchlist_id}\", response_model=WatchlistOut)
def update_watchlist(
    watchlist_id: int,
    payload: WatchlistUpdate,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> WatchlistOut:
    try:
        watchlist = service.update_watchlist(
            watchlist_id, **payload.model_dump(exclude_none=True)
        )
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return WatchlistOut.model_validate(watchlist)


@router.delete(
    \"/watchlists/{watchlist_id}\",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_watchlist(
    watchlist_id: int,
    service: PortfolioService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    try:
        service.delete_watchlist(watchlist_id)
    except PortfolioError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
