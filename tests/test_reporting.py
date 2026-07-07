from geo_monitor.analysis.pipeline import build_job_markdown
from geo_monitor.reporting import build_html, markdown_text, table_cell


def test_table_cell_escapes_pipe_newlines_and_raw_html():
    assert table_cell("a|b\nc") == "a；b c"
    assert table_cell("<img src=x onerror=alert(1)>") == "&lt;img src=x onerror=alert(1)&gt;"


def test_markdown_text_escapes_raw_html_but_keeps_quotes():
    assert markdown_text("<script>alert('x')</script> & copy") == "&lt;script&gt;alert('x')&lt;/script&gt; &amp; copy"


def test_build_job_markdown_escapes_dynamic_raw_html():
    markdown = build_job_markdown(
        {
            "title": "<script>bad</script>",
            "sample_mode": "live",
            "success_record_count": 1,
            "brand_summary": [
                {
                    "brand_name_canonical": "<img src=x onerror=alert(1)>",
                    "sov_event_share": "100.0%",
                    "query_coverage_rate": "100.0%",
                    "sov_rank": 1,
                    "response_mention_rate": "100.0%",
                    "recommended_rate_when_mentioned": "0.0%",
                    "recommended_rate_over_success": "0.0%",
                    "avg_rank_position": "",
                    "rank_observed_rate": "0.0%",
                    "positive_rate": "0.0%",
                    "sentiment_unknown_rate": "100.0%",
                    "is_target_brand": 0,
                }
            ],
            "target_diagnosis": {"target_detected": False, "missing_queries": [{"query_id": "<script>qid</script>", "query": "<b>query</b>"}]},
            "target_brand": "<script>target</script>",
            "industry": "<b>industry</b>",
            "market": "CN",
            "expected_queries": 1,
            "expected_repeats": 1,
            "extracted_mention_count": 1,
            "extraction_error_count": 0,
            "data_quality": {"conclusion_strength": "strong", "partial_sample": False},
            "source_domains": [],
            "brand_by_query": [],
            "query_stability": [],
            "analysis_files": {},
            "report_files": {},
            "aggregate_files": {},
        }
    )

    assert "<script" not in markdown
    assert "<img" not in markdown
    assert "&lt;script&gt;target&lt;/script&gt;" in markdown
    assert "&lt;img src=x onerror=alert(1)&gt;" in markdown


def test_build_html_renders_markdown_table_and_inline_formatting():
    markdown = "\n".join([
        "# Test Report",
        "",
        "| Col | Value |",
        "|---|---|",
        "| A | **B** |",
        "",
        "- item",
        "> note",
    ])

    html = build_html(markdown, {"title": "Test Report"})

    assert "<h1>Test Report</h1>" in html
    assert "<table>" in html
    assert "<strong>B</strong>" in html
    assert "<blockquote>note</blockquote>" in html
