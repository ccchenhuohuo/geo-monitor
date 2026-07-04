import json

from typer.testing import CliRunner

from geo_monitor.cli import app


def _write_job_config(tmp_path):
    config = tmp_path / "job_config.json"
    config.write_text(
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
    return config


def test_validate_job_config_cli_smoke(tmp_path):
    config = _write_job_config(tmp_path)

    result = CliRunner().invoke(app, ["validate-job-config", str(config)])

    assert result.exit_code == 0
    assert "任务配置有效" in result.output


def test_build_and_cleanup_job_cli_smoke(tmp_path):
    config = _write_job_config(tmp_path)
    bundle = tmp_path / "bundle"

    build = CliRunner().invoke(app, ["build-job", str(config), "--out-dir", str(bundle)])
    cleanup = CliRunner().invoke(app, ["cleanup-job", str(bundle)])

    assert build.exit_code == 0
    assert (bundle / "job_manifest.json").exists()
    assert cleanup.exit_code == 0
    assert "清理完成" in cleanup.output


def test_export_csv_cli_skips_bad_jsonl_lines(tmp_path):
    raw = tmp_path / "attempts.jsonl"
    out = tmp_path / "attempts.csv"
    raw.write_text(
        json.dumps({"run_id": "r", "query_id": "q001", "status": "success", "input_query": "q"}, ensure_ascii=False)
        + "\n{bad\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["export-csv", str(raw), "--out", str(out)])

    assert result.exit_code == 0
    assert "已跳过 1 行" in result.output
    assert out.exists()


def test_run_job_cli_does_not_override_manifest_start_interval(tmp_path, monkeypatch):
    captured = {}

    def fake_estimate_job_run(bundle_dir, *, dry_run=False, mock=False, resume=True, limit=None, only_query_ids=None):
        return {
            "planned_units": 0,
            "completed_units": 0,
            "sampling_requests_remaining": 0,
            "analysis_llm_requests_estimate": 0,
            "concurrency": 1,
            "start_interval_seconds": 0.5,
        }

    def fake_run_job_bundle(
        bundle_dir,
        *,
        resume=True,
        dry_run=False,
        mock=False,
        sleep_seconds=0.0,
            start_interval_seconds=None,
            limit=None,
            only_query_ids=None,
            confirm_cost=False,
        ):
        captured["bundle_dir"] = bundle_dir
        captured["start_interval_seconds"] = start_interval_seconds
        return {
            "executed": 0,
            "skipped": 0,
            "completed_units": 0,
            "errors": 0,
            "raw_jsonl": str(tmp_path / "raw.jsonl"),
        }

    monkeypatch.setattr("geo_monitor.cli.estimate_job_run", fake_estimate_job_run)
    monkeypatch.setattr("geo_monitor.cli.run_job_bundle", fake_run_job_bundle)

    result = CliRunner().invoke(app, ["run-job", str(tmp_path / "bundle"), "--mock"])

    assert result.exit_code == 0
    assert captured["start_interval_seconds"] is None


def test_run_job_cli_requires_cost_confirmation_for_live_requests(tmp_path, monkeypatch):
    def fake_estimate_job_run(bundle_dir, *, dry_run=False, mock=False, resume=True, limit=None, only_query_ids=None):
        return {
            "planned_units": 10,
            "completed_units": 0,
            "sampling_requests_remaining": 10,
            "analysis_llm_requests_estimate": 11,
            "concurrency": 2,
            "start_interval_seconds": 0.5,
        }

    monkeypatch.setattr("geo_monitor.cli.estimate_job_run", fake_estimate_job_run)

    result = CliRunner().invoke(app, ["run-job", str(tmp_path / "bundle")])

    assert result.exit_code != 0
    assert "--confirm-cost" in result.output


def test_run_job_cli_returns_nonzero_when_live_records_errors(tmp_path, monkeypatch):
    def fake_estimate_job_run(bundle_dir, *, dry_run=False, mock=False, resume=True, limit=None, only_query_ids=None):
        return {
            "planned_units": 1,
            "completed_units": 0,
            "sampling_requests_remaining": 1,
            "analysis_llm_requests_estimate": 2,
            "concurrency": 1,
            "start_interval_seconds": 0,
        }

    def fake_run_job_bundle(
        bundle_dir,
        *,
        resume=True,
        dry_run=False,
        mock=False,
        sleep_seconds=0.0,
        start_interval_seconds=None,
        limit=None,
        only_query_ids=None,
        confirm_cost=False,
    ):
        return {"executed": 1, "skipped": 0, "completed_units": 0, "errors": 1, "raw_jsonl": str(tmp_path / "raw.jsonl")}

    monkeypatch.setattr("geo_monitor.cli.estimate_job_run", fake_estimate_job_run)
    monkeypatch.setattr("geo_monitor.cli.run_job_bundle", fake_run_job_bundle)

    result = CliRunner().invoke(app, ["run-job", str(tmp_path / "bundle"), "--confirm-cost"])

    assert result.exit_code == 1
    assert "live 错误" in result.output


def test_run_job_cli_no_resume_requires_cost_confirmation_even_when_complete(tmp_path, monkeypatch):
    def fake_estimate_job_run(bundle_dir, *, dry_run=False, mock=False, resume=True, limit=None, only_query_ids=None):
        return {
            "planned_units": 10,
            "completed_units": 10,
            "sampling_requests_remaining": 10 if not resume else 0,
            "analysis_llm_requests_estimate": 11,
            "concurrency": 2,
            "start_interval_seconds": 0.5,
        }

    monkeypatch.setattr("geo_monitor.cli.estimate_job_run", fake_estimate_job_run)

    result = CliRunner().invoke(app, ["run-job", str(tmp_path / "bundle"), "--no-resume"])

    assert result.exit_code != 0
    assert "--confirm-cost" in result.output


def test_analyze_job_cli_requires_cost_confirmation_for_live_extraction(tmp_path, monkeypatch):
    def fake_estimate_job_analysis(bundle_dir, *, include_mock=False):
        return {"analysis_record_count": 3, "sample_mode": "live", "analysis_llm_requests_estimate": 4}

    monkeypatch.setattr("geo_monitor.cli.estimate_job_analysis", fake_estimate_job_analysis)

    result = CliRunner().invoke(app, ["analyze-job", str(tmp_path / "bundle")])

    assert result.exit_code != 0
    assert "--confirm-cost" in result.output
