import json

import pytest

import geo_monitor
import geo_monitor.api
from geo_monitor import GeoMonitorResult, StudyPaths, resolve_study_paths, run_geo_monitor
from geo_monitor.api import run_geo_monitor as api_run_geo_monitor

PUBLIC_API = {"GeoMonitorResult", "StudyPaths", "resolve_study_paths", "run_geo_monitor"}


def test_public_api_facade_exports_stable_symbols():
    assert run_geo_monitor is api_run_geo_monitor
    assert GeoMonitorResult.__name__ == "GeoMonitorResult"
    assert StudyPaths.__name__ == "StudyPaths"
    assert GeoMonitorResult.__module__ == "geo_monitor.api"
    assert StudyPaths.__module__ == "geo_monitor.api"
    assert resolve_study_paths(study_dir="/tmp/study").runs_dir.endswith("/tmp/study/runs")
    assert set(geo_monitor.__all__) == {"__version__", *PUBLIC_API}
    assert set(geo_monitor.api.__all__) == PUBLIC_API
    assert "_analyze_job_bundle_unlocked" not in geo_monitor.__all__
    assert geo_monitor.__version__ == "0.2.0"


def test_geo_monitor_result_serializes_to_json():
    result = GeoMonitorResult(status="ok", metrics={"x": 1})

    assert result.to_dict()["status"] == "ok"
    assert json.loads(result.to_json())["metrics"] == {"x": 1}


def test_resolve_study_paths_derives_only_core_workspace_paths():
    paths = resolve_study_paths(runs_dir="/tmp/acme/runs")

    assert paths.runs_dir == "/tmp/acme/runs"
    assert not hasattr(paths, "db_path")
    assert not hasattr(paths, "dashboard_out")


def test_run_geo_monitor_can_resume_existing_bundle_without_rebuilding(tmp_path, monkeypatch):
    bundle = tmp_path / "runs" / "job-existing"
    bundle.mkdir(parents=True)
    manifest = {"job_id": "job-existing", "status": "analyzed"}
    monkeypatch.setattr(geo_monitor.api, "load_job_manifest", lambda path: manifest)
    monkeypatch.setattr(
        geo_monitor.api,
        "build_job_bundle",
        lambda *args, **kwargs: pytest.fail("existing bundle must not be rebuilt"),
    )
    monkeypatch.setattr(
        geo_monitor.api,
        "run_job_bundle",
        lambda path, **kwargs: {"run_id": "job-existing", "errors": 0},
    )

    result = run_geo_monitor(bundle_dir=bundle, dry_run=True)

    assert result.job_id == "job-existing"
    assert result.artifact_paths["bundle_dir"] == str(bundle)
    assert result.study_paths["runs_dir"] == str(bundle.parent)


def test_run_geo_monitor_reports_only_artifacts_that_were_rendered(tmp_path, monkeypatch):
    bundle = tmp_path / "runs" / "job-existing"
    bundle.mkdir(parents=True)
    manifest = {"job_id": "job-existing", "status": "analyzed"}
    monkeypatch.setattr(geo_monitor.api, "load_job_manifest", lambda path: manifest)
    monkeypatch.setattr(geo_monitor.api, "run_job_bundle", lambda path, **kwargs: {"run_id": "job-existing", "errors": 0})

    def fake_analyze(path, **kwargs):
        html = bundle / "result" / "report.html"
        html.parent.mkdir()
        html.write_text("<h1>report</h1>", encoding="utf-8")
        return {"report_files": {"html": "result/report.html"}, "analysis_files": {"missing": "result/missing.csv"}}

    monkeypatch.setattr(geo_monitor.api, "analyze_job_bundle", fake_analyze)

    result = run_geo_monitor(bundle_dir=bundle, report_formats=("html",))

    assert result.summary_markdown == ""
    assert result.artifact_paths["report_html"] == str(bundle / "result" / "report.html")
    assert "report_markdown" not in result.artifact_paths
    assert "missing" not in result.artifact_paths


def test_run_geo_monitor_rejects_blank_query_filter_before_execution(tmp_path, monkeypatch):
    bundle = tmp_path / "runs" / "job-existing"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(geo_monitor.api, "load_job_manifest", lambda path: {"job_id": "job-existing", "status": "built"})
    monkeypatch.setattr(
        geo_monitor.api,
        "run_job_bundle",
        lambda path, **kwargs: pytest.fail("blank filter must fail closed"),
    )

    with pytest.raises(ValueError, match="拒绝回退为全量"):
        run_geo_monitor(bundle_dir=bundle, only_query_id=" , ", dry_run=True)
