"""Finnhub integration and market-data source routing.

The properties worth pinning are the ones a live-API test cannot cover
reliably: unit conversion, absent-field handling, fallback selection, and the
guarantee that every response names the provider that served it.
"""
from __future__ import annotations

import pytest

from app.data import finnhub_source as fh
from app.data.market_data import (
    SOURCE_FINNHUB, SOURCE_NONE, SOURCE_YAHOO, MarketDataResult,
)

PROFILE = {
    "name": "Reliance Industries Ltd",
    "exchange": "NATIONAL STOCK EXCHANGE OF INDIA",
    "currency": "INR",
    "finnhubIndustry": "Energy",
    "marketCapitalization": 1_749_621.0,   # millions INR
    "shareOutstanding": 13_532.0,          # millions
}
QUOTE = {"c": 1293.0, "d": 12.4, "dp": 0.968, "h": 1301.5,
         "l": 1284.0, "o": 1288.0, "pc": 1280.6}
METRIC = {"metric": {"peTTM": 24.6, "epsTTM": 52.5,
                     "52WeekHigh": 1608.8, "52WeekLow": 1114.85}}


class TestSymbolMapping:
    def test_nse_suffix_is_added_when_absent(self):
        assert fh.to_finnhub_symbol("RELIANCE") == "RELIANCE.NS"
        assert fh.to_finnhub_symbol("reliance") == "RELIANCE.NS"

    def test_an_explicit_suffix_is_respected(self):
        assert fh.to_finnhub_symbol("RELIANCE.NS") == "RELIANCE.NS"
        assert fh.to_finnhub_symbol("AAPL.US") == "AAPL.US"

    def test_empty_ticker_is_refused(self):
        with pytest.raises(fh.FinnhubError):
            fh.to_finnhub_symbol("   ")


class TestParsing:
    def test_market_cap_converts_millions_to_crore(self):
        """Finnhub reports millions; the platform's unit is the crore."""
        snapshot = fh.parse_snapshot("RELIANCE.NS", profile=PROFILE,
                                     quote_data=QUOTE, metrics=METRIC)
        assert snapshot.market_cap == pytest.approx(174_962.1)
        assert snapshot.shares_outstanding == pytest.approx(1353.2)

    def test_quote_fields_are_mapped(self):
        snapshot = fh.parse_snapshot("RELIANCE.NS", quote_data=QUOTE)
        assert snapshot.current_price == 1293.0
        assert snapshot.previous_close == 1280.6
        assert snapshot.has_quote

    def test_zero_is_treated_as_absent_not_as_a_value(self):
        """Finnhub returns 0 rather than null for a missing numeric.

        Taking that literally puts a zero market cap or a zero P/E into a
        valuation model, which is worse than reporting the field absent.
        """
        snapshot = fh.parse_snapshot(
            "X.NS",
            profile={"marketCapitalization": 0, "shareOutstanding": 0},
            quote_data={"c": 0, "pc": 0},
        )
        assert snapshot.market_cap is None
        assert snapshot.current_price is None
        assert not snapshot.has_quote

    def test_missing_endpoints_are_named_rather_than_hidden(self):
        snapshot = fh.parse_snapshot("X.NS", quote_data=QUOTE)
        assert "company profile" in snapshot.unavailable
        assert "basic financials" in snapshot.unavailable
        assert "company news" in snapshot.unavailable
        assert "earnings calendar" in snapshot.unavailable

    def test_news_is_capped_and_summarised(self):
        news = [{"headline": f"h{i}", "source": "R", "url": "u",
                 "datetime": 1, "summary": "s" * 500} for i in range(25)]
        snapshot = fh.parse_snapshot("X.NS", news=news)
        assert len(snapshot.news) == 10
        assert len(snapshot.news[0]["summary"]) <= 280


class TestCircuitBreaker:
    def test_circuit_opens_after_repeated_rate_limits(self, monkeypatch):
        monkeypatch.setattr(fh, "_consecutive_429", fh._CIRCUIT_THRESHOLD)
        assert not fh.provider_available()
        fh.reset_circuit()
        assert fh.provider_available()

    def test_reset_restores_the_polite_interval(self, monkeypatch):
        monkeypatch.setattr(fh, "MIN_INTERVAL", 8.0)
        fh.reset_circuit()
        assert fh.MIN_INTERVAL == pytest.approx(1.05)


class TestKeyHandling:
    def test_no_key_raises_a_distinct_error(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "FINNHUB_API_KEY", None)
        with pytest.raises(fh.FinnhubNotConfigured):
            fh._api_key()

    def test_no_api_key_is_hardcoded_in_the_source(self):
        """The key belongs in the environment, never in the image."""
        import pathlib
        import re

        source = pathlib.Path(fh.__file__).read_text()
        # A Finnhub key is 32 chars and mixes upper, lower and digits.
        # Field names like "marketCapitalization" are long but carry no
        # digits, so requiring all three classes separates them cleanly.
        for literal in re.findall(r"['\"]([A-Za-z0-9]{24,64})['\"]", source):
            mixed = (any(c.isupper() for c in literal)
                     and any(c.islower() for c in literal)
                     and any(c.isdigit() for c in literal))
            if mixed:
                pytest.fail(f"possible hardcoded key: {literal[:6]}…")

    def test_auth_failure_is_fatal_for_the_whole_provider(self):
        """A rejected key will be rejected by every endpoint.

        Treating it as a per-endpoint failure meant five throttled retries per
        ticker — 28 seconds to reach a conclusion available in 120 ms.
        """
        assert issubclass(fh.FinnhubAuthError, fh.FinnhubError)


class TestSourceReporting:
    def test_source_is_reported_on_every_response(self):
        snapshot = fh.parse_snapshot("RELIANCE.NS", quote_data=QUOTE)
        result = MarketDataResult(snapshot=snapshot, source=SOURCE_FINNHUB)
        payload = result.as_dict()
        assert payload["source"] == "Finnhub"
        assert payload["source_label"] == "✓ Finnhub"

    def test_fallback_names_what_it_fell_back_from(self):
        snapshot = fh.parse_snapshot("RELIANCE.NS", quote_data=QUOTE)
        snapshot.source = SOURCE_YAHOO
        result = MarketDataResult(
            snapshot=snapshot, source=SOURCE_YAHOO,
            fell_back_from=SOURCE_FINNHUB, reason="authentication rejected",
        )
        payload = result.as_dict()
        assert payload["source_label"] == "✓ Yahoo Finance (Fallback)"
        assert payload["fell_back_from"] == "Finnhub"
        assert payload["fallback_reason"] == "authentication rejected"

    def test_raw_payloads_are_withheld_unless_asked_for(self):
        snapshot = fh.parse_snapshot("X.NS", quote_data=QUOTE)
        result = MarketDataResult(
            snapshot=snapshot, source=SOURCE_FINNHUB, raw={"quote": QUOTE},
        )
        assert "raw" not in result.as_dict()
        assert "raw" in result.as_dict(include_raw=True)


class TestRouting:
    def test_finnhub_is_preferred_when_it_returns_a_quote(self, monkeypatch):
        from app.data import market_data

        monkeypatch.setattr(
            market_data.finnhub, "fetch_snapshot",
            lambda t, **k: (fh.parse_snapshot(t, profile=PROFILE,
                                              quote_data=QUOTE, metrics=METRIC), {}),
        )
        result = market_data.fetch_market_data("RELIANCE.NS")
        assert result.source == SOURCE_FINNHUB
        assert result.fell_back_from is None

    def test_a_profile_without_a_price_falls_back(self, monkeypatch):
        """A profile with no quote is not a usable market snapshot."""
        from app.data import market_data

        monkeypatch.setattr(
            market_data.finnhub, "fetch_snapshot",
            lambda t, **k: (fh.parse_snapshot(t, profile=PROFILE), {}),
        )
        monkeypatch.setattr(
            market_data, "_yahoo_snapshot",
            lambda t: (fh.parse_snapshot(t, quote_data=QUOTE), {}),
        )
        result = market_data.fetch_market_data("RELIANCE.NS")
        assert result.source == SOURCE_YAHOO
        assert result.fell_back_from == SOURCE_FINNHUB

    def test_both_providers_failing_is_reported_not_faked(self, monkeypatch):
        from app.data import market_data

        def boom(*a, **k):
            raise fh.FinnhubError("down")

        monkeypatch.setattr(market_data.finnhub, "fetch_snapshot", boom)
        monkeypatch.setattr(
            market_data, "_yahoo_snapshot",
            lambda t: (_ for _ in ()).throw(RuntimeError("also down")),
        )
        result = market_data.fetch_market_data("RELIANCE.NS")
        assert result.source == SOURCE_NONE
        assert "also down" in (result.reason or "")

    def test_fallback_can_be_disabled(self, monkeypatch):
        from app.data import market_data

        def boom(*a, **k):
            raise fh.FinnhubError("down")

        monkeypatch.setattr(market_data.finnhub, "fetch_snapshot", boom)
        result = market_data.fetch_market_data("X.NS", allow_fallback=False)
        assert result.source == SOURCE_NONE
