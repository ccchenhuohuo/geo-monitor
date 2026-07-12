import json
from pathlib import Path

import pytest

from geo_monitor.analysis.pipeline import analyze_job_bundle
from geo_monitor.config import Settings
from geo_monitor.db import INTELLIGENCE_CSV_STEMS, build_duckdb, connect_readonly, query_duckdb, validate_schema
from geo_monitor.job import build_job_bundle, run_job_bundle

pytest.importorskip("duckdb")


def _build_mock_bundle(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "job.json"
    runs = tmp_path / "runs"
    config.write_text(
        json.dumps(
            {
                "target_brand": "ExampleBrand",
                "industry": "Software",
                "market": "CN",
                "queries": [{"query_id": "q001", "query": "best software", "persona": "buyer", "seed_id": "seed-1"}],
                "repeats": 1,
                "model": "test-model",
                "concurrency": 1,
            }
        ),
        encoding="utf-8",
    )
    built = build_job_bundle(config, runs_dir=runs, settings=Settings(llm_api_key=None))
    bundle = Path(built["bundle_dir"])
    run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))
    return runs, bundle


def _analyze_mock_bundle(bundle: Path) -> dict:
    return analyze_job_bundle(bundle, include_mock=True, settings=Settings(llm_api_key=None))


def test_duckdb_separates_raw_retries_and_orders_latest_fact_by_real_timestamp(tmp_path):
    runs, bundle = _build_mock_bundle(tmp_path)
    raw = bundle / "raw" / "attempts.jsonl"
    original = json.loads(raw.read_text(encoding="utf-8").splitlines()[0])
    original.update(
        {
            "attempt_id": "attempt-old-success",
            "started_at": "2026-01-01T11:59:59+08:00",
            "completed_at": "2026-01-01T12:00:00+08:00",
        }
    )
    retry = dict(original)
    retry.update(
        {
            "attempt_id": "attempt-new-error",
            "status": "error",
            "response_text": None,
            "raw_response": None,
            "error": {"type": "SyntheticRetryError", "message": "latest retry failed"},
            "started_at": "2026-01-01T04:59:59+00:00",
            "completed_at": "2026-01-01T05:00:00+00:00",
        }
    )
    raw.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in (original, retry)) + "\n",
        encoding="utf-8",
    )
    _analyze_mock_bundle(bundle)

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)

    _, raw_count = query_duckdb(db, "select count(*) from attempts")
    _, current = query_duckdb(
        db,
        "select attempt_id, status, fact_source from current_attempts order by completed_at_ts",
    )
    _, metrics = query_duckdb(db, "select attempt_count from metrics_by_run")
    _, quality = query_duckdb(
        db,
        "select latest_terminal_attempt_count, latest_failed_attempt_count from attempt_quality_by_run",
    )

    assert raw_count == [(2,)]
    assert current == [("attempt-new-error", "error", "analysis_fact")]
    assert metrics == [(1,)]
    assert quality == [(1, 1)]


def test_duckdb_rejects_stale_analysis_csvs_even_when_summary_generation_matches(tmp_path):
    runs, bundle = _build_mock_bundle(tmp_path)
    _analyze_mock_bundle(bundle)
    raw = bundle / "raw" / "attempts.jsonl"
    latest = json.loads(raw.read_text(encoding="utf-8").splitlines()[-1])
    stale_generation = latest.get("run_generation")
    latest.update(
        {
            "attempt_id": "attempt-after-analysis",
            "status": "error",
            "response_text": None,
            "raw_response": None,
            "error": {"type": "PostAnalysisRetry", "message": "invalidates old facts"},
            "run_generation": stale_generation,
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
    )
    with raw.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(latest, ensure_ascii=False) + "\n")

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)

    _, facts = query_duckdb(db, "select count(*) from attempt_facts")
    _, brands = query_duckdb(db, "select count(*) from brand_summary")
    _, current = query_duckdb(db, "select attempt_id, status, fact_source from current_attempts")
    _, flags = query_duckdb(
        db,
        "select type from quality_flags where type='stale_analysis_artifacts_ignored'",
    )

    assert facts == [(0,)]
    assert brands == [(0,)]
    assert current == [("attempt-after-analysis", "error", "latest_raw")]
    assert flags == [("stale_analysis_artifacts_ignored",)]


def test_duckdb_ingests_stable_intelligence_csvs_through_registry_and_typed_views(tmp_path):
    runs, bundle = _build_mock_bundle(tmp_path)
    _analyze_mock_bundle(bundle)

    db = tmp_path / "geo.duckdb"
    build_duckdb(runs, db)

    _, registry = query_duckdb(
        db,
        "select artifact_stem, row_count from intelligence_artifacts where artifact_stem='visibility_summary'",
    )
    _, rows = query_duckdb(
        db,
        "select brand_name_canonical, response_mention_rate, eligible_attempts from visibility_summary order by brand_name_canonical",
    )
    _, overview = query_duckdb(
        db,
        "select brand_name_canonical, visibility_score, typeof(completed_at) from geo_overview_scores order by brand_name_canonical",
    )
    _, views = query_duckdb(
        db,
        "select table_name from information_schema.tables where table_name like 'opportunity_%' order by table_name",
    )
    con = connect_readonly(db)
    try:
        validate_schema(con)
    finally:
        con.close()

    assert registry[0][0] == "visibility_summary"
    assert registry[0][1] >= 1
    assert ("ExampleBrand", 1.0, 1) in rows
    assert ("ExampleBrand", 95.0, "TIMESTAMP WITH TIME ZONE") in overview
    assert {row[0] for row in views} == {stem for stem in INTELLIGENCE_CSV_STEMS if stem.startswith("opportunity_")}
