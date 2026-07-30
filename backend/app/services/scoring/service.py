"""Scoring orchestration.

Assembles :class:`ScoringInputs` once from the existing engines, runs the
composite, and manages weight profiles and history.

This is the reuse point the brief calls for. The dashboard, AI analyst,
portfolio, watchlist, alerts and report generator all call
:meth:`ScoringService.score_company` and receive the same object — none of them
re-derive a score, and none can drift from another.
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.calc import safe_div
from app.domain.forecast.assumptions import Scenario
from app.domain.scoring.inputs import QualitativeInputs, ScoringInputs
from app.domain.scoring.weights import (
    BUILTIN_PROFILES, Category, DEFAULT_PROFILE, WeightProfile, get_profile,
)
from app.models.analysis import ShareholdingSnapshot
from app.models.scoring import ScoreSnapshot, ScoringWeightProfile
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.scoring.overall_score import ScoreResult, compute_score
from app.services.valuation.service import ValuationService


class ScoringError(ValueError):
    """Raised for invalid scoring configuration."""


class ScoringService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------- profiles
    def list_profiles(self, owner: str | None = None) -> list[WeightProfile]:
        """Built-in profiles plus any the user has saved."""
        profiles = list(BUILTIN_PROFILES.values())
        stored = self.db.execute(
            select(ScoringWeightProfile).where(
                ScoringWeightProfile.owner.is_(None)
                if owner is None else ScoringWeightProfile.owner == owner
            )
        ).scalars().all()
        for record in stored:
            profiles.append(WeightProfile(
                key=record.key, label=record.label,
                description=record.description or "",
                weights=dict(record.weights), is_builtin=False,
            ))
        return profiles

    def resolve_profile(self, key: str | None, owner: str | None = None) -> WeightProfile:
        """Look up a profile by key, checking built-ins then stored profiles."""
        if not key:
            return DEFAULT_PROFILE
        if key.lower() in BUILTIN_PROFILES:
            return get_profile(key)

        record = self.db.execute(
            select(ScoringWeightProfile)
            .where(ScoringWeightProfile.key == key)
            .where(
                ScoringWeightProfile.owner.is_(None)
                if owner is None else ScoringWeightProfile.owner == owner
            )
        ).scalar_one_or_none()
        if record is None:
            raise ScoringError(f"unknown weight profile '{key}'")
        return WeightProfile(
            key=record.key, label=record.label,
            description=record.description or "",
            weights=dict(record.weights), is_builtin=False,
        )

    def save_profile(
        self,
        key: str,
        label: str,
        weights: dict[str, float],
        *,
        owner: str | None = None,
        description: str | None = None,
        derived_from: str | None = None,
    ) -> WeightProfile:
        """Create or update a custom profile."""
        if key.lower() in BUILTIN_PROFILES:
            raise ScoringError(f"'{key}' is a built-in profile and cannot be overwritten")

        valid = {c.value for c in Category}
        unknown = set(weights) - valid
        if unknown:
            raise ScoringError(f"unknown categories: {sorted(unknown)}")
        if not weights or sum(weights.values()) <= 0:
            raise ScoringError("profile must have at least one positive weight")
        if any(w < 0 for w in weights.values()):
            raise ScoringError("weights cannot be negative")

        # Absent categories are explicitly zero, not silently defaulted.
        full = {c.value: float(weights.get(c.value, 0.0)) for c in Category}

        record = self.db.execute(
            select(ScoringWeightProfile)
            .where(ScoringWeightProfile.key == key)
            .where(
                ScoringWeightProfile.owner.is_(None)
                if owner is None else ScoringWeightProfile.owner == owner
            )
        ).scalar_one_or_none()

        if record:
            record.label = label
            record.weights = full
            record.description = description
            record.derived_from = derived_from
        else:
            record = ScoringWeightProfile(
                key=key, label=label, weights=full, owner=owner,
                description=description, derived_from=derived_from,
            )
            self.db.add(record)
        self.db.commit()

        return WeightProfile(key=key, label=label, description=description or "",
                             weights=full, is_builtin=False)

    # --------------------------------------------------------------- inputs
    def build_inputs(
        self,
        analysis: AnalysisService,
        forecast_service: ForecastService,
        valuation_service: ValuationService,
        *,
        horizon: int = 5,
        qualitative: QualitativeInputs | None = None,
    ) -> ScoringInputs:
        """Resolve every scorer's inputs in one pass."""
        company = analysis.company

        forecast_result = None
        bundle = None
        try:
            bundle = valuation_service.value_company(
                analysis, forecast_service, horizon=horizon, scenario=Scenario.BASE
            )
            context = forecast_service.build_context(
                company, analysis.statements, years=horizon
            )
            saved = forecast_service.active_for_company(company.id)
            forecast_result = forecast_service.run(context, saved, Scenario.BASE)
        except Exception:
            # Scoring must still work when a company cannot be valued — the
            # valuation category simply reports missing inputs.
            pass

        qualitative = qualitative or self._qualitative_from_db(company.id)

        income = analysis.incomes[-1] if analysis.incomes else None
        balance = analysis.balances[-1] if analysis.balances else None
        bvps = safe_div(
            balance.shareholders_equity if balance else None,
            income.weighted_shares if income else None,
        )

        justified_premium = None
        if bundle:
            premiums = [
                j.premium_discount for j in bundle.relative.justified
                if j.premium_discount is not None
            ]
            justified_premium = sum(premiums) / len(premiums) if premiums else None

        return ScoringInputs(
            company_id=company.id, ticker=company.ticker, name=company.name,
            incomes=analysis.incomes, balances=analysis.balances,
            cash_flows=analysis.cash_flows,
            forecast=forecast_result,
            wacc=bundle.wacc.wacc if bundle else None,
            cost_of_equity=bundle.wacc.cost_of_equity if bundle else None,
            intrinsic_value=bundle.summary.weighted_value if bundle else None,
            current_price=company.current_price,
            upside=bundle.summary.upside if bundle else None,
            ev_ebitda=bundle.relative.current.ev_ebitda if bundle else None,
            pe_ratio=bundle.relative.current.pe if bundle else None,
            pb_ratio=safe_div(company.current_price, bvps),
            justified_premium=justified_premium,
            margin_of_safety=bundle.summary.margin_of_safety if bundle else None,
            quality_report=bundle.quality if bundle else None,
            qualitative=qualitative,
        )

    def _qualitative_from_db(self, company_id: str) -> QualitativeInputs:
        """Populate the qualitative inputs that the platform can observe.

        Promoter pledge comes from the shareholding disclosures ingested in
        Module 2. Everything else remains unset until an analyst supplies it or
        Module 7 extracts it from documents — and the confidence engine reports
        those gaps rather than guessing.
        """
        latest = self.db.execute(
            select(ShareholdingSnapshot)
            .where(ShareholdingSnapshot.company_id == company_id)
            .order_by(ShareholdingSnapshot.fiscal_year.desc(),
                      ShareholdingSnapshot.quarter.desc())
            .limit(1)
        ).scalar_one_or_none()

        return QualitativeInputs(
            promoter_pledge=latest.promoter_pledged if latest else None,
        )

    # ---------------------------------------------------------------- score
    def score_company(
        self,
        analysis: AnalysisService,
        forecast_service: ForecastService,
        valuation_service: ValuationService,
        *,
        profile_key: str | None = None,
        horizon: int = 5,
        owner: str | None = None,
        qualitative: QualitativeInputs | None = None,
    ) -> ScoreResult:
        """The single entry point every consumer calls."""
        profile = self.resolve_profile(profile_key, owner)
        inputs = self.build_inputs(
            analysis, forecast_service, valuation_service,
            horizon=horizon, qualitative=qualitative,
        )
        return compute_score(inputs, profile)

    # -------------------------------------------------------------- history
    def save_snapshot(self, result: ScoreResult, as_of: date | None = None) -> ScoreSnapshot:
        """Persist a scoring run so trends can be measured later."""
        as_of = as_of or date.today()

        existing = self.db.execute(
            select(ScoreSnapshot)
            .where(ScoreSnapshot.company_id == result.company_id)
            .where(ScoreSnapshot.as_of == as_of)
            .where(ScoreSnapshot.profile_key == result.profile_key)
        ).scalar_one_or_none()

        payload = {
            "overall_score": result.overall_score,
            "grade": result.grade,
            "stars": result.stars,
            "recommendation": result.recommendation,
            "conviction": result.conviction,
            "confidence": result.confidence.confidence,
            "verified_pct": result.confidence.verified_pct,
            "estimated_pct": result.confidence.estimated_pct,
            "missing_pct": result.confidence.missing_pct,
            "category_scores": {c.key: round(c.raw_score, 4) for c in result.categories},
            "summary": result.summary,
        }

        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
            snapshot = existing
        else:
            snapshot = ScoreSnapshot(
                company_id=result.company_id, as_of=as_of,
                profile_key=result.profile_key, **payload,
            )
            self.db.add(snapshot)
        self.db.commit()
        return snapshot

    def history(
        self, company_id: str, profile_key: str | None = None, limit: int = 24
    ) -> list[ScoreSnapshot]:
        stmt = select(ScoreSnapshot).where(ScoreSnapshot.company_id == company_id)
        if profile_key:
            stmt = stmt.where(ScoreSnapshot.profile_key == profile_key)
        rows = self.db.execute(
            stmt.order_by(ScoreSnapshot.as_of.desc()).limit(limit)
        ).scalars().all()
        return list(reversed(rows))

    # ------------------------------------------------------------ comparison
    def peer_scores(
        self,
        peers: list[AnalysisService],
        forecast_service: ForecastService,
        valuation_service: ValuationService,
        *,
        profile_key: str | None = None,
        horizon: int = 5,
    ) -> list[ScoreResult]:
        """Score a set of companies on identical assumptions, for comparison."""
        results: list[ScoreResult] = []
        for peer in peers:
            if not peer.has_data:
                continue
            results.append(self.score_company(
                peer, forecast_service, valuation_service,
                profile_key=profile_key, horizon=horizon,
            ))
        return sorted(results, key=lambda r: -r.overall_score)
