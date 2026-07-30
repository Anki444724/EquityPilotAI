"""Forecast orchestration and persistence.

Assumptions are stored; projections are not. Every request recomputes from the
saved driver rows, so a stored forecast can never drift from what the engine
would produce today.

Resolution order for any driver:

1. explicit per-scenario override row  (scenario = 'bull' | 'base' | 'bear')
2. base row for the forecast           (scenario IS NULL)
3. value calibrated from the company's own history
4. documented platform fallback

Steps 1–2 come from the database, 3–4 from the calibrator. This is the same
override-then-derive pattern the canonical financials use, applied to inputs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.forecast.assumptions import (
    Driver, ForecastAssumptions, Provenance, RevenueMethod, Scenario,
    SegmentAssumption,
)
from app.domain.forecast.engine import ForecastBase, ForecastEngine, ForecastResult
from app.domain.forecast.scenarios import (
    DEFAULT_PROBABILITIES, ScenarioAnalysis, derive_scenario, run_scenarios,
)
from app.models.company import Company
from app.models.forecast import Forecast, ForecastAssumptionRecord, ForecastStatus
from app.services.financials.service import FinancialStatementsService
from app.services.forecast.calibration import AssumptionCalibrator

#: Horizons the product supports.
ALLOWED_HORIZONS = (3, 5, 10)


class ForecastError(ValueError):
    """Raised for invalid forecast configuration."""


@dataclass(slots=True)
class ForecastContext:
    """Everything needed to run a forecast for one company."""

    company: Company
    base: ForecastBase
    calibrated: ForecastAssumptions


class ForecastService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------- context
    def build_context(
        self,
        company: Company,
        financials_service: FinancialStatementsService,
        years: int = 5,
        method: RevenueMethod = RevenueMethod.CAGR,
    ) -> ForecastContext:
        """Calibrate defaults from the company's reported history."""
        calibrator = AssumptionCalibrator(
            financials_service.income_statements(),
            financials_service.balance_sheets(),
            financials_service.cash_flows(),
        )
        return ForecastContext(
            company=company,
            base=calibrator.base_position(),
            calibrated=calibrator.calibrate(years=years, method=method),
        )

    # -------------------------------------------------------- persistence
    def get(self, forecast_id: str) -> Forecast | None:
        return self.db.get(Forecast, forecast_id)

    def list_for_company(self, company_id: str) -> list[Forecast]:
        return list(
            self.db.execute(
                select(Forecast)
                .where(Forecast.company_id == company_id)
                .where(Forecast.status != ForecastStatus.ARCHIVED)
                .order_by(Forecast.created_at.desc())
            ).scalars().all()
        )

    def active_for_company(self, company_id: str) -> Forecast | None:
        forecasts = self.list_for_company(company_id)
        return forecasts[0] if forecasts else None

    def create(
        self,
        company_id: str,
        name: str = "Base forecast",
        horizon_years: int = 5,
        revenue_method: str = RevenueMethod.CAGR.value,
        segments: list | None = None,
        created_by: str | None = None,
        notes: str | None = None,
    ) -> Forecast:
        if horizon_years not in ALLOWED_HORIZONS:
            raise ForecastError(
                f"horizon must be one of {ALLOWED_HORIZONS}, got {horizon_years}"
            )
        try:
            RevenueMethod(revenue_method)
        except ValueError as exc:
            raise ForecastError(f"unknown revenue method '{revenue_method}'") from exc

        forecast = Forecast(
            id=str(uuid.uuid4()),
            company_id=company_id,
            name=name,
            horizon_years=horizon_years,
            revenue_method=revenue_method,
            segments=segments,
            created_by=created_by,
            notes=notes,
        )
        self.db.add(forecast)
        self.db.commit()
        return forecast

    def update_assumptions(
        self,
        forecast: Forecast,
        drivers: dict[str, float],
        scenario: Scenario | None = None,
        source: Provenance = Provenance.ANALYST,
        citation: str | None = None,
        requires_review: bool = False,
        by_year: dict[str, dict[int, float]] | None = None,
    ) -> Forecast:
        """Upsert driver rows.

        This single method serves analyst edits and, unchanged, future AI
        writes — the only difference is the ``source`` and ``citation``.
        """
        template = ForecastAssumptions()
        valid = set(template.driver_names())
        unknown = set(drivers) - valid
        if unknown:
            raise ForecastError(f"unknown drivers: {sorted(unknown)}")

        scenario_value = scenario.value if scenario else None
        by_year = by_year or {}

        for name, value in drivers.items():
            existing = self.db.execute(
                select(ForecastAssumptionRecord)
                .where(ForecastAssumptionRecord.forecast_id == forecast.id)
                .where(ForecastAssumptionRecord.driver == name)
                .where(ForecastAssumptionRecord.scenario.is_(scenario_value)
                       if scenario_value is None
                       else ForecastAssumptionRecord.scenario == scenario_value)
            ).scalar_one_or_none()

            periods = {str(k): v for k, v in (by_year.get(name) or {}).items()} or None

            if existing:
                existing.value = float(value)
                existing.by_year = periods
                existing.source = source.value
                existing.citation = citation
                existing.requires_review = requires_review
            else:
                self.db.add(
                    ForecastAssumptionRecord(
                        forecast_id=forecast.id,
                        driver=name,
                        scenario=scenario_value,
                        value=float(value),
                        by_year=periods,
                        source=source.value,
                        citation=citation,
                        requires_review=requires_review,
                    )
                )

        forecast.revision += 1
        self.db.commit()
        return forecast

    def stored_drivers(
        self, forecast_id: str, scenario: Scenario | None
    ) -> list[ForecastAssumptionRecord]:
        scenario_value = scenario.value if scenario else None
        stmt = select(ForecastAssumptionRecord).where(
            ForecastAssumptionRecord.forecast_id == forecast_id
        )
        stmt = stmt.where(
            ForecastAssumptionRecord.scenario.is_(None)
            if scenario_value is None
            else ForecastAssumptionRecord.scenario == scenario_value
        )
        return list(self.db.execute(stmt).scalars().all())

    # ----------------------------------------------------------- resolution
    def _apply_records(
        self, assumptions: ForecastAssumptions, records: list[ForecastAssumptionRecord]
    ) -> ForecastAssumptions:
        patch: dict[str, object] = {}
        for rec in records:
            current = getattr(assumptions, rec.driver, None)
            if not isinstance(current, Driver):
                continue
            patch[rec.driver] = Driver(
                value=rec.value,
                by_year={int(k): float(v) for k, v in (rec.by_year or {}).items()},
                source=Provenance(rec.source),
                citation=rec.citation,
                note=rec.note,
            )
        return assumptions.override(**patch) if patch else assumptions

    def resolve_assumptions(
        self,
        context: ForecastContext,
        forecast: Forecast | None,
        scenario: Scenario = Scenario.BASE,
    ) -> ForecastAssumptions:
        """Apply the four-tier resolution chain for one scenario."""
        assumptions = context.calibrated

        if forecast is not None:
            assumptions = assumptions.override(
                years=forecast.horizon_years,
                revenue_method=RevenueMethod(forecast.revenue_method),
                segments=self._segments(forecast),
            )
            # tier 2: base rows shared by all scenarios
            assumptions = self._apply_records(
                assumptions, self.stored_drivers(forecast.id, None)
            )

        # derive the scenario shift from the (possibly edited) base case
        assumptions = derive_scenario(assumptions, scenario)

        if forecast is not None:
            # tier 1: explicit per-scenario overrides win over the derived shift
            assumptions = self._apply_records(
                assumptions, self.stored_drivers(forecast.id, scenario)
            )
        return assumptions

    @staticmethod
    def _segments(forecast: Forecast) -> tuple[SegmentAssumption, ...]:
        if not forecast.segments:
            return ()
        return tuple(
            SegmentAssumption(
                name=s["name"],
                base_revenue=float(s["base_revenue"]),
                growth=Driver(value=float(s.get("growth", 0.0)), source=Provenance.ANALYST),
            )
            for s in forecast.segments
        )

    # --------------------------------------------------------------- runners
    def run(
        self,
        context: ForecastContext,
        forecast: Forecast | None = None,
        scenario: Scenario = Scenario.BASE,
    ) -> ForecastResult:
        assumptions = self.resolve_assumptions(context, forecast, scenario)
        return ForecastEngine(context.base, assumptions).run()

    def run_all_scenarios(
        self,
        context: ForecastContext,
        forecast: Forecast | None = None,
        cmp_price: float | None = None,
    ) -> ScenarioAnalysis:
        """Run all three cases from the resolved base assumptions.

        The base case is resolved through the full chain first, so analyst edits
        propagate into the derived bull and bear cases.
        """
        base_assumptions = self.resolve_assumptions(context, forecast, Scenario.BASE)
        analysis = run_scenarios(
            context.base, base_assumptions, cmp_price=cmp_price
        )

        # Re-run any scenario that carries explicit per-scenario overrides.
        if forecast is not None:
            for scenario in (Scenario.BEAR, Scenario.BULL):
                if self.stored_drivers(forecast.id, scenario):
                    resolved = self.resolve_assumptions(context, forecast, scenario)
                    analysis.results[scenario.value] = ForecastEngine(
                        context.base, resolved
                    ).run()
        return analysis
