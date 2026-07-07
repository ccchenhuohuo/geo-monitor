import json
from pathlib import Path

import pytest

from geo_monitor.adapters.registry import build_sampling_profile, get_adapter
from geo_monitor.config import Settings
from geo_monitor.db import build_duckdb, query_duckdb
from geo_monitor.job import JobError, build_job_bundle, run_job_bundle
from geo_monitor.request_fingerprint import REQUEST_FINGERPRINT_VERSION, request_fingerprint
from geo_monitor.schemas import QueryRecord


def test_qwen_responses_web_search_call_is_provider_trace():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    adapter = get_adapter("qwen_responses_web_search_basic")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen3.7-plus", settings=settings)
    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    normalized = adapter.normalize_response(
        {
            "status": "completed",
            "output_text": "answer",
            "output": [{"type": "web_search_call", "action": {"query": "best providers", "sources": []}}],
        },
        request,
    )

    assert normalized.web_search_performed is True
    assert normalized.web_search_evidence == "provider_trace"
    assert normalized.web_search_requirement_status == "satisfied"


def test_qwen_responses_request_only_is_not_verifiable():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    adapter = get_adapter("qwen_responses_web_search_basic")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen3.7-plus", settings=settings)
    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    normalized = adapter.normalize_response({"status": "completed", "output_text": "answer"}, request)

    assert normalized.web_search_performed is None
    assert normalized.web_search_evidence == "request_only"
    assert normalized.web_search_requirement_status == "not_verifiable"
    assert normalized.source_parse_status == "provider_returned_empty"


def test_qwen_chat_required_search_sets_forced_search():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    adapter = get_adapter("qwen_chat_enable_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings, web_search_required=True)

    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    assert request.payload["extra_body"]["enable_search"] is True
    assert request.payload["extra_body"]["search_options"]["forced_search"] is True


def test_adapter_options_fail_fast(tmp_path):
    config = tmp_path / "job_config.json"
    config.write_text(
        json.dumps(
            {
                "target_brand": "Example",
                "industry": "Industry",
                "queries": ["best providers"],
                "model": "qwen-plus",
                "adapter": "qwen_chat_enable_search",
                "adapter_options": {"unknown": True},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(JobError, match="adapter_options"):
        build_job_bundle(config, tmp_path / "bundle", settings=Settings(llm_api_key=None))


def test_request_fingerprint_changes_when_adapter_version_changes():
    basis = {
        "provider": "qwen",
        "adapter": "qwen_responses_web_search_basic",
        "adapter_version": "1",
        "api_family": "responses",
        "base_url_fingerprint": "abc",
        "model": "qwen3.7-plus",
        "query_id": "q001",
        "input": "best providers",
        "payload": {"tools": [{"type": "web_search"}]},
    }
    changed = dict(basis)
    changed["adapter_version"] = "2"

    assert REQUEST_FINGERPRINT_VERSION == "request-fingerprint-v1"
    assert request_fingerprint(basis) != request_fingerprint(changed)


def test_duckdb_comparison_observational_when_analysis_fingerprint_differs(tmp_path):
    pytest.importorskip("duckdb")
    runs = tmp_path / "runs"
    settings = Settings(llm_api_key=None)
    first = _build_and_run_mock_job(tmp_path, runs, "analysis-a", settings)
    second = _build_and_run_mock_job(tmp_path, runs, "analysis-b", settings)
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    columns, rows = query_duckdb(
        db,
        "select comparison_conclusion_strength, analysis_fingerprint_count from comparison_cohorts",
    )

    assert columns == ["comparison_conclusion_strength", "analysis_fingerprint_count"]
    assert rows == [("observational", 2)]


def test_duckdb_comparison_observational_when_web_or_source_evidence_is_bad(tmp_path):
    pytest.importorskip("duckdb")
    settings = Settings(llm_api_key=None)

    web_runs = tmp_path / "web_runs"
    first = _build_and_run_mock_job(tmp_path, web_runs, "analysis-fixed", settings)
    second = _build_and_run_mock_job(tmp_path, web_runs, "analysis-fixed", settings)
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))
    _rewrite_attempts(Path(second["bundle_dir"]), web_search_requirement_status="not_verifiable")
    web_db = tmp_path / "web.duckdb"
    build_duckdb(web_runs, web_db)
    _, web_rows = query_duckdb(web_db, "select comparison_conclusion_strength from comparison_cohorts")
    assert web_rows == [("observational",)]

    source_runs = tmp_path / "source_runs"
    first = _build_and_run_mock_job(tmp_path, source_runs, "analysis-fixed", settings)
    second = _build_and_run_mock_job(tmp_path, source_runs, "analysis-fixed", settings)
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))
    _rewrite_attempts(Path(second["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="parse_error")
    source_db = tmp_path / "source.duckdb"
    build_duckdb(source_runs, source_db)
    _, source_rows = query_duckdb(source_db, "select comparison_conclusion_strength, source_metrics_comparable from comparison_cohorts")
    assert source_rows == [("observational", False)]


def _build_and_run_mock_job(tmp_path: Path, runs: Path, analysis_model: str, settings: Settings) -> dict:
    config = tmp_path / f"{analysis_model}.json"
    config.write_text(
        json.dumps(
            {
                "target_brand": "Example",
                "industry": "Industry",
                "queries": ["best providers"],
                "repeats": 1,
                "model": "test-model",
                "analysis_model": analysis_model,
            }
        ),
        encoding="utf-8",
    )
    bundle = build_job_bundle(config, runs_dir=runs, settings=settings)
    run_job_bundle(bundle["bundle_dir"], mock=True, settings=settings)
    return bundle


def _write_strong_summary(bundle: Path) -> None:
    summary = {"job_conclusion_strength": "strong", "data_quality": {"conclusion_strength": "strong"}}
    (bundle / "logs" / "analysis_summary.json").write_text(json.dumps(summary), encoding="utf-8")


def _rewrite_attempts(bundle: Path, **updates) -> None:
    raw = bundle / "raw" / "attempts.jsonl"
    rows = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        row.update(updates)
        rows.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    raw.write_text("\n".join(rows) + "\n", encoding="utf-8")
