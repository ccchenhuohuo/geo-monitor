import json
from pathlib import Path

import pytest

from geo_monitor.adapters.registry import build_sampling_profile, get_adapter
from geo_monitor.config import Settings
from geo_monitor.db import build_duckdb, query_duckdb
from geo_monitor.job import JobError, build_job_bundle, run_job_bundle
from geo_monitor.providers.openai_compatible import OpenAICompatibleProvider
from geo_monitor.request_fingerprint import REQUEST_FINGERPRINT_VERSION, request_fingerprint
from geo_monitor.schemas import QueryRecord


def test_adapter_client_factory_disables_sdk_retries(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("geo_monitor.providers.openai_compatible.OpenAI", FakeOpenAI)

    client = OpenAICompatibleProvider().create_client(Settings(llm_api_key="test", llm_base_url="https://provider.example/v1"))

    assert isinstance(client, FakeOpenAI)
    assert captured["max_retries"] == 0


def test_openai_compatible_responses_payload_omits_legacy_limit_and_requires_search():
    settings = Settings(llm_api_key=None, llm_base_url="https://api.openai.com/v1")
    adapter = get_adapter("openai_compatible_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="gpt-5.5", settings=settings, web_search_limit=5)

    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    assert request.payload["tools"] == [{"type": "web_search"}]
    assert "limit" not in request.payload["tools"][0]
    assert request.payload["tool_choice"] == "required"
    assert "include" not in request.payload
    assert request.legacy_request_hashes
    legacy_basis = dict(request.request_fingerprint_basis)
    legacy_basis["adapter"] = "openai_responses_web_search"
    legacy_basis.pop("provider_sdk")
    assert request_fingerprint(legacy_basis) in request.legacy_request_hashes


def test_openai_compatible_responses_preserves_explicit_provider_specific_include():
    settings = Settings(llm_api_key=None, llm_base_url="https://api.openai.com/v1")
    adapter = get_adapter("openai_compatible_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="gpt-5.5", settings=settings)

    request = adapter.build_request(
        QueryRecord(query_id="q001", query="best providers"),
        profile,
        settings,
        {"include": ["web_search_call.action.sources"]},
    )

    assert request.payload["include"] == ["web_search_call.action.sources"]


@pytest.mark.parametrize("tool_choice", ["auto", "none"])
def test_openai_compatible_responses_rejects_optional_tool_choice_when_search_required(tool_choice):
    settings = Settings(llm_api_key=None, llm_base_url="https://api.openai.com/v1")
    adapter = get_adapter("openai_compatible_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="gpt-5.5", settings=settings)

    with pytest.raises(ValueError, match="tool_choice"):
        adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {"tool_choice": tool_choice})


def test_openai_compatible_responses_parses_action_sources_include_shape():
    settings = Settings(llm_api_key=None, llm_base_url="https://api.openai.com/v1")
    adapter = get_adapter("openai_compatible_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="gpt-5.5", settings=settings)
    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    normalized = adapter.normalize_response(
        {
            "status": "completed",
            "output_text": "answer",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"title": "Example", "url": "https://www.example.com/a"},
                        ]
                    },
                }
            ],
        },
        request,
    )

    assert normalized.sources[0].domain == "example.com"
    assert normalized.source_parse_status == "parsed"


def test_qwen_dashscope_web_search_call_is_provider_trace():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/api/v1")
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings)
    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    normalized = adapter.normalize_response(
        {
            "status_code": 200,
            "request_id": "req-1",
            "output": {
                "choices": [{"message": {"role": "assistant", "content": "answer"}}],
                "search_info": {"search_results": [{"title": "Example", "url": "https://example.com/a"}]},
            },
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
        request,
    )

    assert normalized.web_search_performed is True
    assert normalized.web_search_evidence == "provider_trace"
    assert normalized.web_search_requirement_status == "satisfied"
    assert normalized.text == "answer"
    assert normalized.sources[0].domain == "example.com"
    assert normalized.provider_meta["request_id"] == "req-1"
    assert "choices" not in normalized.raw
    assert "web_search" not in normalized.raw
    assert normalized.raw["output"]["search_info"]["search_results"][0]["title"] == "Example"


def test_qwen_dashscope_request_only_is_not_verifiable():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/api/v1")
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings)
    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    normalized = adapter.normalize_response(
        {"status_code": 200, "output": {"choices": [{"message": {"role": "assistant", "content": "answer"}}]}},
        request,
    )

    assert normalized.web_search_performed is None
    assert normalized.web_search_evidence == "request_only"
    assert normalized.web_search_requirement_status == "not_verifiable"
    assert normalized.source_parse_status == "provider_returned_empty"


def test_qwen_dashscope_required_search_sets_forced_search_and_sources():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/api/v1")
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings, web_search_required=True)

    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    assert request.payload["enable_search"] is True
    assert request.payload["search_options"]["forced_search"] is True
    assert request.payload["search_options"]["enable_source"] is True


def test_qwen_dashscope_required_search_rejects_forced_search_false():
    settings = Settings(llm_api_key=None, llm_base_url="https://dashscope.aliyuncs.com/api/v1")
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings, web_search_required=True)

    with pytest.raises(ValueError, match="forced_search"):
        adapter.build_request(
            QueryRecord(query_id="q001", query="best providers"),
            profile,
            settings,
            {"search_options": {"forced_search": False}},
        )


@pytest.mark.parametrize(
    "unsupported_model",
    [
        "qwen3-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
        "qwen-plus-2024-11-25",
        "qwen-turbo-latest",
        "qwen-vl-max",
        "qwen-coder-plus",
    ],
)
def test_qwen_dashscope_search_rejects_models_owned_by_other_api_families(unsupported_model):
    with pytest.raises(ValueError, match="不支持模型"):
        build_sampling_profile(
            adapter_name="qwen_dashscope_generation_web_search",
            model=unsupported_model,
            settings=Settings(),
        )


@pytest.mark.parametrize("unsupported_model", ["qwen3.7-max", "qwen3.7-plus", "qwen3.6-flash", "qwen3.5-plus"])
def test_qwen_dashscope_text_rejects_models_requiring_other_current_interfaces(unsupported_model):
    with pytest.raises(ValueError, match="不支持模型"):
        build_sampling_profile(
            adapter_name="qwen_dashscope_generation_text",
            model=unsupported_model,
            settings=Settings(),
            web_search_required=False,
        )


def test_qwen_dashscope_text_allows_dated_stable_generation_family():
    profile = build_sampling_profile(
        adapter_name="qwen_dashscope_generation_text",
        model="qwen-plus-2024-11-25",
        settings=Settings(),
        web_search_required=False,
    )

    assert profile["model"] == "qwen-plus-2024-11-25"


def test_doubao_ark_rejects_invalid_search_sources():
    settings = Settings(llm_api_key=None, llm_base_url="https://ark.cn-beijing.volces.com/api/v3")
    adapter = get_adapter("doubao_ark_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="doubao-test", settings=settings, web_search_required=True)

    with pytest.raises(ValueError, match="sources"):
        adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {"sources": ["unknown"]})


@pytest.mark.parametrize(
    "user_location",
    [{"type": "exact"}, {"type": "approximate", "unknown": "x"}, {"type": "approximate", "timezone": True}],
)
def test_doubao_ark_rejects_invalid_user_location(user_location):
    settings = Settings(llm_api_key=None, llm_base_url="https://ark.cn-beijing.volces.com/api/v3")
    adapter = get_adapter("doubao_ark_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="doubao-test", settings=settings)

    with pytest.raises(ValueError, match="user_location"):
        adapter.build_request(
            QueryRecord(query_id="q001", query="best providers"),
            profile,
            settings,
            {"user_location": user_location},
        )


@pytest.mark.parametrize(
    ("adapter_name", "model"), [("openai_compatible_responses_web_search", "gpt-5.5")],
)
@pytest.mark.parametrize("include", ["web_search_call.action.sources", ["valid", 3], {"bad": "shape"}])
def test_responses_adapters_reject_invalid_include_shapes(adapter_name, model, include):
    settings = Settings(llm_api_key=None, llm_base_url="https://provider.example/v1")
    adapter = get_adapter(adapter_name)
    profile = build_sampling_profile(adapter_name=adapter.name, model=model, settings=settings)

    with pytest.raises(ValueError, match="include.*字符串数组"):
        adapter.build_request(
            QueryRecord(query_id="q001", query="best providers"),
            profile,
            settings,
            {"include": include},
        )


@pytest.mark.parametrize("adapter_name", ["openai_compatible_responses_web_search", "doubao_ark_responses_web_search"])
@pytest.mark.parametrize(
    "tool_choice",
    ["require", "web_search", {"type": "web_search", "extra": True}, {"type": "web-search"}, 1],
)
def test_forced_search_adapters_reject_tool_choice_typos_and_extra_fields(adapter_name, tool_choice):
    settings = Settings(llm_api_key=None, llm_base_url="https://provider.example/v1")
    adapter = get_adapter(adapter_name)
    model = "gpt-5.5" if adapter_name.startswith("openai") else "doubao-test"
    profile = build_sampling_profile(adapter_name=adapter.name, model=model, settings=settings)

    with pytest.raises(ValueError, match="tool_choice"):
        adapter.build_request(
            QueryRecord(query_id="q001", query="best providers"),
            profile,
            settings,
            {"tool_choice": tool_choice},
        )


@pytest.mark.parametrize(
    ("adapter_name", "model"),
    [
        ("openai_compatible_responses_web_search", "gpt-5.5"),
        ("doubao_ark_responses_web_search", "doubao-test"),
        ("qwen_dashscope_generation_web_search", "qwen-plus"),
    ],
)
@pytest.mark.parametrize("max_tool_calls", [0, 11, True, 1.5, "many"])
def test_responses_adapters_bound_max_tool_calls(adapter_name, model, max_tool_calls):
    settings = Settings(llm_api_key=None, llm_base_url="https://provider.example/v1")
    adapter = get_adapter(adapter_name)
    profile = build_sampling_profile(adapter_name=adapter.name, model=model, settings=settings)

    with pytest.raises(ValueError, match="max_tool_calls"):
        adapter.build_request(
            QueryRecord(query_id="q001", query="best providers"),
            profile,
            settings,
            {"max_tool_calls": max_tool_calls},
        )


def test_qwen_dashscope_preserves_valid_native_search_options():
    settings = Settings(llm_api_key=None, llm_base_url="https://provider.example/v1")
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings)

    request = adapter.build_request(
        QueryRecord(query_id="q001", query="best providers"),
        profile,
        settings,
        {
            "search_options": {
                "freshness": 7,
                "search_strategy": "turbo",
                "assigned_site_list": ["example.com"],
                "intention_options": {"prompt_intervene": "Only authoritative sources"},
            }
        },
    )

    assert request.payload["search_options"]["freshness"] == 7
    assert request.payload["search_options"]["assigned_site_list"] == ["example.com"]
    assert request.payload["search_options"]["forced_search"] is True


@pytest.mark.parametrize(
    ("search_options", "error_field"),
    [
        ({"freshness": "week"}, "freshness"),
        ({"freshness": 7, "search_strategy": "max"}, "freshness"),
        ({"search_strategy": "standard"}, "search_strategy"),
        ({"citation_format": "[1]"}, "citation_format"),
        ({"assigned_site_list": ["example.com"] * 26}, "assigned_site_list"),
        ({"assigned_site_list": ["example.com"], "search_strategy": "max"}, "assigned_site_list"),
        ({"intention_options": {"unknown": "value"}}, "intention_options"),
        ({"unknown": True}, "未知字段"),
    ],
)
def test_qwen_dashscope_rejects_invalid_native_search_options(search_options, error_field):
    settings = Settings()
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings)

    with pytest.raises(ValueError, match=error_field):
        adapter.build_request(
            QueryRecord(query_id="q001", query="best providers"),
            profile,
            settings,
            {"search_options": search_options},
        )


def test_doubao_ark_payload_uses_native_search_contract():
    settings = Settings(llm_api_key=None, llm_base_url="https://ark.cn-beijing.volces.com/api/v3")
    adapter = get_adapter("doubao_ark_responses_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="doubao-test", settings=settings, web_search_limit=4)

    request = adapter.build_request(QueryRecord(query_id="q001", query="best providers"), profile, settings, {})

    assert request.payload["tools"] == [{"type": "web_search", "sources": ["search_engine"], "limit": 4}]
    assert request.payload["tool_choice"] == {"type": "web_search"}
    assert profile["web_search_limit_effective"] is True
    assert adapter.capabilities.supports_forced_search is True


@pytest.mark.parametrize("invalid_limit", [0, -1, 21, True, "5"])
def test_sampling_profile_rejects_invalid_web_search_limit(invalid_limit):
    with pytest.raises(ValueError, match="web_search_limit"):
        build_sampling_profile(
            adapter_name="doubao_ark_responses_web_search",
            model="doubao-test",
            settings=Settings(),
            web_search_limit=invalid_limit,
        )


def test_adapter_options_fail_fast(tmp_path):
    config = tmp_path / "job_config.json"
    config.write_text(
        json.dumps(
            {
                "target_brand": "Example",
                "industry": "Industry",
                "queries": ["best providers"],
                "model": "qwen-plus",
                "adapter": "qwen_dashscope_generation_web_search",
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
        "adapter": "qwen_dashscope_generation_web_search",
        "adapter_version": "1",
        "api_family": "responses",
        "base_url_fingerprint": "abc",
        "model": "qwen-plus",
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


def test_duckdb_comparison_observational_for_single_adapter_group(tmp_path):
    pytest.importorskip("duckdb")
    runs = tmp_path / "runs"
    settings = Settings(llm_api_key=None)
    first = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings)
    second = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings)
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))
    _rewrite_attempts(Path(first["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="parsed")
    _rewrite_attempts(Path(second["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="parsed")

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    columns, rows = query_duckdb(
        db,
        "select comparison_group_count, comparison_conclusion_strength from comparison_cohorts",
    )

    assert columns == ["comparison_group_count", "comparison_conclusion_strength"]
    assert rows == [(1, "observational")]


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


def test_duckdb_comparison_observational_when_sampling_fingerprint_differs(tmp_path):
    pytest.importorskip("duckdb")
    runs = tmp_path / "runs"
    settings = Settings(llm_api_key=None)
    first = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings, adapter="doubao_ark_responses_web_search", model="doubao-test")
    second = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings, adapter="qwen_dashscope_generation_web_search", model="qwen-plus")
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))
    _rewrite_attempts(Path(first["bundle_dir"]), web_search_requirement_status="satisfied", web_search_evidence="provider_trace", source_parse_status="parsed")
    _rewrite_attempts(Path(second["bundle_dir"]), web_search_requirement_status="satisfied", web_search_evidence="provider_trace", source_parse_status="parsed")

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    _, rows = query_duckdb(db, "select sampling_fingerprint_count, comparison_conclusion_strength from comparison_cohorts")

    assert rows == [(2, "observational")]


def test_duckdb_source_metrics_not_comparable_when_sources_are_empty(tmp_path):
    pytest.importorskip("duckdb")
    runs = tmp_path / "runs"
    settings = Settings(llm_api_key=None)
    first = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings, adapter="doubao_ark_responses_web_search", model="doubao-test")
    second = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings, adapter="qwen_dashscope_generation_web_search", model="qwen-plus")
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))
    _rewrite_attempts(Path(first["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="provider_returned_empty")
    _rewrite_attempts(Path(second["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="provider_returned_empty")

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    _, rows = query_duckdb(
        db,
        "select comparison_group_count, comparison_conclusion_strength, source_metrics_comparable from comparison_cohorts",
    )

    assert rows == [(2, "observational", False)]


def test_duckdb_source_metrics_not_comparable_without_source_url_facts(tmp_path):
    pytest.importorskip("duckdb")
    runs = tmp_path / "runs"
    settings = Settings(llm_api_key=None)
    first = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings, adapter="doubao_ark_responses_web_search", model="doubao-test")
    second = _build_and_run_mock_job(tmp_path, runs, "analysis-fixed", settings, adapter="qwen_dashscope_generation_web_search", model="qwen-plus")
    _write_strong_summary(Path(first["bundle_dir"]))
    _write_strong_summary(Path(second["bundle_dir"]))
    _rewrite_attempts(Path(first["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="parsed")
    _rewrite_attempts(Path(second["bundle_dir"]), web_search_requirement_status="satisfied", source_parse_status="parsed")

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    _, rows = query_duckdb(
        db,
        "select comparison_group_count, comparison_conclusion_strength, source_metrics_comparable from comparison_cohorts",
    )

    assert rows == [(2, "observational", False)]


def _build_and_run_mock_job(
    tmp_path: Path,
    runs: Path,
    analysis_model: str,
    settings: Settings,
    *,
    adapter: str = "openai_compatible_responses_web_search",
    model: str = "test-model",
) -> dict:
    config = tmp_path / f"{analysis_model}.json"
    config.write_text(
        json.dumps(
            {
                "target_brand": "Example",
                "industry": "Industry",
                "queries": ["best providers"],
                "repeats": 1,
                "model": model,
                "adapter": adapter,
                "analysis_model": analysis_model,
            }
        ),
        encoding="utf-8",
    )
    bundle = build_job_bundle(config, runs_dir=runs, settings=settings)
    run_job_bundle(bundle["bundle_dir"], mock=True, settings=settings)
    return bundle


def _write_strong_summary(bundle: Path) -> None:
    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "analyzed"
    (bundle / "job_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    summary = {
        "job_conclusion_strength": "strong",
        "sample_mode": "live",
        "partial_sample": False,
        "success_record_count": 1,
        "stats_record_count": 1,
        "run_generation": manifest.get("run_generation", 0),
        "data_quality": {"conclusion_strength": "strong", "partial_sample": False},
    }
    (bundle / "logs" / "analysis_summary.json").write_text(json.dumps(summary), encoding="utf-8")


def _rewrite_attempts(bundle: Path, **updates) -> None:
    raw = bundle / "raw" / "attempts.jsonl"
    rows = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        row.update(updates)
        if row.get("web_search_requirement_status") == "satisfied" and not row.get("web_search_evidence"):
            row["web_search_evidence"] = "provider_trace"
        rows.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    raw.write_text("\n".join(rows) + "\n", encoding="utf-8")
