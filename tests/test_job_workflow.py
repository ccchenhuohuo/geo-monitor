import csv
import json
from pathlib import Path

import pytest

from geo_monitor.analysis import analyze_job_bundle
from geo_monitor.analysis.cache import extraction_cache_key, response_text_hash
from geo_monitor.analysis.contracts import CSV_FIELD_SCHEMAS, EXTRACTION_SCHEMA_VERSION
from geo_monitor.config import Settings
from geo_monitor.db import build_duckdb, query_duckdb
from geo_monitor.exporters import read_jsonl
from geo_monitor.job import (
    BUNDLE_LOCK,
    JobError,
    build_job_bundle,
    cleanup_job_bundle,
    estimate_job_run,
    load_job_manifest,
    run_job_bundle,
    validate_job_config,
)
from geo_monitor.jobs.locking import JobBundleLock
from geo_monitor.runner import compute_request_hash


def _live_test_settings() -> Settings:
    return Settings(llm_api_key="test-key", llm_base_url="https://provider.example/v1")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _write_job_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "industry": "TestIndustry",
                "market": "TestMarket",
                "queries": ["best local providers", {"query": "top premium studios"}],
                "repeats": 2,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_contract_valid(rows, *, model: str = "test-model", web_search_limit: int = 5):
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        payload = {
            "model": model,
            "input": row["input_query"],
            "tools": [{"type": "web_search", "limit": web_search_limit}],
            "max_tool_calls": Settings(llm_api_key=None).max_tool_calls,
        }
        row["model"] = model
        row["raw_request"] = payload
        row["request_hash"] = compute_request_hash(payload)
        row.setdefault("raw_response", {"status": "completed", "output_text": row.get("response_text", "")})
    return rows


def test_build_job_creates_query_manifest_without_business_context(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)

    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    query_manifest = (bundle / "work" / "query_manifest.csv").read_text(encoding="utf-8-sig")
    assert manifest["target_brand"] == "TestAEntity"
    assert manifest["status"] == "built"
    assert manifest["paths"]["query_manifest"] == "work/query_manifest.csv"
    assert manifest["queries"] == [
        {"query_id": "q001", "query": "best local providers"},
        {"query_id": "q002", "query": "top premium studios"},
    ]
    assert query_manifest.splitlines()[0] == "query_id,query"
    assert "TestAEntity" not in query_manifest
    assert "TestIndustry" not in query_manifest


def test_inline_query_metadata_changes_query_set_hash(tmp_path):
    first_config = tmp_path / "first.json"
    second_config = tmp_path / "second.json"
    base = {
        "target_brand": "TestAEntity",
        "industry": "TestIndustry",
        "queries": [{"query_id": "q001", "query": "best local providers", "persona": "buyer"}],
        "repeats": 1,
        "model": "test-model",
    }
    first_config.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    changed = dict(base)
    changed["queries"] = [{"query_id": "q001", "query": "best local providers", "persona": "architect"}]
    second_config.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    first = build_job_bundle(first_config, tmp_path / "first", settings=Settings(llm_api_key=None))
    second = build_job_bundle(second_config, tmp_path / "second", settings=Settings(llm_api_key=None))

    assert first["comparability_profile"]["query_manifest_sha256"] != second["comparability_profile"]["query_manifest_sha256"]


def test_v3_external_query_manifest_requires_non_empty_sha(tmp_path):
    config_path = tmp_path / "job_config.json"
    query_manifest = tmp_path / "queries.csv"
    query_manifest.write_text("query_id,query\nq001,best local providers\n", encoding="utf-8")
    config_path.write_text(
        json.dumps({"target_brand": "TestAEntity", "industry": "TestIndustry", "queries": ["placeholder"], "repeats": 1, "model": "test-model"}),
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    build_job_bundle(config_path, bundle, query_manifest_path=query_manifest, settings=Settings(llm_api_key=None))
    manifest_path = bundle / "job_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["query_manifest"]["sha256"] = ""
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(JobError, match="sha256"):
        load_job_manifest(bundle)


def test_query_manifest_preserves_formula_like_query_text(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "industry": "TestIndustry",
                "queries": ["=formula-like user query"],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    text = (bundle / "work" / "query_manifest.csv").read_text(encoding="utf-8-sig")
    assert "=formula-like user query" in text
    assert "'=formula-like user query" in text
    assert load_job_manifest(bundle)["queries"][0]["query"] == "=formula-like user query"


def test_validate_job_config_reports_planned_units(tmp_path):
    config_path = tmp_path / "job_config.json"
    _write_job_config(config_path)

    result = validate_job_config(config_path, settings=Settings(llm_api_key=None))

    assert result["query_count"] == 2
    assert result["planned_units"] == 4
    assert result["market"] == "TestMarket"


def test_build_and_validate_job_share_canonical_config_resolution(tmp_path):
    config_path = tmp_path / "job_config.json"
    _write_job_config(config_path)

    validated = validate_job_config(config_path, settings=Settings(llm_api_key=None))
    built = build_job_bundle(config_path, tmp_path / "bundle", settings=Settings(llm_api_key=None))

    shared_keys = {
        "target_brand",
        "target_aliases",
        "owned_domains",
        "industry",
        "market",
        "repeats",
        "model",
        "web_search_limit",
        "adapter",
        "sampling_profile",
        "analysis_profile",
        "comparability_profile",
        "concurrency",
        "start_interval_seconds",
    }
    assert {key: validated[key] for key in shared_keys} == {key: built[key] for key in shared_keys}


def test_build_job_preserves_query_metadata_and_target_aliases(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "target_aliases": ["TestAlias"],
                "industry": "TestIndustry",
                "queries": [{"query_id": "scene_a", "query": "best local providers", "persona": "owner", "tags": ["decision", "local"]}],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    query_manifest = (bundle / "work" / "query_manifest.csv").read_text(encoding="utf-8-sig")
    assert manifest["target_aliases"] == ["TestAlias"]
    assert manifest["queries"][0]["query_id"] == "scene_a"
    assert "persona" in query_manifest
    assert "decision,local" in query_manifest


def test_run_job_accepts_tags_array_metadata_from_build(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "industry": "TestIndustry",
                "queries": [{"query_id": "scene_a", "query": "best local providers", "persona": "owner", "tags": ["decision", "local"]}],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    result = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    rows = read_jsonl(bundle / "raw" / "attempts.jsonl")
    assert result["executed"] == 1
    assert rows[0]["metadata"]["tags"] == ["decision", "local"]
    assert rows[0]["metadata"]["persona"] == "owner"


def test_build_job_rejects_non_empty_out_dir_without_force(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "old.txt").write_text("old", encoding="utf-8")
    _write_job_config(config_path)

    try:
        build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("expected JobError")

    try:
        build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None), force=True)
    except JobError as exc:
        assert "job_manifest" in str(exc)
    else:
        raise AssertionError("expected JobError")

    safe_bundle = tmp_path / "safe_bundle"
    build_job_bundle(config_path, safe_bundle, settings=Settings(llm_api_key=None))
    (safe_bundle / "old.txt").write_text("old", encoding="utf-8")
    result = build_job_bundle(config_path, safe_bundle, settings=Settings(llm_api_key=None), force=True)
    assert Path(result["bundle_dir"]) == safe_bundle
    assert not (safe_bundle / "old.txt").exists()


def test_build_job_invalid_config_does_not_create_out_dir(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(json.dumps({"industry": "TestIndustry", "queries": ["q"]}, ensure_ascii=False), encoding="utf-8")

    try:
        build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    except JobError:
        pass
    else:
        raise AssertionError("expected JobError")

    assert not bundle.exists()


def test_build_job_rejects_invalid_web_search_limit(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["web_search_limit"] = 999
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    try:
        build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "web_search_limit" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_build_job_rejects_float_integer_fields(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["repeats"] = 1.9
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    try:
        build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "repeats" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_validate_job_config_rejects_unknown_root_fields(tmp_path):
    config_path = tmp_path / "job_config.json"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["repeat"] = 3
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    try:
        validate_job_config(config_path, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "未知字段" in str(exc)
        assert "repeat" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_schema_documents_external_manifest_runtime_mode_and_runtime_rejects_bare_no_query_config(tmp_path):
    schema = json.loads(Path("data/job_config.schema.json").read_text(encoding="utf-8"))
    assert "external manifest mode" in schema["description"]
    assert schema["properties"]["repeats"]["oneOf"]
    config_path = tmp_path / "job_config.json"
    config_path.write_text(
        json.dumps({"target_brand": "TestAEntity", "industry": "TestIndustry", "repeats": "1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    try:
        validate_job_config(config_path, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "queries" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_validate_job_config_reports_json_line_and_column(tmp_path):
    config_path = tmp_path / "job_config.json"
    config_path.write_text('{"target_brand": "TestAlias",', encoding="utf-8")

    try:
        validate_job_config(config_path, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "JSON 格式错误" in str(exc)
        assert ":1:" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_build_job_allows_missing_market(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "industry": "TestIndustry",
                "queries": ["best local providers"],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    query_manifest = (bundle / "work" / "query_manifest.csv").read_text(encoding="utf-8-sig")
    assert manifest["industry"] == "TestIndustry"
    assert manifest["market"] == "未指定市场"
    assert "未指定市场" not in query_manifest


def test_build_job_requires_explicit_output_root(tmp_path):
    config_path = tmp_path / "job_config.json"
    _write_job_config(config_path)

    with pytest.raises(JobError, match="--out-dir.*--runs-dir"):
        build_job_bundle(config_path, settings=Settings(llm_api_key=None))


def test_run_job_mock_uses_only_query_text_in_raw_request(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    result = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    assert result["executed"] == 4
    assert (bundle / "logs" / "run_summary.json").exists()
    rows = read_jsonl(bundle / "raw" / "attempts.jsonl")
    assert len(rows) == 4
    assert {row["raw_request"]["input"] for row in rows} == {"best local providers", "top premium studios"}
    assert "TestAEntity" not in json.dumps([row["raw_request"] for row in rows], ensure_ascii=False)
    assert "TestIndustry" not in json.dumps([row["raw_request"] for row in rows], ensure_ascii=False)


def test_run_job_resume_all_skipped_keeps_ran_status(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    rows = []
    for query in manifest["queries"]:
        for repeat_index in [1, 2]:
            payload = {
                "model": manifest["model"],
                "input": query["query"],
                "tools": [{"type": "web_search", "limit": manifest["web_search_limit"]}],
                "max_tool_calls": Settings(llm_api_key=None).max_tool_calls,
            }
            rows.append(
                {
                    "run_id": "done",
                    "query_id": query["query_id"],
                    "repeat_index": repeat_index,
                    "repeat_total": 2,
                    "request_hash": compute_request_hash(payload),
                    "model": manifest["model"],
                    "input_query": query["query"],
                    "status": "success",
                    "response_text": "done",
                    "sources": [],
                    "raw_request": payload,
                    "raw_response": {"status": "completed", "output_text": "done"},
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "completed_at": f"2026-01-01T00:00:0{repeat_index}+00:00",
                }
            )
    (bundle / "raw" / "attempts.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    second = run_job_bundle(bundle, settings=Settings(llm_api_key=None))

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    assert second["executed"] == 0
    assert second["completed_units"] == 4
    assert second["skipped"] == 4
    assert manifest["status"] == "ran"


def test_run_job_limit_executes_subset_and_keeps_partial_status(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    result = run_job_bundle(bundle, mock=True, limit=1, settings=Settings(llm_api_key=None))

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((bundle / "logs" / "run_summary.json").read_text(encoding="utf-8"))
    assert result["executed"] == 2
    assert result["completed_units"] == 2
    assert result["job_completed_units"] == 2
    assert summary["selected_query_ids"] == ["q001"]
    assert manifest["status"] == "ran_partial"


def test_run_job_only_query_id_executes_selected_query(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    result = run_job_bundle(bundle, mock=True, only_query_ids=["q002"], settings=Settings(llm_api_key=None))

    rows = read_jsonl(bundle / "raw" / "attempts.jsonl")
    summary = json.loads((bundle / "logs" / "run_summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    assert result["executed"] == 2
    assert {row["query_id"] for row in rows} == {"q002"}
    assert summary["selected_query_ids"] == ["q002"]
    assert manifest["status"] == "ran_partial"


def test_run_job_bundle_live_requires_confirm_cost(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    try:
        run_job_bundle(bundle, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "confirm_cost" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_run_job_live_cost_gate_uses_request_hash_for_resume(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    old_payload = {
        "model": "old-model",
        "input": "best local providers",
        "tools": [{"type": "web_search", "limit": 5}],
        "max_tool_calls": Settings(llm_api_key=None).max_tool_calls,
    }
    old_row = {
        "run_id": "old",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": compute_request_hash(old_payload),
        "model": "old-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "old",
        "sources": [],
        "raw_request": old_payload,
        "raw_response": {"status": "completed", "output_text": "old"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(old_row, ensure_ascii=False), encoding="utf-8")

    estimate = estimate_job_run(bundle, settings=Settings(llm_api_key=None))
    assert estimate["completed_units"] == 1
    assert estimate["resume_matched_units"] == 0
    assert estimate["sampling_requests_remaining"] == 1
    try:
        run_job_bundle(bundle, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "confirm_cost" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_bundle_lock_blocks_run_analyze_and_cleanup(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    with JobBundleLock(bundle / BUNDLE_LOCK):
        for action in [
            lambda: run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None)),
            lambda: analyze_job_bundle(bundle, settings=Settings(llm_api_key=None)),
            lambda: cleanup_job_bundle(bundle),
        ]:
            try:
                action()
            except JobError as exc:
                assert "正在运行" in str(exc)
            else:
                raise AssertionError("expected JobError")


def test_build_job_force_respects_bundle_lock(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    with JobBundleLock(bundle / BUNDLE_LOCK):
        try:
            build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None), force=True)
        except JobError as exc:
            assert "正在运行" in str(exc)
        else:
            raise AssertionError("expected JobError")


def test_build_job_force_rejects_symlink_logs_dir(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside_logs"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    import shutil

    shutil.rmtree(bundle / "logs")
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (bundle / "logs").symlink_to(outside, target_is_directory=True)

    try:
        build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None), force=True)
    except JobError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("expected JobError")
    assert (outside / "keep.txt").exists()


def test_bundle_lock_with_dead_pid_is_recovered(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    lock_path = bundle / BUNDLE_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999999, "token": "dead"}), encoding="utf-8")

    result = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    assert result["executed"] == 4
    assert not lock_path.exists()


def test_bundle_lock_with_old_live_pid_is_not_stolen(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    lock_path = bundle / BUNDLE_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 1, "token": "old"}), encoding="utf-8")
    old_time = 1
    import os

    os.utime(lock_path, (old_time, old_time))

    with pytest.raises(JobError, match="任务正在运行"):
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    assert lock_path.exists()


def test_run_job_rejects_mismatched_work_query_manifest(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    (bundle / "work" / "query_manifest.csv").write_text("query_id,query\nq001,different query\n", encoding="utf-8")

    try:
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "不一致" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_run_job_rejects_mismatched_query_manifest_metadata(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "industry": "TestIndustry",
                "queries": [{"query_id": "q001", "query": "best local providers", "persona": "owner"}],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    (bundle / "work" / "query_manifest.csv").write_text("query_id,query,persona\nq001,best local providers,competitor\n", encoding="utf-8")

    try:
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    except JobError as exc:
        assert "元数据不一致" in str(exc)
    else:
        raise AssertionError("expected JobError")


def test_analyze_job_with_injected_extractor_does_not_need_alias(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    rows = [
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 1,
            "repeat_total": 2,
            "request_hash": "a",
            "model": "m",
            "input_query": "best local providers",
            "status": "success",
            "response_text": "TestStudio and TestBEntity are often mentioned.",
            "sources": [],
            "raw_request": {"model": "m", "input": "best local providers"},
            "raw_response": {"status": "completed", "output_text": "TestStudio and TestBEntity are often mentioned."},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "run_id": "r",
            "query_id": "q002",
            "repeat_index": 1,
            "repeat_total": 2,
            "request_hash": "b",
            "model": "m",
            "input_query": "top premium studios",
            "status": "success",
            "response_text": "TestStudio appears again.",
            "sources": [],
            "raw_request": {"model": "m", "input": "top premium studios"},
            "raw_response": {"status": "completed", "output_text": "TestStudio appears again."},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    _make_contract_valid(rows)
    raw_path = bundle / "raw" / "attempts.jsonl"
    raw_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    def extractor(record):
        if record["query_id"] == "q001":
            return [
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "TestStudio",
                    "brand_type": "公司",
                    "evidence": "TestStudio",
                    "role": "mentioned",
                    "confidence": 0.9,
                },
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "TestBEntity",
                    "brand_type": "公司",
                    "evidence": "TestBEntity",
                    "role": "mentioned",
                    "confidence": 0.8,
                },
            ], None
        return [
            {
                "query_id": "q002",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "公司",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    def canonicalizer(names):
        return {"TestStudio": "TestStudio", "TestBEntity": "TestBEntity"}, None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, canonicalizer=canonicalizer)

    assert result["extracted_mention_count"] == 3
    assert not (bundle / "work").exists()
    assert (bundle / "result" / "brand_summary.csv").exists()
    assert (bundle / "result" / "brand_canonical_map.csv").exists()
    assert (bundle / "result" / "report.md").exists()
    assert (bundle / "logs" / "analysis_summary.json").exists()
    assert result["brand_summary"][0]["brand_name_canonical"] == "TestStudio"
    assert (bundle / "result" / "brand_summary.csv").exists()
    assert not (bundle / "result" / "sov_summary.csv").exists()
    assert not (bundle / "result" / "discovered_brands.csv").exists()
    assert "Brand Visibility / SOV" in (bundle / "result" / "report.md").read_text(encoding="utf-8")


def test_analyze_job_ignores_superseded_contract_mismatch_for_latest_stats(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    old = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "old-bad-hash",
        "model": "test-model",
        "input_query": "wrong query",
        "status": "success",
        "response_text": "OldBrand should be superseded.",
        "sources": [],
        "raw_request": {"model": "test-model", "input": "wrong query", "tools": [{"type": "web_search", "limit": 5}]},
        "raw_response": {"status": "completed", "output_text": "OldBrand should be superseded."},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    latest = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "LatestBrand is the valid answer.",
        "sources": [],
        "started_at": "2026-01-01T00:00:02+00:00",
        "completed_at": "2026-01-01T00:00:03+00:00",
    }
    _make_contract_valid(latest)
    (bundle / "raw" / "attempts.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [old, latest]), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": record["query_id"],
                "repeat_index": record["repeat_index"],
                "input_query": record["input_query"],
                "brand_name_raw": "LatestBrand",
                "brand_type": "公司",
                "evidence": "LatestBrand",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["data_quality"]["duplicate_units"] == [{"query_id": "q001", "repeat_index": 1, "run_execution_id": "r", "count": 2}]
    assert result["data_quality"]["contract_mismatches"] == []
    assert result["data_quality"]["superseded_contract_mismatches"]
    assert result["data_quality"]["stats_record_count"] == 1
    assert result["data_quality"]["excluded_from_stats_count"] == 0
    assert result["brand_summary"][0]["brand_name_canonical"] == "LatestBrand"


def test_analyze_job_failure_marks_analysis_failed(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    try:
        analyze_job_bundle(bundle, settings=Settings(llm_api_key=None))
    except ValueError as exc:
        assert "缺少 raw attempts" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "analysis_failed"


def test_analyze_job_bundle_live_extraction_requires_confirm_cost(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    try:
        analyze_job_bundle(bundle, settings=Settings(llm_api_key=None))
    except ValueError as exc:
        assert "confirm_cost" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "analysis_failed"


def test_analyze_job_reuses_live_extraction_and_canonicalization_cache(tmp_path, monkeypatch):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=_live_test_settings())
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "CacheBrand is mentioned.",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    calls = {"extract": 0, "canonicalize": 0}

    class FakeLLMBrandExtractor:
        def __init__(self, settings, *, model=None, analysis_run_id=None):
            self.model = model or "test-model"

        def extract_record(self, record):
            calls["extract"] += 1
            return [
                {
                    "query_id": record["query_id"],
                    "repeat_index": record["repeat_index"],
                    "input_query": record["input_query"],
                    "brand_name_raw": "CacheBrand",
                    "brand_type": "公司",
                    "evidence": "CacheBrand",
                    "role": "mentioned",
                    "confidence": 0.9,
                    "sov_eligible": True,
                }
            ], None

        def canonicalize(self, names):
            calls["canonicalize"] += 1
            return {name: name for name in names}, None

    monkeypatch.setattr("geo_monitor.analysis.orchestrator.LLMBrandExtractor", FakeLLMBrandExtractor)

    first = analyze_job_bundle(bundle, settings=_live_test_settings(), confirm_cost=True)
    second = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None))
    monkeypatch.undo()
    third = analyze_job_bundle(bundle, settings=_live_test_settings(), confirm_cost=True)

    assert calls == {"extract": 1, "canonicalize": 1}
    assert first["cache"]["extraction_cache_writes"] == 1
    assert second["cache"]["extraction_cache_hits"] == 1
    assert second["cache"]["canonicalization_cache_hits"] == 1
    assert third["cache"]["extraction_cache_hits"] == 1
    assert third["cache"]["canonicalization_cache_hits"] == 1
    assert third["cache"]["analysis_llm_requests_remaining"] == 0


def test_analyze_job_cache_invalidates_when_response_text_changes(tmp_path, monkeypatch):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=_live_test_settings())
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "CacheBrand is mentioned.",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    raw_path = bundle / "raw" / "attempts.jsonl"
    raw_path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    class FakeLLMBrandExtractor:
        def __init__(self, settings, *, model=None, analysis_run_id=None):
            self.model = model or "test-model"

        def extract_record(self, record):
            return [
                {
                    "query_id": record["query_id"],
                    "repeat_index": record["repeat_index"],
                    "input_query": record["input_query"],
                    "brand_name_raw": "CacheBrand",
                    "brand_type": "公司",
                    "evidence": "CacheBrand",
                    "role": "mentioned",
                    "confidence": 0.9,
                    "sov_eligible": True,
                }
            ], None

        def canonicalize(self, names):
            return {name: name for name in names}, None

    monkeypatch.setattr("geo_monitor.analysis.orchestrator.LLMBrandExtractor", FakeLLMBrandExtractor)
    analyze_job_bundle(bundle, settings=_live_test_settings(), confirm_cost=True)
    row["response_text"] = "ChangedBrand is mentioned."
    _make_contract_valid(row)
    raw_path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    try:
        analyze_job_bundle(bundle, settings=Settings(llm_api_key=None))
    except ValueError as exc:
        assert "confirm_cost" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_extraction_cache_rebinds_record_context_for_identical_response_text(tmp_path, monkeypatch):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=_live_test_settings())
    rows = [
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 1,
            "repeat_total": 1,
            "model": "test-model",
            "input_query": "best local providers",
            "status": "success",
            "response_text": "SharedBrand is mentioned.",
            "sources": [],
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "run_id": "r",
            "query_id": "q002",
            "repeat_index": 1,
            "repeat_total": 1,
            "model": "test-model",
            "input_query": "top premium studios",
            "status": "success",
            "response_text": "SharedBrand is mentioned.",
            "sources": [],
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:02+00:00",
        },
    ]
    _make_contract_valid(rows)
    (bundle / "raw" / "attempts.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    calls = {"extract": 0, "canonicalize": 0}

    class FakeLLMBrandExtractor:
        def __init__(self, settings, *, model=None, analysis_run_id=None):
            self.model = model or "test-model"

        def extract_record(self, record):
            calls["extract"] += 1
            return [
                {
                    "query_id": record["query_id"],
                    "repeat_index": record["repeat_index"],
                    "input_query": record["input_query"],
                    "brand_name_raw": "SharedBrand",
                    "brand_type": "公司",
                    "evidence": "SharedBrand",
                    "role": "mentioned",
                    "confidence": 0.9,
                    "sov_eligible": True,
                }
            ], None

        def canonicalize(self, names):
            calls["canonicalize"] += 1
            return {name: name for name in names}, None

    monkeypatch.setattr("geo_monitor.analysis.orchestrator.LLMBrandExtractor", FakeLLMBrandExtractor)

    result = analyze_job_bundle(bundle, settings=_live_test_settings(), confirm_cost=True)

    assert calls["extract"] == 1
    assert result["cache"]["extraction_cache_hits"] == 1
    assert result["cache"]["extraction_cache_misses"] == 1
    assert {row["query_id"] for row in result["brand_by_query"]} == {"q001", "q002"}
    extracted = _read_csv(bundle / "result" / "brand_mentions_extracted.csv")
    assert [row["query_id"] for row in extracted] == ["q001", "q002"]


def test_extraction_cache_hit_revalidates_traceability_and_schema_version(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TraceableBrand is mentioned.",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    cache_path = bundle / "logs" / "extraction_cache.jsonl"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    current_key = extraction_cache_key(
        response_text_hash_value=response_text_hash(row),
        schema_version=EXTRACTION_SCHEMA_VERSION,
        extractor_model="test-model",
    )
    old_key = extraction_cache_key(
        response_text_hash_value=response_text_hash(row),
        schema_version="brand-extraction-v2",
        extractor_model="test-model",
    )
    cache_rows = [
        {
            "cache_key": current_key,
            "rows": [
                {
                    "query_id": "old",
                    "repeat_index": 1,
                    "input_query": "old",
                    "brand_name_raw": "HallucinatedBrand",
                    "brand_type": "公司",
                    "evidence": "TraceableBrand is mentioned",
                }
            ],
            "error": None,
        },
        {
            "cache_key": old_key,
            "rows": [
                {
                    "query_id": "old",
                    "repeat_index": 1,
                    "input_query": "old",
                    "brand_name_raw": "TraceableBrand",
                    "brand_type": "公司",
                    "evidence": "TraceableBrand",
                }
            ],
            "error": None,
        },
    ]
    cache_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in cache_rows), encoding="utf-8")

    try:
        analyze_job_bundle(bundle, settings=Settings(llm_api_key=None))
    except ValueError as exc:
        assert "confirm_cost" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_analyze_job_logs_traceability_quarantine_rows(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TraceableBrand is mentioned.",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TraceableBrand",
                "brand_type": "公司",
                "evidence": "TraceableBrand",
                "role": "mentioned",
                "confidence": 0.9,
                "sov_eligible": True,
            }
        ], {
            "type": "TraceabilityQuarantine",
            "message": "3 items quarantined",
            "query_id": "q001",
            "repeat_index": 1,
            "quarantined_rows": [
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "HallucinatedBrandA",
                    "evidence": "not in response",
                    "reason": "untraceable_extraction_item",
                },
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "HallucinatedBrandB",
                    "evidence": "not in response",
                    "reason": "untraceable_extraction_item",
                },
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "HallucinatedBrandC",
                    "evidence": "not in response",
                    "reason": "untraceable_extraction_item",
                },
            ],
        }

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)
    errors = read_jsonl(bundle / "logs" / "extraction_errors.jsonl")

    assert result["extracted_mention_count"] == 1
    assert result["data_quality"]["conclusion_strength"] == "observational"
    assert result["data_quality"]["traceability_quarantine_count"] == 3
    assert result["data_quality"]["extraction_error_record_count"] == 1
    assert result["data_quality"]["extraction_error_row_count"] == 3
    assert result["data_quality"]["extraction_error_rate"] == "100.0%"
    assert result["extraction_error_record_count"] == 1
    assert result["extraction_error_row_count"] == 3
    assert len(errors) == 3
    assert errors[0]["brand_name_raw"] == "HallucinatedBrandA"
    assert errors[0]["reason"] == "untraceable_extraction_item"


def test_analyze_job_computes_sov_and_upserts_cross_job_aggregates(tmp_path):
    config_path = tmp_path / "job_config.json"
    runs_root = tmp_path / ".runs"
    bundle = runs_root / "job_test"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    rows = [
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 1,
            "repeat_total": 2,
            "request_hash": "a",
            "model": "m",
            "input_query": "best local providers",
            "status": "success",
            "response_text": "1. TestAEntity is recommended. TestBEntity is also mentioned.",
            "sources": [],
            "raw_request": {"model": "m", "input": "best local providers"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 2,
            "repeat_total": 2,
            "request_hash": "b",
            "model": "m",
            "input_query": "best local providers",
            "status": "success",
            "response_text": "TestBEntity is recommended.",
            "sources": [],
            "raw_request": {"model": "m", "input": "best local providers"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:02+00:00",
        },
        {
            "run_id": "r",
            "query_id": "q002",
            "repeat_index": 1,
            "repeat_total": 2,
            "request_hash": "c",
            "model": "m",
            "input_query": "top premium studios",
            "status": "success",
            "response_text": "TestAEntity appears again.",
            "sources": [],
            "raw_request": {"model": "m", "input": "top premium studios"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:03+00:00",
        },
    ]
    _make_contract_valid(rows)
    (bundle / "raw" / "attempts.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    def extractor(record):
        if record["query_id"] == "q001" and record["repeat_index"] == 1:
            return [
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "TestAEntity",
                    "brand_type": "品牌",
                    "evidence": "1. TestAEntity is recommended",
                    "role": "recommended",
                    "confidence": 0.9,
                    "is_recommended": True,
                    "rank_position": 1,
                    "sentiment": "positive",
                },
                {
                    "query_id": "q001",
                    "repeat_index": 1,
                    "input_query": record["input_query"],
                    "brand_name_raw": "TestBEntity",
                    "brand_type": "公司",
                    "evidence": "TestBEntity is also mentioned",
                    "role": "mentioned",
                    "confidence": 0.8,
                    "is_recommended": False,
                    "rank_position": "",
                    "sentiment": "neutral",
                },
            ], None
        if record["query_id"] == "q001":
            return [
                {
                    "query_id": "q001",
                    "repeat_index": 2,
                    "input_query": record["input_query"],
                    "brand_name_raw": "TestBEntity",
                    "brand_type": "公司",
                    "evidence": "TestBEntity is recommended",
                    "role": "recommended",
                    "confidence": 0.8,
                    "is_recommended": True,
                    "rank_position": 1,
                    "sentiment": "positive",
                }
            ], None
        return [
            {
                "query_id": "q002",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestAEntity",
                "brand_type": "品牌",
                "evidence": "TestAEntity appears again",
                "role": "mentioned",
                "confidence": 0.9,
                "is_recommended": False,
                "rank_position": "",
                "sentiment": "neutral",
            }
        ], None

    (bundle / "result" / "discovered_brands.csv").write_text("legacy\n", encoding="utf-8")
    (bundle / "result" / "sov_summary.csv").write_text("legacy\n", encoding="utf-8")

    first = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, write_aggregates=True)
    second = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, write_aggregates=True)

    target = next(row for row in second["brand_summary"] if row["brand_name_canonical"] == "TestAEntity")
    assert target["sov_response_share"] == "50.0%"
    assert target["recommended_rate_when_mentioned"] == "50.0%"
    assert target["recommended_rate_over_success"] == "33.3%"
    assert target["avg_rank_position"] == 1
    assert second["target_diagnosis"]["target_rank_by_sov"] == 1
    assert second["target_diagnosis"]["target_sov_gap_to_leader"] == "0.0pp"
    assert (bundle / "result" / "brand_summary.csv").exists()
    assert not (bundle / "result" / "discovered_brands.csv").exists()
    assert not (bundle / "result" / "sov_summary.csv").exists()

    index_rows = read_jsonl(runs_root / "index.jsonl")
    assert len(index_rows) == 1
    assert index_rows[0]["job_id"] == first["job_id"]
    brand_trends = _read_csv(runs_root / "aggregate" / "brand_trends.csv")
    assert len(brand_trends) == 2
    target_trends = _read_csv(runs_root / "aggregate" / "target_brand_trends.csv")
    assert len(target_trends) == 1
    assert target_trends[0]["sov_response_share"] == "50.0%"
    assert "neutral_rate" in target_trends[0]
    assert "target_sov_gap_to_leader" in target_trends[0]


def test_cross_job_brand_trends_has_stable_header_when_no_brands(tmp_path):
    config_path = tmp_path / "job_config.json"
    runs_root = tmp_path / ".runs"
    bundle = runs_root / "job_empty"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "a",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "No brands",
        "sources": [],
        "raw_request": {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, write_aggregates=True)

    header = (runs_root / "aggregate" / "brand_trends.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "job_id" in header
    assert "sov_response_share" in header
    assert "sov_event_share" not in header
    assert header != "empty"


def test_analyze_job_can_disable_cross_job_aggregates(tmp_path):
    config_path = tmp_path / "job_config.json"
    runs_root = tmp_path / ".runs"
    bundle = runs_root / "job_no_aggregate"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "No brands",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=lambda record: ([], None), write_aggregates=False)

    assert not (runs_root / "index.jsonl").exists()
    assert not (runs_root / "aggregate").exists()


def test_analyze_job_uses_target_aliases_and_filters_source_entities(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["target_aliases"] = ["TestAlias"]
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 2,
        "request_hash": "a",
        "model": "m",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestAlias is mentioned. TestSourceEntity is a source.",
        "sources": [],
        "raw_request": {"model": "m", "input": "best local providers"},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestAlias",
                "brand_type": "品牌",
                "evidence": "TestAlias",
                "role": "mentioned",
                "confidence": 0.9,
            },
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestSourceEntity",
                "brand_type": "媒体",
                "evidence": "TestSourceEntity",
                "role": "source",
                "mention_context": "source",
                "confidence": 0.9,
            },
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["target_detected"] is True
    assert [row["brand_name_canonical"] for row in result["brand_summary"]] == ["TestAEntity"]
    source_mentions = _read_csv(bundle / "result" / "source_entity_mentions.csv")
    assert source_mentions[0]["brand_name_raw"] == "TestSourceEntity"


def test_target_alias_canonicalization_merges_entire_llm_group(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["target_aliases"] = ["ShortName"]
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "a",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "ShortName and Official Target Ltd",
        "sources": [],
        "raw_request": {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "ShortName",
                "brand_type": "品牌",
                "evidence": "ShortName",
                "role": "mentioned",
                "confidence": 0.9,
            },
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "Official Target Ltd",
                "brand_type": "公司",
                "evidence": "Official Target Ltd",
                "role": "mentioned",
                "confidence": 0.9,
            },
        ], None

    def canonicalizer(names):
        return {"ShortName": "LLM Canonical", "Official Target Ltd": "LLM Canonical"}, None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, canonicalizer=canonicalizer)

    assert [row["brand_name_canonical"] for row in result["brand_summary"]] == ["TestAEntity"]
    assert result["brand_summary"][0]["responses_mentioned"] == 1


def test_analyze_job_dedupes_canonical_brand_event_within_response(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio and TestAlias",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "公司",
                "evidence": "TestStudio",
                "role": "recommended",
                "is_recommended": True,
                "rank_position": 1,
                "sentiment": "positive",
                "confidence": 0.9,
            },
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestAlias",
                "brand_type": "品牌",
                "evidence": "TestAlias",
                "role": "mentioned",
                "is_recommended": False,
                "rank_position": 2,
                "sentiment": "neutral",
                "confidence": 0.8,
            },
        ], None

    def canonicalizer(names):
        return {"TestStudio": "TestAEntity", "TestAlias": "TestAEntity"}, None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, canonicalizer=canonicalizer)

    row = result["brand_summary"][0]
    assert row["brand_name_canonical"] == "TestAEntity"
    assert row["responses_mentioned"] == 1
    assert row["rank_observed_count"] == 1
    assert row["rank_observed_rate"] == "100.0%"


def test_analyze_job_allows_business_institution_and_honors_string_sov_false(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "a",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestDesignInstitute and TestExcludedEntity",
        "sources": [],
        "raw_request": {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestDesignInstitute",
                "brand_type": "机构",
                "sov_eligible": True,
                "evidence": "TestDesignInstitute",
                "role": "mentioned",
                "confidence": 0.9,
            },
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestExcludedEntity",
                "brand_type": "公司",
                "sov_eligible": "false",
                "evidence": "TestExcludedEntity",
                "role": "mentioned",
                "confidence": 0.9,
            },
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert [row["brand_name_canonical"] for row in result["brand_summary"]] == ["TestDesignInstitute"]
    source_mentions = _read_csv(bundle / "result" / "source_entity_mentions.csv")
    assert source_mentions[0]["brand_name_raw"] == "TestExcludedEntity"


def test_analyze_job_missing_sov_eligible_falls_back_to_brand_type(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "a",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "公司",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert [row["brand_name_canonical"] for row in result["brand_summary"]] == ["TestStudio"]


def test_analyze_job_data_quality_detects_request_hash_mismatch(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    raw_request = {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]}
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "wrong",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "raw_request": raw_request,
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["data_quality"]["conclusion_strength"] == "observational"
    assert any(item["field"] == "request_hash" for item in result["data_quality"]["contract_mismatches"])


def test_analyze_job_data_quality_detects_missing_request_hash(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    raw_request = {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]}
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "raw_request": raw_request,
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert any(item["field"] == "request_hash" for item in result["data_quality"]["contract_mismatches"])


def test_analyze_job_data_quality_detects_missing_raw_response(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    row.pop("raw_response")
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert any(item["field"] == "raw_response" for item in result["data_quality"]["contract_mismatches"])
    assert result["data_quality"]["excluded_from_stats_count"] == 1
    assert result["brand_summary"] == []


def test_analyze_job_latest_error_overrides_old_success(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    success = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "OldBrand",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(success)
    error = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "error",
        "error": {"type": "ProviderError", "message": "later failure"},
        "started_at": "2026-01-01T00:00:02+00:00",
        "completed_at": "2026-01-01T00:00:03+00:00",
    }
    (bundle / "raw" / "attempts.jsonl").write_text(
        json.dumps(success, ensure_ascii=False) + "\n" + json.dumps(error, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "OldBrand",
                "brand_type": "品牌",
                "evidence": "OldBrand",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["brand_summary"] == []
    assert result["data_quality"]["latest_failed_units"] == [{"query_id": "q001", "repeat_index": 1, "status": "error", "error": "later failure"}]
    assert result["data_quality"]["conclusion_strength"] == "observational"


def test_analyze_job_missing_legacy_web_and_source_evidence_downgrades(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["data_quality"]["conclusion_strength"] == "observational"
    assert result["data_quality"]["web_search_quality_flags"]
    assert result["data_quality"]["source_quality_flags"]


def test_target_missing_queries_excludes_unsampled_queries(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 2,
        "request_hash": "a",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "Beta Studio",
        "sources": [],
        "raw_request": {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "Beta Studio",
                "brand_type": "品牌",
                "evidence": "Beta Studio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    missing_ids = {item["query_id"] for item in result["target_diagnosis"]["missing_queries"]}
    unsampled_ids = {item["query_id"] for item in result["target_diagnosis"]["unsampled_queries"]}
    assert missing_ids == {"q001"}
    assert "q002" in unsampled_ids


def test_analyze_job_writes_data_quality_for_missing_extra_and_bad_raw(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    rows = [
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 1,
            "repeat_total": 2,
            "request_hash": "a",
            "model": "m",
            "input_query": "best local providers",
            "status": "success",
            "response_text": "TestStudio",
            "sources": [],
            "raw_request": {"model": "m", "input": "best local providers"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "run_id": "r",
            "query_id": "q999",
            "repeat_index": 1,
            "repeat_total": 2,
            "request_hash": "x",
            "model": "m",
            "input_query": "extra",
            "status": "success",
            "response_text": "Extra",
            "sources": [],
            "raw_request": {"model": "m", "input": "extra"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    (bundle / "raw" / "attempts.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n{bad", encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": record["query_id"],
                "repeat_index": record["repeat_index"],
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["data_quality"]["conclusion_strength"] == "observational"
    assert result["data_quality"]["missing_units"]
    assert result["data_quality"]["extra_units"]
    assert result["data_quality"]["raw_read_errors"]
    assert (bundle / "logs" / "data_quality.json").exists()
    assert (bundle / "logs" / "raw_read_errors.jsonl").exists()


def test_analyze_job_downgrades_on_extraction_errors(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 2,
        "request_hash": "a",
        "model": "m",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "raw_request": {"model": "m", "input": "best local providers"},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [], {"type": "ParseError", "message": "bad extraction", "query_id": record["query_id"]}

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    assert result["data_quality"]["conclusion_strength"] == "observational"
    assert result["data_quality"]["extraction_error_record_count"] == 1
    report = (bundle / "result" / "report.md").read_text(encoding="utf-8")
    assert "抽取质量不足" in report
    header = (bundle / "result" / "brand_summary.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "sov_rank" in header
    assert header != "empty"


def test_analyze_job_writes_denominator_fact_csvs(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = [{"query": "best local providers", "persona": "buyer", "tags": ["local", "urgent"]}]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "web_search_requirement_status": "satisfied",
        "web_search_evidence": "provider_trace",
        "source_parse_status": "provider_returned_empty",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    quality = _read_csv(bundle / "result" / "quality_summary.csv")
    attempts = _read_csv(bundle / "result" / "attempt_facts.csv")
    queries = _read_csv(bundle / "result" / "query_facts.csv")
    brands = _read_csv(bundle / "result" / "brand_attempt_facts.csv")
    assert quality[0]["stats_record_count"] == "1"
    assert attempts[0]["latest_status"] == "success"
    assert queries[0]["planned_attempts"] == "1"
    assert "buyer" in queries[0]["query_metadata_json"]
    assert brands[0]["brand_name_canonical"] == "TestStudio"


def test_mock_rerun_preserves_existing_analysis_outputs_before_db_ingest(tmp_path):
    pytest.importorskip("duckdb")
    config_path = tmp_path / "job_config.json"
    runs = tmp_path / "runs"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    bundle_info = build_job_bundle(config_path, runs_dir=runs, settings=Settings(llm_api_key=None))
    bundle = Path(bundle_info["bundle_dir"])
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), include_mock=True)
    assert (bundle / "result" / "brand_summary.csv").exists()
    original_brand_summary = (bundle / "result" / "brand_summary.csv").read_bytes()
    analyzed_status = load_job_manifest(bundle)["status"]

    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    assert (bundle / "result" / "brand_summary.csv").read_bytes() == original_brand_summary
    manifest = load_job_manifest(bundle)
    assert manifest["status"] == analyzed_status
    assert manifest.get("run_generation", 0) == 0
    assert manifest["diagnostic_generation"] == 2

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    _, brand_rows = query_duckdb(db, "select count(*) from brand_summary")
    _, run_rows = query_duckdb(db, "select status, job_conclusion_strength from runs")
    assert brand_rows == [(0,)]
    assert run_rows[0][0] == analyzed_status
    assert run_rows[0][1] == "observational"


def test_duckdb_queries_use_planned_manifest_universe_for_partial_run(tmp_path):
    pytest.importorskip("duckdb")
    config_path = tmp_path / "job_config.json"
    runs = tmp_path / "runs"
    _write_job_config(config_path)
    bundle_info = build_job_bundle(config_path, runs_dir=runs, settings=Settings(llm_api_key=None))
    bundle = Path(bundle_info["bundle_dir"])
    run_job_bundle(bundle, mock=True, only_query_ids=["q001"], settings=Settings(llm_api_key=None))
    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), include_mock=True)

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)
    _, rows = query_duckdb(db, "select count(distinct query_id) from queries")

    assert rows == [(2,)]


def test_analyze_job_uses_exact_schema_for_brand_summary_csv(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_name_canonical": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
                "temporary_debug_field": "drop me",
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    with (bundle / "result" / "brand_summary.csv").open(encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f))
    assert header == CSV_FIELD_SCHEMAS["brand_summary"]
    assert "temporary_debug_field" not in header


def test_analyze_job_uses_live_records_when_include_mock_has_mixed_data(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    rows = [
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 1,
            "repeat_total": 1,
            "request_hash": "mock",
            "model": "m",
            "input_query": "best local providers",
            "status": "mock",
            "response_text": "MockBrand",
            "sources": [],
            "raw_request": {"model": "m", "input": "best local providers"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:02+00:00",
        },
        {
            "run_id": "r",
            "query_id": "q001",
            "repeat_index": 1,
            "repeat_total": 1,
            "request_hash": "live",
            "model": "m",
            "input_query": "best local providers",
            "status": "success",
            "response_text": "LiveBrand",
            "sources": [],
            "raw_request": {"model": "m", "input": "best local providers"},
            "raw_response": {"status": "completed"},
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    _make_contract_valid(rows)
    (bundle / "raw" / "attempts.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": record["query_id"],
                "repeat_index": record["repeat_index"],
                "input_query": record["input_query"],
                "brand_name_raw": record["response_text"],
                "brand_type": "品牌",
                "evidence": record["response_text"],
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, include_mock=True)

    assert result["sample_mode"] == "live"
    assert result["brand_summary"][0]["brand_name_canonical"] == "LiveBrand"
    assert result["data_quality"]["ignored_mock_record_count"] == 1


def test_analyze_job_include_mock_generates_demo_report(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    result = analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), include_mock=True)

    assert result["sample_mode"] == "mock"
    assert result["brand_summary"]
    report = (bundle / "result" / "report.md").read_text(encoding="utf-8")
    assert "mock 样本" in report


def test_analyze_job_outputs_source_citation_tables(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 2,
        "request_hash": "a",
        "model": "m",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [{"domain": "example.com", "url": "https://example.com/a", "title": "A", "rank": 1}],
        "raw_request": {"model": "m", "input": "best local providers"},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    domains = _read_csv(bundle / "result" / "source_domains.csv")
    assert domains[0]["domain"] == "example.com"
    assert "parsed_source_occurrences" in domains[0]
    assert "Source & Citation Opportunities" in (bundle / "result" / "report.md").read_text(encoding="utf-8")


def test_source_stats_normalizes_domain_from_domain_or_url(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": "a",
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [
            {"domain": "WWW.Example.com:443", "url": "https://www.example.com/a", "title": "A", "rank": 1},
            {"url": "https://www.example.com/b", "title": "B", "rank": 2},
        ],
        "raw_request": {"model": "test-model", "input": "best local providers", "tools": [{"type": "web_search", "limit": 5}]},
        "raw_response": {"status": "completed"},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    domains = _read_csv(bundle / "result" / "source_domains.csv")
    assert len(domains) == 1
    assert domains[0]["domain"] == "example.com"
    assert domains[0]["parsed_source_occurrences"] == "2"


def test_source_stats_ignores_missing_rank_in_source_order_average(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [
            {"domain": "example.com", "url": "https://example.com/a", "title": "A", "rank": 2},
            {"domain": "example.com", "url": "https://example.com/b", "title": "B"},
        ],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    domains = _read_csv(bundle / "result" / "source_domains.csv")
    assert domains[0]["parsed_source_occurrences"] == "2"
    assert domains[0]["avg_source_order"] == "2"
    assert domains[0]["best_source_order"] == "2"


def test_source_stats_leaves_source_order_blank_when_all_ranks_missing(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [{"domain": "example.com", "url": "https://example.com/a", "title": "A"}],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    domains = _read_csv(bundle / "result" / "source_domains.csv")
    assert domains[0]["parsed_source_occurrences"] == "1"
    assert domains[0]["avg_source_order"] == ""
    assert domains[0]["best_source_order"] == ""


def test_source_stats_dedupes_same_url_within_response(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["queries"] = ["best local providers"]
    data["repeats"] = 1
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 1,
        "model": "test-model",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio",
        "sources": [
            {"domain": "example.com", "url": "https://example.com/a", "title": "A", "rank": 1},
            {"domain": "www.example.com:443", "url": "https://example.com/a", "title": "A duplicate", "rank": 2},
        ],
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    _make_contract_valid(row)
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "品牌",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor)

    domains = _read_csv(bundle / "result" / "source_domains.csv")
    assert domains[0]["domain"] == "example.com"
    assert domains[0]["parsed_source_occurrences"] == "1"


def test_analyze_job_keep_work_preserves_intermediate_files(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    row = {
        "run_id": "r",
        "query_id": "q001",
        "repeat_index": 1,
        "repeat_total": 2,
        "request_hash": "a",
        "model": "m",
        "input_query": "best local providers",
        "status": "success",
        "response_text": "TestStudio is mentioned.",
        "sources": [],
        "raw_request": {"model": "m", "input": "best local providers"},
        "raw_response": {"status": "completed", "output_text": "TestStudio is mentioned."},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    (bundle / "raw" / "attempts.jsonl").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    def extractor(record):
        return [
            {
                "query_id": "q001",
                "repeat_index": 1,
                "input_query": record["input_query"],
                "brand_name_raw": "TestStudio",
                "brand_type": "公司",
                "evidence": "TestStudio",
                "role": "mentioned",
                "confidence": 0.9,
            }
        ], None

    analyze_job_bundle(bundle, settings=Settings(llm_api_key=None), extractor=extractor, keep_work=True)

    assert (bundle / "work" / "brand_mentions_raw.jsonl").exists()
    assert (bundle / "work" / "brand_canonical_map_work.json").exists()


def test_cleanup_job_is_idempotent(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))

    first = cleanup_job_bundle(bundle)
    second = cleanup_job_bundle(bundle)

    assert first["removed_work_dir"] is True
    assert second["removed_work_dir"] is False
    assert (bundle / "logs" / "cleanup_summary.json").exists()


def test_run_job_recreates_query_manifest_after_cleanup(tmp_path):
    config_path = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    _write_job_config(config_path)
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    cleanup_job_bundle(bundle)

    result = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    assert result["executed"] == 4
    assert (bundle / "work" / "query_manifest.csv").exists()
