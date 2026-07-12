import json
import stat

import pytest
from pydantic import ValidationError

import geo_monitor.dataset as dataset_module
import geo_monitor.fanout as fanout_module
from geo_monitor.analysis.cache import (
    canonicalization_cache_key,
    extraction_cache_key,
    raw_names_hash,
    response_text_hash,
)
from geo_monitor.analysis.pipeline import (
    EXTRACTION_SCHEMA_VERSION,
    _estimate_live_cache_requests,
    _extract_mentions,
    _validate_analysis_runtime_profile,
)
from geo_monitor.api import run_geo_monitor
from geo_monitor.brand_extraction import LLMBrandExtractor
from geo_monitor.config import Settings
from geo_monitor.dataset import DatasetError, load_queries, select_queries
from geo_monitor.db import DuckDBError, build_duckdb
from geo_monitor.exporters import sanitize_csv_cell
from geo_monitor.fanout import FanoutError, build_query_manifest
from geo_monitor.filesystem import UnsafeOutputPathError, open_private_text
from geo_monitor.job import JobError, validate_job_config
from geo_monitor.request_fingerprint import base_url_fingerprint
from geo_monitor.schemas import MAX_QUERY_CHARS, QueryRecord


def test_query_and_dataset_limits_fail_closed(tmp_path, monkeypatch):
    with pytest.raises(ValidationError, match="最大长度"):
        QueryRecord(query_id="q1", query="x" * (MAX_QUERY_CHARS + 1))

    path = tmp_path / "queries.jsonl"
    path.write_bytes(b"x" * 11)
    monkeypatch.setattr(dataset_module, "MAX_DATASET_BYTES", 10)
    with pytest.raises(DatasetError, match="超过 10 bytes"):
        load_queries(path)

    records = [QueryRecord(query_id="q1", query="one")]
    with pytest.raises(DatasetError, match="拒绝回退为全量"):
        select_queries(records, only_query_ids=[])


def test_fanout_input_limit_is_checked_before_parsing(tmp_path, monkeypatch):
    source = tmp_path / "seed.yaml"
    source.write_bytes(b"x" * 11)
    monkeypatch.setattr(fanout_module, "MAX_FANOUT_INPUT_BYTES", 10)

    with pytest.raises(FanoutError, match="超过 10 bytes"):
        build_query_manifest(source, tmp_path / "manifest.csv")


def test_private_writer_rejects_symlinks_and_sets_owner_only_mode(tmp_path):
    output = tmp_path / "private" / "artifact.json"
    with open_private_text(output) as handle:
        handle.write("{}")

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700

    outside = tmp_path / "outside.txt"
    outside.write_text("do not replace", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(UnsafeOutputPathError, match="symlink"):
        open_private_text(link)
    assert outside.read_text(encoding="utf-8") == "do not replace"


def test_duckdb_output_is_private_and_symlink_refusal_uses_public_error(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    output = tmp_path / "db" / "geo.duckdb"

    build_duckdb(runs, output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    output.unlink()
    outside = tmp_path / "outside.duckdb"
    outside.write_bytes(b"unchanged")
    output.symlink_to(outside)
    with pytest.raises(DuckDBError, match="symlink"):
        build_duckdb(runs, output)
    assert outside.read_bytes() == b"unchanged"


def test_nested_csv_values_are_canonical_json_not_python_repr():
    value = {"z": ["\u4e2d\u6587", {"a": True}], "a": None}

    encoded = sanitize_csv_cell(value)

    assert encoded == '{"a":null,"z":["\u4e2d\u6587",{"a":true}]}'
    assert json.loads(encoded) == value


def test_malformed_extraction_cache_is_a_miss_and_never_trusted(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    record = {
        "query_id": "q1",
        "repeat_index": 1,
        "input_query": "compare",
        "response_text": "RealBrand is present.",
    }
    key = extraction_cache_key(
        response_text_hash_value=response_text_hash(record),
        schema_version=EXTRACTION_SCHEMA_VERSION,
        extractor_model="test-model",
    )
    (logs / "extraction_cache.jsonl").write_text(
        json.dumps(
            {
                "cache_key": key,
                "rows": [{"brand_name_raw": "InventedBrand", "evidence": "RealBrand"}],
                "error": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _estimate_live_cache_requests(
        logs,
        [record],
        extractor_model="test-model",
        refresh_extraction_cache=False,
    )

    assert result["extraction_cache_hits"] == 0
    assert result["extraction_cache_misses"] == 1
    assert result["cache_validation_error_count"] == 1
    assert result["analysis_llm_requests_remaining"] == 2


def test_incomplete_canonicalization_cache_is_not_accepted(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    record = {
        "query_id": "q1",
        "repeat_index": 1,
        "input_query": "compare",
        "response_text": "RealBrand is present.",
    }
    extraction_key = extraction_cache_key(
        response_text_hash_value=response_text_hash(record),
        schema_version=EXTRACTION_SCHEMA_VERSION,
        extractor_model="test-model",
    )
    (logs / "extraction_cache.jsonl").write_text(
        json.dumps(
            {
                "cache_key": extraction_key,
                "rows": [{"brand_name_raw": "RealBrand", "evidence": "RealBrand"}],
                "error": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    canonical_key = canonicalization_cache_key(
        sorted_raw_names_hash=raw_names_hash(["RealBrand"]),
        canonicalizer_model="test-model",
    )
    (logs / "canonicalization_cache.jsonl").write_text(
        json.dumps({"cache_key": canonical_key, "canonical_map": {}}) + "\n",
        encoding="utf-8",
    )

    result = _estimate_live_cache_requests(
        logs,
        [record],
        extractor_model="test-model",
        refresh_extraction_cache=False,
    )

    assert result["extraction_cache_hits"] == 1
    assert result["canonicalization_cache_hits"] == 0
    assert result["canonicalization_cache_misses"] == 1
    assert result["cache_validation_error_count"] == 1


def test_analysis_extraction_circuit_breaker_marks_unstarted_records(tmp_path):
    records = [{"query_id": f"q{index}", "repeat_index": 1, "input_query": "q", "response_text": "x"} for index in range(5)]

    def failing_extractor(record):
        return [], {"type": "ProviderError", "message": "failed", "query_id": record["query_id"]}

    _, errors, stats = _extract_mentions(
        records=records,
        active_extractor=failing_extractor,
        settings=Settings(max_consecutive_errors=2, max_error_rate=1.0),
        logs=tmp_path,
        cache_enabled=False,
        extractor_model="test-model",
        refresh_extraction_cache=False,
    )

    assert stats["analysis_circuit_breaker"] is True
    assert stats["analysis_circuit_breaker_reason"] == "consecutive_errors"
    assert stats["analysis_not_started_count"] == 3
    assert [row["reason"] for row in errors[-3:]] == ["analysis_not_started"] * 3


def test_canonicalization_limit_returns_traceable_fallback_without_api_call():
    extractor = LLMBrandExtractor.__new__(LLMBrandExtractor)
    extractor.settings = Settings(analysis_max_canonical_names=1)
    extractor.model = "test-model"

    mapping, error = extractor.canonicalize(["BrandA", "BrandB"])

    assert mapping == {"BrandA": "BrandA", "BrandB": "BrandB"}
    assert error["type"] == "CanonicalizationLimitExceeded"
    assert error["scope"] == "global"


def test_analysis_runtime_profile_freezes_endpoint_and_token_budget():
    endpoint = "https://provider-a.example/v1"
    profile = {
        "base_url_fingerprint": base_url_fingerprint(endpoint),
        "max_output_tokens": 512,
    }
    _validate_analysis_runtime_profile(
        profile,
        Settings(llm_base_url=endpoint, analysis_max_output_tokens=512),
    )

    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        _validate_analysis_runtime_profile(
            profile,
            Settings(llm_base_url="https://provider-b.example/v1", analysis_max_output_tokens=512),
        )
    with pytest.raises(ValueError, match="ANALYSIS_MAX_OUTPUT_TOKENS"):
        _validate_analysis_runtime_profile(
            profile,
            Settings(llm_base_url=endpoint, analysis_max_output_tokens=513),
        )


def test_owned_domains_are_normalized_and_invalid_values_rejected(tmp_path):
    config = tmp_path / "job.json"
    base = {
        "target_brand": "ExampleBrand",
        "industry": "Software",
        "queries": ["best software"],
        "repeats": 1,
        "model": "test-model",
        "owned_domains": ["WWW.Example.COM.", "example.com"],
    }
    config.write_text(json.dumps(base), encoding="utf-8")

    result = validate_job_config(config, settings=Settings(llm_api_key=None))

    assert result["owned_domains"] == ["example.com"]
    base["owned_domains"] = ["https://example.com/private"]
    config.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(JobError, match="只能包含域名"):
        validate_job_config(config, settings=Settings(llm_api_key=None))


def test_public_api_rejects_study_bundle_split_brain(tmp_path):
    bundle = tmp_path / "study-a" / "runs" / "job-existing"
    bundle.mkdir(parents=True)

    with pytest.raises(ValueError, match="bundle 的父目录"):
        run_geo_monitor(
            bundle_dir=bundle,
            study_dir=tmp_path / "study-b",
            dry_run=True,
            build_db=False,
        )


def test_packaged_and_source_job_schemas_are_identical():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    assert (root / "data" / "job_config.schema.json").read_bytes() == (root / "src" / "geo_monitor" / "data" / "job_config.schema.json").read_bytes()
