"""Source classification, usability, and the ranking rule that matters.

The rule: **every web class ranks below every filing class**. Not "most web
classes" — all of them, including the lowest filing tier. These tests assert
it against the platform's *live* authority table rather than against a copy,
so a later edit to either table that breaks the ordering fails here rather
than in a prompt nobody reads.

Second: nothing in this module may ever reach ``TRUSTED_SOURCES``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.web.types import (
    WebContentClass,
    WebFetchPolicy,
    WebRejectionReason,
    WebSourceClass,
)
from app.services.web.quality import (
    EXCHANGE_HOSTS,
    IR_URL_VERIFIED_CONFIDENCE,
    MEDIA_HOSTS,
    REGULATOR_HOSTS,
    WEB_AUTHORITY,
    assess,
    classify,
    host_is_pinned_by,
    is_primary_source,
    near_duplicate_key,
    web_authority,
)

FILING_TIERS = (
    "annual_report", "quarterly_report", "exchange_filing", "credit_rating",
    "conference_call", "investor_presentation", "esg_report",
    "press_release", "research_note", "other",
)


# ===========================================================================
class TestRankingBelowFilings:
    def test_every_web_class_is_below_every_filing_class(self):
        from app.domain.knowledge.vault import SOURCE_AUTHORITY

        lowest_filing = min(
            SOURCE_AUTHORITY[tier] for tier in FILING_TIERS
        )
        for source_class, weight in WEB_AUTHORITY.items():
            assert weight < lowest_filing, (
                f"{source_class.value} ({weight}) is not below the lowest "
                f"filing tier ({lowest_filing})"
            )

    def test_the_recency_discount_can_never_lift_a_web_class_above_a_filing(
        self,
    ):
        from app.domain.knowledge.vault import SOURCE_AUTHORITY

        lowest_filing = min(SOURCE_AUTHORITY[tier] for tier in FILING_TIERS)
        today = datetime.now(timezone.utc)
        for age in (0, 100, 400, 800, 2000, 5000):
            for source_class in WEB_AUTHORITY:
                assert web_authority(
                    source_class, published_at=today - timedelta(days=age)
                ) < lowest_filing

    def test_the_ordering_within_the_web_tier_is_the_one_documented(self):
        ordered = sorted(WEB_AUTHORITY, key=lambda k: -WEB_AUTHORITY[k])
        assert ordered == [
            WebSourceClass.REGULATOR,
            WebSourceClass.EXCHANGE,
            WebSourceClass.VERIFIED_IR,
            WebSourceClass.COMPANY_WEBSITE,
            WebSourceClass.REPUTABLE_MEDIA,
            WebSourceClass.UNKNOWN,
        ]

    def test_the_company_s_own_pages_are_primary_sources_within_the_tier(self):
        assert is_primary_source(WebSourceClass.VERIFIED_IR) is True
        assert is_primary_source(WebSourceClass.COMPANY_WEBSITE) is True
        assert is_primary_source(WebSourceClass.REGULATOR) is True
        assert is_primary_source(WebSourceClass.EXCHANGE) is True
        assert is_primary_source(WebSourceClass.REPUTABLE_MEDIA) is False
        assert is_primary_source(WebSourceClass.UNKNOWN) is False

    def test_an_unknown_class_gets_the_lowest_weight(self):
        assert WEB_AUTHORITY[WebSourceClass.UNKNOWN] == min(WEB_AUTHORITY.values())

    def test_the_lowest_filing_tier_is_still_the_one_the_vault_uses(self):
        """If the vault's floor moves, the assertion above must move with it."""
        from app.domain.knowledge.vault import SOURCE_AUTHORITY

        assert SOURCE_AUTHORITY["other"] == 0.40


class TestNeverTrusted:
    def test_no_web_class_appears_in_the_valuation_trusted_sources(self):
        from app.domain.valuation.data_quality import TRUSTED_SOURCES

        for source_class in WebSourceClass:
            assert source_class.value not in TRUSTED_SOURCES
            assert source_class.name not in TRUSTED_SOURCES

    def test_no_web_provenance_string_is_a_trusted_source(self):
        from app.domain.valuation.data_quality import TRUSTED_SOURCES

        # Note "exchange_filing" is deliberately absent from this list: it IS
        # a trusted-source string, because it is a *filing* provenance. The
        # web class of the same name must never be written there, which is
        # what the class-level test above asserts.
        for provenance in (
            "web", "web_page", "web_evidence", "company_website",
            "verified_ir", "regulator", "reputable_media", "web_page_regulator",
        ):
            assert provenance not in TRUSTED_SOURCES

    def test_a_web_page_takes_the_vault_fallback_authority(self):
        """Below every named filing type, which is what the fallback means."""
        from app.domain.knowledge.vault import SOURCE_AUTHORITY, authority_of

        assert authority_of("web_page") == SOURCE_AUTHORITY["other"]
        assert authority_of("web_page") == min(
            SOURCE_AUTHORITY[tier] for tier in FILING_TIERS
        )


# ===========================================================================
class TestClassification:
    def test_each_pinned_role_is_classified_by_host(self):
        assert classify("www.nseindia.com") is WebSourceClass.EXCHANGE
        assert classify("www.bseindia.com") is WebSourceClass.EXCHANGE
        assert classify("api.bseindia.com") is WebSourceClass.EXCHANGE
        assert classify("www.sec.gov") is WebSourceClass.REGULATOR
        assert classify("data.sec.gov") is WebSourceClass.REGULATOR

    def test_the_company_s_own_hosts_are_classified_from_its_own_records(self):
        assert classify(
            "www.acme.example", company_hosts=frozenset({"www.acme.example"})
        ) is WebSourceClass.COMPANY_WEBSITE
        assert classify(
            "ir.acme.example", ir_hosts=frozenset({"ir.acme.example"})
        ) is WebSourceClass.VERIFIED_IR

    def test_the_verified_ir_class_outranks_the_plain_website_class(self):
        assert WEB_AUTHORITY[WebSourceClass.VERIFIED_IR] > (
            WEB_AUTHORITY[WebSourceClass.COMPANY_WEBSITE]
        )

    def test_an_unlisted_host_is_unknown_rather_than_guessed(self):
        assert classify("blog.someone.example") is WebSourceClass.UNKNOWN
        assert classify("") is WebSourceClass.UNKNOWN

    def test_media_is_low_ranked_and_empty_in_phase_one(self):
        assert MEDIA_HOSTS == frozenset()
        assert WEB_AUTHORITY[WebSourceClass.REPUTABLE_MEDIA] < (
            WEB_AUTHORITY[WebSourceClass.COMPANY_WEBSITE]
        )

    def test_the_host_lists_are_still_the_ones_the_filing_providers_read(self):
        """The exchange hosts are duplicated here; this keeps them honest."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        sources = "\n".join(
            (root / "app" / "data" / "filings" / name).read_text()
            for name in ("indian.py", "sec.py")
        )
        for host in EXCHANGE_HOSTS | REGULATOR_HOSTS:
            assert host in sources, f"{host} is no longer used by a filing provider"

    def test_the_ir_confidence_mirror_matches_the_filings_module(self):
        from app.services.filings.ir_discovery import CONFIDENCE_VERIFIED

        assert IR_URL_VERIFIED_CONFIDENCE == CONFIDENCE_VERIFIED

    def test_label_boundary_matching_not_suffix_matching(self):
        assert host_is_pinned_by("www.acme.example", frozenset({"acme.example"}))
        assert not host_is_pinned_by("evilacme.example", frozenset({"acme.example"}))
        assert not host_is_pinned_by("", frozenset({"acme.example"}))


class TestAuthority:
    def test_an_absent_date_is_discounted_by_the_platform_s_own_factor(self):
        from app.data.filings.base import recency_factor

        weight = web_authority(WebSourceClass.COMPANY_WEBSITE)
        assert weight == round(
            WEB_AUTHORITY[WebSourceClass.COMPANY_WEBSITE] * recency_factor(None), 4
        )

    def test_a_fresh_page_outweighs_the_same_page_two_years_later(self):
        now = datetime.now(timezone.utc)
        fresh = web_authority(WebSourceClass.EXCHANGE, published_at=now)
        old = web_authority(
            WebSourceClass.EXCHANGE, published_at=now - timedelta(days=1_000)
        )
        assert fresh > old

    def test_a_date_object_and_a_datetime_agree(self):
        moment = datetime(2026, 7, 24, 5, 35, tzinfo=timezone.utc)
        assert web_authority(
            WebSourceClass.REGULATOR, published_at=moment
        ) == web_authority(WebSourceClass.REGULATOR, published_at=moment.date())


# ===========================================================================
class TestAssess:
    def test_a_substantive_page_is_accepted(self):
        assessment = assess(
            "Acme reported consolidated revenue of 1,234 crore for the "
            "quarter, up 18.4 per cent from the same quarter last year, with "
            "operating margin expanding to 21.2 per cent and an interim "
            "dividend of 4 per share approved by the board."
        )
        assert assessment.accepted is True
        assert assessment.reason is None

    def test_an_empty_extraction_is_reported_as_empty(self):
        for text in ("", "   \n\t  ", "\u200b\u200b"):
            assessment = assess(text)
            assert assessment.rejected is True
            assert assessment.reason is WebRejectionReason.EXTRACTION_EMPTY

    def test_a_stub_is_reported_as_too_short(self):
        assessment = assess("Acme Limited")
        assert assessment.reason is WebRejectionReason.CONTENT_TOO_SHORT

    @pytest.mark.parametrize("marker", [
        "Accept all cookies to continue reading this page",
        "We use cookies. Manage your preferences.",
        "Please enable JavaScript to view this page",
        "Checking your browser before accessing the site",
        "Access denied",
        "Are you a robot? Verify you are human",
    ])
    def test_a_wall_is_refused_rather_than_ingested(self, marker):
        assessment = assess(marker + " " * 50)
        assert assessment.rejected is True
        assert assessment.reason is WebRejectionReason.UNSUPPORTED_CONTENT

    def test_a_long_article_that_mentions_cookies_is_not_a_wall(self):
        body = (
            "Acme's board discussed the cookie consent regulation at length. "
            * 40
        )
        assert assess(body).accepted is True

    def test_a_substantive_page_with_cookie_banner_is_not_a_wall(self):
        body = (
            "JSW Steel reported consolidated crude steel capacity of 37.9 MTPA. "
            "The company provides investor results, annual reports and business "
            "updates across its steel operations. "
            * 20
            + " We use cookies on this website. Please indicate whether or not "
              "you accept our use of cookies."
        )
        assert assess(body).accepted is True

    def test_the_floor_is_the_policy_s_floor(self):
        policy = WebFetchPolicy(min_content_chars=10)
        assert assess("too short", policy=policy).accepted is False
        assert assess("long enough now", policy=policy).accepted is True

    def test_the_content_class_is_reported_in_the_detail(self):
        assessment = assess("", content_class=WebContentClass.PDF)
        assert "pdf" in assessment.detail


class TestNearDuplicates:
    def test_case_whitespace_and_punctuation_do_not_change_the_key(self):
        a = near_duplicate_key("Acme reported revenue of 1,234 crore.")
        b = near_duplicate_key("ACME   reported revenue, of 1 234 crore")
        assert a == b

    def test_a_different_sentence_is_a_different_key(self):
        assert near_duplicate_key("Acme reported revenue") != (
            near_duplicate_key("Acme reported a loss")
        )

    def test_the_key_is_bounded_before_hashing(self):
        long_a = near_duplicate_key("a" * 50_000)
        long_b = near_duplicate_key("a" * 50_000 + "DIFFERENT")
        assert long_a == long_b  # beyond the limit, content is out of scope
        assert len(long_a) == 64
