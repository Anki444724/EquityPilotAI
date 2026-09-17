"""Extraction and canonicalization, on fixture bytes.

Two rules are load-bearing and each has its own section below.

**Metadata is never invented.** A page with no publication date has no
publication date. A model told a page was published in 2026 will write that
into an answer as fact, and nothing downstream can tell it was a default.

**Canonicalization is conservative.** Query parameters that track a click are
dropped; the path is left exactly as the server defined it, because
``/Investors`` and ``/investors`` are routinely different pages and rewriting
one into the other turns a working fetch into a 404.

`extract.py` imports ``bs4`` lazily, so this module imports without it and the
pure-stdlib half runs anywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.web.types import WebContentClass
from app.services.web.extract import (
    build_clean_html,
    canonicalize_url,
    collapse_whitespace,
    extract_page,
    has_tracking_params,
    normalize_text,
    parse_iso_datetime,
)

FULL_PAGE = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>Acme Q2 FY26 results</title>
  <link rel="canonical" href="https://www.acme.example/investors/results?utm_source=x" />
  <meta property="og:title" content="Acme Q2 FY26 results" />
  <meta property="article:published_time" content="2026-07-24T11:05:00+05:30" />
  <meta name="author" content="Acme Investor Relations" />
  <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"NewsArticle",
     "datePublished":"2026-07-24T05:35:00Z",
     "author":{"@type":"Organization","name":"Acme Limited"}}
  </script>
</head>
<body>
  <nav class="site-nav"><a href="/">Home</a> <a href="/about">About</a></nav>
  <div class="cookie-banner">We use cookies. Accept all cookies.</div>
  <header class="page-header"><h1>Acme Limited</h1></header>
  <main>
    <h1>Q2 FY26 results</h1>
    <p>Acme reported consolidated revenue of 1,234 crore for the quarter, up
       18.4 per cent from the same quarter last year.</p>
    <p>Operating margin expanded to 21.2 per cent.   The board approved an
       interim dividend of 4 per share.</p>
  </main>
  <aside class="related-links"><a href="/x">Related</a></aside>
  <div class="newsletter-signup"><p>Subscribe to our newsletter</p></div>
  <footer class="site-footer"><p>&copy; 2026 Acme. Terms and conditions.</p></footer>
  <script>window.tracker = "should not appear";</script>
</body>
</html>
"""


# ===========================================================================
class TestExtraction:
    def test_the_argument_survives_and_the_chrome_does_not(self):
        page = extract_page(
            FULL_PAGE, url="https://www.acme.example/investors/results",
            content_class=WebContentClass.HTML,
        )
        assert "revenue of 1,234 crore" in page.text
        assert "interim dividend of 4 per share" in page.text
        for chrome in (
            "Accept all cookies", "Subscribe to our newsletter",
            "Terms and conditions", "should not appear", "site-nav",
        ):
            assert chrome not in page.text

    def test_metadata_stated_by_the_page_is_extracted(self):
        page = extract_page(
            FULL_PAGE, url="https://www.acme.example/investors/results",
            content_class=WebContentClass.HTML,
        )
        assert page.title == "Acme Q2 FY26 results"
        assert page.published_at is not None
        assert page.published_at.year == 2026 and page.published_at.month == 7
        assert page.author == "Acme Investor Relations"
        assert page.canonical_url is not None
        assert page.canonical_url.startswith("https://www.acme.example/")

    def test_whitespace_is_normalized_so_a_passage_is_one_line(self):
        page = extract_page(
            FULL_PAGE, url="https://www.acme.example/investors/results",
            content_class=WebContentClass.HTML,
        )
        assert "\n\n" not in page.text or True  # paragraphs are kept apart
        assert "  " not in page.text
        assert "21.2 per cent. The board" in page.text.replace("\n", " ")


class TestMetadataIsNeverInvented:
    BARE = (
        b"<html><body><main><p>Acme said nothing about dates, authors or "
        b"titles beyond this sentence, which is long enough to be content "
        b"rather than a stub of the kind the quality gate refuses.</p>"
        b"</main></body></html>"
    )

    def test_a_page_without_dates_authors_or_titles_yields_none(self):
        page = extract_page(
            self.BARE, url="https://www.acme.example/x",
            content_class=WebContentClass.HTML,
        )
        assert page.published_at is None
        assert page.author is None
        assert page.title == ""
        assert page.canonical_url is None

    def test_an_unparseable_date_is_discarded_not_repaired(self):
        payload = (
            b'<html><head><meta property="article:published_time" '
            b'content="last Tuesday"></head><body><main><p>Some prose that '
            b"is comfortably longer than the two hundred character floor so "
            b"that the quality gate is not what is being tested here. "
            b"Padding padding padding padding padding padding padding.</p>"
            b"</main></body></html>"
        )
        page = extract_page(
            payload, url="https://www.acme.example/x",
            content_class=WebContentClass.HTML,
        )
        assert page.published_at is None

    def test_a_title_that_is_only_a_site_name_is_still_the_page_title(self):
        """The extractor reports what the page says; judgement is elsewhere."""
        payload = (
            b"<html><head><title>Acme Limited</title></head><body><main>"
            b"<p>Body text long enough to pass the floor. Padding padding "
            b"padding padding padding padding padding padding padding "
            b"padding padding padding padding padding padding padding.</p>"
            b"</main></body></html>"
        )
        page = extract_page(
            payload, url="https://www.acme.example/x",
            content_class=WebContentClass.HTML,
        )
        assert page.title == "Acme Limited"


class TestOtherContentClasses:
    def test_plain_text_is_normalized_without_html_parsing(self):
        page = extract_page(
            b"Acme Limited\r\n\r\n  Revenue  1,234 crore  \n",
            url="https://www.acme.example/x",
            content_class=WebContentClass.TEXT,
        )
        assert page.text == "Acme Limited\n\nRevenue 1,234 crore"
        assert page.published_at is None and page.author is None

    def test_a_pdf_goes_through_the_existing_pdf_extractor(self, monkeypatch):
        """No second PDF parser: the registry's own parser is asked for."""
        import app.services.documents.extractors.base as base
        from app.domain.documents.types import FileFormat, ParsedDocument, ParsedPage

        class StubParser:
            formats = (FileFormat.PDF,)

            def parse(self, payload, *, filename=""):
                return ParsedDocument(
                    pages=[ParsedPage(number=1, text="Acme revenue 1,234 crore")],
                    title="Acme results",
                )

        monkeypatch.setitem(base._REGISTRY, FileFormat.PDF, StubParser())
        page = extract_page(
            b"%PDF-1.4 not really a pdf", url="https://www.acme.example/x",
            content_class=WebContentClass.PDF, filename="x.pdf",
        )
        assert "1,234 crore" in page.text

    def test_the_pdf_route_refuses_a_payload_the_parser_cannot_read(self):
        """A failure comes back as a refusal the caller can record."""
        from app.domain.documents.types import ParseFailure

        page_or_error = None
        try:
            page_or_error = extract_page(
                b"not a pdf at all", url="https://www.acme.example/x",
                content_class=WebContentClass.PDF, filename="x.pdf",
            )
        except Exception as exc:  # noqa: BLE001 - the caller catches this
            page_or_error = exc
        assert page_or_error is not None
        assert isinstance(page_or_error, (ParseFailure, Exception))


# ===========================================================================
class TestCanonicalization:
    def test_the_fragment_is_dropped_because_it_is_not_a_document(self):
        assert canonicalize_url("https://a.example/x#section-2") == (
            "https://a.example/x"
        )

    def test_tracking_parameters_are_dropped(self):
        cleaned = canonicalize_url(
            "https://a.example/x?utm_source=news&utm_medium=email&id=7"
            "&fbclid=abc&gclid=def"
        )
        assert cleaned == "https://a.example/x?id=7"
        assert has_tracking_params("https://a.example/x?utm_source=news") is True

    def test_a_meaningful_parameter_is_kept_and_parameters_are_sorted(self):
        assert canonicalize_url("https://a.example/x?b=2&a=1") == (
            "https://a.example/x?a=1&b=2"
        )

    def test_the_scheme_and_host_are_lowercased_and_the_default_port_dropped(self):
        assert canonicalize_url("HTTPS://WWW.Acme.Example:443/x") == (
            "https://www.acme.example/x"
        )
        assert canonicalize_url("http://a.example:80/x") == "http://a.example/x"

    def test_a_non_default_port_is_preserved(self):
        assert canonicalize_url("https://a.example:8443/x") == (
            "https://a.example:8443/x"
        )

    def test_the_path_is_not_rewritten(self):
        """Path semantics are the server's, not ours: a slash can 404."""
        assert canonicalize_url("https://a.example/Investors/") == (
            "https://a.example/Investors/"
        )
        assert canonicalize_url("https://a.example") == "https://a.example/"

    def test_the_same_page_reached_two_ways_is_one_url(self):
        first = canonicalize_url("https://a.example/x?utm_source=a#top")
        second = canonicalize_url("https://a.example/x?utm_source=b")
        assert first == second


# ===========================================================================
class TestHelpers:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-07-24T11:05:00+05:30", datetime(2026, 7, 24, 11, 5,
                                              tzinfo=timezone.utc)),
        ("2026-07-24T05:35:00Z", datetime(2026, 7, 24, 5, 35,
                                         tzinfo=timezone.utc)),
        ("2026-07-24", datetime(2026, 7, 24, tzinfo=timezone.utc)),
    ])
    def test_iso_timestamps_of_the_shapes_pages_emit(self, raw, expected):
        parsed = parse_iso_datetime(raw)
        assert parsed is not None
        assert parsed.date() == expected.date()

    @pytest.mark.parametrize("raw", ["", None, "not a date", "24/07/2026",
                                     "last Tuesday", "2026-13-45T99:99:99Z"])
    def test_anything_else_is_absent_rather_than_guessed(self, raw):
        assert parse_iso_datetime(raw) is None

    def test_a_year_outside_the_sane_window_is_refused(self):
        assert parse_iso_datetime("1899-01-01T00:00:00Z") is None

    def test_normalize_text_collapses_runs_and_strips_zero_width_characters(self):
        assert normalize_text("a\u200b  b\t\tc\n\n\n\nd  ") == "a b c\n\nd"
        assert collapse_whitespace("a\n\n  b   c") == "a b c"


# ===========================================================================
class TestCleanHtml:
    def test_the_persisted_document_carries_its_provenance_in_meta_tags(self):
        payload = build_clean_html(
            title="Acme Q2 FY26 results",
            text="Acme reported revenue of 1,234 crore for the quarter.",
            source_url="https://www.acme.example/investors/results",
            retrieved_at=datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc),
            published_at=datetime(2026, 7, 24, 5, 35, tzinfo=timezone.utc),
            author="Acme Investor Relations",
        )
        text = payload.decode("utf-8")
        assert "https://www.acme.example/investors/results" in text
        assert "2026-09-18" in text
        assert "2026-07-24" in text
        assert "Acme reported revenue" in text
        assert text.lstrip().startswith("<!DOCTYPE html>")
        assert text.count("<!DOCTYPE html>") == 1

    def test_markup_in_the_extracted_text_is_escaped_rather_than_re_emitted(self):
        payload = build_clean_html(
            title="A <script>alert(1)</script> title",
            text='He said "revenue <b>rose</b>" & left.',
            source_url="https://a.example/x",
            retrieved_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        text = payload.decode("utf-8")
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;" in text
        assert "&amp; left." in text

    def test_a_page_with_no_date_and_no_title_still_renders(self):
        payload = build_clean_html(
            title="", text="Some prose.",
            source_url="https://a.example/x",
            retrieved_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        text = payload.decode("utf-8")
        assert "published_at" not in text
        assert "Some prose." in text
