import json
from pathlib import Path

import pytest

from geo_monitor.adapters import build_sampling_profile, get_adapter
from geo_monitor.config import Settings
from geo_monitor.dataset import load_queries
from geo_monitor.exporters import append_jsonl, read_jsonl, successful_result_hashes
from geo_monitor.job import JobError, build_job_bundle, load_job_manifest, run_job_bundle
from geo_monitor.runner import MonitorRunner
from geo_monitor.schemas import ErrorRecord, MonitorResult, QueryRecord

FIXTURES = Path(__file__).parent / "fixtures"


class FakeStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _patch_live_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr("geo_monitor.runner.create_runtime_client", lambda provider, settings, *, concurrency: client)


def _write_single_unit_job(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "target_brand": "TestBrand",
                "industry": "TestIndustry",
                "queries": ["best providers"],
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _matching_success(bundle: Path, settings: Settings) -> MonitorResult:
    manifest = load_job_manifest(bundle)
    query = load_queries(bundle / "work" / "query_manifest.csv")[0]
    adapter = get_adapter(str(manifest["sampling_profile"]["adapter"]))
    request = adapter.build_request(query, manifest["sampling_profile"], settings, manifest["adapter_options"])
    return MonitorResult(
        job_id=str(manifest["job_id"]),
        run_id="old-run",
        query_id=query.query_id,
        repeat_index=1,
        repeat_total=1,
        request_hash=request.request_hash,
        request_fingerprint_version=request.request_fingerprint_version,
        request_fingerprint_basis=request.request_fingerprint_basis,
        model=request.model,
        input_query=query.query,
        status="success",
        response_text="old success",
        raw_request=request.payload,
        raw_response={"status": "completed", "output_text": "old success"},
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
    )


def test_production_runner_uses_configured_retry_policy(tmp_path, monkeypatch):
    settings = Settings(llm_api_key=None, retry_max_attempts=3)
    query = load_queries(FIXTURES / "queries.small.csv")[0]
    calls = {"count": 0}

    class FlakyClient:
        def create_response(self, payload):
            calls["count"] += 1
            if calls["count"] < 3:
                raise FakeStatusError(500)
            return {"status": "completed", "output_text": "ok", "usage": {}}

    _patch_live_client(monkeypatch, FlakyClient())

    result = MonitorRunner(settings).run([query], output_path=tmp_path / "attempts.jsonl")[0]

    assert result.status == "success"
    assert calls["count"] == 3
    assert result.provider_meta["api_attempt_count"] == 3
    assert result.provider_meta["retry_count"] == 2


@pytest.mark.parametrize("concurrency", [1, 2])
def test_runner_closes_provider_client_when_result_persistence_fails(tmp_path, monkeypatch, concurrency):
    settings = Settings(llm_api_key=None)
    queries = [QueryRecord(query_id="q1", query="one"), QueryRecord(query_id="q2", query="two")]

    class CloseTrackingClient:
        close_calls = 0

        def create_response(self, payload):
            return {"status": "completed", "output_text": "ok", "usage": {}}

        def close(self):
            self.close_calls += 1

    client = CloseTrackingClient()
    _patch_live_client(monkeypatch, client)

    def fail_to_persist(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("geo_monitor.runner.append_jsonl", fail_to_persist)

    with pytest.raises(OSError, match="disk full"):
        MonitorRunner(settings).run(queries, output_path=tmp_path / "attempts.jsonl", concurrency=concurrency)

    assert client.close_calls == 1


def test_resume_retries_when_newer_terminal_record_is_error_across_timezones(tmp_path):
    settings = Settings(llm_api_key=None)
    query = load_queries(FIXTURES / "queries.small.csv")[0]
    profile = build_sampling_profile(
        adapter_name="openai_compatible_responses_web_search",
        model=settings.llm_model,
        settings=settings,
        web_search_limit=settings.web_search_limit,
    )
    request = get_adapter("openai_compatible_responses_web_search").build_request(query, profile, settings, {})
    output = tmp_path / "attempts.jsonl"
    common = {
        "run_id": "old",
        "query_id": query.query_id,
        "repeat_index": 1,
        "repeat_total": 1,
        "request_hash": request.request_hash,
        "request_fingerprint_version": request.request_fingerprint_version,
        "request_fingerprint_basis": request.request_fingerprint_basis,
        "model": request.model,
        "input_query": query.query,
        "raw_request": request.payload,
    }
    append_jsonl(
        output,
        MonitorResult(
            **common,
            status="success",
            response_text="old success",
            raw_response={"status": "completed", "output_text": "old success"},
            started_at="2026-01-01T00:29:59+08:00",
            completed_at="2026-01-01T00:30:00+08:00",
        ),
    )
    append_jsonl(
        output,
        MonitorResult(
            **common,
            status="error",
            error=ErrorRecord(type="ProviderError", message="newer failure"),
            started_at="2025-12-31T16:59:59Z",
            completed_at="2025-12-31T17:00:00Z",
        ),
    )

    assert successful_result_hashes(output) == {}
    results = MonitorRunner(settings).run([query], output_path=output, mock=True, resume=True)

    assert len(results) == 1
    assert results[0].status == "mock"


def test_execution_and_attempt_ids_are_unique_while_logical_unit_is_stable(tmp_path):
    settings = Settings(llm_api_key=None)
    query = load_queries(FIXTURES / "queries.small.csv")[0]
    output = tmp_path / "attempts.jsonl"
    runner = MonitorRunner(settings)

    runner.run([query], output_path=output, mock=True, run_id="study", run_generation=1)
    runner.run([query], output_path=output, mock=True, run_id="study", run_generation=2)
    rows = read_jsonl(output)

    assert len({row["run_execution_id"] for row in rows}) == 2
    assert len({row["attempt_id"] for row in rows}) == 2
    assert len({row["logical_unit_id"] for row in rows}) == 1
    assert [row["run_generation"] for row in rows] == [1, 2]


def test_zero_execution_dry_run_and_mock_preserve_analysis_artifacts_and_status(tmp_path):
    settings = Settings(llm_api_key=None)
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    build_job_bundle(config, bundle, settings=settings)
    append_jsonl(bundle / "raw" / "attempts.jsonl", _matching_success(bundle, settings))
    manifest_path = bundle / "job_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "analyzed"
    manifest["run_generation"] = 7
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    report = bundle / "result" / "report.md"
    report.write_text("keep me", encoding="utf-8")

    for mode in ({"dry_run": True}, {"mock": True}):
        result = run_job_bundle(bundle, resume=True, settings=settings, **mode)

        assert result["executed"] == 0
        assert report.read_text(encoding="utf-8") == "keep me"
        current = load_job_manifest(bundle)
        assert current["status"] == "analyzed"
        assert current["run_generation"] == 7

    executed = run_job_bundle(bundle, mock=True, resume=False, settings=settings)

    assert executed["executed"] == 1
    assert report.read_text(encoding="utf-8") == "keep me"
    current = load_job_manifest(bundle)
    assert current["status"] == "analyzed"
    assert current["run_generation"] == 7
    assert current["diagnostic_generation"] == 1
    latest = read_jsonl(bundle / "raw" / "attempts.jsonl")[-1]
    assert latest["execution_mode"] == "mock"
    assert latest["run_generation"] == 7
    assert latest["diagnostic_generation"] == 1
    assert successful_result_hashes(bundle / "raw" / "attempts.jsonl")


def test_live_run_rejects_endpoint_drift_from_frozen_profile(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    build_settings = Settings(llm_api_key=None, llm_base_url="https://provider-a.example/v1")
    run_settings = Settings(llm_api_key="test-key", llm_base_url="https://provider-b.example/v1")
    build_job_bundle(config, bundle, settings=build_settings)

    with pytest.raises(JobError, match="base_url_fingerprint"):
        run_job_bundle(bundle, settings=run_settings, confirm_cost=True)


def test_run_rejects_effective_max_tool_calls_drift(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None, max_tool_calls=2))

    with pytest.raises(JobError, match="MAX_TOOL_CALLS"):
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None, max_tool_calls=3))


def test_adapter_override_freezes_effective_max_tool_calls(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    data = json.loads(config.read_text(encoding="utf-8"))
    data.update(
        {
            "adapter": "doubao_ark_responses_web_search",
            "adapter_options": {"max_tool_calls": 4},
        }
    )
    config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None, max_tool_calls=2))

    result = run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None, max_tool_calls=9))

    assert result["executed"] == 1
    row = read_jsonl(bundle / "raw" / "attempts.jsonl")[0]
    assert row["raw_request"]["max_tool_calls"] == 4


def test_run_rejects_effective_max_output_tokens_drift(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None, max_output_tokens=1_000))

    with pytest.raises(JobError, match="MAX_OUTPUT_TOKENS"):
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None, max_output_tokens=1_001))


def test_job_unit_limit_is_enforced_at_build_and_run(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["queries"] = ["one", "two"]
    config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(JobError, match="MAX_JOB_UNITS"):
        build_job_bundle(config, bundle, settings=Settings(llm_api_key=None, max_job_units=1))

    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None, max_job_units=10))
    with pytest.raises(JobError, match="MAX_JOB_UNITS"):
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None, max_job_units=1))

    queries = [QueryRecord(query_id="q1", query="one"), QueryRecord(query_id="q2", query="two")]
    with pytest.raises(ValueError, match="MAX_JOB_UNITS"):
        MonitorRunner(Settings(llm_api_key=None, max_job_units=1)).run(
            queries,
            output_path=tmp_path / "too_many.jsonl",
            mock=True,
        )


@pytest.mark.parametrize(("concurrency", "maximum_calls"), [(1, 2), (3, 3)])
def test_circuit_breaker_stops_sequential_and_bounded_concurrent_submission(
    tmp_path,
    monkeypatch,
    concurrency,
    maximum_calls,
):
    settings = Settings(
        llm_api_key=None,
        retry_max_attempts=1,
        max_consecutive_errors=2,
        max_error_rate=1.0,
        concurrency=concurrency,
    )
    calls = {"count": 0}

    class FailingClient:
        def create_response(self, payload):
            calls["count"] += 1
            raise FakeStatusError(500)

    _patch_live_client(monkeypatch, FailingClient())
    queries = [QueryRecord(query_id=f"q{index}", query=f"query {index}") for index in range(10)]
    runner = MonitorRunner(settings)

    results = runner.run(queries, output_path=tmp_path / f"breaker-{concurrency}.jsonl", concurrency=concurrency)

    assert 2 <= calls["count"] <= maximum_calls
    assert len(results) == calls["count"]
    assert runner.last_run_info["circuit_breaker"] is True
    assert runner.last_run_info["circuit_breaker_reason"] == "consecutive_errors"
    assert runner.last_run_info["not_started"] == 10 - len(results)


def test_error_rate_breaker_waits_for_minimum_observation_sample(tmp_path, monkeypatch):
    settings = Settings(
        llm_api_key=None,
        retry_max_attempts=1,
        max_consecutive_errors=99,
        max_error_rate=0.5,
    )
    calls = {"count": 0}

    class MixedClient:
        def create_response(self, payload):
            calls["count"] += 1
            if calls["count"] <= 3:
                raise FakeStatusError(500)
            return {"status": "completed", "output_text": "ok", "usage": {}}

    _patch_live_client(monkeypatch, MixedClient())
    queries = [QueryRecord(query_id=f"q{index}", query=f"query {index}") for index in range(10)]
    runner = MonitorRunner(settings)

    runner.run(queries, output_path=tmp_path / "rate-breaker.jsonl")

    assert calls["count"] == 5
    assert runner.last_run_info["circuit_breaker_reason"] == "error_rate"
    assert runner.last_run_info["circuit_breaker_trigger_observed"] == 5
    assert runner.last_run_info["circuit_breaker_trigger_error_rate"] == pytest.approx(0.6)


def test_job_circuit_breaker_marks_partial_and_persists_reason(tmp_path, monkeypatch):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["queries"] = [f"query {index}" for index in range(6)]
    config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    settings = Settings(
        llm_api_key="test-key",
        llm_base_url="https://provider.example/v1",
        retry_max_attempts=1,
        max_consecutive_errors=2,
        max_error_rate=1.0,
    )
    build_job_bundle(config, bundle, settings=settings)

    class FailingClient:
        def create_response(self, payload):
            raise FakeStatusError(500)

    _patch_live_client(monkeypatch, FailingClient())

    result = run_job_bundle(bundle, settings=settings, confirm_cost=True)

    assert result["circuit_breaker"] is True
    assert result["executed"] == 2
    manifest = load_job_manifest(bundle)
    assert manifest["status"] == "ran_partial"
    assert manifest["last_run_circuit_breaker"]["circuit_breaker_reason"] == "consecutive_errors"
    summary = json.loads((bundle / "logs" / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["circuit_breaker"] is True
    assert summary["circuit_breaker_details"]["not_started"] == 4


def test_sampling_fingerprint_tracks_only_effective_frozen_conditions(tmp_path):
    base = {
        "target_brand": "TestBrand",
        "industry": "TestIndustry",
        "queries": ["best providers"],
        "repeats": 1,
        "model": "test-model",
        "web_search_limit": 1,
    }

    def fingerprint(name, config_data, settings):
        config = tmp_path / f"{name}.json"
        config.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")
        result = build_job_bundle(config, tmp_path / name, settings=settings)
        return result["comparability_profile"]["sampling_fingerprint"]

    baseline_settings = Settings(llm_api_key=None, llm_base_url="https://provider-a.example/v1", max_output_tokens=1_000)
    baseline = fingerprint("baseline", base, baseline_settings)
    changed_ineffective_limit = fingerprint("limit", {**base, "web_search_limit": 20}, baseline_settings)
    changed_model = fingerprint("model", {**base, "model": "other-model"}, baseline_settings)
    changed_endpoint = fingerprint(
        "endpoint",
        base,
        Settings(llm_api_key=None, llm_base_url="https://provider-b.example/v1", max_output_tokens=1_000),
    )
    changed_output_budget = fingerprint(
        "output",
        base,
        Settings(llm_api_key=None, llm_base_url="https://provider-a.example/v1", max_output_tokens=1_001),
    )

    assert changed_ineffective_limit == baseline
    assert changed_model != baseline
    assert changed_endpoint != baseline
    assert changed_output_budget != baseline


def test_manifest_rejects_top_level_and_sampling_profile_drift(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None))
    manifest_path = bundle / "job_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model"] = "silently-changed-model"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(JobError, match="sampling_profile.model"):
        load_job_manifest(bundle)


def test_keyboard_interrupt_marks_job_interrupted(tmp_path, monkeypatch):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    _write_single_unit_job(config)
    build_job_bundle(config, bundle, settings=Settings(llm_api_key=None))

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("geo_monitor.job.MonitorRunner.run", interrupt)

    with pytest.raises(KeyboardInterrupt):
        run_job_bundle(bundle, mock=True, settings=Settings(llm_api_key=None))

    manifest = load_job_manifest(bundle)
    assert manifest["status"] == "interrupted"
    assert manifest["last_run_execution_id"]
    assert manifest["last_run_interrupted_at"]
