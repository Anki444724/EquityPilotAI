"""Aggregate v1 API router."""
from fastapi import APIRouter

from app.api.v1 import (
    admin, ai, analysis, auth, companies, dashboard, documents, forecast,
    portfolio, reports, scoring, valuation,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(companies.router)
api_router.include_router(dashboard.router)
api_router.include_router(analysis.router)
api_router.include_router(forecast.router)
api_router.include_router(valuation.router)
api_router.include_router(scoring.router)
api_router.include_router(ai.router)
api_router.include_router(documents.router)
api_router.include_router(portfolio.router)
api_router.include_router(reports.router)
