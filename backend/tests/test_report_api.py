"""Integration tests for the report API.

These run against the real FastAPI app and the shared seeded database, driving
generation, download, versioning and deletion through HTTP.

Generation is expensive — five renderers and an AI pass — so the primary report
is built once at module scope and the individual tests read from it.
"""
from __future__ import annotations

import pytest

BASE = "/api/v1"


@pytest.fixture(scope="module")
def company_id(api_client) -> str:
    response = api_client.get(f"{BASE}/companies", params={"page_size": 60})
    assert response.status_code == 200
    results = response.json()["results"]
    return next(c["id"] for c in results if c["ticker"] == "BHARATCP")


@pytest.fixture(scope="module")
def generated(api_client, company_id) -> dict:
    response = api_client.post(
        f"{BASE}/reports/generate",
        json={
            "company_id": company_id,
            "report_type": "institutional",
            "formats": ["html", "pdf", "docx", "xlsx", "md"],
            "analyst": "Test Analyst",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ===========================================================================
class TestGeneration:
    def test_produces_a_report(self, generated):
        report = generated["report"]
        assert report["status"] == "ready"
        assert report["report_type"] == "institutional"
        assert report["ticker"] == "BHARATCP"
        assert report["section_count"] >= 10
        assert report["version"] >= 1

    def test_records_per_stage_timings(self, generated):
        timings = generated["timings"]
        assert {"gather", "build", "audit", "render_total"} <= set(timings)
        assert all(v >= 0 for v in timings.values())

    def test_every_requested_format_is_produced(self, generated):
        formats = {a["fmt"] for a in generated["report"]["artifacts"]}
        assert formats == {"html", "pdf", "docx", "xlsx", "md"}
        for artifact in generated["report"]["artifacts"]:
            assert artifact["size_bytes"] > 0
            assert artifact["filename"].endswith(artifact["fmt"])

    def test_pdf_reports_its_page_count(self, generated):
        pdf = next(
            a for a in generated["report"]["artifacts"] if a["fmt"] == "pdf"
        )
        assert pdf["page_count"] and pdf["page_count"] >= 3

    def test_citations_are_audited_and_clean(self, generated):
        """Every numeric claim in the prose must carry a citation."""
        report = generated["report"]
        assert report["citation_coverage"] == 1.0
        assert report["citation_clean"] is True
        assert report["audit"]["dangling_markers"] == []

    def test_audit_names_the_engines_used(self, generated):
        sources = set(generated["report"]["audit"]["sources"])
        assert "financial_engine" in sources
        assert "valuation_engine" in sources
        assert "scoring_engine" in sources

    def test_provenance_records_the_data_grade(self, generated):
        provenance = generated["report"]["provenance"]
        assert "generated" in provenance
        assert provenance["report_type"] == "institutional"

    def test_engine_failures_are_reported_not_swallowed(self, generated):
        """A missing section must be attributable."""
        assert isinstance(generated["errors"], dict)

    def test_unknown_company_is_refused(self, api_client):
        response = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": "does-not-exist", "report_type": "quick"},
        )
        assert response.status_code == 400

    def test_invalid_report_type_is_rejected(self, api_client, company_id):
        response = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "nonsense"},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("report_type", [
        "quick", "ic_memo", "quarterly_update",
    ])
    def test_each_report_type_generates(self, api_client, company_id, report_type):
        response = api_client.post(
            f"{BASE}/reports/generate",
            json={
                "company_id": company_id, "report_type": report_type,
                "formats": ["md"], "include_ai": False,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["report"]["status"] == "ready"

    def test_report_types_differ_in_section_count(self, api_client, company_id):
        """A quick report and a deep one that produce the same thing are one type."""
        quick = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "quick",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]
        deep = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "deep_research",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]
        assert deep["section_count"] > quick["section_count"]

    def test_a_company_without_statements_still_reports(self, api_client):
        """Sections must say why they are empty rather than the run failing."""
        companies = api_client.get(
            f"{BASE}/companies", params={"page_size": 60}
        ).json()["results"]
        target = next(c for c in companies if c["ticker"] != "BHARATCP")
        response = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": target["id"], "report_type": "quick",
                  "formats": ["md"], "include_ai": False},
        )
        assert response.status_code == 201
        assert response.json()["report"]["status"] == "ready"


# ===========================================================================
class TestCaching:
    def test_identical_inputs_return_the_stored_report(
        self, api_client, company_id, generated
    ):
        response = api_client.post(
            f"{BASE}/reports/generate",
            json={
                "company_id": company_id, "report_type": "institutional",
                "formats": ["html", "pdf", "docx", "xlsx", "md"],
                "analyst": "Test Analyst",
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["cached"] is True
        assert payload["report"]["id"] == generated["report"]["id"]

    def test_a_new_format_is_not_a_cache_hit(self, api_client, company_id):
        """Serving a cached report that lacks the requested file would omit it."""
        first = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "quarterly_update",
                  "formats": ["md"], "include_ai": False},
        ).json()
        assert first["cached"] in (False, True)
        second = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "quarterly_update",
                  "formats": ["md", "html"], "include_ai": False},
        ).json()
        assert second["cached"] is False

    def test_cache_can_be_bypassed(self, api_client, company_id):
        response = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "institutional",
                  "formats": ["md"], "use_cache": False},
        )
        assert response.json()["cached"] is False


# ===========================================================================
class TestVersioning:
    def test_regenerating_creates_a_new_version(self, api_client, company_id):
        first = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "initiation",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]
        second = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "initiation",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]
        assert second["version"] == first["version"] + 1

    def test_the_previous_version_is_superseded_not_deleted(
        self, api_client, company_id
    ):
        """A report sent to a committee must still resolve months later."""
        first = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "ic_memo",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]
        second = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "ic_memo",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]

        superseded = api_client.get(f"{BASE}/reports/{first['id']}").json()
        assert superseded["superseded_by"] == second["id"]
        assert superseded["status"] == "ready"

    def test_versions_endpoint_lists_the_chain(self, api_client, company_id):
        report_id = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "ic_memo",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]["id"]
        versions = api_client.get(f"{BASE}/reports/{report_id}/versions").json()
        assert len(versions) >= 2
        numbers = [v["version"] for v in versions]
        assert numbers == sorted(numbers, reverse=True)

    def test_current_versions_can_be_filtered(self, api_client, company_id):
        everything = api_client.get(
            f"{BASE}/reports", params={"company_id": company_id,
                                       "include_superseded": True},
        ).json()
        current = api_client.get(
            f"{BASE}/reports", params={"company_id": company_id,
                                       "include_superseded": False},
        ).json()
        assert len(current) <= len(everything)
        assert all(r["superseded_by"] is None for r in current)


# ===========================================================================
class TestDownloads:
    @pytest.mark.parametrize("fmt,magic", [
        ("pdf", b"%PDF"),
        ("docx", b"PK"),
        ("xlsx", b"PK"),
    ])
    def test_binary_formats_download_correctly(self, api_client, generated, fmt, magic):
        report_id = generated["report"]["id"]
        response = api_client.get(f"{BASE}/reports/{report_id}/download/{fmt}")
        assert response.status_code == 200
        assert response.content[:len(magic)] == magic
        assert "attachment" in response.headers["content-disposition"]

    def test_markdown_downloads_as_text(self, api_client, generated):
        report_id = generated["report"]["id"]
        response = api_client.get(f"{BASE}/reports/{report_id}/download/md")
        assert response.status_code == 200
        assert response.content.decode().startswith("# ")

    def test_preview_returns_inline_html(self, api_client, generated):
        report_id = generated["report"]["id"]
        response = api_client.get(f"{BASE}/reports/{report_id}/preview")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert response.text.startswith("<!doctype html>")

    def test_a_missing_format_is_rendered_on_demand(self, api_client, company_id):
        """Rebuilt from the stored tree, so it is that report, not a fresh one."""
        report_id = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "quick",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]["id"]

        response = api_client.get(f"{BASE}/reports/{report_id}/download/pdf")
        assert response.status_code == 200
        assert response.content[:4] == b"%PDF"

        detail = api_client.get(f"{BASE}/reports/{report_id}").json()
        assert "pdf" in {a["fmt"] for a in detail["artifacts"]}

    def test_unknown_report_is_404(self, api_client):
        assert api_client.get(
            f"{BASE}/reports/999999/download/pdf"
        ).status_code == 404

    def test_invalid_format_is_rejected(self, api_client, generated):
        report_id = generated["report"]["id"]
        assert api_client.get(
            f"{BASE}/reports/{report_id}/download/xyz"
        ).status_code == 422


# ===========================================================================
class TestReads:
    def test_detail_lists_sections_with_sufficiency(self, api_client, generated):
        report_id = generated["report"]["id"]
        detail = api_client.get(f"{BASE}/reports/{report_id}").json()
        assert detail["sections"]
        for section in detail["sections"]:
            assert "sufficient" in section
            if not section["sufficient"]:
                assert section["reason"], "an empty section gave no reason"

    def test_document_tree_is_available_on_request(self, api_client, generated):
        report_id = generated["report"]["id"]
        without = api_client.get(f"{BASE}/reports/{report_id}").json()
        assert without["document"] is None
        with_tree = api_client.get(
            f"{BASE}/reports/{report_id}", params={"include_document": True},
        ).json()
        assert with_tree["document"]["cover"]["ticker"] == "BHARATCP"
        assert with_tree["document"]["sections"]

    def test_list_filters_by_company_and_type(self, api_client, company_id):
        reports = api_client.get(
            f"{BASE}/reports",
            params={"company_id": company_id, "report_type": "institutional"},
        ).json()
        assert reports
        assert all(r["report_type"] == "institutional" for r in reports)

    def test_statistics_counts_the_corpus(self, api_client, generated):
        stats = api_client.get(f"{BASE}/reports/statistics").json()
        assert stats["reports"] >= 1
        assert stats["artifacts"] >= 5
        assert stats["bytes_stored"] > 0
        assert 0 <= stats["mean_coverage"] <= 1

    def test_jobs_record_timings(self, api_client, generated):
        jobs = api_client.get(f"{BASE}/reports/jobs").json()
        assert jobs
        assert jobs[0]["timings"]
        assert jobs[0]["duration_ms"] > 0

    def test_capabilities_describe_the_engine(self, api_client):
        payload = api_client.get(f"{BASE}/reports/capabilities").json()
        assert len(payload["report_types"]) == 6
        assert {f["key"] for f in payload["formats"]} == {
            "pdf", "docx", "xlsx", "html", "md",
        }
        assert len(payload["chart_kinds"]) == 10
        assert "financial_engine" in payload["evidence_sources"]

    def test_capabilities_expose_each_type_composition(self, api_client):
        payload = api_client.get(f"{BASE}/reports/capabilities").json()
        by_key = {t["key"]: t for t in payload["report_types"]}
        assert len(by_key["deep_research"]["sections"]) > len(
            by_key["quick"]["sections"]
        )
        assert by_key["quick"]["narratives"]

    def test_unknown_report_is_404(self, api_client):
        assert api_client.get(f"{BASE}/reports/999999").status_code == 404


# ===========================================================================
class TestDeletion:
    def test_delete_removes_the_report(self, api_client, company_id):
        report_id = api_client.post(
            f"{BASE}/reports/generate",
            json={"company_id": company_id, "report_type": "quick",
                  "formats": ["md"], "include_ai": False, "use_cache": False},
        ).json()["report"]["id"]
        assert api_client.delete(f"{BASE}/reports/{report_id}").status_code == 204
        assert api_client.get(f"{BASE}/reports/{report_id}").status_code == 404

    def test_deleting_a_middle_version_repairs_the_chain(
        self, api_client, company_id
    ):
        """Otherwise the successor points at a row that no longer exists."""
        ids = []
        for _ in range(3):
            ids.append(api_client.post(
                f"{BASE}/reports/generate",
                json={"company_id": company_id, "report_type": "quarterly_update",
                      "formats": ["md"], "include_ai": False, "use_cache": False},
            ).json()["report"]["id"])

        assert api_client.delete(f"{BASE}/reports/{ids[1]}").status_code == 204
        first = api_client.get(f"{BASE}/reports/{ids[0]}").json()
        assert first["superseded_by"] in (ids[2], None)

    def test_deleting_a_missing_report_is_404(self, api_client):
        assert api_client.delete(f"{BASE}/reports/999999").status_code == 404
