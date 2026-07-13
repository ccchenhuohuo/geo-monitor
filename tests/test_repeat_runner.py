from pathlib import Path

import pytest

from geo_monitor.adapters import build_sampling_profile, get_adapter
from geo_monitor.config import Settings
from geo_monitor.dataset import load_queries
from geo_monitor.exporters import append_jsonl, read_jsonl
from geo_monitor.request_fingerprint import REQUEST_FINGERPRINT_VERSION
from geo_monitor.runner import MonitorRunner, compute_request_hash
from geo_monitor.schemas import MonitorResult

FIXTURES = Path(__file__).parent / "fixtures"


def _patch_live_client(monkeypatch, client):
    monkeypatch.setattr("geo_monitor.runner.create_runtime_client", lambda provider, settings, *, concurrency: client)


def test_mock_repeats_create_expected_units(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "mock_repeat.jsonl"
    runner = MonitorRunner(settings)
    results = runner.run(queries, output_path=out, mock=True, repeats=3, run_id="repeat_test")
    assert len(results) == 6
    rows = read_jsonl(out)
    assert len(rows) == 6
    assert sorted({row["repeat_index"] for row in rows}) == [1, 2, 3]
    assert all(row["repeat_total"] == 3 for row in rows)
    assert all(row.get("request_hash") for row in rows)


def test_resume_skips_only_live_success(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "resume_repeat.jsonl"
    runner = MonitorRunner(settings)
    runner.run(queries[:1], output_path=out, mock=True, repeats=1, run_id="resume_test")
    second = runner.run(queries[:1], output_path=out, dry_run=True, repeats=1, run_id="resume_test", resume=True)
    assert len(second) == 1
    rows = read_jsonl(out)
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"mock", "dry_run"}


def test_runner_attempt_contract_for_all_statuses(tmp_path, monkeypatch):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    runner = MonitorRunner(settings)

    runner.run(queries[:1], output_path=tmp_path / "dry.jsonl", dry_run=True, repeats=1, run_id="dry")
    runner.run(queries[:1], output_path=tmp_path / "mock.jsonl", mock=True, repeats=1, run_id="mock")

    class SuccessClient:
        def create_response(self, payload):
            return {"status": "completed", "output_text": "ok", "usage": {}}

    _patch_live_client(monkeypatch, SuccessClient())
    runner.run(queries[:1], output_path=tmp_path / "success.jsonl", repeats=1, run_id="success")

    class ErrorClient:
        def create_response(self, payload):
            return {"status": "completed", "output_text": "", "usage": {}}

    _patch_live_client(monkeypatch, ErrorClient())
    runner.run(queries[:1], output_path=tmp_path / "error.jsonl", repeats=1, run_id="error")

    rows = []
    for name in ["dry.jsonl", "mock.jsonl", "success.jsonl", "error.jsonl"]:
        rows.extend(read_jsonl(tmp_path / name))

    assert {row["status"] for row in rows} == {"dry_run", "mock", "success", "error"}
    for row in rows:
        assert row["query"]
        assert row["query"] == row["input_query"]
        assert row["query_meta"]["schema_version"] == "query-meta-v1"
        assert "generation_method" in row["query_meta"]


def test_monitor_result_fills_attempts_v2_contract_for_manual_records(tmp_path):
    out = tmp_path / "manual.jsonl"
    append_jsonl(
        out,
        MonitorResult(
            run_id="manual",
            query_id="q001",
            repeat_index=1,
            repeat_total=1,
            model="test-model",
            input_query="manual query",
            status="dry_run",
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
        ),
    )

    row = read_jsonl(out)[0]

    assert row["query"] == "manual query"
    assert row["query_meta"]["schema_version"] == "query-meta-v1"
    assert row["query_meta"]["generation_method"] == "config"


def test_resume_existing_row_without_request_hash_is_skipped(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "existing_resume.jsonl"
    runner = MonitorRunner(settings)
    payload = {
        "model": settings.llm_model,
        "input": queries[0].query,
        "tools": [{"type": "web_search", "limit": settings.web_search_limit}],
        "max_tool_calls": settings.max_tool_calls,
    }
    existing = MonitorResult(
        run_id="existing",
        query_id=queries[0].query_id,
        repeat_index=1,
        repeat_total=1,
        model=settings.llm_model,
        input_query=queries[0].query,
        status="success",
        response_text="existing success",
        raw_request=payload,
        raw_response={"status": "completed", "output_text": "existing success"},
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
    )
    append_jsonl(out, existing)
    results = runner.run(queries[:1], output_path=out, mock=True, repeats=1, run_id="existing", resume=True)
    assert len(results) == 0


def test_resume_reruns_when_request_hash_changes(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "changed_hash_resume.jsonl"
    runner = MonitorRunner(settings)
    old_payload = {
        "model": "old-model",
        "input": queries[0].query,
        "tools": [{"type": "web_search", "limit": settings.web_search_limit}],
        "max_tool_calls": settings.max_tool_calls,
    }
    old_success = MonitorResult(
        run_id="old",
        query_id=queries[0].query_id,
        repeat_index=1,
        repeat_total=1,
        model="old-model",
        input_query=queries[0].query,
        status="success",
        response_text="old success",
        raw_request=old_payload,
        raw_response={"status": "completed", "output_text": "old success"},
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
    )
    append_jsonl(out, old_success)

    with pytest.warns(RuntimeWarning, match="request_hash changed"):
        results = runner.run(queries[:1], output_path=out, mock=True, repeats=1, run_id="new", resume=True, model="new-model")

    assert len(results) == 1
    rows = read_jsonl(out)
    assert len(rows) == 2
    assert rows[-1]["model"] == "new-model"


def test_resume_recomputes_fingerprint_before_trusting_stored_hash(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "basis_mismatch_resume.jsonl"
    runner = MonitorRunner(settings)
    adapter = get_adapter("openai_compatible_responses_web_search")
    old_profile = build_sampling_profile(
        adapter_name="openai_compatible_responses_web_search",
        model="old-model",
        settings=settings,
        web_search_limit=settings.web_search_limit,
    )
    new_profile = build_sampling_profile(
        adapter_name="openai_compatible_responses_web_search",
        model=settings.llm_model,
        settings=settings,
        web_search_limit=settings.web_search_limit,
    )
    old_request = adapter.build_request(queries[0], old_profile, settings, {})
    new_request = adapter.build_request(queries[0], new_profile, settings, {})
    old_success = MonitorResult(
        run_id="old",
        query_id=queries[0].query_id,
        repeat_index=1,
        repeat_total=1,
        request_hash=new_request.request_hash,
        request_fingerprint_version=REQUEST_FINGERPRINT_VERSION,
        request_fingerprint_basis=old_request.request_fingerprint_basis,
        model="old-model",
        input_query=queries[0].query,
        status="success",
        response_text="old success",
        raw_request=old_request.payload,
        raw_response={"status": "completed", "output_text": "old success"},
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
    )
    append_jsonl(out, old_success)

    with pytest.warns(RuntimeWarning, match="request_hash changed"):
        results = runner.run(queries[:1], output_path=out, mock=True, repeats=1, run_id="new", resume=True)

    assert len(results) == 1
    assert len(read_jsonl(out)) == 2


def test_resume_ignores_malformed_jsonl_tail(tmp_path):
    queries = load_queries(Path("tests/fixtures/queries.small.csv"))
    out = tmp_path / "broken_tail.jsonl"
    runner = MonitorRunner(Settings(llm_api_key=None))
    runner.run(queries[:1], output_path=out, mock=True, repeats=1, run_id="broken")
    out.write_text(out.read_text(encoding="utf-8") + "{bad", encoding="utf-8")

    results = runner.run(queries[:1], output_path=out, dry_run=True, repeats=1, run_id="broken", resume=True)

    assert len(results) == 1
    assert results[0].status == "dry_run"


def test_resume_all_done_does_not_construct_live_client_without_key(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "all_done_live_resume.jsonl"
    runner = MonitorRunner(settings)
    payload = {
        "model": settings.llm_model,
        "input": queries[0].query,
        "tools": [{"type": "web_search", "limit": settings.web_search_limit}],
        "max_tool_calls": settings.max_tool_calls,
    }
    append_jsonl(
        out,
        MonitorResult(
            run_id="done",
            query_id=queries[0].query_id,
            repeat_index=1,
            repeat_total=1,
            request_hash=compute_request_hash(payload),
            model=settings.llm_model,
            input_query=queries[0].query,
            status="success",
            response_text="done",
            raw_request=payload,
            raw_response={"status": "completed", "output_text": "done"},
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
        ),
    )

    results = runner.run(queries[:1], output_path=out, repeats=1, run_id="done", resume=True)

    assert results == []


def test_concurrent_mock_writes_all_planned_units(tmp_path):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "concurrent_mock.jsonl"
    runner = MonitorRunner(settings)

    results = runner.run(queries, output_path=out, mock=True, repeats=3, run_id="concurrent", concurrency=4)

    assert len(results) == 6
    rows = read_jsonl(out)
    assert sorted((row["query_id"], row["repeat_index"]) for row in rows) == [
        ("q001", 1),
        ("q001", 2),
        ("q001", 3),
        ("q002", 1),
        ("q002", 2),
        ("q002", 3),
    ]


def test_empty_live_response_becomes_error(tmp_path, monkeypatch):
    settings = Settings(llm_api_key=None)
    queries = load_queries(FIXTURES / "queries.small.csv")
    out = tmp_path / "empty_live.jsonl"
    runner = MonitorRunner(settings)

    class DummyClient:
        def create_response(self, payload):
            return {"status": "completed", "output_text": "", "usage": {}}

    _patch_live_client(monkeypatch, DummyClient())
    results = runner.run(queries[:1], output_path=out, repeats=1, run_id="live_test")
    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].error is not None
    assert "EmptyResponseText" in results[0].error.message
