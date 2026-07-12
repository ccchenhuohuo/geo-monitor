import csv
import json
import shutil
from pathlib import Path

import pytest

from geo_monitor.config import Settings
from geo_monitor.db import build_duckdb, query_duckdb
from geo_monitor.exporters import read_jsonl
from geo_monitor.fanout import build_query_manifest
from geo_monitor.job import (
    JobError,
    _resolve_query_manifest_source,
    _trusted_query_manifest_roots,
    build_job_bundle,
    estimate_job_run,
    run_job_bundle,
    validate_job_config,
)
from geo_monitor.job_analysis import analyze_job_bundle
from geo_monitor.tool import run_geo_monitor


def _write_config(path):
    path.write_text(
        json.dumps(
            {
                "target_brand": "ExampleBrand",
                "industry": "ExampleIndustry",
                "market": "ExampleMarket",
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_seed(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
seeds:
  - seed_id: sample_beginner
    category: sample_category
    intent: product_recommendation
    seed_query: "推荐一款适合新手的示例产品"
    personas:
      - beginner
      - budget_sensitive
""".strip(),
        encoding="utf-8",
    )


def _build_external_job(tmp_path):
    study = tmp_path / "study"
    runs = study / "runs"
    manifest = study / "manifests" / "query_manifest.v1.csv"
    config = tmp_path / "job_config.json"
    _write_seed(study / "seed_prompts.yaml")
    _write_config(config)
    build_query_manifest(study / "seed_prompts.yaml", manifest)
    result = build_job_bundle(config, query_manifest_path=manifest, runs_dir=runs, settings=Settings(llm_api_key=None))
    return study, runs, manifest, config, result


def test_external_manifest_job_manifest_does_not_store_query_rows(tmp_path):
    _, _, manifest_path, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "geo-job-v3"
    assert manifest["sampling_profile"]["adapter"] == "openai_responses_web_search"
    assert manifest["analysis_profile"]["adapter"] == "openai_responses_text"
    assert manifest["comparability_profile"]["query_manifest_sha256"]
    assert "queries" not in manifest
    assert manifest["target_brand"] == "ExampleBrand"
    assert manifest["query_manifest"]["source_type"] == "external_file"
    assert manifest["query_manifest"]["row_count"] == 2
    assert manifest["query_manifest"]["sha256"]
    assert manifest["query_manifest"]["source_uri"] == str(manifest_path)


def test_validate_job_config_accepts_external_manifest_without_inline_queries(tmp_path):
    _, _, manifest_path, config, _ = _build_external_job(tmp_path)

    result = validate_job_config(config, query_manifest_path=manifest_path, settings=Settings(llm_api_key=None))

    assert result["query_count"] == 2
    assert result["planned_units"] == 2
    assert result["query_manifest"]["row_count"] == 2
    assert result["query_manifest"]["sha256"]


def test_raw_attempts_have_query_meta_and_analyze_does_not_need_work(tmp_path):
    _, _, _, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])

    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    shutil.rmtree(bundle / "work")
    analysis = analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))

    rows = read_jsonl(bundle / "raw" / "attempts.jsonl")
    assert rows
    for row in rows:
        assert row["job_id"]
        assert "__r" in row["attempt_id"]
        assert row["query"]
        assert row["query_meta"]["schema_version"] == "query-meta-v1"
        assert row["query_meta"]["seed_id"] == "sample_beginner"
        assert row["query_meta"]["persona"] in {"beginner", "budget_sensitive"}
    assert analysis["report_files"]["markdown"] == "result/report.md"
    assert analysis["report_files"]["pdf"] == "result/report.pdf"
    assert analysis["report_files"]["model"] == "result/report.json"
    assert not (bundle / "result" / "report.html").exists()
    assert not (bundle.parent / "aggregate").exists()
    assert not (bundle.parent / ".runs").exists()


def test_run_job_replacement_manifest_is_used_before_preflight(tmp_path):
    study, _, manifest, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    backup = manifest.with_suffix(".backup.csv")
    manifest.rename(backup)
    shutil.rmtree(bundle / "work")

    estimate = estimate_job_run(bundle, mock=True, settings=Settings(llm_api_key=None), query_manifest_path=backup)
    run = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None), query_manifest_path=backup)

    assert estimate["query_count"] == 2
    assert run["executed"] == 2
    assert (bundle / "work" / "query_manifest.csv").exists()


def test_partial_v2_analysis_keeps_full_manifest_universe_after_work_cleanup(tmp_path):
    _, _, _, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    run_job_bundle(bundle, mock=True, limit=1, settings=Settings(llm_api_key=None))
    shutil.rmtree(bundle / "work")

    analysis = analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))

    assert analysis["expected_queries"] == 2
    assert analysis["data_quality"]["planned_units"] == 2
    assert analysis["data_quality"]["partial_sample"] is True
    assert analysis["data_quality"]["missing_units"]


def test_raw_only_partial_analysis_marks_manifest_unavailable(tmp_path):
    _, _, manifest, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    run_job_bundle(bundle, mock=True, limit=1, settings=Settings(llm_api_key=None))
    shutil.rmtree(bundle / "work")
    manifest.rename(str(manifest) + ".bak")

    analysis = analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))

    assert analysis["expected_queries"] == 2
    assert analysis["data_quality"]["query_manifest_unavailable"] is True
    assert analysis["data_quality"]["missing_unknown_units_count"] == 1


def test_analyze_job_external_manifest_tamper_fails_closed(tmp_path):
    _, _, manifest, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    shutil.rmtree(bundle / "work")
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("推荐一款适合新手的示例产品", "tampered query"), encoding="utf-8")

    with pytest.raises(JobError, match="sha256"):
        analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))

    final_manifest = json.loads((bundle / "job_manifest.json").read_text(encoding="utf-8"))
    assert final_manifest["status"] == "analysis_failed"


def test_job_manifest_source_uri_outside_study_is_rejected_without_replacement(tmp_path):
    _, _, manifest, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    outside = Path("/tmp") / f"geo-monitor-outside-{bundle.name}.csv"
    outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    job_manifest_path = bundle / "job_manifest.json"
    data = json.loads(job_manifest_path.read_text(encoding="utf-8"))
    data["query_manifest"]["source_uri"] = str(outside)
    data["query_manifest"]["source_uri_base"] = str(outside.parent)
    job_manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(bundle / "work")

    with pytest.raises(JobError, match="--query-manifest"):
        estimate_job_run(bundle, mock=True, settings=Settings(llm_api_key=None))

    run = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None), query_manifest_path=outside)
    assert run["executed"] == 2


def test_query_manifest_trusted_roots_do_not_degrade_to_filesystem_root():
    bundle = Path("/tmp/geo-monitor-shallow-bundle")

    assert Path("/") not in _trusted_query_manifest_roots(bundle)
    with pytest.raises(JobError, match="--query-manifest"):
        _resolve_query_manifest_source({"query_manifest": {"source_type": "external_file", "source_uri": "/etc/passwd"}}, bundle_dir=bundle)


def test_legacy_config_queries_still_write_query_meta(tmp_path):
    config = tmp_path / "job_config.json"
    bundle = tmp_path / "bundle"
    config.write_text(
        json.dumps(
            {
                "target_brand": "ExampleBrand",
                "industry": "ExampleIndustry",
                "queries": [{"query_id": "q001", "query": "example query"}],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None))
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    row = read_jsonl(bundle / "raw" / "attempts.jsonl")[0]

    assert row["query"] == "example query"
    assert row["query_meta"]["generation_method"] == "config"
    assert row["query_meta"]["seed_id"] == ""


def test_duckdb_build_uses_raw_query_meta_without_work_or_external_manifest(tmp_path):
    pytest.importorskip("duckdb")
    study, runs, manifest, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    shutil.rmtree(bundle / "work")
    analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))
    manifest.rename(str(manifest) + ".bak")

    db = study / "geo.duckdb"
    build_duckdb(runs, db)
    columns, rows = query_duckdb(db, "select seed_id, persona, count(*) from queries group by 1,2 order by 1,2")

    assert columns == ["seed_id", "persona", "count_star()"]
    assert rows
    assert {row[0] for row in rows} == {"sample_beginner"}


def test_raw_only_rebuild_preserves_query_dimensions_and_custom_manifest_metadata(tmp_path):
    pytest.importorskip("duckdb")
    study = tmp_path / "study"
    runs = study / "runs"
    manifest = study / "manifests" / "query_manifest.v1.csv"
    config = tmp_path / "job_config.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        'query_id,query,locale,market,category,tags,locked_at,channel\nq001,example query,zh-CN,CN,test,"alpha,beta",2026-07-06T00:00:00Z,vip\n',
        encoding="utf-8",
    )
    _write_config(config)
    result = build_job_bundle(config, query_manifest_path=manifest, runs_dir=runs, settings=Settings(llm_api_key=None))
    bundle = Path(result["bundle_dir"])

    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    raw_row = read_jsonl(bundle / "raw" / "attempts.jsonl")[0]
    assert raw_row["query_meta"]["locale"] == "zh-CN"
    assert raw_row["query_meta"]["market"] == "CN"
    assert raw_row["query_meta"]["tags"] == "alpha,beta"
    shutil.rmtree(bundle / "work")
    manifest.rename(str(manifest) + ".bak")
    analysis = analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))

    db = study / "geo.duckdb"
    build_duckdb(runs, db)
    columns, rows = query_duckdb(db, "select locale, market, tags, locked_at, query_metadata_json from queries")

    assert analysis["expected_queries"] == 1
    assert columns == ["locale", "market", "tags", "locked_at", "query_metadata_json"]
    assert rows == [("zh-CN", "CN", "alpha,beta", "2026-07-06T00:00:00Z", '{"channel":"vip"}')]


def test_duckdb_merges_later_query_meta_and_keeps_duplicate_attempts(tmp_path):
    pytest.importorskip("duckdb")
    study, runs, manifest, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    raw = bundle / "raw" / "attempts.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("r", encoding="utf-8", newline="") as f:
        qid = next(row["query_id"] for row in csv.DictReader(f) if row["persona"] == "beginner")
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {"job_id": result["job_id"], "attempt_id": "same", "query_id": qid, "repeat_index": 1, "status": "error", "query": "q", "model": "m"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "job_id": result["job_id"],
                        "attempt_id": "same",
                        "query_id": qid,
                        "repeat_index": 1,
                        "status": "mock",
                        "query": "q",
                        "model": "m",
                        "query_meta": {"seed_id": "sample_beginner", "persona": "beginner", "variant_id": qid},
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    db = study / "geo.duckdb"
    build_duckdb(runs, db)
    _, query_rows = query_duckdb(db, "select seed_id, persona from queries")
    _, attempt_rows = query_duckdb(db, "select count(*) from attempts")
    _, flag_rows = query_duckdb(db, "select type from quality_flags where type='duplicate_attempt_id'")

    assert sorted(query_rows) == [("sample_beginner", "beginner"), ("sample_beginner", "budget_sensitive")]
    assert attempt_rows == [(2,)]
    assert flag_rows == [("duplicate_attempt_id",)]


def test_duckdb_flags_missing_result_csv(tmp_path):
    pytest.importorskip("duckdb")
    study, runs, _, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))
    (bundle / "result" / "brand_summary.csv").unlink()

    db = study / "geo.duckdb"
    build_duckdb(runs, db)
    _, rows = query_duckdb(db, "select type, path from quality_flags where type='missing_result_csv'")

    assert rows
    assert any(str(row[1]).endswith("brand_summary.csv") for row in rows)


def test_duckdb_skips_aggregate_auxiliary_directory(tmp_path):
    pytest.importorskip("duckdb")
    study, runs, _, _, result = _build_external_job(tmp_path)
    bundle = Path(result["bundle_dir"])
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))
    (runs / "aggregate").mkdir(parents=True, exist_ok=True)

    db = study / "geo.duckdb"
    build_duckdb(runs, db)
    _, rows = query_duckdb(db, "select type, path from quality_flags where type='missing_job_manifest'")

    assert rows == []


def test_tool_api_build_dashboard_true_and_seed_requires_manifest(tmp_path):
    pytest.importorskip("duckdb")
    study, _, manifest, config, _ = _build_external_job(tmp_path)

    result = run_geo_monitor(
        config_path=config,
        study_dir=study,
        query_manifest_path=manifest,
        mock=True,
        build_db=True,
        build_dashboard=True,
    )

    assert result.dashboard_path
    assert Path(result.dashboard_path).exists()
    html = Path(result.dashboard_path).read_text(encoding="utf-8")
    for text in ["GEO Monitor Dashboard", "Overview", "Runs", "Comparison Cohorts", "Quality", "Top Brands"]:
        assert text in html

    try:
        run_geo_monitor(config_path=config, study_dir=study, seed_prompts_path=study / "seed_prompts.yaml", mock=True)
    except ValueError as exc:
        assert "query_manifest_path" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_tool_api_builds_dashboard_from_existing_db_without_rebuilding_db(tmp_path):
    pytest.importorskip("duckdb")
    study, _, manifest, config, _ = _build_external_job(tmp_path)

    first = run_geo_monitor(config_path=config, study_dir=study, query_manifest_path=manifest, mock=True, build_db=True)
    assert first.db_path
    second = run_geo_monitor(config_path=config, study_dir=study, query_manifest_path=manifest, mock=True, build_db=False, build_dashboard=True)

    assert second.db_path == first.db_path
    assert second.dashboard_path
    assert Path(second.dashboard_path).exists()


def test_tool_api_build_dashboard_without_db_fails_fast(tmp_path):
    study, _, manifest, config, _ = _build_external_job(tmp_path)
    db = study / "geo.duckdb"
    assert not db.exists()

    with pytest.raises(ValueError, match="build_db"):
        run_geo_monitor(config_path=config, study_dir=study, query_manifest_path=manifest, mock=True, build_db=False, build_dashboard=True)

    assert not db.exists()


def test_tool_api_fanout_force_overwrites_existing_manifest(tmp_path):
    study = tmp_path / "study"
    runs = study / "runs"
    manifest = study / "manifests" / "query_manifest.v1.csv"
    config = tmp_path / "job_config.json"
    seed = study / "seed_prompts.yaml"
    _write_config(config)
    _write_seed(seed)
    build_query_manifest(seed, manifest)
    seed.write_text(
        """
seeds:
  - seed_id: changed
    category: sample_category
    intent: product_recommendation
    seed_query: "changed query"
    personas:
      - beginner
""".strip(),
        encoding="utf-8",
    )

    result = run_geo_monitor(
        config_path=config,
        runs_dir=runs,
        seed_prompts_path=seed,
        query_manifest_path=manifest,
        fanout_force=True,
        mock=True,
        build_db=False,
    )

    assert result.metrics["fanout"]["action"] == "overwritten"
    assert "changed query" in manifest.read_text(encoding="utf-8")


def test_tool_api_uses_persona_template_registry_for_generated_manifest(tmp_path):
    pytest.importorskip("duckdb")
    study = tmp_path / "study"
    runs = study / "runs"
    manifest = study / "manifests" / "query_manifest.v1.csv"
    config = tmp_path / "job_config.json"
    seed = study / "seed_prompts.yaml"
    registry = study / "persona_templates.yaml"
    _write_config(config)
    _write_seed(seed)
    registry.write_text(
        """
schema_version: persona-template-registry-v1
registry_id: api_registry
registry_version: v1
personas:
  beginner:
    template_id: api_beginner
    template: "API registry: {seed_query}"
  budget_sensitive:
    template_id: api_budget
    template: "API budget: {seed_query}"
""".strip(),
        encoding="utf-8",
    )

    result = run_geo_monitor(
        config_path=config,
        runs_dir=runs,
        seed_prompts_path=seed,
        query_manifest_path=manifest,
        persona_template_registry_path=registry,
        mock=True,
        build_db=False,
    )
    bundle = Path(result.artifact_paths["bundle_dir"])
    raw_rows = read_jsonl(bundle / "raw" / "attempts.jsonl")

    assert result.metrics["fanout"]["action"] == "generated"
    assert result.metrics["fanout"]["template_registry_id"] == "api_registry"
    assert "API registry: 推荐一款适合新手的示例产品" in manifest.read_text(encoding="utf-8")
    assert raw_rows
    for row in raw_rows:
        metadata = json.loads(row["query_meta"]["query_metadata_json"])
        assert metadata["template_registry_id"] == "api_registry"
        assert metadata["template_source"] == "registry"
        assert len(metadata["template_hash"]) == 64

    db = study / "geo.duckdb"
    build_duckdb(runs, db)
    columns, rows = query_duckdb(db, "select query_metadata_json from queries order by query_id")
    assert columns == ["query_metadata_json"]
    assert rows
    assert all(json.loads(row[0])["template_registry_id"] == "api_registry" for row in rows)


def test_tool_api_reuses_existing_manifest_without_fanout_force(tmp_path):
    study = tmp_path / "study"
    runs = study / "runs"
    manifest = study / "manifests" / "query_manifest.v1.csv"
    config = tmp_path / "job_config.json"
    seed = study / "seed_prompts.yaml"
    _write_config(config)
    _write_seed(seed)
    build_query_manifest(seed, manifest)
    before = manifest.read_text(encoding="utf-8")
    seed.write_text(
        """
seeds:
  - seed_id: changed
    seed_query: "changed query"
    personas:
      - beginner
""".strip(),
        encoding="utf-8",
    )

    result = run_geo_monitor(
        config_path=config,
        runs_dir=runs,
        seed_prompts_path=seed,
        query_manifest_path=manifest,
        fanout_force=False,
        mock=True,
        build_db=False,
    )

    assert result.metrics["fanout"]["action"] == "reused"
    assert manifest.read_text(encoding="utf-8") == before


def test_tool_api_status_matches_partial_cleaned_manifest(tmp_path):
    study, _, manifest, config, _ = _build_external_job(tmp_path)

    result = run_geo_monitor(config_path=config, study_dir=study, query_manifest_path=manifest, mock=True, limit=1, build_db=False)
    final_manifest = json.loads((Path(result.artifact_paths["bundle_dir"]) / "job_manifest.json").read_text(encoding="utf-8"))

    assert result.status == final_manifest["status"]
    assert result.status == "analyzed_partial_cleaned"
    assert any(flag["type"] == "partial_sample" for flag in result.quality_flags)


def test_gitignore_protects_common_local_study_outputs():
    ignore = Path(".gitignore").read_text(encoding="utf-8")

    assert "my-geo-study/" in ignore
    assert "geo-study/" in ignore
    assert "study/" in ignore
    assert "*.duckdb" in ignore
    assert "outputs/*" not in ignore
    assert "!outputs/.gitkeep" not in ignore
    assert not Path("outputs/.gitkeep").exists()
