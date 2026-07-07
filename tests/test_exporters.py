import json
from pathlib import Path

from geo_monitor.exporters import canonical_request_hash, export_csv, latest_success_records, read_jsonl, sanitize_csv_cell, sanitize_csv_row, successful_result_hashes, successful_result_keys
from geo_monitor.request_fingerprint import legacy_payload_hash
from geo_monitor.runner import MonitorRunner
from geo_monitor.config import Settings
from geo_monitor.dataset import load_queries


FIXTURES = Path(__file__).parent / "fixtures"


def test_mock_run_and_export_csv(tmp_path):
    settings = Settings()
    queries = load_queries(FIXTURES / "queries.small.csv")
    jsonl_path = tmp_path / "mock.jsonl"
    csv_path = tmp_path / "mock.csv"

    runner = MonitorRunner(settings)
    results = runner.run(queries, output_path=jsonl_path, mock=True, run_id="test_run")

    assert len(results) == 2
    assert jsonl_path.exists()
    records = read_jsonl(jsonl_path)
    assert len(records) == 2
    assert records[0]["status"] == "mock"
    assert records[0]["sources"]

    export_csv(records, csv_path)
    assert csv_path.exists()
    assert "source_count" in csv_path.read_text(encoding="utf-8-sig")


def test_dry_run_does_not_need_api_key(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    jsonl_path = tmp_path / "dry.jsonl"

    runner = MonitorRunner(settings)
    results = runner.run(queries, output_path=jsonl_path, dry_run=True, run_id="dry_run")

    assert len(results) == 2
    assert results[0].status == "dry_run"
    assert results[0].raw_request["tools"][0]["type"] == "web_search"


def test_successful_result_keys_only_accept_live_success(tmp_path):
    path = tmp_path / "keys.jsonl"
    path.write_text(
        json.dumps({"query_id": "q1", "repeat_index": 1, "status": "mock"}, ensure_ascii=False) + "\n" +
        json.dumps({"query_id": "q2", "repeat_index": 1, "status": "dry_run"}, ensure_ascii=False) + "\n" +
        json.dumps({"query_id": "q3", "repeat_index": 1, "status": "success"}, ensure_ascii=False),
        encoding="utf-8",
    )
    keys = successful_result_keys(path)
    assert keys == {("q3", 1)}


def test_resume_helpers_skip_records_with_invalid_repeat_index(tmp_path):
    path = tmp_path / "keys_bad_field.jsonl"
    path.write_text(
        json.dumps({"query_id": "q1", "repeat_index": "bad", "status": "success", "request_hash": "x"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"query_id": "q_float", "repeat_index": 1.5, "status": "success", "request_hash": "float"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"query_id": "q_bool", "repeat_index": True, "status": "success", "request_hash": "bool"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"query_id": "q_zero", "repeat_index": 0, "status": "success", "request_hash": "zero"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"query_id": "q2", "repeat_index": 1, "status": "success", "request_hash": "y"}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert successful_result_keys(path) == {("q2", 1)}
    assert successful_result_hashes(path) == {("q2", 1): {"y"}}


def test_latest_success_records_uses_query_and_repeat_as_logical_key():
    records = [
        {"query_id": "q1", "repeat_index": 1, "status": "success", "request_hash": None, "completed_at": "2026-01-01T00:00:01+00:00", "raw_request": {"model": "m", "input": "a"}},
        {"query_id": "q1", "repeat_index": 1, "status": "success", "request_hash": "abc", "completed_at": "2026-01-01T00:00:02+00:00", "raw_request": {"model": "m", "input": "a"}},
    ]
    latest = latest_success_records(records)
    assert len(latest) == 1
    assert latest[0]["request_hash"] == legacy_payload_hash({"model": "m", "input": "a"})


def test_canonical_request_hash_recomputes_before_trusting_stored_hash():
    record = {
        "request_hash": "stale",
        "raw_request": {"model": "m", "input": "fresh"},
    }

    assert canonical_request_hash(record) == legacy_payload_hash({"model": "m", "input": "fresh"})


def test_csv_formula_injection_is_sanitized():
    assert sanitize_csv_cell("=1+1") == "'=1+1"
    assert sanitize_csv_cell("+cmd") == "'+cmd"
    assert sanitize_csv_cell("-sum") == "'-sum"
    assert sanitize_csv_cell("@test") == "'@test"
    assert sanitize_csv_cell(" =1+1") == "' =1+1"
    assert sanitize_csv_cell("\n=1+1") == "'\n=1+1"
    assert sanitize_csv_cell("normal") == "normal"


def test_sanitize_csv_row_covers_all_fields():
    row = sanitize_csv_row({"run_id": "=run", "model": "+model", "status": "ok", "count": 1})

    assert row["run_id"] == "'=run"
    assert row["model"] == "'+model"
    assert row["status"] == "ok"
    assert row["count"] == 1


def test_export_csv_sanitizes_formula_values_at_writer_boundary(tmp_path):
    out = tmp_path / "attempts.csv"
    export_csv(
        [
            {
                "run_id": "=run",
                "query_id": "+qid",
                "model": "@model",
                "status": "success",
                "input_query": "normal",
                "response_text": "=response",
                "error": {"type": "-type", "message": " =message"},
            }
        ],
        out,
    )

    text = out.read_text(encoding="utf-8-sig")
    assert "'=run" in text
    assert "'+qid" in text
    assert "'@model" in text
    assert "'=response" in text
    assert "'-type" in text
    assert "' =message" in text
