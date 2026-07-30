"""Integration tests for the scoring API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.scoring.weights import Category
from app.main import app

client = TestClient(app)

REF = "BHARATCP"     # coherent economics
SYNTH = "TITAN"      # crude synthetic data


def scoring(ticker: str = REF, **params):
    r = client.get(f"/api/v1/company/{ticker}/scoring", params=params)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    return r.json()


class TestScoringEndpoint:
    def test_returns_thirteen_categories(self):
        assert len(scoring()["categories"]) == 13

    def test_all_expected_categories_present(self):
        keys = {c["key"] for c in scoring()["categories"]}
        assert keys == {c.value for c in Category}

    def test_composite_within_range(self):
        assert 0 <= scoring()["overall_score"] <= 100

    def test_grade_and_stars_present(self):
        body = scoring()
        assert body["grade"] in {"AAA", "AA", "A", "BBB", "BB", "B", "C"}
        assert 0.5 <= body["stars"] <= 5.0

    def test_recommendation_is_one_of_five(self):
        assert scoring()["recommendation"] in {
            "BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL"
        }

    def test_every_category_reports_the_five_required_fields(self):
        for category in scoring()["categories"]:
            assert "raw_score" in category
            assert "weighted_score" in category
            assert "confidence" in category
            assert category["explanation"]
            assert "data_sources" in category

    def test_confidence_shares_sum_to_one(self):
        c = scoring()["confidence"]
        total = (c["verified_pct"] + c["estimated_pct"]
                 + c["analyst_pct"] + c["missing_pct"])
        assert total == pytest.approx(1.0)

    def test_weighted_score_is_raw_times_weight(self):
        for category in scoring()["categories"]:
            assert category["weighted_score"] == pytest.approx(
                category["raw_score"] * category["weight"]
            )

    def test_category_weights_sum_to_one(self):
        weights = sum(c["weight"] for c in scoring()["categories"])
        assert weights == pytest.approx(1.0)

    def test_metrics_carry_explanations_and_sources(self):
        for category in scoring()["categories"]:
            for metric in category["metrics"]:
                assert metric["explanation"]
                assert metric["origin"] in {
                    "verified", "estimated", "analyst", "missing"
                }

    def test_unknown_ticker_404(self):
        assert client.get("/api/v1/company/NOSUCH/scoring").status_code == 404

    def test_unknown_profile_rejected(self):
        r = client.get(f"/api/v1/company/{REF}/scoring", params={"profile": "nonsense"})
        assert r.status_code == 422


class TestWeightProfiles:
    @pytest.mark.parametrize(
        "profile", ["balanced", "conservative", "growth", "value", "quality"]
    )
    def test_every_builtin_profile_scores(self, profile):
        body = scoring(profile=profile)
        assert body["profile_key"] == profile
        assert 0 <= body["overall_score"] <= 100

    def test_profiles_produce_different_scores(self):
        scores = {p: scoring(profile=p)["overall_score"]
                  for p in ("conservative", "growth", "value", "quality")}
        assert len(set(round(s, 2) for s in scores.values())) > 1

    def test_value_profile_weights_valuation_most(self):
        body = scoring(profile="value")
        weights = {c["key"]: c["weight"] for c in body["categories"]}
        assert weights["valuation"] == max(weights.values())

    def test_list_profiles(self):
        body = client.get("/api/v1/scoring/weights").json()
        assert len(body["profiles"]) >= 5
        assert all(p["is_builtin"] for p in body["profiles"][:5])

    def test_profile_weights_are_normalised(self):
        for profile in client.get("/api/v1/scoring/weights").json()["profiles"]:
            assert sum(profile["weights"].values()) == pytest.approx(1.0)

    def test_create_custom_profile(self):
        r = client.put("/api/v1/scoring/weights", json={
            "key": "test_custom", "label": "Test Custom",
            "weights": {"valuation": 50, "financial_risk": 30, "governance": 20},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["is_builtin"] is False
        assert body["weights"]["valuation"] == pytest.approx(0.5)
        assert sum(body["weights"].values()) == pytest.approx(1.0)

    def test_custom_profile_scores(self):
        client.put("/api/v1/scoring/weights", json={
            "key": "deep_value", "label": "Deep Value",
            "weights": {"valuation": 60, "financial_risk": 40},
        })
        body = scoring(profile="deep_value")
        assert body["profile_key"] == "deep_value"
        weights = {c["key"]: c["weight"] for c in body["categories"]}
        assert weights["valuation"] == pytest.approx(0.6)
        assert weights["momentum"] == pytest.approx(0.0)

    def test_cannot_overwrite_a_builtin(self):
        r = client.put("/api/v1/scoring/weights", json={
            "key": "balanced", "label": "Hijacked", "weights": {"valuation": 1},
        })
        assert r.status_code == 422

    def test_unknown_category_rejected(self):
        r = client.put("/api/v1/scoring/weights", json={
            "key": "bad", "label": "Bad", "weights": {"astrology": 1},
        })
        assert r.status_code == 422

    def test_negative_weight_rejected(self):
        r = client.put("/api/v1/scoring/weights", json={
            "key": "neg", "label": "Neg", "weights": {"valuation": -1},
        })
        assert r.status_code == 422

    def test_empty_weights_rejected(self):
        r = client.put("/api/v1/scoring/weights", json={
            "key": "empty", "label": "Empty", "weights": {},
        })
        assert r.status_code == 422


class TestExplanation:
    def _explanation(self, ticker: str = REF):
        r = client.get(f"/api/v1/company/{ticker}/scoring/explanation")
        assert r.status_code == 200
        return r.json()

    def test_category_and_metric_narratives(self):
        body = self._explanation()
        assert len(body["categories"]) == 13
        assert len(body["metrics"]) > 30

    def test_key_positives_and_negatives_ordered(self):
        body = self._explanation()
        positives = [m["score"] for m in body["key_positives"]]
        negatives = [m["score"] for m in body["key_negatives"]]
        assert positives == sorted(positives, reverse=True)
        assert negatives == sorted(negatives)
        if positives and negatives:
            assert positives[0] >= negatives[0]

    def test_data_gaps_are_listed(self):
        body = self._explanation()
        assert all(m["origin"] == "missing" for m in body["data_gaps"])

    def test_explanations_are_prose_not_codes(self):
        """The AI Analyst consumes these; they must be readable sentences."""
        for metric in self._explanation()["metrics"][:20]:
            text = metric["explanation"]
            assert len(text) > 20
            assert text[0].isupper() or text[0].isdigit()
            assert text.rstrip().endswith((".", "!"))

    def test_explanations_cite_figures(self):
        """A useful explanation states the number that drove the score."""
        texts = [m["explanation"] for m in self._explanation()["metrics"]]
        with_numbers = [t for t in texts if any(ch.isdigit() for ch in t)]
        assert len(with_numbers) / len(texts) > 0.6

    def test_summary_and_rationale_present(self):
        body = self._explanation()
        assert body["summary"]
        assert body["recommendation_rationale"]


class TestHistory:
    def test_saving_creates_a_snapshot(self):
        client.get(f"/api/v1/company/{REF}/scoring",
                   params={"profile": "balanced", "save": True})
        body = client.get(f"/api/v1/company/{REF}/scoring/history",
                          params={"profile": "balanced"}).json()
        assert len(body["points"]) >= 1

    def test_snapshot_carries_category_scores(self):
        client.get(f"/api/v1/company/{REF}/scoring",
                   params={"profile": "balanced", "save": True})
        body = client.get(f"/api/v1/company/{REF}/scoring/history",
                          params={"profile": "balanced"}).json()
        assert len(body["points"][-1]["category_scores"]) == 13

    def test_empty_history_is_not_an_error(self):
        body = client.get(f"/api/v1/company/TCS/scoring/history").json()
        assert body["points"] == []
        assert body["trend"] == "flat"

    def test_saving_is_idempotent_within_a_day(self):
        for _ in range(3):
            client.get(f"/api/v1/company/{REF}/scoring",
                       params={"profile": "growth", "save": True})
        body = client.get(f"/api/v1/company/{REF}/scoring/history",
                          params={"profile": "growth"}).json()
        assert len(body["points"]) == 1


class TestPeerComparison:
    def test_returns_the_subject_and_peers(self):
        body = client.get(f"/api/v1/company/{REF}/scoring/peers",
                          params={"limit": 4}).json()
        assert len(body["peers"]) >= 1
        assert any(p["company"]["ticker"] == REF for p in body["peers"])

    def test_sorted_by_score(self):
        body = client.get(f"/api/v1/company/{REF}/scoring/peers",
                          params={"limit": 4}).json()
        scores = [p["overall_score"] for p in body["peers"]]
        assert scores == sorted(scores, reverse=True)

    def test_category_medians_for_radar_overlay(self):
        body = client.get(f"/api/v1/company/{REF}/scoring/peers",
                          params={"limit": 4}).json()
        assert len(body["category_medians"]) == 13


class TestDataQualityIntegration:
    def test_synthetic_company_surfaces_a_warning(self):
        body = scoring(ticker=SYNTH)
        assert any("Illustrative" in w for w in body["warnings"])

    def test_low_confidence_prevents_a_directional_call(self):
        """Every seeded company lacks qualitative data, so confidence is capped."""
        body = scoring(ticker=SYNTH)
        if body["confidence"]["confidence"] < 0.55:
            assert body["recommendation"] in {"HOLD", "REDUCE", "SELL"}

    def test_missing_pct_is_reported_honestly(self):
        body = scoring()
        assert body["confidence"]["missing_pct"] > 0
        assert body["confidence"]["metrics_missing"] > 0


class TestUniverseWide:
    TICKERS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "BHARATCP"]

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_scoring_runs(self, ticker):
        assert client.get(f"/api/v1/company/{ticker}/scoring").status_code == 200

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_composite_is_sane(self, ticker):
        body = scoring(ticker=ticker)
        assert 0 <= body["overall_score"] <= 100
        assert len(body["categories"]) == 13

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_confidence_never_overstated(self, ticker):
        """No seeded company has qualitative data, so none may claim high confidence."""
        assert scoring(ticker=ticker)["confidence"]["confidence"] < 0.95
