"""Phase 1 — the deterministic mock provider and the provider switch.

Guarantees under test:
* determinism (same symbol → identical quote/bars, across provider instances)
* offline by construction
* DATA_PROVIDER chains are mutually exclusive — mock mode constructs no real
  provider, real mode constructs no mock — so mock rows can never mix into
  real records.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.data.providers.mock import (
    MockMarketProvider, mock_bar, mock_close, mock_history, mock_quote,
)


class TestDeterminism:
    def test_same_symbol_same_quote_across_instances(self):
        a = mock_quote("MCK0042")
        b = MockMarketProvider().fetch_quote("MCK0042")
        assert a.price == b.price
        assert a.volume == b.volume
        assert a.week_52_high == b.week_52_high

    def test_same_symbol_same_history(self):
        assert mock_history("MCK0042", 30) == MockMarketProvider().fetch_history("MCK0042", 30)

    def test_history_is_date_stable(self):
        """A bar for a past date never changes as today moves."""
        day = date.today() - timedelta(days=10)
        assert mock_close("MCK0042", day) == mock_close("MCK0042", day)
        bar = mock_bar("MCK0042", day)
        assert bar["date"] == day.isoformat()

    def test_different_symbols_differ(self):
        assert mock_quote("MCK0001").price != mock_quote("MCK0002").price

    def test_weekends_are_excluded(self):
        for bar in mock_history("MCK0042", 60):
            d = date.fromisoformat(bar["date"])
            assert d.weekday() < 5

    def test_quote_shape_is_complete(self):
        q = mock_quote("MCK0042")
        assert q.price and q.price > 0
        assert q.previous_close and q.previous_close > 0
        assert q.day_low <= q.price <= q.day_high
        assert q.week_52_low <= q.price <= q.week_52_high
        assert q.market_status in {"open", "closed", "weekend"}
        assert q.volume and q.volume > 0


class TestProviderSwitch:
    def test_mock_mode_constructs_only_the_mock_provider(self, mock_provider_mode):
        from app.data.providers.router import default_providers, get_router

        chain = default_providers()
        assert len(chain) == 1
        assert chain[0].name == "Mock (synthetic)"
        router = get_router()
        assert [p.name for p in router.providers] == ["Mock (synthetic)"]

    def test_real_mode_constructs_no_mock_provider(self):
        from app.data.providers.router import default_providers, reset_router

        reset_router()
        chain = default_providers()
        assert all("mock" not in p.name.lower() for p in chain)
        assert {p.name for p in chain} >= {
            "Yahoo Finance (Fallback)",
        }

    def test_active_provider_name_labels_provenance(self, mock_provider_mode):
        from app.data.providers.router import active_provider_name

        assert active_provider_name() == "mock"

    def test_mock_snapshot_is_labelled_synthetic(self, mock_provider_mode):
        from app.data.providers.router import get_router

        result = get_router().fetch(
            "MCK0042", use_cache=False, include_history=True,
        )
        assert result.snapshot.source == "Mock (synthetic)"
        assert "synthetic" in result.snapshot.profile.description.lower()
        assert result.snapshot.quote.price > 0


class TestOffline:
    def test_no_network_is_used(self, monkeypatch):
        """Any socket use fails the test — the mock must be pure computation."""
        import socket

        def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("mock provider attempted a network call")

        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "socket", refuse)
        provider = MockMarketProvider()
        snapshot, raw = provider.fetch("MCK0042", include_history=True)
        assert snapshot.quote.price > 0
        assert raw["mock"]["deterministic"] is True
