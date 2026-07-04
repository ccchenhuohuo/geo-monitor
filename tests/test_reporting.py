from geo_monitor.reporting import build_html, table_cell


def test_table_cell_escapes_pipe_and_newlines():
    assert table_cell("a|b\nc") == "a；b c"


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
