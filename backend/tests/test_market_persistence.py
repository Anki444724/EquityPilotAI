"""Phase 1 — persistent quotes and historical prices.

Covers requirement D (quote persistence: company, price, timestamp, exchange,
provider, volume, market status), requirement E (OHLC history, idempotent)
and the read-back API endpoints with their provenance labelling.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from app.data.providers.base import Quote
from app.data.providers.mock import mock_history, mock_quote
from app.models.company import Company
from app.models.market import MarketQuote
from app.models.portfolio import PriceHistory
from app.services.market.persistence import (
    bars_for_range, price_series, upsert_daily_bars, upsert_quote,
)


def _company(phase1_db, ticker="MCK0001") -> Company:
    row = phase1_db.scalar(select(Company).where(Company.ticker == ticker))
    if row is None:
        row = Company(
            id=f"id-{ticker}", ticker=ticker, name=f"{ticker} Ltd",
            exchange="NSE", isin=f"INM{ticker[-4:]}00000000",
        )
        phase1_db.add(row)
        phase1_db.commit()
    return row


class TestQuotePersistence:
    def test_quote_row_carries_the_required_fields(self, phase1_db):
        company = _company(phase1_db)
        quote = mock_quote(company.ticker)
        row = upsert_quote(phase1_db, company, quote, provider="mock")

        assert row.company_id == company.id
        assert row.symbol == company.ticker
        assert row.exchange == "NSE"
        assert row.ltp == quote.price
        assert row.volume == quote.volume
        assert row.market_status in {"open", "closed", "weekend"}
        assert row.provider == "mock"
        assert row.fetched_at is not None
        assert row.week_52_low <= row.ltp <= row.week_52_high

    def test_second_sync_updates_in_place_one_row(self, phase1_db):
        company = _company(phase1_db)
        first = upsert_quote(phase1_db, company, mock_quote(company.ticker), provider="mock")
        original = first.ltp          # plain float: the session may refresh the object
        later = Quote(price=original + 10.0, previous_close=original,
                      change=10.0, percent_change=1.0)
        upsert_quote(phase1_db, company, later, provider="mock")

        assert phase1_db.scalar(select(func.count()).select_from(MarketQuote)) == 1
        row = phase1_db.get(MarketQuote, company.id)
        assert row.ltp == original + 10.0

    def test_provenance_follows_the_row_not_the_config(self, phase1_db):
        """A row written by the real tier stays labelled real even after the
        deployment switches providers — the label travels with the data."""
        company = _company(phase1_db)
        upsert_quote(phase1_db, company, mock_quote(company.ticker), provider="yahoo")
        assert phase1_db.get(MarketQuote, company.id).provider == "yahoo"


class TestHistoricalPrices:
    def test_bars_upsert_is_idempotent(self, phase1_db):
        ticker = "MCK0042"
        _company(phase1_db, ticker)
        bars = mock_history(ticker, 30)
        written = upsert_daily_bars(phase1_db, ticker, bars, provider="mock")
        assert written == 30
        assert phase1_db.scalar(select(func.count()).select_from(PriceHistory)) == 30

        # Same bars again: same count, no duplicates.
        upsert_daily_bars(phase1_db, ticker, bars, provider="mock")
        assert phase1_db.scalar(select(func.count()).select_from(PriceHistory)) == 30

        # A shifted series (one bar changed): still 30 rows, value updated.
        changed = [dict(bar) for bar in bars]
        changed[-1]["close"] = changed[-1]["close"] + 1.0
        upsert_daily_bars(phase1_db, ticker, changed, provider="mock")
        assert phase1_db.scalar(select(func.count()).select_from(PriceHistory)) == 30
        series = price_series(phase1_db, ticker)
        assert series[-1].close == changed[-1]["close"]

    def test_ohlc_and_provider_are_stored(self, phase1_db):
        ticker = "MCK0043"
        _company(phase1_db, ticker)
        upsert_daily_bars(phase1_db, ticker, mock_history(ticker, 5), provider="mock")
        bars = price_series(phase1_db, ticker)
        assert len(bars) == 5
        for bar in bars:
            assert bar.day_open is not None
            assert bar.day_high >= bar.day_low
            assert bar.provider == "mock"
            assert bar.day_low <= bar.close <= bar.day_high

    def test_range_selection(self, phase1_db):
        ticker = "MCK0044"
        _company(phase1_db, ticker)
        # 40 trading days ≈ 56 calendar days, so 1M (31 calendar days) holds
        # a strict subset — trading days, weekends excluded.
        upsert_daily_bars(phase1_db, ticker, mock_history(ticker, 40), provider="mock")
        assert len(bars_for_range(phase1_db, ticker, "1D")) == 1
        week = bars_for_range(phase1_db, ticker, "1W")
        assert 1 <= len(week) <= 7
        month = bars_for_range(phase1_db, ticker, "1M")
        assert 7 < len(month) < 40
        everything = bars_for_range(phase1_db, ticker, "MAX")
        assert len(everything) == 40
        # Ranges are contiguous suffixes of the same series.
        dates = [b.as_of for b in everything]
        assert dates[-1] == bars_for_range(phase1_db, ticker, "1D")[0].as_of


class TestQuoteAndPriceAPI:
    def test_quote_endpoint_reports_provenance(self, phase1_client, phase1_db, mock_provider_mode):
        company = _company(phase1_db)
        upsert_quote(phase1_db, company, mock_quote(company.ticker), provider="mock")

        response = phase1_client.get(f"/api/v1/companies/{company.id}/quote")
        assert response.status_code == 200
        body = response.json()
        assert body["data_kind"] == "mock"
        assert body["provider"] == "mock"
        assert body["ltp"] > 0

    def test_quote_endpoint_404_when_never_synced(self, phase1_client, phase1_db):
        company = _company(phase1_db, "MCK9999")
        assert phase1_client.get(
            f"/api/v1/companies/{company.id}/quote"
        ).status_code == 404

    def test_prices_endpoint_serves_bars_with_granularity_label(self, phase1_client, phase1_db):
        company = _company(phase1_db, "MCK0045")
        upsert_daily_bars(phase1_db, company.ticker, mock_history(company.ticker, 25),
                          provider="mock")
        response = phase1_client.get(
            f"/api/v1/companies/{company.id}/prices", params={"range": "1M"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["granularity"] == "daily"
        assert body["data_kind"] == "mock"
        # 25 trading days ≈ 35 calendar days; the 1M window keeps ~3 weeks.
        assert 15 <= len(body["bars"]) <= 25
        first = body["bars"][0]
        assert {"date", "open", "high", "low", "close", "volume"} <= set(first)

    def test_prices_endpoint_rejects_unknown_range(self, phase1_client, phase1_db):
        company = _company(phase1_db, "MCK0046")
        assert phase1_client.get(
            f"/api/v1/companies/{company.id}/prices",
            params={"range": "2Y"},
        ).status_code == 422

    def test_data_status_endpoint(self, phase1_client, phase1_db, mock_provider_mode):
        from app.data.mock_financials import upsert_mock_financials

        company = _company(phase1_db, "MCK0050")
        upsert_mock_financials(phase1_db, company.ticker, years=5)
        upsert_quote(phase1_db, company, mock_quote(company.ticker), provider="mock")

        body = phase1_client.get(
            f"/api/v1/companies/{company.id}/data-status"
        ).json()
        assert body["has_financials"] is True
        assert body["fact_count"] > 0
        assert body["fiscal_years"] == 5
        assert body["has_quote"] is True
        assert body["financial_sources"] == ["mock (synthetic)"]
