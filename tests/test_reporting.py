import json

import pytest

import geo_monitor.reporting as reporting_module
from geo_monitor.renderers import render_html
from geo_monitor.renderers.pdf import PdfRenderError
from geo_monitor.report_builder import build_report_model
from geo_monitor.report_model import REPORT_MODEL_SCHEMA_VERSION
from geo_monitor.reporting import (
    ReportRenderError,
    build_job_markdown,
    markdown_text,
    normalize_report_formats,
    render_report_bundle,
    table_cell,
)


def _summary() -> dict:
    return {
        "title": "Test GEO Report",
        "job_id": "job-1",
        "generated_at": "2026-01-01T00:00:00Z",
        "sample_mode": "live",
        "success_record_count": 1,
        "brand_summary": [
            {
                "brand_name_canonical": "TestBrand",
                "sov_event_share": "100.0%",
                "query_coverage_rate": "100.0%",
                "sov_rank": 1,
                "response_mention_rate": "100.0%",
                "is_target_brand": 1,
            }
        ],
        "target_diagnosis": {
            "target_detected": True,
            "target_sov_event_share": "100.0%",
            "target_rank_by_sov": 1,
            "target_query_coverage_rate": "100.0%",
            "missing_queries": [],
        },
        "target_brand": "TestBrand",
        "industry": "Software",
        "market": "CN",
        "expected_queries": 1,
        "expected_repeats": 1,
        "expected_units": 1,
        "extracted_mention_count": 1,
        "extraction_error_count": 0,
        "data_quality": {"conclusion_strength": "strong", "partial_sample": False, "planned_units": 1},
        "source_domains": [],
        "brand_by_query": [],
        "query_stability": [],
        "analysis_files": {"attempt_facts": "result/attempt_facts.csv"},
        "aggregate_files": {},
        "intelligence": {
            "geo_overview_scores": [
                {
                    "brand_name_canonical": "TestBrand",
                    "visibility_score": 100.0,
                    "recommendation_score": 80.0,
                    "competitor_score": 50.0,
                    "source_score": None,
                    "quality_score": 100.0,
                }
            ]
        },
    }


def test_table_and_markdown_text_escape_dynamic_content():
    assert table_cell("a|b\nc") == "a；b c"
    assert table_cell("<img src=x>") == "&lt;img src=x&gt;"
    assert markdown_text("<script>alert('x')</script> & copy") == "&lt;script&gt;alert('x')&lt;/script&gt; & copy"
    assert markdown_text("[click](javascript:alert(1))") == r"\[click\](javascript:alert(1))"


def test_report_model_is_versioned_and_renderer_neutral():
    model = build_report_model(_summary())

    assert model.schema_version == REPORT_MODEL_SCHEMA_VERSION
    assert model.job_id == "job-1"
    assert [section.key for section in model.sections] == [
        "executive_summary",
        "configuration",
        "data_quality",
        "visibility",
        "target_diagnosis",
        "intelligence",
        "sources",
        "situation",
        "perception",
        "trends_opportunities",
        "query_findings",
        "methodology",
    ]


def test_markdown_and_html_render_same_report_model_without_raw_html():
    summary = _summary()
    summary["title"] = "<script>bad</script>"
    summary["brand_summary"][0]["brand_name_canonical"] = "<img src=x onerror=alert(1)>"

    markdown = build_job_markdown(summary)
    html = render_html(build_report_model(summary))

    assert "<script>" not in markdown
    assert "<img" not in markdown
    assert "&lt;script&gt;bad&lt;/script&gt;" in markdown
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;bad&lt;/script&gt;" in html


def test_default_report_bundle_produces_model_markdown_and_real_pdf(tmp_path):
    model, files = render_report_bundle(_summary(), tmp_path)

    assert set(files) == {"model", "markdown", "pdf"}
    assert not (tmp_path / "report.html").exists()
    payload = json.loads(files["model"].read_text(encoding="utf-8"))
    assert payload["schema_version"] == REPORT_MODEL_SCHEMA_VERSION
    assert payload["title"] == model.title
    assert files["markdown"].read_text(encoding="utf-8").startswith("# Test GEO Report")
    assert files["pdf"].read_bytes().startswith(b"%PDF-")
    assert files["pdf"].stat().st_size > 1_000


def test_html_is_explicit_optional_derivative(tmp_path):
    _, files = render_report_bundle(_summary(), tmp_path, formats=("markdown", "pdf", "html"))

    assert "html" in files
    html = files["html"].read_text(encoding="utf-8")
    assert 'id="report-model"' in html
    assert REPORT_MODEL_SCHEMA_VERSION in html


def test_report_formats_fail_closed_and_pdf_implies_markdown():
    assert normalize_report_formats(("pdf",)) == ("markdown", "pdf")
    with pytest.raises(ReportRenderError, match="不能为空"):
        normalize_report_formats(())
    with pytest.raises(ReportRenderError, match="不支持"):
        normalize_report_formats(("docx",))


def test_pdf_failure_is_explicit_and_does_not_commit_partial_file(tmp_path, monkeypatch):
    def fail_pdf(model, output):
        raise PdfRenderError("synthetic failure")

    monkeypatch.setattr(reporting_module, "render_pdf", fail_pdf)

    with pytest.raises(ReportRenderError, match="synthetic failure"):
        render_report_bundle(_summary(), tmp_path)

    assert not (tmp_path / "report.pdf").exists()
    assert not (tmp_path / "report.md").exists()
    assert not (tmp_path / "report.json").exists()
    assert not list(tmp_path.glob(".report-bundle.*"))


def test_successful_bundle_removes_stale_unrequested_derivatives(tmp_path):
    render_report_bundle(_summary(), tmp_path, formats=("markdown", "pdf", "html"))
    assert (tmp_path / "report.html").exists()

    _, files = render_report_bundle(_summary(), tmp_path, formats=("markdown",))

    assert set(files) == {"model", "markdown"}
    assert not (tmp_path / "report.html").exists()
    assert not (tmp_path / "report.pdf").exists()
