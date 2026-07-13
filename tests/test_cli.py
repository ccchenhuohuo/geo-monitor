import json
import re

from typer.testing import CliRunner

from geo_monitor.cli import app
from geo_monitor.config import Settings
from geo_monitor.providers import ProviderDependencyError

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return ANSI_RE.sub("", output)


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


def test_doctor_reports_native_endpoint_contract_errors(monkeypatch):
    monkeypatch.setattr(
        "geo_monitor.cli.get_settings",
        lambda: Settings(dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "qwen 原生 endpoint 配置无效" in _plain(result.output)
    assert "/compatible-mode/v1" in _plain(result.output)


def test_doctor_reports_missing_configured_native_provider_sdk(monkeypatch):
    monkeypatch.setattr("geo_monitor.cli.get_settings", lambda: Settings(ark_api_key="configured-key"))
    monkeypatch.setattr(
        "geo_monitor.cli.provider_dependency_available",
        lambda name: False if name == "doubao" else True,
    )

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "geo-monitor[doubao]" in _plain(result.output)


def test_doctor_does_not_treat_empty_dedicated_key_as_native_provider_intent(monkeypatch):
    monkeypatch.setattr(
        "geo_monitor.cli.get_settings",
        lambda: Settings(llm_api_key="generic-key", ark_api_key="", dashscope_api_key=""),
    )
    monkeypatch.setattr("geo_monitor.cli.provider_dependency_available", lambda name: False)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "provider SDK 未安装" not in _plain(result.output)


def test_build_job_cli_requires_and_documents_explicit_output(tmp_path):
    config = _write_job_config(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["build-job", str(config)])
    help_result = runner.invoke(app, ["build-job", "--help"])

    assert result.exit_code != 0
    assert "--out-dir" in _plain(result.output)
    assert "--runs-dir" in _plain(result.output)
    assert help_result.exit_code == 0
    assert "显式任务交付目录" in _plain(help_result.output)
    assert ".runs" not in _plain(help_result.output)


def test_validate_job_config_cli_accepts_external_manifest_without_inline_queries(tmp_path):
    config = tmp_path / "job_config.json"
    seed = tmp_path / "seed_prompts.yaml"
    manifest = tmp_path / "manifests" / "query_manifest.v1.csv"
    config.write_text(
        json.dumps(
            {
                "target_brand": "TestAEntity",
                "industry": "TestIndustry",
                "repeats": 1,
                "model": "test-model",
                "web_search_limit": 5,
                "concurrency": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seed.write_text(
        "seeds:\n  - seed_id: sample\n    seed_query: example query\n    personas:\n      - beginner\n",
        encoding="utf-8",
    )

    fanout = CliRunner().invoke(app, ["fanout", "--input", str(seed), "--output", str(manifest)])
    result = CliRunner().invoke(app, ["validate-job-config", str(config), "--query-manifest", str(manifest)])

    assert fanout.exit_code == 0
    assert result.exit_code == 0
    assert "任务配置有效" in result.output


def test_build_and_validate_job_cli_report_bad_query_manifest_without_traceback(tmp_path):
    config = _write_job_config(tmp_path)
    bad_manifest = tmp_path / "bad.csv"
    bad_manifest.write_text("query_id\nq001\n", encoding="utf-8")

    build = CliRunner().invoke(app, ["build-job", str(config), "--query-manifest", str(bad_manifest), "--out-dir", str(tmp_path / "bundle")])
    validate = CliRunner().invoke(app, ["validate-job-config", str(config), "--query-manifest", str(bad_manifest)])

    assert build.exit_code != 0
    assert validate.exit_code != 0
    assert "CSV 缺少必填字段" in _plain(build.output)
    assert "CSV 缺少必填字段" in _plain(validate.output)
    assert "Traceback" not in build.output
    assert "Traceback" not in validate.output


def test_fanout_cli_accepts_persona_template_registry(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    registry = tmp_path / "persona_templates.yaml"
    manifest = tmp_path / "query_manifest.csv"
    seed.write_text(
        "seeds:\n  - seed_id: sample\n    seed_query: example query\n    personas:\n      - beginner\n",
        encoding="utf-8",
    )
    registry.write_text(
        """
schema_version: persona-template-registry-v1
registry_id: cli_registry
registry_version: v1
personas:
  beginner:
    template_id: cli_template
    template: "CLI registry: {seed_query}"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "fanout",
            "--input",
            str(seed),
            "--output",
            str(manifest),
            "--persona-template-registry",
            str(registry),
        ],
    )

    assert result.exit_code == 0
    text = manifest.read_text(encoding="utf-8")
    assert "template_registry_id" in text
    assert "CLI registry: example query" in text


def test_fanout_cli_reports_malformed_persona_template_registry(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    registry = tmp_path / "persona_templates.yaml"
    manifest = tmp_path / "query_manifest.csv"
    seed.write_text(
        "seeds:\n  - seed_id: sample\n    seed_query: example query\n    personas:\n      - beginner\n",
        encoding="utf-8",
    )
    registry.write_text(
        """
schema_version: persona-template-registry-v1
registry_id: bad_registry
registry_version: v1
personas:
  beginner:
    template_id: cli_template
    template: "missing placeholder"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "fanout",
            "--input",
            str(seed),
            "--output",
            str(manifest),
            "--persona-template-registry",
            str(registry),
        ],
    )

    assert result.exit_code != 0
    assert "seed_query" in result.output


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
        json.dumps({"run_id": "r", "query_id": "q001", "status": "success", "input_query": "q"}, ensure_ascii=False) + "\n{bad\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["export-csv", str(raw), "--out", str(out)])

    assert result.exit_code == 0
    assert "已跳过 1 行" in _plain(result.output)
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
    assert "--confirm-cost" in _plain(result.output)


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


def test_run_job_cli_formats_missing_provider_sdk_without_traceback(tmp_path, monkeypatch):
    def fake_estimate_job_run(bundle_dir, *, dry_run=False, mock=False, resume=True, limit=None, only_query_ids=None):
        return {
            "planned_units": 1,
            "completed_units": 0,
            "sampling_requests_remaining": 1,
            "analysis_llm_requests_estimate": 0,
            "concurrency": 1,
            "start_interval_seconds": 0,
            "sampling_profile": {"provider": "doubao"},
        }

    def fail_run(*args, **kwargs):
        raise ProviderDependencyError("请安装 geo-monitor[doubao]")

    monkeypatch.setattr("geo_monitor.cli.estimate_job_run", fake_estimate_job_run)
    monkeypatch.setattr("geo_monitor.cli.run_job_bundle", fail_run)

    result = CliRunner().invoke(app, ["run-job", str(tmp_path / "bundle"), "--confirm-cost"])

    assert result.exit_code != 0
    assert "geo-monitor[doubao]" in _plain(result.output)
    assert "Traceback" not in result.output


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
    assert "--confirm-cost" in _plain(result.output)


def test_analyze_job_cli_requires_cost_confirmation_for_live_extraction(tmp_path, monkeypatch):
    def fake_estimate_job_analysis(bundle_dir, *, include_mock=False, refresh_extraction_cache=False):
        return {"analysis_record_count": 3, "sample_mode": "live", "analysis_llm_requests_estimate": 4}

    monkeypatch.setattr("geo_monitor.cli.estimate_job_analysis", fake_estimate_job_analysis)

    result = CliRunner().invoke(app, ["analyze-job", str(tmp_path / "bundle")])

    assert result.exit_code != 0
    assert "--confirm-cost" in _plain(result.output)


def test_analyze_job_cli_reports_analysis_model_and_provider_endpoint(tmp_path, monkeypatch):
    def fake_estimate_job_analysis(bundle_dir, *, include_mock=False, refresh_extraction_cache=False):
        return {
            "analysis_record_count": 3,
            "sample_mode": "live",
            "analysis_llm_requests_estimate": 1,
            "model": "deepseek-v4-flash",
            "analysis_profile": {"provider": "deepseek"},
        }

    monkeypatch.setattr("geo_monitor.cli.estimate_job_analysis", fake_estimate_job_analysis)

    result = CliRunner().invoke(app, ["analyze-job", str(tmp_path / "bundle")])
    output = _plain(result.output)

    assert result.exit_code != 0
    assert "deepseek-v4-flash" in output
    assert "https://api.deepseek.com" in output
