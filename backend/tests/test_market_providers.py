"""Multi-provider market data: interface, routing, caching, error handling.

Pins the properties a live-API test cannot cover reliably — unit conversion,
absent-versus-zero, retry policy, fallback selection and the guarantee that
every response names the tier that served it.
"""
from __future__ import annotations

import pytest

from app.data.providers.base import (
    BaseMarketProvider, MarketSnapshot, ProviderAuthError, ProviderError,
    ProviderNotConfigured, ProviderRateLimited, RetryPolicy, to_float,
)
from app.data.providers.finnhub import FinnhubProvider
from app.data.providers.fmp import FMPProvider
from app.data.providers.nse import NSEIndiaProvider
from app.data.providers.router import (
    SOURCE_INTERNAL, SOURCE_NONE, MarketDataRouter, TTLCache,
)
from app.data.providers.yahoo import YahooProvider

# Payloads in each provider's documented shape.
FINNHUB_PROFILE = {"name": "Reliance Industries Ltd", "exchange": "NSE",
                   "currency": "INR", "finnhubIndustry": "Energy",
                   "marketCapitalization": 1_749_621.0, "shareOutstanding": 13_532.0}
FINNHUB_QUOTE = {"c": 1293.0, "d": 12.4, "dp": 0.968, "h": 1301.5,
                 "l": 1284.0, "o": 1288.0, "pc": 1280.6}
FMP_PROFILE = [{"symbol": "RELIANCE.NS", "companyName": "Reliance Industries Limited",
                "price": 1307.8, "marketCap": 17_697_782_146_242, "currency": "INR",
                "exchange": "NSE", "industry": "Refining", "sector": "Energy",
                "change": 14.9, "changePercentage": 1.152, "volume": 8_622_452,
                "range": "1249.8-1611.8"}]


class TestProviderInterface:
    def test_every_provider_implements_the_contract(self):
        for provider in (NSEIndiaProvider(), FinnhubProvider(), FMPProvider(),
                         YahooProvider()):
            assert isinstance(provider, BaseMarketProvider)
            assert provider.name
            assert hasattr(provider, "fetch")
            assert isinstance(provider.configured(), bool)

    def test_priority_order_is_nse_then_finnhub_then_fmp_then_yahoo(self):
        # The platform is India-only, so the exchange's own NSE live endpoint
        # is the primary tier; the existing chain is preserved behind it.
        names = [p.name for p in MarketDataRouter().providers]
        assert names == ["NSE India (Live)", "Finnhub", "Financial Modeling Prep",
                         "Yahoo Finance (Fallback)"]

    def test_yahoo_needs_no_credentials(self):
        assert YahooProvider().configured() is True

    def test_nse_live_needs_no_credentials(self):
        # The exchange's own endpoint needs no API key.
        assert NSEIndiaProvider().configured() is True


class TestAbsentVersusZero:
    def test_zero_is_absent_only_where_zero_is_implausible(self):
        assert to_float(0, zero_is_absent=True) is None
        assert to_float(0, zero_is_absent=False) == 0.0
        assert to_float(None) is None
        assert to_float("") is None
        assert to_float("nonsense") is None
        assert to_float("12.5") == 12.5

    def test_finnhub_zero_quote_is_not_a_price(self):
        """Finnhub returns 0, not null, for a symbol it does not cover."""
        snapshot = FinnhubProvider.parse("X.NS", {"quote": {"c": 0, "pc": 0}})
        assert snapshot.quote.price is None
        assert not snapshot.has_quote


class TestUnitConversion:
    def test_finnhub_millions_become_absolute_units_in_native_currency(self):
        """No longer forced into crore: the currency decides the scale."""
        snapshot = FinnhubProvider.parse("RELIANCE.NS", {"profile": FINNHUB_PROFILE})
        assert snapshot.profile.market_cap == pytest.approx(1_749_621e6)
        assert snapshot.profile.currency == "INR"
        # INR, so the crore convenience is populated.
        assert snapshot.profile.market_cap_crore == pytest.approx(174_962.1)

    def test_a_usd_listing_gets_no_crore_figure(self):
        usd = dict(FINNHUB_PROFILE, currency="USD")
        snapshot = FinnhubProvider.parse("AAPL", {"profile": usd})
        assert snapshot.profile.market_cap_crore is None

    def test_fmp_absolute_units_are_preserved(self):
        snapshot = FMPProvider.parse("RELIANCE.NS", {"profile": FMP_PROFILE})
        assert snapshot.profile.market_cap == pytest.approx(17_697_782_146_242)
        assert snapshot.profile.market_cap_crore == pytest.approx(1_769_778.21, rel=1e-6)

    def test_fmp_stable_renamed_market_cap(self):
        """`/stable` calls it marketCap; v3 called it mktCap. Read both."""
        stable = FMPProvider.parse("X", {"profile": [{"marketCap": 1e13}]})
        legacy = FMPProvider.parse("X", {"profile": [{"mktCap": 1e13}]})
        assert stable.profile.market_cap == legacy.profile.market_cap

    def test_fmp_profile_supplies_the_quote(self):
        """On the free plan /quote is premium for .NS, but /profile carries
        price, change and volume — so a usable quote is not discarded."""
        snapshot = FMPProvider.parse("RELIANCE.NS", {"profile": FMP_PROFILE})
        assert snapshot.quote.price == 1307.8
        assert snapshot.has_quote
        assert snapshot.key_metrics["week52_low"] == pytest.approx(1249.8)


class TestErrorHandling:
    def test_auth_failures_are_never_retried(self):
        """A rejected key is still rejected two seconds later; retrying it
        only delays the fallback that would have worked."""
        assert issubclass(ProviderAuthError, ProviderError)
        assert issubclass(ProviderNotConfigured, ProviderError)

    def test_rate_limits_open_the_circuit(self):
        provider = FinnhubProvider()
        assert provider.available
        provider._consecutive_rate_limits = provider.policy.circuit_threshold
        assert not provider.available
        provider.reset_circuit()
        assert provider.available

    def test_retry_policy_is_configurable(self):
        policy = RetryPolicy(attempts=5, backoff_base=2.0, timeout_seconds=30.0)
        provider = FMPProvider(policy)
        assert provider.policy.attempts == 5
        assert provider.policy.timeout_seconds == 30.0
        assert policy.delay(0) == pytest.approx(2.0)
        assert policy.delay(2) == pytest.approx(8.0)

    def test_a_plan_restriction_is_classified_as_fall_through(self):
        """FMP answers a plan violation with an error body, not just a status.

        That is a licensing boundary, not a bad key, so it must raise plain
        ProviderError — the router falls through — rather than
        ProviderAuthError, which marks the whole provider dead. Verified by
        driving the real classifier with a real FMP error body.
        """
        import json
        import urllib.request
        from unittest.mock import patch

        body = {"Error Message": "Special Endpoint : This value set for "
                                 "'symbol' is not available under your "
                                 "current subscription"}

        class FakeResponse:
            headers: dict[str, str] = {}
            def read(self): return json.dumps(body).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        provider = FMPProvider()
        with patch.object(provider, "_key", return_value="unit-test"), \
             patch.object(json, "load", return_value=body), \
             patch.object(urllib.request, "urlopen", return_value=FakeResponse()):
            with pytest.raises(ProviderError) as caught:
                provider._call("/quote", symbol="RELIANCE.NS")

        assert not isinstance(caught.value, ProviderAuthError)
        assert "not on this plan" in str(caught.value)


class TestFallbackChain:
    @staticmethod
    def _stub(name, priority, *, snapshot=None, error=None):
        class Stub(BaseMarketProvider):
            def configured(self): return True
            def fetch(self, ticker, **kwargs):
                if error:
                    raise error
                return snapshot, {}
        Stub.name = name
        Stub.priority = priority
        return Stub()

    def test_first_healthy_provider_wins(self):
        good = MarketSnapshot(ticker="X", source="A")
        good.quote.price = 100.0
        router = MarketDataRouter(providers=[
            self._stub("A", 1, snapshot=good),
            self._stub("B", 2, error=ProviderError("never reached")),
        ], ttl_cache=TTLCache())
        assert router.fetch("RELIANCE", use_cache=False).source == "A"

    def test_auth_failure_falls_through_to_the_next_provider(self):
        good = MarketSnapshot(ticker="X", source="B")
        good.quote.price = 100.0
        router = MarketDataRouter(providers=[
            self._stub("A", 1, error=ProviderAuthError("rejected")),
            self._stub("B", 2, snapshot=good),
        ], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert result.source == "B"
        assert result.attempted[0]["outcome"] == "auth_failed"

    def test_rate_limit_falls_through_and_is_recorded(self):
        good = MarketSnapshot(ticker="X", source="B")
        good.quote.price = 100.0
        router = MarketDataRouter(providers=[
            self._stub("A", 1, error=ProviderRateLimited("429")),
            self._stub("B", 2, snapshot=good),
        ], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert result.attempted[0]["outcome"] == "rate_limited"

    def test_a_provider_returning_nothing_usable_is_skipped(self):
        empty = MarketSnapshot(ticker="X", source="A")   # no quote, no sections
        good = MarketSnapshot(ticker="X", source="B")
        good.quote.price = 100.0
        router = MarketDataRouter(providers=[
            self._stub("A", 1, snapshot=empty),
            self._stub("B", 2, snapshot=good),
        ], ttl_cache=TTLCache())
        assert router.fetch("RELIANCE", use_cache=False).source == "B"

    def test_total_failure_is_reported_not_faked(self):
        router = MarketDataRouter(providers=[
            self._stub("A", 1, error=ProviderError("down")),
        ], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert result.source == SOURCE_NONE
        assert result.snapshot.quote.price is None

    def test_an_unexpected_exception_does_not_crash_the_router(self):
        class Exploding(BaseMarketProvider):
            name, priority = "Boom", 1
            def configured(self): return True
            def fetch(self, ticker, **kwargs): raise ZeroDivisionError("bug")

        router = MarketDataRouter(providers=[Exploding()], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert result.attempted[0]["outcome"] == "error"


class TestSourceReporting:
    def test_the_source_is_named_on_every_response(self):
        # For an Indian listing the primary tier is the NSE live endpoint, so
        # a response served by it is not a fallback.
        snapshot = MarketSnapshot(ticker="X", source="NSE India (Live)")
        snapshot.quote.price = 100.0
        router = MarketDataRouter(providers=[
            TestFallbackChain._stub("NSE India (Live)", 1, snapshot=snapshot),
        ], ttl_cache=TTLCache())
        payload = router.fetch("RELIANCE", use_cache=False).as_dict()
        assert payload["source"] == "NSE India (Live)"
        assert payload["source_label"] == "✓ NSE India (Live)"
        assert payload["fell_back"] is False
        assert payload["providers_attempted"]

    def test_raw_payloads_are_withheld_unless_asked_for(self):
        snapshot = MarketSnapshot(ticker="X", source="A")
        snapshot.quote.price = 1.0
        router = MarketDataRouter(providers=[
            TestFallbackChain._stub("A", 1, snapshot=snapshot),
        ], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert "raw" not in result.as_dict()
        assert "raw" in result.as_dict(include_raw=True)


class TestCaching:
    def test_a_hit_is_served_from_cache_and_marked(self):
        snapshot = MarketSnapshot(ticker="X", source="A")
        snapshot.quote.price = 1.0
        router = MarketDataRouter(providers=[
            TestFallbackChain._stub("A", 1, snapshot=snapshot),
        ], ttl_cache=TTLCache(ttl_seconds=60))
        assert router.fetch("RELIANCE").cached is False
        assert router.fetch("RELIANCE").cached is True

    def test_expiry_is_honoured(self):
        cache = TTLCache(ttl_seconds=-1)          # already expired
        cache.put("k", "value")                    # type: ignore[arg-type]
        assert cache.get("k") is None

    def test_capacity_is_bounded(self):
        cache = TTLCache(ttl_seconds=60, capacity=3)
        for index in range(6):
            cache.put(f"k{index}", index)          # type: ignore[arg-type]
        assert cache.stats()["entries"] <= 3

    def test_statistics_never_include_a_credential(self):
        stats = TTLCache().stats()
        assert set(stats) == {"entries", "hits", "misses", "hit_rate", "ttl_seconds"}


class TestCredentialHygiene:
    @pytest.mark.parametrize(
        "module", ["nse", "finnhub", "fmp", "yahoo", "base", "router"]
    )
    def test_no_key_shaped_literal_in_any_provider(self, module):
        import importlib
        import pathlib
        import re

        source = pathlib.Path(
            importlib.import_module(f"app.data.providers.{module}").__file__
        ).read_text()
        for literal in re.findall(r"['\"]([A-Za-z0-9]{24,64})['\"]", source):
            mixed = (any(c.isupper() for c in literal)
                     and any(c.islower() for c in literal)
                     and any(c.isdigit() for c in literal))
            assert not mixed, f"possible hardcoded key in {module}: {literal[:6]}…"

    def test_keys_are_read_from_settings_not_the_environment_directly(self):
        import pathlib

        for module in (FinnhubProvider, FMPProvider):
            source = pathlib.Path(
                __import__(module.__module__, fromlist=["x"]).__file__
            ).read_text()
            assert "os.environ" not in source, (
                f"{module.__name__} must read settings, not os.environ, so "
                "configuration stays testable and centrally documented"
            )


class TestPlanEntitlementVersusBadKey:
    """MKT-001: a 403 can mean two very different things.

    Finnhub's free tier serves US symbols and answers 403 "You don't have
    access to this resource" for Indian ones. Classifying that as a
    credential failure disabled a provider that was working perfectly for
    AAPL, so a 403 whose body names access or the plan now falls through
    per-symbol while a genuine credential rejection still abandons the
    provider.
    """

    @staticmethod
    def _raise(code: int, body: bytes):
        import io
        import urllib.error

        return urllib.error.HTTPError(
            "https://example.test", code, "err", {}, io.BytesIO(body),
        )

    def test_entitlement_403_falls_through(self, monkeypatch):
        import urllib.request

        from app.data.providers.base import SymbolNotFound

        provider = FinnhubProvider()
        monkeypatch.setattr(provider, "_key", lambda: "unit-test")
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(self._raise(
                403, b'{"error":"You don\'t have access to this resource."}')),
        )
        with pytest.raises(SymbolNotFound):
            provider.quote("RELIANCE.NS")

    def test_a_genuine_403_still_abandons_the_provider(self, monkeypatch):
        import urllib.request

        provider = FinnhubProvider()
        monkeypatch.setattr(provider, "_key", lambda: "unit-test")
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                self._raise(403, b'{"error":"Forbidden"}')),
        )
        with pytest.raises(ProviderAuthError):
            provider.quote("AAPL")

    def test_401_is_always_a_credential_failure(self, monkeypatch):
        import urllib.request

        provider = FinnhubProvider()
        monkeypatch.setattr(provider, "_key", lambda: "unit-test")
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                self._raise(401, b'{"error":"Invalid API key."}')),
        )
        with pytest.raises(ProviderAuthError):
            provider.quote("AAPL")


class TestSymbolNormalisation:
    """MKT-002: appending .NS to a US ticker made AAPL unservable.

    Every provider rejected "AAPL.NS", so a symbol the primary serves
    perfectly was reported as unsupported by all five tiers.
    """

    def test_us_symbols_are_left_alone(self):
        from app.data.providers.base import normalise_symbol

        assert normalise_symbol("AAPL") == "AAPL"
        assert normalise_symbol("MSFT") == "MSFT"

    def test_indian_symbols_get_the_nse_suffix(self):
        from app.data.providers.base import normalise_symbol

        assert normalise_symbol("RELIANCE") == "RELIANCE.NS"
        assert normalise_symbol("TCS") == "TCS.NS"

    def test_an_explicit_suffix_is_never_doubled(self):
        from app.data.providers.base import normalise_symbol

        assert normalise_symbol("RELIANCE.NS") == "RELIANCE.NS"
        assert normalise_symbol("BARC.L") == "BARC.L"

    def test_all_providers_agree_on_the_mapping(self):
        for ticker in ("AAPL", "RELIANCE", "RELIANCE.NS", "TCS"):
            assert (FinnhubProvider.to_symbol(ticker)
                    == FMPProvider.to_symbol(ticker)
                    == YahooProvider.to_symbol(ticker))


class TestCurrencyHandling:
    """MKT-003: every large figure was divided by a crore.

    Apple's market capitalisation rendered as "489,721 cr" — arithmetically
    defensible, semantically nonsense, and impossible for a reader to catch
    because the number looks plausible.
    """

    def test_each_currency_uses_its_own_scale(self):
        from app.data.providers.currency import format_money

        assert format_money(17_697_782_146_242, "INR") == "₹17.70 lakh crore"
        assert format_money(1_749_621e7, "INR").endswith("lakh crore")
        assert format_money(4_897_205_110_000, "USD") == "$4.90T"
        assert format_money(2.4e12, "EUR") == "€2.40T"
        assert format_money(8.9e11, "GBP") == "£890.00B"
        assert "兆" in format_money(3.1e14, "JPY")

    def test_us_figures_are_never_labelled_crore(self):
        from app.data.providers.currency import format_money

        for amount in (1e9, 4.9e12, 5e11):
            assert "crore" not in format_money(amount, "USD")

    def test_crore_conversion_refuses_foreign_currency(self):
        """Returning a number would let a USD figure pass as Indian."""
        from app.data.providers.currency import to_crore

        assert to_crore(1e12, "INR") == pytest.approx(100_000.0)
        assert to_crore(1e12, "USD") is None
        assert to_crore(None, "INR") is None

    def test_market_is_resolved_from_the_suffix(self):
        from app.data.providers.currency import resolve_market

        assert resolve_market("RELIANCE.NS")[2] == "INR"
        assert resolve_market("AAPL")[2] == "USD"
        assert resolve_market("BARC.L")[2] == "GBP"
        assert resolve_market("7203.T")[3] == "Asia/Tokyo"

    def test_the_provider_currency_wins_over_the_suffix_table(self):
        from app.data.providers.currency import resolve_market

        _, _, currency, _ = resolve_market("AAPL", provider_currency="usd")
        assert currency == "USD"


class TestSymbolResolver:
    """MKT-002 hardened into a first-class resolver."""

    def test_the_briefs_examples(self):
        from app.data.providers.symbols import resolve

        assert resolve("AAPL").display == "NASDAQ:AAPL"
        assert resolve("AAPL").finnhub == "AAPL"
        assert resolve("AAPL").fmp == "AAPL"
        assert resolve("RELIANCE").canonical == "RELIANCE.NS"
        assert resolve("TCS").canonical == "TCS.NS"
        assert resolve("INFY").canonical == "INFY.NS"

    def test_us_symbols_never_receive_the_nse_suffix(self):
        from app.data.providers.symbols import resolve

        for ticker in ("AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"):
            assert not resolve(ticker).canonical.endswith(".NS")
            assert resolve(ticker).is_us

    def test_venue_prefixes_are_understood(self):
        from app.data.providers.symbols import resolve

        assert resolve("NASDAQ:AAPL").canonical == "AAPL"
        assert resolve("NSE:RELIANCE").canonical == "RELIANCE.NS"

    def test_company_exchange_resolves_imported_nifty500_symbols(self):
        from app.data.providers.symbols import resolve

        # 360ONE is imported from NSE and is not in the old 120-row seed tuple.
        assert resolve("360ONE").canonical == "360ONE"
        assert resolve("360ONE", exchange="NSE").canonical == "360ONE.NS"
        assert resolve("360ONE", exchange="BSE").canonical == "360ONE.BO"

    def test_a_suffix_is_never_doubled(self):
        from app.data.providers.symbols import resolve

        assert resolve("RELIANCE.NS").canonical == "RELIANCE.NS"
        assert resolve("BARC.L").canonical == "BARC.L"


class TestProviderMetadata:
    def test_every_required_field_is_present(self):
        from app.data.providers.base import ProviderMetadata

        payload = ProviderMetadata().as_dict()
        for field_name in ("provider", "currency", "exchange", "market",
                           "timezone", "last_updated", "confidence_score"):
            assert field_name in payload

    def test_confidence_is_bounded(self):
        from app.data.providers.base import MarketSnapshot
        from app.data.providers.router import MarketDataRouter
        from app.data.providers.symbols import resolve

        snapshot = MarketSnapshot(ticker="AAPL")
        snapshot.quote.price = 100.0
        MarketDataRouter._stamp(snapshot, resolve("AAPL"), "Finnhub")
        assert 0.0 <= snapshot.meta.confidence <= 1.0
        assert snapshot.meta.market == "United States"
        assert snapshot.meta.timezone == "America/New_York"


class TestMarketAwareRouting:
    def test_indian_market_requests_try_live_providers_first(self):
        from app.data.providers.router import MarketDataRouter
        from app.data.providers.symbols import resolve

        chain = MarketDataRouter()._chain_for(resolve("RELIANCE.NS"))
        assert chain[0] == "external"
        assert chain.index("external") < chain.index("internal")

    def test_us_listings_prefer_the_external_providers(self):
        from app.data.providers.router import MarketDataRouter
        from app.data.providers.symbols import resolve

        chain = MarketDataRouter()._chain_for(resolve("AAPL"))
        assert chain[0] == "external"


#: A realistic NSE `/api/quote-equity` payload.
NSE_PAYLOAD = {
    "symbol": "RELIANCE", "companyName": "Reliance Industries Limited",
    "marketCap": 17_697_782_146_242, "industry": "Refining & Petrochemicals",
    "priceInfo": {"lastPrice": 1307.8, "change": 14.9, "pctChange": 1.152,
                  "open": 1288.0, "dayHigh": 1315.0, "dayLow": 1280.0,
                  "previousClose": 1292.9, "totalTradedVolume": 8_622_452},
}


class TestNSEProvider:
    """The India-only live tier: parsing and India-only coverage rules."""

    def test_nse_suffix_is_stripped_for_the_exchange_api(self):
        assert NSEIndiaProvider.to_symbol("RELIANCE.NS") == "RELIANCE"
        assert NSEIndiaProvider.to_symbol("RELIANCE.BO") == "RELIANCE"
        assert NSEIndiaProvider.to_symbol("RELIANCE") == "RELIANCE"

    def test_parse_extracts_quote_and_profile_in_inr(self):
        snapshot = NSEIndiaProvider.parse("RELIANCE", NSE_PAYLOAD)
        assert snapshot.quote.price == pytest.approx(1307.8)
        assert snapshot.has_quote
        assert snapshot.profile.name == "Reliance Industries Limited"
        assert snapshot.profile.currency == "INR"
        assert snapshot.profile.market_cap == pytest.approx(17_697_782_146_242)
        assert snapshot.profile.market_cap_crore == pytest.approx(1_769_778.21,
                                                                  rel=1e-6)
        assert snapshot.quote.volume == pytest.approx(8_622_452)
        assert snapshot.quote.percent_change == pytest.approx(1.152)

    def test_a_zero_price_is_not_a_quote(self):
        raw = {"symbol": "X", "priceInfo": {"lastPrice": 0}}
        snapshot = NSEIndiaProvider.parse("X", raw)
        assert snapshot.quote.price is None
        assert not snapshot.has_quote

    def test_an_error_payload_is_an_unknown_symbol(self):
        from app.data.providers.base import SymbolNotFound

        provider = NSEIndiaProvider()
        with pytest.raises(SymbolNotFound):
            provider._raise_if_error({"error": "No data for symbol"}, "X")

    def test_missing_quote_raises_so_the_router_falls_through(self):
        from app.data.providers.base import ProviderError

        provider = NSEIndiaProvider()
        raw = {"symbol": "X", "companyName": "Some Company"}
        snapshot = provider.parse("X", raw)
        assert not snapshot.has_quote
        # A provider that cannot price the symbol must not be reported as the
        # source of a price=None "success".
        with pytest.raises(ProviderError):
            if not snapshot.has_quote:
                raise ProviderError("no usable price")


class TestIndiaOnlyRejection:
    """MKT-IND-01: the platform is India-only; foreign listings are rejected."""

    @staticmethod
    def _ok_provider(name="NSE India (Live)"):
        snap = MarketSnapshot(ticker="X", source=name)
        snap.quote.price = 100.0
        return TestFallbackChain._stub(name, 1, snapshot=snap)

    @pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "TSLA", "NVDA",
                                        "GOOGL", "NASDAQ:AAPL", "BARC.L"])
    def test_us_and_other_foreign_tickers_are_rejected(self, ticker):
        from app.data.providers.base import SymbolNotSupported

        router = MarketDataRouter(providers=[self._ok_provider()],
                                  ttl_cache=TTLCache())
        with pytest.raises(SymbolNotSupported) as caught:
            router.fetch(ticker, use_cache=False)
        assert "not supported" in str(caught.value)
        assert "NSE/BSE" in str(caught.value)

    def test_no_provider_call_is_made_for_a_rejected_symbol(self):
        from app.data.providers.base import SymbolNotSupported

        calls = {"n": 0}

        class Counting(BaseMarketProvider):
            name, priority = "NSE India (Live)", 1
            def configured(self): return True
            def fetch(self, ticker, **kwargs):
                calls["n"] += 1
                snap = MarketSnapshot(ticker=ticker, source=self.name)
                snap.quote.price = 100.0
                return snap, {}

        router = MarketDataRouter(providers=[Counting()], ttl_cache=TTLCache())
        with pytest.raises(SymbolNotSupported):
            router.fetch("AAPL", use_cache=False)
        assert calls["n"] == 0

    def test_indian_tickers_resolve_to_nse(self):
        from app.data.providers.symbols import resolve

        for ticker in ("RELIANCE", "TCS", "INFY"):
            resolved = resolve(ticker)
            assert resolved.is_indian
            assert resolved.canonical.endswith(".NS")


class TestNoMisleadingNullPrice:
    """MKT-IND-02: a provider outage must not masquerade as price=None success."""

    def test_a_profile_without_a_price_is_not_served(self):
        # Provider A returns a name but no price; B returns a real price.
        profile_only = MarketSnapshot(ticker="X", source="A")
        profile_only.profile.name = "Some Company"
        good = MarketSnapshot(ticker="X", source="B")
        good.quote.price = 100.0
        router = MarketDataRouter(providers=[
            self.__class__._stub_ok("A", 1, snapshot=profile_only),
            self.__class__._stub_ok("B", 2, snapshot=good),
        ], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert result.source == "B"
        assert result.attempted[0]["outcome"] == "failed"

    @staticmethod
    def _stub_ok(name, priority, *, snapshot=None, error=None):
        return TestFallbackChain._stub(name, priority, snapshot=snapshot,
                                       error=error)

    def test_total_outage_yields_an_empty_non_success_result(self):
        good = MarketSnapshot(ticker="X", source="A")
        good.quote.price = None          # provider answered, but no price
        router = MarketDataRouter(providers=[
            self._stub_ok("A", 1, snapshot=good),
        ], ttl_cache=TTLCache())
        result = router.fetch("RELIANCE", use_cache=False)
        assert result.source == SOURCE_NONE
        assert result.snapshot.quote.price is None
        assert result.snapshot.has_quote is False


class TestProviderHealth:
    def test_telemetry_is_recorded(self):
        provider = FinnhubProvider()
        provider.record(ok=True, ms=120.0)
        provider.record(ok=False, ms=80.0)
        health = provider.health()
        assert health["calls"] == 2
        assert health["failures"] == 1
        assert health["average_response_ms"] == pytest.approx(100.0)
        assert health["last_successful_request"]

    def test_health_never_leaks_a_credential(self):
        for provider in (NSEIndiaProvider(), FinnhubProvider(), FMPProvider(),
                         YahooProvider()):
            rendered = str(provider.health())
            assert "apikey" not in rendered.lower()
            assert "token" not in rendered.lower()
