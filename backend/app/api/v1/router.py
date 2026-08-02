"""Aggregate v1 API router."""
from fastapi import APIRouter

from app.api.v1 import (
    admin, ai, analysis, auth, companies, dashboard, documents, filings_admin, forecast, knowledge, market, portfolio, quality, reports, scoring, storage_admin, valuation,
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
# filings_admin must precede market: market registers "/filings/{ticker}",
# and a path parameter matches any literal segment, so registering it first
# captures "/filings/dashboard" and "/filings/companies" and hands them to the
# per-company filing chain with ticker="DASHBOARD". Observed exactly that —
# the endpoints returned 200 with an empty result, which is far worse than a
# 404 because nothing looks broken.
api_router.include_router(filings_admin.router)
api_router.include_router(storage_admin.router)
api_router.include_router(knowledge.router)
# Before `market.router`: that router declares `/filings/{ticker}` and other
# greedy path parameters, and a static sibling registered after it is
# captured as a parameter value (ROUTE-001).
api_router.include_router(quality.router)
api_router.include_router(market.router)
