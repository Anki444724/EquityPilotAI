"""Portfolio service — persistence, orchestration and caching.

The only module that knows both the engines and the database. It resolves each
portfolio exactly once per request into a `PortfolioView`, caches that view
against a key derived from the data that produced it, and serves every
endpoint from it.

Cache invalidation is by **content, not clock**: the key includes the
transaction count and the highest transaction id, so a new trade invalidates
immediately and a quiet portfolio is never recomputed. A TTL alone would serve
a stale book for its duration; a version counter alone would miss a deletion.
Both are combined.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.portfolio.alerts import (
    ALL_RULES, AlertEngine, AlertRule, RULES_BY_KEY, build_position_metrics,
)
from app.domain.portfolio.allocation import StyleInputs
from app.domain.portfolio.risk import liquidity_days
from app.domain.portfolio.types import (
    AlertCategory, AlertEvaluation, AlertSeverity, AlertStatus, Comparator,
    CostBasisMethod, PortfolioError, WatchStatus,
)
from app.models.company import Company
from app.models.portfolio import (
    AlertEvent, AlertRuleOverride, AllocationTarget, BenchmarkLevel, Portfolio,
    PortfolioSnapshot, PortfolioTransaction, PriceHistory, Watchlist,
    WatchlistEntry,
)
from app.services.portfolio.engine import PortfolioEngine, PortfolioView

logger = logging.getLogger(__name__)

#: View cache lifetime. Short, because prices move; the content key means a
#: structural change invalidates regardless.
CACHE_TTL_SECONDS = 60
CACHE_CAPACITY = 128


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _CacheEntry:
    key: str
    view: PortfolioView
    stored_at: float


class ViewCache:
    """Small LRU with a content-derived key. Defined once; used by the service."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS, capacity: int = CACHE_CAPACITY):
        self.ttl = ttl
        self.capacity = capacity
        self._entries: dict[int, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    def get(self, portfolio_id: int, key: str) -> PortfolioView | None:
        entry = self._entries.get(portfolio_id)
        if entry is None or entry.key != key:
            self.misses += 1
            return None
        if time.monotonic() - entry.stored_at > self.ttl:
            self.misses += 1
            del self._entries[portfolio_id]
            return None
        self.hits += 1
        return entry.view

    def put(self, portfolio_id: int, key: str, view: PortfolioView) -> None:
        if len(self._entries) >= self.capacity:
            oldest = min(self._entries.values(), key=lambda e: e.stored_at)
            self._entries.pop(
                next(k for k, v in self._entries.items() if v is oldest), None
            )
        self._entries[portfolio_id] = _CacheEntry(key, view, time.monotonic())

    def invalidate(self, portfolio_id: int) -> None:
        self._entries.pop(portfolio_id, None)

    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "entries": len(self._entries),
        }


#: Process-wide, so repeated reads within a session share work.
_VIEW_CACHE = ViewCache()


class PortfolioService:
    """All portfolio operations. One instance per request."""

    def __init__(self, db: Session, cache: ViewCache | None = None) -> None:
        self.db = db
        self.cache = cache or _VIEW_CACHE
        #: ticker -> failure reason. Surfaced by the API so a missing score is
        #: attributable rather than merely absent.
        self.analytics_errors: dict[str, str] = {}

    # ================================================================
    # CRUD
    # ================================================================
    def create_portfolio(
        self, owner_id: str, name: str, **kwargs
    ) -> Portfolio:
        if not name.strip():
            raise PortfolioError("a portfolio needs a name")
        existing = self.db.scalar(
            select(Portfolio).where(
                Portfolio.owner_id == owner_id, Portfolio.name == name
            )
        )
        if existing is not None:
            raise PortfolioError(f"a portfolio named '{name}' already exists")
        portfolio = Portfolio(
            owner_id=owner_id, name=name.strip(),
            inception_date=kwargs.pop("inception_date", date.today()), **kwargs,
        )
        self.db.add(portfolio)
        self.db.commit()
        return portfolio

    def get_portfolio(self, portfolio_id: int) -> Portfolio | None:
        return self.db.get(Portfolio, portfolio_id)

    def list_portfolios(self, owner_id: str) -> list[Portfolio]:
        return list(self.db.scalars(
            select(Portfolio)
            .where(Portfolio.owner_id == owner_id)
            .order_by(Portfolio.name)
        ).all())

    def update_portfolio(self, portfolio_id: int, **changes) -> Portfolio:
        portfolio = self._require(portfolio_id)
        for field, value in changes.items():
            if value is not None and hasattr(portfolio, field):
                setattr(portfolio, field, value)
        self.db.commit()
        self.cache.invalidate(portfolio_id)
        return portfolio

    def delete_portfolio(self, portfolio_id: int) -> None:
        portfolio = self._require(portfolio_id)
        self.db.delete(portfolio)
        self.db.commit()
        self.cache.invalidate(portfolio_id)

    def _require(self, portfolio_id: int) -> Portfolio:
        portfolio = self.db.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise PortfolioError(f"portfolio {portfolio_id} not found")
        return portfolio

    # ================================================================
    # Transactions
    # ================================================================
    def add_transaction(
        self,
        portfolio_id: int,
        *,
        ticker: str,
        txn_type: str,
        trade_date: date,
        quantity: float = 0.0,
        price: float = 0.0,
        fees: float = 0.0,
        taxes: float = 0.0,
        ratio_from: float | None = None,
        ratio_to: float | None = None,
        notes: str | None = None,
        sequence: int | None = None,
    ) -> PortfolioTransaction:
        """Record a transaction, then prove the ledger still replays.

        The replay is not a formality. A sell that exceeds the holding at that
        point in history is only detectable by replaying, and letting it commit
        would leave a negative position that misstates every weight in the
        book. The insert is rolled back if the ledger will not replay.
        """
        self._require(portfolio_id)
        company = self.db.scalar(
            select(Company).where(Company.ticker == ticker.upper())
        ) if ticker else None

        if sequence is None:
            sequence = self.db.scalar(
                select(func.coalesce(func.max(PortfolioTransaction.sequence), -1) + 1)
                .where(
                    PortfolioTransaction.portfolio_id == portfolio_id,
                    PortfolioTransaction.trade_date == trade_date,
                )
            ) or 0

        txn = PortfolioTransaction(
            portfolio_id=portfolio_id,
            company_id=company.id if company else None,
            ticker=(ticker or "").upper(),
            txn_type=txn_type, trade_date=trade_date, sequence=sequence,
            quantity=quantity, price=price, fees=fees, taxes=taxes,
            ratio_from=ratio_from, ratio_to=ratio_to, notes=notes,
        )
        self.db.add(txn)
        self.db.flush()
        try:
            PortfolioEngine().positions.replay(self.transactions(portfolio_id))
        except PortfolioError:
            self.db.rollback()
            raise
        self.db.commit()
        self.cache.invalidate(portfolio_id)
        return txn

    def delete_transaction(self, portfolio_id: int, txn_id: int) -> None:
        txn = self.db.get(PortfolioTransaction, txn_id)
        if txn is None or txn.portfolio_id != portfolio_id:
            raise PortfolioError(f"transaction {txn_id} not found")
        self.db.delete(txn)
        self.db.flush()
        try:
            PortfolioEngine().positions.replay(self.transactions(portfolio_id))
        except PortfolioError:
            # Removing a buy can orphan a later sell. Refuse rather than leave
            # the ledger in a state that cannot be replayed.
            self.db.rollback()
            raise PortfolioError(
                "deleting this transaction would leave a sell without a "
                "matching holding; remove the later sale first"
            )
        self.db.commit()
        self.cache.invalidate(portfolio_id)

    def transactions(
        self, portfolio_id: int, *, ticker: str | None = None,
        txn_type: str | None = None, limit: int | None = None,
    ) -> list[PortfolioTransaction]:
        query = select(PortfolioTransaction).where(
            PortfolioTransaction.portfolio_id == portfolio_id
        )
        if ticker:
            query = query.where(PortfolioTransaction.ticker == ticker.upper())
        if txn_type:
            query = query.where(PortfolioTransaction.txn_type == txn_type)
        query = query.order_by(
            PortfolioTransaction.trade_date, PortfolioTransaction.sequence
        )
        if limit:
            query = query.limit(limit)
        return list(self.db.scalars(query).all())

    # ================================================================
    # The view
    # ================================================================
    def _cache_key(self, portfolio_id: int) -> str:
        """Content-derived key: any structural change produces a new key."""
        # `max(updated_at)` is selected without a COALESCE default: coalescing
        # to an empty string made SQLAlchemy parse "" as a datetime on an
        # empty portfolio and raise `Invalid isoformat string`. A NULL is a
        # perfectly good component of a cache key.
        row = self.db.execute(
            select(
                func.count(PortfolioTransaction.id),
                func.coalesce(func.max(PortfolioTransaction.id), 0),
                func.max(PortfolioTransaction.updated_at),
            ).where(PortfolioTransaction.portfolio_id == portfolio_id)
        ).one()
        snapshots = self.db.scalar(
            select(func.count(PortfolioSnapshot.id))
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
        ) or 0
        raw = f"{portfolio_id}:{row[0]}:{row[1]}:{row[2]}:{snapshots}"
        return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]

    def view(self, portfolio_id: int, *, use_cache: bool = True) -> PortfolioView:
        """Resolve a portfolio once. Every endpoint reads from this."""
        portfolio = self._require(portfolio_id)
        key = self._cache_key(portfolio_id)
        if use_cache:
            cached = self.cache.get(portfolio_id, key)
            if cached is not None:
                return cached

        transactions = self.transactions(portfolio_id)
        tickers = sorted({t.ticker for t in transactions if t.ticker})

        companies = {
            c.ticker: c for c in self.db.scalars(
                select(Company).where(Company.ticker.in_(tickers))
            ).all()
        } if tickers else {}

        prices = {t: c.current_price for t, c in companies.items() if c.current_price}
        meta = {
            ticker: {
                "company_id": c.id, "name": c.name, "sector": c.sector,
                "industry": c.industry, "market_cap": c.market_cap,
                "country": "India",
            }
            for ticker, c in companies.items()
        }

        analytics = self._analytics(companies, tickers)
        self._attach_liquidity(analytics, prices, transactions)

        engine = PortfolioEngine(CostBasisMethod(portfolio.cost_basis))
        view = engine.build(
            portfolio_id=portfolio_id, name=portfolio.name,
            benchmark=portfolio.benchmark,
            transactions=transactions, prices=prices, company_meta=meta,
            analytics=analytics,
            targets=self._targets(portfolio_id),
            snapshots=self.snapshots(portfolio_id),
            benchmark_levels=self._benchmark_levels(portfolio),
            style_inputs=self._style_inputs(analytics),
            risk_free=portfolio.risk_free_rate,
            max_position_size=portfolio.max_position_size,
        )
        view.analytics_errors = dict(self.analytics_errors)
        self.cache.put(portfolio_id, key, view)
        return view

    # ---------------------------------------------------------- inputs
    def _analytics(
        self, companies: dict[str, Company], tickers: Sequence[str]
    ) -> dict[str, dict]:
        """Pull scores, ratings and valuation from Modules 4 and 5.

        One resolution per ticker, shared by scoring, valuation and style, so
        the expensive forecast context is built once rather than three times.

        A failure for one holding is recorded in `analytics_errors` and leaves
        that ticker's metrics empty; the alert engine then reports UNAVAILABLE
        rather than a false clear. Swallowing the exception without recording
        it — which the first version did — makes a broken integration
        indistinguishable from a company that simply has no data.
        """
        from app.services.analysis_service import AnalysisService
        from app.services.forecast.service import ForecastService
        from app.services.scoring.service import ScoringService
        from app.services.valuation.service import ValuationService

        forecast_service = ForecastService(self.db)
        valuation_service = ValuationService(self.db)
        scoring_service = ScoringService(self.db)

        out: dict[str, dict] = {}
        for ticker in tickers:
            company = companies.get(ticker)
            if company is None:
                continue
            details: dict = {}
            try:
                analysis = AnalysisService.for_ticker(self.db, ticker)
                if analysis is not None and analysis.has_data:
                    bundle = valuation_service.value_company(
                        analysis, forecast_service
                    )
                    summary = bundle.summary
                    quality = getattr(bundle, "quality", None)
                    grade = getattr(quality, "grade", None)
                    grade_value = getattr(grade, "value", grade)
                    details["valuation_grade"] = grade_value
                    details["valuation_disclosure"] = getattr(
                        quality, "disclosure", None
                    )

                    # Module 4 already grades its own output. A valuation it
                    # calls UNRELIABLE must not drive a price alert or a
                    # position cap here: doing so produced a "Price above
                    # target" on every holding, because a ₹2,945 share was
                    # being compared with a ₹16.79 "fair value" the valuation
                    # engine itself had disowned. The gate is respected rather
                    # than re-litigated.
                    if grade_value == "unreliable":
                        details["intrinsic_value"] = None
                        details["target_price"] = None
                        details["upside"] = None
                        details["valuation_suppressed"] = True
                    else:
                        details["intrinsic_value"] = summary.weighted_value
                        details["target_price"] = summary.weighted_value
                        details["upside"] = summary.upside
                        details["valuation_suppressed"] = False
                    details["terminal_value_share"] = getattr(
                        bundle.dcf_fcff, "terminal_value_share", None
                    )

                    result = scoring_service.score_company(
                        analysis, forecast_service, valuation_service
                    )
                    details["score"] = result.overall_score
                    details["rating"] = result.grade
                    details["recommendation"] = result.recommendation
                    details["stars"] = result.stars
                    # CategoryScore.raw_score is 0-10; the workbook's risk rule
                    # compares against a 0-1 fraction and style thresholds are
                    # 0-100. Both conversions happen here, once.
                    categories = {c.key: c.raw_score for c in result.categories}
                    details["_categories"] = categories
                    if "risk" in categories:
                        details["risk_score"] = categories["risk"] / 10.0
                    details["expected_cagr"] = self._expected_cagr(
                        details.get("target_price"), company.current_price
                    )
                    details.update(self._credit_metrics(analysis))
                    details.update(self._governance_metrics(company.id))
            except Exception as exc:  # pragma: no cover - resilience path
                self.analytics_errors[ticker] = f"{type(exc).__name__}: {exc}"
                logger.warning("analytics failed for %s: %s", ticker, exc)
            out[ticker] = details
        return out

    @staticmethod
    def _credit_metrics(analysis) -> dict:
        """Leverage, cover and cash conversion, from the latest statements.

        Read straight off the statement objects Module 2 already builds rather
        than re-deriving them here. The workbook's alert rows 16, 17 and 19
        read exactly these three, and there must be one definition of each in
        the platform, not two.
        """
        from app.domain.calc import safe_div

        try:
            income = analysis.incomes[-1]
            balance = analysis.balances[-1]
            cash_flow = analysis.cash_flows[-1]
        except (AttributeError, IndexError):
            return {}
        return {
            "net_debt_to_ebitda": safe_div(balance.net_debt, income.ebitda),
            "interest_cover": safe_div(income.ebit, income.finance_costs),
            "cash_conversion": safe_div(cash_flow.cfo, income.pat),
        }

    def _governance_metrics(self, company_id: str) -> dict:
        """Promoter pledge, from the latest shareholding snapshot."""
        from app.models.analysis import ShareholdingSnapshot

        # Shareholding is keyed by fiscal year and quarter, not a date. The
        # first version ordered by a non-existent `as_of` and the resulting
        # AttributeError was recorded for every holding — which is precisely
        # what `analytics_errors` exists to surface.
        row = self.db.scalar(
            select(ShareholdingSnapshot)
            .where(ShareholdingSnapshot.company_id == company_id)
            .order_by(
                ShareholdingSnapshot.fiscal_year.desc(),
                ShareholdingSnapshot.quarter.desc(),
            )
        )
        if row is None:
            return {}
        return {"promoter_pledge": row.promoter_pledged}

    @staticmethod
    def _expected_cagr(
        target: float | None, price: float | None, years: int = 3
    ) -> float | None:
        """Implied annual return if price reaches fair value in `years`.

        The workbook's `(TargetPrice/CMP)^(1/3)-1`. A non-positive price has no
        real root, so it returns ``None`` rather than a complex number.
        """
        if not target or not price or price <= 0 or target <= 0:
            return None
        return (target / price) ** (1.0 / years) - 1.0

    def _attach_liquidity(
        self, analytics: dict[str, dict], prices: dict[str, float],
        transactions: Sequence[PortfolioTransaction],
    ) -> None:
        """Days to exit each holding at 20% of average daily traded value.

        Needs the position size, which is only known after a replay, so it is
        computed here rather than inside `_analytics`.
        """
        replay = PortfolioEngine().positions.replay(transactions)
        for ticker, position in replay.positions.items():
            if not position.is_open:
                continue
            price = prices.get(ticker)
            if price is None:
                continue
            traded = self.db.scalar(
                select(func.avg(PriceHistory.traded_value))
                .where(PriceHistory.ticker == ticker)
                .where(PriceHistory.traded_value.isnot(None))
            )
            days = liquidity_days(position.quantity * price, traded)
            if days is not None:
                analytics.setdefault(ticker, {})["liquidity_days"] = days

    @staticmethod
    def _style_inputs(analytics: dict[str, dict]) -> dict[str, StyleInputs]:
        """Style from Module 5 category scores, rescaled to 0-100.

        Reads the analytics already resolved above rather than re-running the
        scoring engine per ticker, which was the first implementation and
        tripled the cost of building a view.
        """
        out: dict[str, StyleInputs] = {}
        for ticker, details in analytics.items():
            categories = details.get("_categories")
            if not categories:
                continue
            out[ticker] = StyleInputs(
                valuation=_to_percent(categories.get("valuation")),
                growth=_to_percent(categories.get("growth_quality")),
                quality=_to_percent(categories.get("business_quality")),
            )
        return out

    def _targets(self, portfolio_id: int) -> dict[str, dict[str, float]]:
        rows = self.db.scalars(
            select(AllocationTarget).where(
                AllocationTarget.portfolio_id == portfolio_id
            )
        ).all()
        out: dict[str, dict[str, float]] = {}
        for row in rows:
            out.setdefault(row.dimension, {})[row.bucket_key] = row.target_weight
        return out

    def set_target(
        self, portfolio_id: int, dimension: str, bucket_key: str, weight: float
    ) -> AllocationTarget:
        existing = self.db.scalar(
            select(AllocationTarget).where(
                AllocationTarget.portfolio_id == portfolio_id,
                AllocationTarget.dimension == dimension,
                AllocationTarget.bucket_key == bucket_key,
            )
        )
        if existing is not None:
            existing.target_weight = weight
        else:
            existing = AllocationTarget(
                portfolio_id=portfolio_id, dimension=dimension,
                bucket_key=bucket_key, target_weight=weight,
            )
            self.db.add(existing)
        self.db.commit()
        self.cache.invalidate(portfolio_id)
        return existing

    def _benchmark_levels(self, portfolio: Portfolio) -> list[float]:
        rows = self.db.scalars(
            select(BenchmarkLevel)
            .where(BenchmarkLevel.symbol == portfolio.benchmark)
            .order_by(BenchmarkLevel.as_of)
        ).all()
        return [r.close for r in rows]

    # ================================================================
    # Snapshots
    # ================================================================
    def snapshots(self, portfolio_id: int) -> list[PortfolioSnapshot]:
        return list(self.db.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio_id)
            .order_by(PortfolioSnapshot.as_of)
        ).all())

    def record_snapshot(
        self, portfolio_id: int, as_of: date | None = None
    ) -> PortfolioSnapshot:
        """Freeze today's valuation. Idempotent per date."""
        view = self.view(portfolio_id, use_cache=False)
        when = as_of or date.today()
        flow = self.db.scalar(
            select(func.coalesce(func.sum(
                PortfolioTransaction.quantity * PortfolioTransaction.price
            ), 0.0)).where(
                PortfolioTransaction.portfolio_id == portfolio_id,
                PortfolioTransaction.trade_date == when,
                PortfolioTransaction.txn_type.in_(("deposit", "withdrawal")),
            )
        ) or 0.0

        existing = self.db.scalar(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.portfolio_id == portfolio_id,
                PortfolioSnapshot.as_of == when,
            )
        )
        target = existing or PortfolioSnapshot(
            portfolio_id=portfolio_id, as_of=when
        )
        target.market_value = view.market_value
        target.cost_basis = view.cost_basis
        target.cash = view.cash.balance
        target.net_flow = flow
        target.position_count = view.position_count
        if existing is None:
            self.db.add(target)
        self.db.commit()
        self.cache.invalidate(portfolio_id)
        return target

    # ================================================================
    # Alerts
    # ================================================================
    def rules_for(self, owner_id: str, portfolio_id: int | None) -> list[AlertRule]:
        """Built-in rules with any user overrides applied."""
        overrides = {
            o.rule_key: o for o in self.db.scalars(
                select(AlertRuleOverride).where(
                    AlertRuleOverride.owner_id == owner_id,
                    (AlertRuleOverride.portfolio_id == portfolio_id)
                    | AlertRuleOverride.portfolio_id.is_(None),
                )
            ).all()
        }
        rules: list[AlertRule] = []
        for rule in ALL_RULES:
            override = overrides.get(rule.key)
            if override is None:
                rules.append(rule)
                continue
            rules.append(AlertRule(
                key=rule.key, label=override.label or rule.label,
                condition=rule.condition, metric=rule.metric,
                comparator=rule.comparator,
                threshold=override.threshold if override.threshold is not None
                else rule.threshold,
                severity=AlertSeverity(override.severity) if override.severity
                else rule.severity,
                category=rule.category, action=rule.action, scope=rule.scope,
                threshold_metric=rule.threshold_metric,
                enabled=override.enabled,
            ))
        for override in overrides.values():
            if override.is_custom and override.rule_key not in RULES_BY_KEY:
                rules.append(AlertRule(
                    key=override.rule_key, label=override.label or override.rule_key,
                    condition=f"{override.metric} {override.comparator} "
                              f"{override.threshold}",
                    metric=override.metric or "",
                    comparator=Comparator(override.comparator or "lt"),
                    threshold=override.threshold or 0.0,
                    severity=AlertSeverity(override.severity or "medium"),
                    category=AlertCategory.PORTFOLIO,
                    action="Review", scope="position",
                    enabled=override.enabled,
                ))
        return rules

    def evaluate_alerts(
        self, portfolio_id: int, *, persist: bool = True
    ) -> list[AlertEvaluation]:
        """Evaluate every rule against every holding and the book as a whole."""
        portfolio = self._require(portfolio_id)
        view = self.view(portfolio_id)
        engine = AlertEngine(self.rules_for(portfolio.owner_id, portfolio_id))

        evaluations: list[AlertEvaluation] = []
        analytics = getattr(view, "analytics", {}) or {}
        for holding in view.holdings:
            extra = analytics.get(holding.ticker, {})
            metrics = build_position_metrics(
                price=holding.position.current_price,
                intrinsic_value=holding.intrinsic_value,
                target_price=holding.target_price,
                margin_of_safety=portfolio.margin_of_safety,
                score=holding.score,
                rating=holding.rating,
                risk_score=holding.risk_score,
                weight=holding.weight,
                net_debt_to_ebitda=extra.get("net_debt_to_ebitda"),
                interest_cover=extra.get("interest_cover"),
                cash_conversion=extra.get("cash_conversion"),
                promoter_pledge=extra.get("promoter_pledge"),
                terminal_value_share=extra.get("terminal_value_share"),
            )
            metrics["max_position_size"] = holding.max_position_size
            evaluations.extend(engine.evaluate_position(
                holding.ticker, holding.position.company_id, metrics,
                name=holding.position.name,
            ))

        evaluations.extend(engine.evaluate_portfolio(view.portfolio_metrics()))
        if persist:
            self._persist_alerts(portfolio, evaluations)
        return sorted(evaluations, key=lambda e: e.sort_key)

    def _persist_alerts(
        self, portfolio: Portfolio, evaluations: Sequence[AlertEvaluation]
    ) -> None:
        """Open, refresh or clear stored alerts.

        An alert already open is refreshed rather than duplicated, so a
        condition true for a week is one event with seven occurrences, not
        seven events. A condition that stops being true closes its event.
        """
        now = _utcnow()
        open_events = {
            (e.rule_key, e.ticker): e for e in self.db.scalars(
                select(AlertEvent).where(
                    AlertEvent.portfolio_id == portfolio.id,
                    AlertEvent.status.in_(("triggered", "acknowledged")),
                )
            ).all()
        }
        seen: set[tuple[str, str | None]] = set()

        for evaluation in evaluations:
            identity = (evaluation.key, evaluation.ticker)
            if not evaluation.is_triggered:
                continue
            seen.add(identity)
            event = open_events.get(identity)
            if event is not None:
                event.last_seen = now
                event.occurrences += 1
                event.observed = _as_text(evaluation.observed)
                continue
            self.db.add(AlertEvent(
                portfolio_id=portfolio.id, owner_id=portfolio.owner_id,
                rule_key=evaluation.key, ticker=evaluation.ticker,
                company_id=evaluation.company_id, label=evaluation.label,
                category=evaluation.category.value,
                severity=evaluation.severity.value,
                status=AlertStatus.TRIGGERED.value,
                condition=evaluation.condition, action=evaluation.action,
                observed=_as_text(evaluation.observed),
                threshold=_as_text(evaluation.threshold),
                detail=evaluation.detail, first_seen=now, last_seen=now,
            ))

        for identity, event in open_events.items():
            if identity not in seen:
                event.status = AlertStatus.CLEAR.value
                event.cleared_at = now
        self.db.commit()

    def alerts(
        self, portfolio_id: int | None = None, *, owner_id: str | None = None,
        status: str | None = None, severity: str | None = None,
    ) -> list[AlertEvent]:
        query = select(AlertEvent)
        if portfolio_id is not None:
            query = query.where(AlertEvent.portfolio_id == portfolio_id)
        if owner_id is not None:
            query = query.where(AlertEvent.owner_id == owner_id)
        if status:
            query = query.where(AlertEvent.status == status)
        if severity:
            query = query.where(AlertEvent.severity == severity)
        return list(self.db.scalars(
            query.order_by(AlertEvent.severity, AlertEvent.last_seen.desc())
        ).all())

    def acknowledge(self, alert_id: int) -> AlertEvent:
        event = self.db.get(AlertEvent, alert_id)
        if event is None:
            raise PortfolioError(f"alert {alert_id} not found")
        event.status = AlertStatus.ACKNOWLEDGED.value
        event.acknowledged_at = _utcnow()
        self.db.commit()
        return event

    # ================================================================
    # Watchlists
    # ================================================================
    def create_watchlist(self, owner_id: str, name: str, **kwargs) -> Watchlist:
        existing = self.db.scalar(
            select(Watchlist).where(
                Watchlist.owner_id == owner_id, Watchlist.name == name
            )
        )
        if existing is not None:
            raise PortfolioError(f"a watchlist named '{name}' already exists")
        watchlist = Watchlist(owner_id=owner_id, name=name.strip(), **kwargs)
        self.db.add(watchlist)
        self.db.commit()
        return watchlist

    def list_watchlists(self, owner_id: str) -> list[Watchlist]:
        return list(self.db.scalars(
            select(Watchlist).where(Watchlist.owner_id == owner_id)
            .order_by(Watchlist.name)
        ).all())

    def update_watchlist(self, watchlist_id: int, **changes) -> Watchlist:
        watchlist = self.db.get(Watchlist, watchlist_id)
        if watchlist is None:
            raise PortfolioError(f"watchlist {watchlist_id} not found")
        if "name" in changes and changes["name"]:
            name = changes["name"].strip()
            existing = self.db.scalar(
                select(Watchlist).where(
                    Watchlist.owner_id == watchlist.owner_id,
                    Watchlist.name == name,
                    Watchlist.id != watchlist_id,
                )
            )
            if existing:
                raise PortfolioError(f"a watchlist named '{name}' already exists")
            watchlist.name = name
        for k, v in changes.items():
            if k != "name" and hasattr(watchlist, k) and v is not None:
                setattr(watchlist, k, v)
        self.db.commit()
        return watchlist

    def delete_watchlist(self, watchlist_id: int) -> None:
        watchlist = self.db.get(Watchlist, watchlist_id)
        if watchlist is None:
            raise PortfolioError(f"watchlist {watchlist_id} not found")
        self.db.delete(watchlist)
        self.db.commit()

    def add_to_watchlist(
        self, watchlist_id: int, ticker: str, **kwargs
    ) -> WatchlistEntry:
        watchlist = self.db.get(Watchlist, watchlist_id)
        if watchlist is None:
            raise PortfolioError(f"watchlist {watchlist_id} not found")
        company = self.db.scalar(
            select(Company).where(Company.ticker == ticker.upper())
        )
        existing = self.db.scalar(
            select(WatchlistEntry).where(
                WatchlistEntry.watchlist_id == watchlist_id,
                WatchlistEntry.ticker == ticker.upper(),
            )
        )
        if existing is not None:
            raise PortfolioError(f"{ticker} is already on this watchlist")
        entry = WatchlistEntry(
            watchlist_id=watchlist_id, ticker=ticker.upper(),
            company_id=company.id if company else None,
            added_on=kwargs.pop("added_on", date.today()), **kwargs,
        )
        self.db.add(entry)
        self.db.commit()
        return entry

    def remove_from_watchlist(self, watchlist_id: int, entry_id: int) -> None:
        entry = self.db.get(WatchlistEntry, entry_id)
        if entry is None or entry.watchlist_id != watchlist_id:
            raise PortfolioError(f"entry {entry_id} not found")
        self.db.delete(entry)
        self.db.commit()

    def watchlist_view(self, watchlist_id: int) -> list[dict]:
        """Watchlist rows with live price, upside and trigger status.

        The buy-below price falls back to intrinsic value discounted by the
        margin of safety when the user has not set one, so a row added with
        only a ticker is still actionable rather than inert.
        """
        watchlist = self.db.get(Watchlist, watchlist_id)
        if watchlist is None:
            raise PortfolioError(f"watchlist {watchlist_id} not found")

        rows: list[dict] = []
        for entry in watchlist.entries:
            company = self.db.scalar(
                select(Company).where(Company.ticker == entry.ticker)
            )
            price = company.current_price if company else None
            analytics = self._analytics(
                {entry.ticker: company} if company else {}, [entry.ticker]
            ).get(entry.ticker, {})

            intrinsic = analytics.get("intrinsic_value")
            buy_below = entry.buy_below or (
                intrinsic * (1 - 0.20) if intrinsic else None
            )
            target = entry.target_price or analytics.get("target_price")
            upside = (target / price - 1.0) if target and price else None

            status = WatchStatus.WATCHING
            if price and buy_below:
                if price <= buy_below:
                    status = WatchStatus.TRIGGERED
                elif price <= buy_below * 1.10:
                    status = WatchStatus.APPROACHING
                elif target and price >= target:
                    status = WatchStatus.EXPENSIVE
            rows.append({
                "id": entry.id, "ticker": entry.ticker,
                "company_id": entry.company_id,
                "name": company.name if company else entry.ticker,
                "sector": company.sector if company else None,
                "price": price, "buy_below": buy_below, "target_price": target,
                "upside": upside, "score": analytics.get("score"),
                "rating": analytics.get("rating"), "status": status.value,
                "note": entry.note, "conviction": entry.conviction,
                "added_on": entry.added_on,
            })
        rows.sort(key=lambda r: (r["status"] != "triggered", -(r["upside"] or -9)))
        return rows

    # ================================================================
    # Diagnostics
    # ================================================================
    def cache_stats(self) -> dict[str, float]:
        return self.cache.stats()


def _to_percent(raw_score: float | None) -> float | None:
    """CategoryScore.raw_score is 0-10; style thresholds are 0-100."""
    return None if raw_score is None else raw_score * 10.0


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)[:80]
